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
from accent_color import get_accent_color_from_bytes, vivid  # noqa: E402
from matrix import artwork_pixels  # noqa: E402


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
personal_access_token = os.getenv('PERSONAL_ACCESS_TOKEN')
if not personal_access_token:
    sys.exit('PERSONAL_ACCESS_TOKEN must be set in .env')

KODI_HOST = 'kodi'  # ssh alias for 192.168.0.37
ALBUMS_DIR = '/media/hdhdhd/audio/albums'
MATCH_MIN = 0.85  # title similarity needed, 0..1
COVERS_DIR = 'covers'  # downloaded once, reruns reuse them
MIN_HUE_APART = 1 / 6  # the bars' second color differs from the accent by 60 degrees or more
GRAY_MAX = 0.15        # covers duller than this keep the matrix's own bar colors
BAR_MIN_SAT = 0.6      # muted covers would light the bars nearly white
HUE_SHIFT = 1 / 12     # one-colored covers: the bars run from 30 degrees beside the accent to it

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


def download_cover(release):
    path = os.path.join(COVERS_DIR, '{}.jpg'.format(release['id']))
    if not os.path.exists(path):
        response = requests.get(release['cover'], headers={'User-Agent': 'VinylMatch/0.1'}, timeout=20)
        response.raise_for_status()
        with open(path, 'wb') as f:
            f.write(response.content)
    with open(path, 'rb') as f:
        return f.read()


def second_color(data, accent):
    """The most vibrant palette color whose hue is clearly not the accent's, or None."""
    img = Image.open(BytesIO(data)).convert('RGB').resize((128, 128))
    palette = img.quantize(colors=16).getpalette()[:16 * 3]
    accent_hue = colorsys.rgb_to_hsv(*(c / 255 for c in accent))[0]
    best, best_score = None, 0.25  # duller than this reads as gray on the LEDs
    for i in range(0, len(palette), 3):
        rgb = tuple(palette[i:i + 3])
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        apart = abs(h - accent_hue)
        if v < 0.2 or min(apart, 1 - apart) < MIN_HUE_APART or s * v < best_score:
            continue
        best, best_score = rgb, s * v
    return best


def bar_color(hue, saturation):
    rgb = colorsys.hsv_to_rgb(hue % 1.0, max(saturation, BAR_MIN_SAT), 1.0)
    return [round(c * 255) for c in rgb]


def album_art(release):
    """16x16 cover, accent (text and peaks) and the bars' gradient, bottom to top.

    A cover without color gets no bar colors, so the matrix keeps its own."""
    data = download_cover(release)
    accent = get_accent_color_from_bytes(data)
    art = {'art': base64.b64encode(artwork_pixels(data)).decode(), 'accent': [255, 255, 255], 'bars': None}
    if accent:
        accent = vivid(accent)
        art['accent'] = list(accent)
        h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in accent))
        if s >= GRAY_MAX:
            other = second_color(data, accent)
            if other:
                oh, os_, _ = colorsys.rgb_to_hsv(*(c / 255 for c in other))
                art['bars'] = [bar_color(oh, os_), bar_color(h, s)]
            else:
                art['bars'] = [bar_color(h - HUE_SHIFT, s), bar_color(h, s)]
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

    os.makedirs(COVERS_DIR, exist_ok=True)
    found, unsure, missing = [], [], []
    for release in releases:
        try:
            release.update(album_art(release))
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
    print('\nfound {}, unsure {}, missing {} of {} releases'.format(
        len(found), len(unsure), len(missing), len(releases)))

    with open('matches.json', 'w') as f:
        json.dump({'found': found, 'unsure': unsure, 'missing': missing}, f, indent=2)


if __name__ == '__main__':
    main()
