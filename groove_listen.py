import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

# Runs on tape: waits for the matrix to broadcast "clip <reason>" (a track just
# started), fetches 10 s of audio from it, looks the clip up in Olaf's database
# and tells the matrix what plays, with the cover and the bars' colors.

sys.path.insert(0, os.path.expanduser('~/snapserver-lcd'))
from matrix import track_packet  # noqa: E402  the same packet as the spotify view

ASK_PORT = 4213
CLIP_PORT = 4212
MATRIX_PORT = 4210
CLIP_HZ = 20000  # raw 16-bit mono, as sampled by the matrix
MIN_MATCHES = 10  # fingerprints agreeing on one track before it counts
CLIP_S = 10  # length of the matrix's clip
LCD_URL = 'http://127.0.0.1:5000/'  # snapserver-lcd on this Pi shows the record like a Spotify track

BASE = os.path.expanduser('~/vinyl-id')
OLAF = os.path.join(BASE, 'Olaf/bin/olaf_core')
TRACKS_FILE = os.path.join(BASE, 'tracks.json')
RELEASES_FILE = os.path.join(BASE, 'releases.json')  # covers and colors per release
CLIPS_DIR = os.path.join(BASE, 'clips')  # kept for tuning


def fetch_clip(host):
    with socket.create_connection((host, CLIP_PORT), timeout=20) as s:
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)


def query(raw, name):
    path = os.path.join(CLIPS_DIR, name)
    with open(path + '.raw', 'wb') as f:
        f.write(raw)
    # the highpass takes out what is left of the ADC bias and the mains hum
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y',
                    '-f', 's16le', '-ar', str(CLIP_HZ), '-ac', '1', '-i', path + '.raw',
                    '-af', 'highpass=f=80', '-ar', '16000', '-f', 'f32le', path + '.f32'], check=True)
    out = subprocess.run([OLAF, 'query', path + '.f32', name], env=dict(os.environ, HOME=BASE),
                         capture_output=True, text=True).stdout
    os.remove(path + '.f32')

    # matches, query start, query end, name, id, reference start, reference end;
    # names may hold commas
    results = []
    for line in out.splitlines():
        head = line.split(', ', 3)
        if len(head) < 4 or not head[0].isdigit():
            continue
        track, _, ref_start, _ = head[3].rsplit(', ', 3)
        results.append((int(head[0]), track, float(ref_start) - float(head[1])))
    return max(results, default=None)


def release_art(release_id):
    try:
        with open(RELEASES_FILE) as f:
            return json.load(f).get(str(release_id))
    except FileNotFoundError:  # the indexer writes it on its next run
        return None


def show(sock, host, track, art):
    """Every recognition is sent; the matrix only pops in a track it hasn't shown."""
    pixels = base64.b64decode(art['art']) if art else None
    accent = art['accent'] if art else [255, 255, 255]
    bars = bytes(art['bars'][0] + art['bars'][1]) if art and art['bars'] else bytes(6)
    sock.sendto(track_packet(b'V', track['artist'], track['title'], pixels, accent, bars), (host, MATRIX_PORT))


def tell_lcd(event):
    request = urllib.request.Request(LCD_URL, json.dumps(event).encode(), {'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(request, timeout=2).close()
    except OSError as e:  # the LCD service being down mustn't stop the matrix
        print('  lcd: {}'.format(e), flush=True)


def show_on_lcd(track, offset, art):
    """The LCD shows artist and title in the cover's accent until the track should be over,
    or until the matrix says stop."""
    seconds_left = track['duration'] - offset - CLIP_S  # the clip started at offset and is over by now
    tell_lcd({'event': 'vinyl', 'artist': track['artist'], 'title': track['title'],
              'accent': art['accent'] if art else None, 'seconds': max(seconds_left, 0)})


def main():
    os.makedirs(CLIPS_DIR, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', ASK_PORT))
    print('waiting for the matrix on udp {}'.format(ASK_PORT), flush=True)

    while True:
        msg, (host, _) = sock.recvfrom(64)
        if msg == b'stop':  # the turntable went silent
            print('{} stop from {}'.format(time.strftime('%Y%m%d-%H%M%S'), host), flush=True)
            tell_lcd({'event': 'vinyl_stop'})
            continue
        if not msg.startswith(b'clip'):
            continue
        reason = msg.decode(errors='replace').partition(' ')[2]
        name = time.strftime('%Y%m%d-%H%M%S-') + reason
        print('{} from {}: fetching'.format(name, host), flush=True)
        try:
            raw = fetch_clip(host)
        except OSError as e:
            print('  fetch failed: {}'.format(e), flush=True)
            continue

        best = query(raw, name)
        if best is None or best[0] < MIN_MATCHES:
            print('  no match{}'.format(' ({} fps)'.format(best[0]) if best else ''), flush=True)
            continue
        matches, track, offset = best
        with open(TRACKS_FILE) as f:
            t = json.load(f).get(track)  # reread, the indexer may have added albums
        if t is None:
            print('  {} is not in tracks.json'.format(track), flush=True)
            continue
        print('  {} - {} - {}  at {:.0f} s ({} fps)'.format(
            t['artist'], t['album'], t['title'], offset, matches), flush=True)
        art = release_art(t['release'])  # read once: the file holds every cover
        show(sock, host, t, art)
        show_on_lcd(t, offset, art)


if __name__ == '__main__':
    main()
