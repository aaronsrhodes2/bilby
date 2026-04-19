# music organize — Traktor Library Sanitizer

A pipeline that takes a messy Traktor Pro 4 music library (tens of thousands of tracks
accumulated over years) and produces a clean, deduplicated, properly tagged copy —
ready to be loaded back into Traktor.

**Starting point:** ~24,000+ tracks in `~/Music`, many duplicated, poorly tagged,
inconsistently named, or not real individual tracks at all.

**End result:** ~23,700 tracks in `corrected_music/` with clean tags, one genre per
track, consistent artist names, no duplicates, and an updated `collection.nml` Traktor
can load directly.

---

## What each stage does

### Stage 1 — Scan (`stage1_scan.py`)
Walks every audio file under `~/Music` and records path, file size, duration, and basic
tags (artist, title, album) into `state/scan.json`. No files are moved or changed.

### Stage 2 — Hash dedup (`stage2_dedup.py`)
Computes an MD5 of the audio content (not the tags) for every file. Groups files with
identical hashes and picks one keeper per group — the one with the most complete tags
wins. Candidates for deletion written to `state/dedup_report.json`.

### Stage 2b — Metadata dedup (`stage2b_metadata_dedup.py`)
A second dedup pass that catches same-song files that differ slightly in encoding or
trimming. Matches by (artist + title) after normalizing both strings. Cross-checks
against AcoustID fingerprints where available.

### Stage 3 — Acoustic fingerprinting (`stage3_fingerprint.py`)
Submits files to AcoustID (via `lib/acoustid_client.py`) to get a MusicBrainz recording
ID for each track. Also fetches MusicBrainz metadata (canonical artist name, album,
release year, genre tags). Results cached in `state/metadata.json`.
Rate-limited to 3 requests/sec per AcoustID ToS.

### Stage 4 — Copy to clean tree (`stage4_copy.py`)
Copies keeper files into `corrected_music/Artist/Album/Track.ext`, applying corrected
tags from Stage 3. Skips files over 50 MB (almost certainly not individual tracks).
WAV files flagged for later conversion.

### Stage 5 — Build Traktor NML (`stage5_traktor.py`)
Generates `corrected_traktor/collection.nml` from scratch, pointing to the
`corrected_music/` tree. Also copies playlist `.nml` files from Traktor's own data
directory, updating paths to point to the new locations.

### Stage 6 — Title cleaning (`stage6_title_clean.py`)
Finds titles with noise artifacts left over from filename-based imports:
trailing ` - Artist Name`, `(Official Video)`, `[HD]`, `feat.` suffixes on titles
that should be on the artist, redundant album-title prefixes, etc.
Renames files and rewrites NML entries in place. (~514 tracks fixed.)

### Stage 7 — Traktor dedup (`stage7_traktor_dedup.py`)
A final dedup pass operating entirely on the NML. Finds entries pointing to the same
file, entries where artist+title match after normalization, and entries with matching
AcoustID fingerprints. Removes the weaker duplicate from the NML (keeps the one with
more complete metadata). Does not delete any files.

### Stage 8a — Remove long recordings (`stage8a_remove_long.py`)
Removes tracks longer than 12 minutes. These are almost always DJ sets, live show
recordings, or full album compilations — Traktor cannot analyze them properly and they
clutter the library. Deletes files from `corrected_music/` and removes their NML
entries. (Default threshold: 12 min; override with `--minutes N`.)

### Stage 8b — Artist name normalization (`stage8b_normalize.py`)
Normalizes artist names across the whole library:
- Removes `feat. / ft. / featuring` suffixes from artist fields (moves them to title if needed)
- Fixes ALL-CAPS names (preserves known acronyms: EBM, IDM, AC/DC, etc.)
- Merges `The X` and `X` artist variants — whichever spelling has more tracks wins
- Groups artists by MusicBrainz artist ID and picks the canonical MB spelling
- Rewrites both file tags and NML ENTRY ARTIST attributes

### Stage 8c — Genre normalization (`stage8c_genre_normalize.py`)
Maps the hundreds of genre strings in the library down to ~25 canonical genres:
`Deathrock, Gothic Rock, Darkwave, Coldwave, Post-Punk, EBM, Industrial, New Wave,
Synthpop, Ambient, IDM, Electronic, Alternative Rock, Indie Rock, Punk, Metal,
Hard Rock, Classic Rock, Rock, Folk, Pop, Hip-Hop, Soundtrack, Comedy, Other`

