import base64
import colorsys
import json
import os
import re
import subprocess
import sys
import unicodedata
from difflib import SequenceMatcher
from io import BytesIO

import discogs_client as dc
import requests
from dotenv import load_dotenv
from PIL import Image

# the matrix gets vinyl covers exactly like snapserver-lcd's spotify covers
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'snapserver-lcd'))
from accent_color import get_accent_color_from_bytes  # noqa: E402
from matrix import artwork_pixels  # noqa: E402


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
personal_access_token = os.getenv('DISCOGS_PERSONAL_ACCESS_TOKEN')
if not personal_access_token:
    sys.exit('DISCOGS_PERSONAL_ACCESS_TOKEN must be set in .env')
# covers come from Spotify where it has the album: Discogs' are often photos of the sleeve,
# with lamps and stickers in them, and Spotify's match what snapserver-lcd shows when streaming
spotify_id, spotify_secret = os.getenv('SPOTIFY_CLIENT_ID'), os.getenv('SPOTIFY_CLIENT_SECRET')

KODI_HOST = 'kodi'  # ssh alias for 192.168.0.37
ALBUMS_DIR = '/media/hdhdhd/audio/albums'
MATCH_MIN = 0.85  # title similarity needed, 0..1
COVERS_DIR = 'covers'  # downloaded once, reruns reuse them
SPOTIFY_MATCH_MIN = 0.8  # artist and album similarity a Spotify album needs to be taken
# The bars run from the cover's accent at the bottom to its second color on top, so the
# artist's name, in the accent, stands out where it's written over them. The second
# is the biggest part of the cover that isn't near black and clearly differs from the accent,
# in hue or in saturation, so a gray or cream counts as much as another color.
GRAY_MAX = 0.15          # accents duller than this are black and white: their bars start in white
GRAY_BARS = [[255, 255, 255], [160, 160, 160]]  # black and white covers: white to light gray
SECOND_MIN_SHARE = 0.04  # of the cover, so a speck doesn't color the bars
SECOND_MIN_VALUE = 0.3   # darker counts as black
HUE_APART = 30           # degrees between two colors to tell them apart
SAT_APART = 0.25         # or this much saturation, a gray beside a red
NEUTRAL_MAX = 0.25       # second colors duller than this stay gray, at least NEUTRAL_VALUE bright
NEUTRAL_VALUE = 0.55
BAR_MIN_SAT = 0.45       # colored bars, so a muted cover doesn't light them nearly white
SECOND_MIN_SAT = 0.35
TOP_PALER = 0.6          # one-colored covers keep their hue: the top has this much of the bottom's saturation

parenthesis_prog = re.compile(r'[\(\[].*?[\)\]]')
article_prog = re.compile(r'^(a|the) +')
# release suffixes that differ between folder and discogs but say nothing about the album
noise_prog = re.compile(r'\b(ep|lp|2cd|cd|deluxe edition|deluxe|remastered|reissue|advance)\b')


def connect():
    d = dc.Client('VinylMatch/0.1', user_token=personal_access_token)

    me = d.identity()
    print(80*'=')
    print('I am {} ({}) from {}'.format(me.name, me.username, me.location))
    print('Have: {}'.format(me.num_collection))
    print('Want: {}'.format(me.num_wantlist))
    print(80*'=')

    return d


def normalize(s):
    # 'A_Day_To_Remember' and 'A Day To Remember*' end up the same: 'day to remember'
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = s.replace('_', ' ').replace('&', ' and ').lower()
    s = parenthesis_prog.sub(' ', s)
    s = re.sub(r'[^a-z0-9 ]', '', s)
    s = noise_prog.sub(' ', s)
    s = ' '.join(s.split())
    return article_prog.sub('', s)


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def kodi_albums():
    out = subprocess.run(['ssh', KODI_HOST, 'ls', '-1', ALBUMS_DIR],
                         capture_output=True, text=True, check=True).stdout
    return parse_albums(out.splitlines())


def parse_albums(folders):
    albums = []
    for folder in folders:
        parts = folder.split('__')
        if len(parts) != 3:
            continue  # loose mp3s, playlists
        artist, year, title = parts
        albums.append({'folder': folder, 'artist': normalize(artist), 'title': normalize(title), 'year': year})
    return albums


def collection(d):
    releases = []
    # basic_information already holds artist, title and cover, so no request per release
    for item in d.identity().collection_folders[0].releases:
        info = item.data['basic_information']
        releases.append({
            'id': info['id'],
            'artist': ' / '.join(a['name'] for a in info['artists']),
            'title': info['title'],
            'year': info['year'],
            'cover': info['cover_image'],
        })
    return releases


def spotify_token():
    if not spotify_id or not spotify_secret:
        print('no SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env, covers come from Discogs')
        return None
    response = requests.post('https://accounts.spotify.com/api/token', data={'grant_type': 'client_credentials'},
                             auth=(spotify_id, spotify_secret), timeout=20)
    response.raise_for_status()
    return response.json()['access_token']


def spotify_cover_url(token, release):
    """The largest cover of the Spotify album that matches artist and title, or None."""
    artist = normalize(parenthesis_prog.sub('', release['artist']))
    title = normalize(release['title'])
    response = requests.get('https://api.spotify.com/v1/search', headers={'Authorization': 'Bearer ' + token},
                            params={'q': '{} {}'.format(artist, title), 'type': 'album', 'limit': 10}, timeout=20)
    response.raise_for_status()
    best, best_score = None, SPOTIFY_MATCH_MIN
    for album in response.json()['albums']['items']:
        artist_score = max(similarity(artist, normalize(a['name'])) for a in album['artists'])
        score = min(artist_score, similarity(title, normalize(album['name'])))
        if score >= best_score and album['images']:
            best, best_score = album['images'][0]['url'], score  # largest first, 640 px
    return best


