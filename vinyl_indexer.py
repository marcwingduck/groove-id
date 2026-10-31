import argparse
import json
import os
import shlex
import subprocess

from discogs_kodi_matcher import ALBUMS_DIR, KODI_HOST, MATCH_MIN, SSH, best_match, parse_albums, plain_artist, tar_stream

# Fingerprints the mp3s of every matched record into Olaf's database on tape.
# The albums travel from the kodi drive through this machine (ssh kodi | ssh tape),
# so tape needs no access to kodi. Already indexed releases are skipped.
# With --local DIR the albums come from a folder on this machine instead, e.g.
# downloads that are only good enough to fingerprint and never go to kodi.

TAPE_HOST = 'tape'
REMOTE_DIR = 'vinyl-id'  # in tape's home
TRACKS_FILE = 'tracks.json'
RELEASES_FILE = 'releases.json'  # covers and colors per release, kept after the mp3s are gone


def load_tracks():
    if not os.path.exists(TRACKS_FILE):
        return {}
    with open(TRACKS_FILE) as f:
        return json.load(f)


def save_tracks(tracks):
    with open(TRACKS_FILE, 'w') as f:
        json.dump(tracks, f, indent=2, ensure_ascii=False)
    subprocess.run(['scp', '-q', TRACKS_FILE, '{}:{}/{}'.format(TAPE_HOST, REMOTE_DIR, TRACKS_FILE)], check=True)


def index_release(release, local_dir=None):
    if local_dir:
        source = tar_stream(local_dir, release['folder'])
    else:
        # ssh hands the command to a remote shell, so names like You're_Not_You need quoting
        source = subprocess.Popen(SSH + [KODI_HOST, 'tar', '-C', ALBUMS_DIR, '-cf', '-', shlex.quote(release['folder'])],
                                  stdout=subprocess.PIPE)
    tape = subprocess.run(SSH + [TAPE_HOST, '{}/store_album.sh'.format(REMOTE_DIR), str(release['id'])],
                          stdin=source.stdout, capture_output=True, text=True)
    source.stdout.close()
    if tape.returncode != 0:
        source.kill()  # tar is stuck writing into the dead connection
    if source.wait() != 0 or tape.returncode != 0:
        raise RuntimeError(tape.stderr.strip() or 'connection to {} lost'.format(TAPE_HOST))

    tracks = {}
    for line in tape.stdout.splitlines():
        name, duration, title = line.split('\t')
        tracks[name] = {
            'release': release['id'],
            'artist': plain_artist(release),
            'album': release['title'],
            'title': title or os.path.splitext(os.path.basename(name))[0],
            'duration': float(duration),
            'cover': release['cover'],
        }
    return tracks


def update_releases(matches):
    """Keeps every release's 16x16 cover and colors in releases.json, once per release.

    Entries are never removed, so a record stays recognizable with its cover after
    its mp3s are gone from kodi and the matcher lists it as missing."""
    releases = {}
    if os.path.exists(RELEASES_FILE):
        with open(RELEASES_FILE) as f:
            releases = json.load(f)
    for group in matches.values():
        for r in group:
            if 'art' in r:
                releases[str(r['id'])] = {key: r[key] for key in ('art', 'accent', 'bars')}
    with open(RELEASES_FILE, 'w') as f:
        json.dump(releases, f, indent=2)


def local_releases(matches, local_dir):
    """Releases from matches.json whose album folder is in local_dir, matched like the matcher does."""
    folders = [f for f in os.listdir(local_dir) if os.path.isdir(os.path.join(local_dir, f))]
    albums = parse_albums(folders)
    releases = []
    for release in (r for group in matches.values() for r in group):
        album, score = best_match(release, albums)
        if score >= MATCH_MIN:
            releases.append(dict(release, folder=album['folder']))
    for folder in sorted(set(folders) - {r['folder'] for r in releases}):
        print('no Discogs release for {}, skipped (run prepare_albums.py and the matcher first)'.format(folder))
    return releases


def main():
    parser = argparse.ArgumentParser(description='Fingerprint matched records into Olaf on tape')
    parser.add_argument('--local', metavar='DIR', help='index Artist__Year__Album folders from DIR instead of kodi')
    args = parser.parse_args()
    local_dir = os.path.abspath(os.path.expanduser(args.local)) if args.local else None

    with open('matches.json') as f:
        matches = json.load(f)
    releases = local_releases(matches, local_dir) if local_dir else matches['found']
    # the listener on tape takes covers and colors from releases.json; tracks.json goes along on
    # every run too, so a copy that failed last time (tape dropping off the network) is mended
    update_releases(matches)
    files = ['store_album.sh', RELEASES_FILE] + ([TRACKS_FILE] if os.path.exists(TRACKS_FILE) else [])
    subprocess.run(['scp', '-q'] + files + ['{}:{}/'.format(TAPE_HOST, REMOTE_DIR)], check=True)

    tracks = load_tracks()
    done = {t['release'] for t in tracks.values()}
    todo = [r for r in releases if r['id'] not in done]
    print('{} releases indexed, {} to go'.format(len(done), len(todo)))

    for i, release in enumerate(todo, 1):
        print('[{}/{}] {} - {}'.format(i, len(todo), release['artist'], release['title']), flush=True)
        try:
            new = index_release(release, local_dir)
        except RuntimeError as e:
            print('  failed: {}'.format(e))
            continue
        print('  {} tracks'.format(len(new)))
        tracks.update(new)
        save_tracks(tracks)  # after every album, so an interrupted run resumes


if __name__ == '__main__':
    main()
