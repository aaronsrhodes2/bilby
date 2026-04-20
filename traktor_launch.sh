#!/bin/bash
# traktor_launch.sh — Git-synced Traktor launcher
#
# Usage:  ./traktor_launch.sh
#   or double-click the .app wrapper (see README for Automator setup)
#
# What it does:
#   1. git pull — gets latest NML from GitHub (e.g. cue updates from PC)
#   2. Copies corrected_traktor/collection.nml → Traktor's live path
#   3. Opens Traktor and WAITS for it to fully quit
#   4. Copies Traktor's NML back → corrected_traktor/collection.nml
#   5. git commit + push if anything changed

set -euo pipefail

REPO="/Users/aaronrhodes/development/music organize"
TRAKTOR_NML="$HOME/Documents/Native Instruments/Traktor 4.0.2/collection.nml"
CURATED="$REPO/corrected_traktor/collection.nml"
LOG="$REPO/state/traktor_launch.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== Traktor launch ==="

# ── Pre-launch: pull latest NML ───────────────────────────────────────────────
cd "$REPO"
log "Pulling from GitHub..."
if git pull --ff-only 2>&1 | tee -a "$LOG"; then
    log "Pull OK"
else
    log "Pull had conflicts — using local version"
fi

# Copy our curated NML into Traktor's expected location
log "Installing NML → Traktor..."
cp "$CURATED" "$TRAKTOR_NML"
ENTRIES=$(python3 -c "import xml.etree.ElementTree as ET; print(len(ET.parse('$CURATED').getroot().find('COLLECTION').findall('ENTRY')))")
log "  $ENTRIES entries loaded into Traktor"

# ── Launch Traktor and wait ───────────────────────────────────────────────────
log "Opening Traktor (waiting for quit)..."
open -W -a "Traktor"
log "Traktor closed."

# ── Post-close: copy back and push ───────────────────────────────────────────
log "Copying NML back from Traktor..."
cp "$TRAKTOR_NML" "$CURATED"
ENTRIES_AFTER=$(python3 -c "import xml.etree.ElementTree as ET; print(len(ET.parse('$CURATED').getroot().find('COLLECTION').findall('ENTRY')))")
log "  $ENTRIES_AFTER entries after session"

cd "$REPO"
git add corrected_traktor/collection.nml

if git diff --cached --quiet; then
    log "NML unchanged — nothing to push."
else
    MSG="traktor: session sync $(date '+%Y-%m-%d %H:%M')  ($ENTRIES_AFTER entries)"
    git commit -m "$MSG"
    log "Committed: $MSG"
    if git push; then
        log "Pushed to GitHub."
    else
        log "Push failed — commit saved locally, push manually when online."
    fi
fi

log "=== Done ==="