def download_cover(release, token):
    """The cover bytes, from Spotify if it has the album, else Discogs'; cached either way."""
    spotify = os.path.join(COVERS_DIR, '{}.spotify.jpg'.format(release['id']))
    discogs = os.path.join(COVERS_DIR, '{}.jpg'.format(release['id']))
    if token and not os.path.exists(spotify):
        url = spotify_cover_url(token, release)
        if url:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            with open(spotify, 'wb') as f:
                f.write(response.content)
    path = spotify if os.path.exists(spotify) else discogs
    release['cover_source'] = 'spotify' if path == spotify else 'discogs'
    if not os.path.exists(path):
        response = requests.get(release['cover'], headers={'User-Agent': 'VinylMatch/0.1'}, timeout=20)
        response.raise_for_status()
        with open(path, 'wb') as f:
            f.write(response.content)
    with open(path, 'rb') as f:
        return f.read()


def hsv(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))


def rgb(h, s, v=1.0):
    return [round(c * 255) for c in colorsys.hsv_to_rgb(h % 1.0, min(s, 1.0), min(v, 1.0))]


def lit_accent(color):
    """The accent at full brightness, for LEDs. A dark color looks more saturated than its
    numbers say, so it gains saturation as it's brightened: a dark red stays red, not salmon."""
    h, s, v = hsv(color)
    return rgb(h, s ** v if s else 0.0)


def second_color(data, accent):
    """The biggest part of the cover besides the accent and black, as (h, s, v), or None."""
    img = Image.open(BytesIO(data)).convert('RGB').resize((128, 128)).quantize(colors=16)
    palette = img.getpalette()[:16 * 3]
    ah, as_, _ = hsv(accent)
    for count, i in sorted(img.getcolors(), reverse=True):
        h, s, v = hsv(palette[i * 3:i * 3 + 3])
        if count < SECOND_MIN_SHARE * 128 * 128 or v < SECOND_MIN_VALUE:
            continue
        apart = min(abs(h - ah), 1 - abs(h - ah)) * 360
        if (s > 0.2 and as_ > 0.2 and apart >= HUE_APART) or abs(s - as_) >= SAT_APART:
            return h, s, v
    return None


def album_art(release, token):
    """16x16 cover, accent (text and peaks) and the bars' gradient, bottom to top.

    Only a cover whose colors couldn't be read at all leaves the matrix its own bars."""
    data = download_cover(release, token)
    raw = get_accent_color_from_bytes(data)
    art = {'art': base64.b64encode(artwork_pixels(data)).decode(), 'accent': [255, 255, 255], 'bars': None}
    if not raw:
        return art
    accent = lit_accent(raw)
    art['accent'] = accent
    h, s, _ = hsv(accent)
    second = second_color(data, accent)
    lit = rgb(h, max(s, BAR_MIN_SAT)) if s >= GRAY_MAX else [255, 255, 255]  # the accent, as bars
    if second:
        sh, ss, sv = second
        other = rgb(sh, ss, max(sv, NEUTRAL_VALUE)) if ss < NEUTRAL_MAX else rgb(sh, max(ss, SECOND_MIN_SAT))
        art['bars'] = [lit, other]
    elif s < GRAY_MAX:
        art['bars'] = GRAY_BARS
    else:
        art['bars'] = [lit, rgb(h, max(s, BAR_MIN_SAT) * TOP_PALER)]
    return art


def best_match(release, albums):
    artist = normalize(parenthesis_prog.sub('', release['artist']))  # 'Ghost (2)' -> 'ghost'
    title = normalize(release['title'])
    best, best_score = None, 0.0
    for album in albums:
        artist_score = similarity(artist, album['artist'])
        if artist_score < MATCH_MIN:
            continue
        score = min(artist_score, similarity(title, album['title']))
        if score > best_score:
            best, best_score = album, score
    return best, best_score


def main():
    d = connect()
    albums = kodi_albums()
    releases = collection(d)
    token = spotify_token()

    os.makedirs(COVERS_DIR, exist_ok=True)
    found, unsure, missing = [], [], []
    for release in releases:
        try:
            release.update(album_art(release, token))
        except Exception as e:
            print('no cover for {} - {}: {}'.format(release['artist'], release['title'], e))
        album, score = best_match(release, albums)
        release['folder'] = album['folder'] if album else None
        release['score'] = round(score, 2)
        if score >= MATCH_MIN:
            found.append(release)
        elif album:
            unsure.append(release)
        else:
            missing.append(release)

    print('\nUNSURE (artist found, title differs)')
    for r in sorted(unsure, key=lambda r: r['artist'].lower()):
        print('  {:.2f}  {} - {}  ->  {}'.format(r['score'], r['artist'], r['title'], r['folder']))
    print('\nMISSING')
    for r in sorted(missing, key=lambda r: r['artist'].lower()):
        print('  {} - {} ({})'.format(r['artist'], r['title'], r['year']))
    print('\nCOVER FROM DISCOGS (not found on Spotify)')
    for r in sorted((r for r in releases if r.get('cover_source') == 'discogs'), key=lambda r: r['artist'].lower()):
        print('  {} - {}'.format(r['artist'], r['title']))
    print('\nfound {}, unsure {}, missing {} of {} releases; {} covers from Spotify'.format(
        len(found), len(unsure), len(missing), len(releases),
        sum(r.get('cover_source') == 'spotify' for r in releases)))

    with open('matches.json', 'w') as f:
        json.dump({'found': found, 'unsure': unsure, 'missing': missing}, f, indent=2)


if __name__ == '__main__':
    main()
