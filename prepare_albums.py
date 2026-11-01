import argparse
import os
import re
import shlex
import subprocess

from mutagen import MutagenError
from mutagen.id3 import TALB, TDRC, TIT2, TPE1, TPE2, TRCK
from mutagen.mp3 import MP3

from discogs_kodi_matcher import (ALBUMS_DIR, KODI_HOST, MATCH_MIN, SSH, album_score, collection, connect,
                                  normalize, plain_artist, release_names, tar_stream)

# Gets a folder of downloaded albums ready for indexing: finds each album in the
# Discogs collection, renames its folder to Artist__Year__Album with Discogs'
# spelling and year, tags the mp3s and, with --upload, copies them to kodi.
# Without --apply it only prints what it would do.

number_prog = re.compile(r'^(\d{1,3})[-_\s.]+')


def parse_folder(name):
    """Artist, year and album from 'Artist__Year__Album' or 'Artist - Album'."""
    parts = name.split('__')
    if len(parts) == 3:
        artist, year, album = (p.replace('_', ' ').strip() for p in parts)
        return artist, year, album
    if ' - ' in name:
        artist, album = name.split(' - ', 1)
        return artist.strip(), '', album.strip()
    return None


def find_release(artist, album, releases):
    artist, album = normalize(artist), normalize(album)
    best, best_score = None, 0.0
    for release in releases:
        score = album_score(*release_names(release), artist, album)
        if score > best_score:
            best, best_score = release, score
    return (best, best_score) if best_score >= MATCH_MIN else (None, best_score)


def original_year(d, release):
    """The master release's year: reissues carry their pressing year, 2022 for a 1997 album."""
    try:
        master = d.release(release['id']).master
        if master and master.year:
            return master.year
    except Exception as e:  # discogs_client raises plain HTTP errors
        print('  no master year for {}: {}'.format(release['title'], e))
    return release['year']


def folder_name(artist, year, album):
    return '__'.join(s.strip().replace('/', '-').replace(' ', '_') for s in (artist, str(year or ''), album))


def mp3s(path):
    return sorted(f for f in os.listdir(path) if f.lower().endswith('.mp3') and not f.startswith('.'))


def tagged_year(path):
    for f in mp3s(path):
        try:
            tags = MP3(os.path.join(path, f)).tags
        except MutagenError:
            continue
        if tags and 'TDRC' in tags:
            return str(tags['TDRC'])[:4]
    return ''


def title_from_filename(filename, artists):
    title = number_prog.sub('', os.path.splitext(filename)[0]).replace('_', ' ').strip()
    for artist in artists:  # '01.Northlane - Details Matter.mp3'
        title = re.sub(r'^' + re.escape(artist) + r'\s*-\s*', '', title, flags=re.IGNORECASE)
    return title.strip()


def tag_album(path, artist, year, album, folder_artist, overwrite):
    """Artist, album and year everywhere (only where missing unless overwrite), title and
    track number only where missing, so correct titles from the tags survive."""
    files = mp3s(path)
    for position, f in enumerate(files, 1):
        song = MP3(os.path.join(path, f))
        if song.tags is None:
            song.add_tags()
        tags = song.tags
        for frame, text in ((TPE1, artist), (TPE2, artist), (TALB, album), (TDRC, year)):
            if text and (overwrite or frame.__name__ not in tags):
                tags.add(frame(encoding=3, text=str(text)))
        if not str(tags.get('TIT2', '')).strip():
            tags.add(TIT2(encoding=3, text=title_from_filename(f, {artist, folder_artist})))
        if 'TRCK' not in tags:
            number = number_prog.match(f)
            tags.add(TRCK(encoding=3, text='{}/{}'.format(int(number.group(1)) if number else position, len(files))))
        song.save()


def upload(base, folder):
    target = '{}/{}'.format(ALBUMS_DIR, folder)
    if subprocess.run(SSH + [KODI_HOST, 'test', '-e', shlex.quote(target)]).returncode == 0:
        print('  already on {}, skipped'.format(KODI_HOST))
        return
    pack = tar_stream(base, folder)
    unpack = subprocess.run(SSH + [KODI_HOST, 'tar', '-C', shlex.quote(ALBUMS_DIR), '-xf', '-'], stdin=pack.stdout)
    pack.stdout.close()
    if pack.wait() != 0 or unpack.returncode != 0:
        raise RuntimeError('upload of {} failed'.format(folder))
    print('  uploaded to {}:{}'.format(KODI_HOST, target))


def main():
    parser = argparse.ArgumentParser(description='Rename, tag and upload downloaded albums for indexing')
    parser.add_argument('directory', help='folder with one subfolder per album, e.g. ~/Desktop/new_music')
    parser.add_argument('--apply', action='store_true', help='rename and tag, otherwise only show the plan')
    parser.add_argument('--upload', action='store_true', help='also copy albums found on Discogs to kodi')
    args = parser.parse_args()
    base = os.path.abspath(os.path.expanduser(args.directory))

    d = connect()
    releases = collection(d)
    found = []
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        parsed = parse_folder(name) if os.path.isdir(path) and not name.startswith(('.', '!')) else None
        if not parsed or not mp3s(path):
            continue
        folder_artist, year, album = parsed
        release, score = find_release(folder_artist, album, releases)
        if release:
            artist = plain_artist(release)
            year, album = original_year(d, release) or year or tagged_year(path), release['title'].strip()
            status = 'discogs {:.2f}'.format(score)
        else:
            artist, year = folder_artist, year or tagged_year(path)
            status = 'NOT IN DISCOGS ({:.2f}), will not be indexed'.format(score)
        new_name = folder_name(artist, year, album)
        print('{}\n  -> {}  [{}]'.format(name, new_name, status))

        if not args.apply:
            continue
        new_path = os.path.join(base, new_name)
        # macOS ignores case: a rename that only changes it finds the album itself
        if new_path != path and os.path.exists(new_path) and not os.path.samefile(new_path, path):
            print('  {} already exists, skipped'.format(new_name))
            continue
        tag_album(path, artist, year, album, folder_artist, overwrite=bool(release))
        if new_path != path:
            os.rename(path, new_path)
        if release:
            found.append(new_name)

    if not args.apply:
        print('\nnothing changed, run again with --apply (and --upload) to do it')
        return
    if args.upload:
        for folder in found:
            print(folder)
            upload(base, folder)
        print('\nnext: .venv/bin/python discogs_kodi_matcher.py && .venv/bin/python vinyl_indexer.py')


if __name__ == '__main__':
    main()
