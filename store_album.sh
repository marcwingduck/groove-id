#!/bin/bash
# Runs on tape: reads one album as a tar stream on stdin and stores the
# fingerprints of every mp3 in Olaf's database (~/vinyl-id/.olaf/db).
# Prints one line per track: name, duration in s, title tag (tab separated).
# usage: store_album.sh <discogs_release_id>
set -euo pipefail

id=$1
base=$HOME/vinyl-id
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$base/.olaf/db"  # olaf won't create it

mkdir "$tmp/album"
tar -C "$tmp/album" -xf -

find "$tmp/album" -iname '*.mp3' | sort | while read -r f; do
    name="$id/${f#$tmp/album/}"  # release id and path inside the album, e.g. 2CD folders
    ffmpeg -nostdin -loglevel error -i "$f" -ac 1 -ar 16000 -f f32le -y "$tmp/audio.raw"
    HOME=$base "$base/Olaf/bin/olaf_core" store "$tmp/audio.raw" "$name" >/dev/null
    duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
    title=$(ffprobe -v error -show_entries format_tags=title -of csv=p=0 "$f" | tr '\t' ' ')
    printf '%s\t%s\t%s\n' "$name" "$duration" "$title"
done
