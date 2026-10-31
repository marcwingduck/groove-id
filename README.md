# groove-id

Recognizes the record on the turntable and shows it on the [vinyl spectrum analyzer](../vinyl-spectrum-analyzer)'s LED matrix. groove-id doesn't use Shazam or any other cloud service. It fingerprints the MP3s of the records in your Discogs collection with [Olaf](https://github.com/JorenSix/Olaf), and the matrix sends a 10 s clip whenever a new track starts.

Three machines are involved:

| Machine | Role |
|---------|------|
| Kodi box (`kodi`, 192.168.0.37) | the MP3s, on the USB drive mounted at `/media/hdhdhd`, under `audio/albums/Artist__Year__Album` |
| Mac | runs the matcher and the indexer: talks to Discogs and moves albums from `kodi` to `tape`, so `tape` needs no access to `kodi` |
| `tape` (Raspberry Pi 5, also runs snapserver-lcd) | holds the fingerprints in `~/vinyl-id`, runs the listener |

## Files

| File | Runs on | What it does |
|------|---------|--------------|
| `discogs_kodi_matcher.py` | Mac | Pulls the Discogs collection, matches each release to an album folder on `kodi` and writes `matches.json`: `found`, `unsure` (artist there, album not) and `missing`. Each release gets its 16×16 cover, accent and the bars' two colors, computed with snapserver-lcd's `matrix.py` and `accent_color.py` |
| `prepare_albums.py` | Mac | Gets a folder of downloaded albums ready: finds each in the Discogs collection, renames it to `Artist__Year__Album` with Discogs' spelling and the original (master) year, tags the MP3s and with `--upload` copies them to `kodi` |
| `vinyl_indexer.py` | Mac | Streams each `found` album with `ssh kodi tar` (or, with `--local DIR`, from a folder on the Mac) into `store_album.sh` on `tape` and writes `tracks.json`. It skips releases already in `tracks.json` and saves after every album, so an interrupted run resumes |
| `store_album.sh` | `tape` | Unpacks one album to a temporary folder, converts each MP3 to 16 kHz mono and stores it with `olaf_core store` |
| `groove_listen.py` | `tape` | Waits for the matrix's `clip <reason>` broadcast, fetches the clip, queries Olaf and sends the matrix a `V` datagram with cover, colors, artist and title. It also posts the track to snapserver-lcd on the same Pi, which shows it on the LCD in the cover's accent color until the track is over, or until the matrix broadcasts `stop` (the turntable went silent), which the listener relays |
| `groove-id.service` | `tape` | Runs the listener at boot |
| `deploy.sh` | Mac | Copies the listener, `store_album.sh` and the service to `tape`, installs the service and restarts it |

`tracks.json` maps Olaf's track names (`<release id>/<path in the album>`) to release, artist, album, title and duration. `releases.json` holds each release's 16×16 cover and colors, taken from `matches.json`. The indexer only adds to it, so a record stays recognizable with its cover after its MP3s are deleted from `kodi`. Both files are copied to `~/vinyl-id` on `tape`, and the listener rereads them on every match.

On `tape`:

```
~/vinyl-id/
  Olaf/              source, built with gcc
  .olaf/db/          the fingerprints (LMDB), ~290 MB for ~1000 tracks
  tracks.json, releases.json
  store_album.sh, groove_listen.py, groove-id.service
  clips/             every clip the matrix sent, raw 16-bit mono 20 kHz, for tuning
```

## Setup

On the Mac, with SSH access to `kodi` and `tape`, and snapserver-lcd checked out next to this folder:

```sh
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`.env` holds a Discogs personal access token (Settings → Developers) and, for the covers, the client ID and secret of a Spotify app (developer.spotify.com). Spotify's cover is taken where it has the album, since Discogs' are often photos of the sleeve; without the Spotify keys, or for albums it doesn't have, the Discogs cover is used:

```
DISCOGS_PERSONAL_ACCESS_TOKEN=...
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
```

On `tape`, Olaf needs ffmpeg and gcc. Its Makefile uses `-std=c11`, which hides `realpath` from gcc 14, so switch to `gnu11`:

```sh
sudo apt install -y ffmpeg git
git clone https://github.com/JorenSix/Olaf ~/vinyl-id/Olaf
cd ~/vinyl-id/Olaf
sed -i 's/-std=c11/-std=gnu11/' Makefile
make compile_core
```

Olaf keeps its database in `$HOME/.olaf/db`, so the scripts run it with `HOME=~/vinyl-id`.

The listener, from the Mac, installed as a service and restarted, after every change too:

```sh
./deploy.sh
ssh tape 'journalctl -u groove-id -f'
```

The listener uses snapserver-lcd's `matrix.py` from `~/snapserver-lcd` on `tape`. After a change there, run snapserver-lcd's `deploy.sh` first, then this one, which restarts the listener with the new module.

## New records

Albums that aren't on `kodi` yet, in a folder with one subfolder per album (`Artist - Album` or `Artist__Year__Album`):

```sh
.venv/bin/python prepare_albums.py ~/Desktop/new_music                    # shows the plan
.venv/bin/python prepare_albums.py ~/Desktop/new_music --apply --upload   # renames, tags, copies to kodi
```

Downloads that are only good enough to fingerprint skip `kodi`: `--apply` without `--upload`, then the matcher, then the indexer straight from the folder. Afterwards the folder can go, and the matcher's `MISSING` list keeps showing these records as wanting proper MP3s:

```sh
.venv/bin/python discogs_kodi_matcher.py
.venv/bin/python vinyl_indexer.py --local ~/Desktop/new_music
```

Then, and after adding records on Discogs:

```sh
.venv/bin/python discogs_kodi_matcher.py
.venv/bin/python vinyl_indexer.py
```

Only new albums get indexed, at ~17 s per album. The matcher's `UNSURE` and `MISSING` lists show which records have no MP3s on `kodi`. Those can't be recognized until an MP3 version of them is on `kodi`.

The indexer moves ~40 Mbit/s through the Mac while it runs, which can make snapclients on WiFi drop out.

## Recognition

The matrix asks for a clip 4 s after a needle drop or after a gap between tracks, and every 2 minutes when a side has no gaps. Tuning is in the firmware, see its README. A track counts as recognized when at least `MIN_MATCHES` (10) fingerprints agree. Wrong candidates stay at 6–8, and right ones land at 14–70, even with the board's 12-bit ADC. Olaf reports where in the track the clip sits, and the listener prints that to the log.

Olaf tolerates noise well, but not wrong speed. A clip 1% fast lost most of its matches in a test, so a turntable that runs noticeably off won't be recognized.
