#!/bin/bash
# art_pipeline.sh — automated art fetch → force-retry → embed pipeline
# Run this after the initial fetch is already going (it waits for it first)

cd "$(dirname "$0")/.."
LOG="state/art_pipeline.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== Art pipeline started ==="

# ── Step 1: wait for any running fetch to finish ───────────────────────────
FETCH_PID=$(pgrep -f "fetch_album_art.py --run" | head -1)
if [ -n "$FETCH_PID" ]; then
    log "Waiting for initial fetch (PID $FETCH_PID) to finish..."
    while kill -0 "$FETCH_PID" 2>/dev/null; do
        sleep 30
    done
    log "Initial fetch done."
fi

# ── Step 2: --force pass (retries nulls, now with iTunes as extra source) ──
log "Starting --force pass (retry nulls with iTunes+MB+mutagen)..."
python3 tools/fetch_album_art.py --run --force >> "$LOG" 2>&1
log "--force pass complete."

# ── Step 3: embed art into audio files for Traktor ─────────────────────────
log "Starting --embed pass..."
python3 tools/fetch_album_art.py --embed >> "$LOG" 2>&1
log "--embed pass complete."

# ── Step 4: final report ───────────────────────────────────────────────────
log "Final coverage report:"
python3 tools/fetch_album_art.py --report >> "$LOG" 2>&1

log "=== Pipeline complete ==="

# macOS notification
osascript -e 'display notification "Album art pipeline complete — fetch, force-retry, and embed done." with title "DJ Block Planner"' 2>/dev/null || true
