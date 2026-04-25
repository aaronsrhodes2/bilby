## Mac Bilby — convenience targets
## ─────────────────────────────────────────────────────────────────────────────

TRACK   ?=
LIMIT   ?=

.PHONY: add sync-up intake drive-folder-id stage9 help

## ── Track ingestion ──────────────────────────────────────────────────────────

## Add a single track through the full pipeline
##   make add TRACK=/path/to/file.mp3
##   make add TRACK=/path/to/file.mp3 EXTRA="--no-cues"
add:
	@if [ -z "$(TRACK)" ]; then \
	  echo "Usage: make add TRACK=/path/to/file.mp3"; exit 1; fi
	python3 tools/add_track.py "$(TRACK)" $(EXTRA)

## ── Google Drive sync ────────────────────────────────────────────────────────

## Push corrected_music/ to Google Drive backup (Mac → Drive, one-way)
sync-up:
	rclone sync "corrected_music/" "gdrive:Music/" \
	  --progress --transfers=8 --checkers=16 \
	  --log-file="state/drive_upload.log" --log-level=INFO

## Scan Drive Music/ for new files not yet in library
intake-scan:
	python3 tools/drive_intake.py --scan

## Download new tracks from Drive + run lyrics pipeline
intake:
	python3 tools/drive_intake.py --intake --process

## Find + print the My Drive/Music/ folder ID (run once, then set in .env)
drive-folder-id:
	python3 tools/drive_intake.py --find-folder

## ── Stage9 / Mac Bilby ───────────────────────────────────────────────────────

## Start Mac Bilby (DJ Block Planner) in foreground
stage9:
	python3 stage9_dj_suggest.py

## ── Help ─────────────────────────────────────────────────────────────────────

help:
	@grep -E '^## ' Makefile | sed 's/^## //'
