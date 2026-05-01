# Incoming Music Drop Folder

Drop any audio file here (.mp3, .m4a, .aac, .flac, etc.) and the pipeline
will process it automatically.

## How to use

1. Copy or move audio files into this folder
2. Run: make watch-intake   (daemon — watches continuously)
   Or:   make intake-once   (process current files then exit)

## What happens

Each file goes through the full pipeline:
  - AcoustID fingerprint → MusicBrainz artist/title/album lookup
  - Renamed and moved to corrected_music/{Artist}/{Album}/
  - Added to Traktor collection.nml
  - Lyrics fetched (LRCLIB → Genius fallback)
  - AI summary generated
  - Album art fetched and embedded
  - Autocue points set (Vocal In, Vocal Out, drop beat)
  - Uploaded to Google Drive (Music/ and Traktor/ metadata)
  - NML committed to git

## Results

- Success → moved to incoming/done/
- Failure → moved to incoming/failed/ (check state/intake_watcher.log)