Resolution order per track:
1. MusicBrainz genre (from `state/mb_genre_cache.json`) if available
2. Existing tag already in canonical list — keep it
3. Multi-value tag (e.g. `"Ebm / Industrial / Synth-Pop"`) — split, map each part,
   pick highest-priority canonical
4. Single non-canonical string — map via GENRE_MAP lookup table
5. No match — infer from the most common genre among tracks by the same artist

Writes one genre per track to both file tags and NML.

### Stage 8d — Unknown artist recovery (`stage8d_unknown_artists.py`)
Recovers artist names for tracks tagged `<unknown>` or blank:
- Parses `Artist - Title` patterns embedded in the filename or title tag
- Cross-checks candidate artist names against the known artist list in the collection
- Applies high-confidence recoveries automatically; reports medium-confidence for review

### Stage 8e — Artist cleanup (`stage8e_artist_cleanup.py`)
Final artist tag sweep: removes stray punctuation, fixes unicode quote variants,
catches remaining ALL-CAPS or all-lowercase names missed by 8b.

### Stage 8f — WAV to MP3 conversion (`stage8f_wav_convert.py`)
Converts remaining WAV files to MP3 (LAME V0 VBR, ~245 kbps average) using ffmpeg.
Tags are preserved via mutagen. The NML entry is updated to point to the new `.mp3`
path. Original WAVs are deleted after successful conversion and verification.
Deduplication guard prevents converting a WAV that is a duplicate of an existing MP3.

### Stage 8g (inline) — Genre inference from artist
For tracks that still have no genre after 8c, looks up the most common genre among
other tracks by the same artist in the collection and assigns it. Falls back to a
hardcoded `ARTIST_GENRE_MAP` for well-known artists.

### Stage 8h — Junk track deletion (`stage8h` logic, run inline)
Deletes three categories of confirmed junk:
- **Live recordings** — tracks identified by title patterns like `(Live)`, `Live at …`,
  `(Unplugged)` combined with structural context. Not individual studio tracks.
- **A&C mystery tracks** — import artifacts with titles like `A&CdaysOFgold` that do not
  correspond to real audio. Origin unknown; confirmed junk.
- **Track-N placeholders** — entries titled `Track 01`, `Track 02`, etc. with no other
  identifying information.
- **Stem files** — multi-channel stems that are not playable tracks.

Removes both the NML entries and the files from `corrected_music/`. (~185 tracks, ~1 GB.)

---

## Prerequisites

```bash
python3 -m venv .venv
.venv/bin/pip install mutagen requests
# ffmpeg must be installed: brew install ffmpeg
```

AcoustID and MusicBrainz fetches (Stages 2b, 3, 8c) require a free AcoustID API key.
Set it in `lib/acoustid_client.py` or via the `ACOUSTID_API_KEY` environment variable.

---

## Running the pipeline

Each stage is a standalone script. Run dry-run first, then `--apply`:

```bash
.venv/bin/python stage1_scan.py
.venv/bin/python stage2_dedup.py
.venv/bin/python stage2_dedup.py --apply
# ... and so on through stage8x
```

Most scripts print a summary of what they would change, then require `--apply` to
actually write anything. State files in `state/` persist between runs so stages can
be re-run safely.

---

## Loading into Traktor

See `switch_library.md` for the complete procedure. Short version:

1. Quit Traktor
2. Copy `corrected_traktor/collection.nml` to
   `~/Documents/Native Instruments/Traktor 4.0.2/collection.nml`
3. In Traktor Preferences → File Management, add `corrected_music/` as a music folder
4. Relaunch Traktor

---

## Directory layout

```
corrected_music/          clean audio files (Artist/Album/Track.ext)
corrected_traktor/        NML files for Traktor
lib/                      shared helpers (NML parser, tag cleaner, MB/AcoustID clients)
state/                    JSON state files written between stages
misc/                     session logs, operatic play scenes
stage1_scan.py            … through stage8x_*.py
switch_library.md         step-by-step Traktor swap procedure
traktor_sanitize.py       standalone consolidation script (portable, no API keys needed)
```
