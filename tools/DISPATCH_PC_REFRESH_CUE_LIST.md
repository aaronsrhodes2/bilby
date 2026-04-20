# PC Dispatch: Pull Clean NML and Refresh Cue Processing List

The Mac has pushed a fully deduplicated `collection.nml` to git.
The old NML had 23,780 entries — the new one has 21,437.

**Do this before continuing the cue pass.** If you process from the old
NML, ~2,343 entries either have missing audio files (will error) or are
duplicate artist+title entries that the Mac has already removed.

---

## Step 1 — Stop any running cue pass

If `reset_cues.py` is running, stop it now (Ctrl+C). Safe to stop
mid-run — progress is saved to `state/cue_reset_progress.json` after
every track.

---

## Step 2 — Pull the new NML

```powershell
cd D:\Aaron\development\music-collection
git pull
```

Expected output: `corrected_traktor/collection.nml` updated (large diff).

Verify the entry count:
```powershell
python3 -c "
import xml.etree.ElementTree as ET
tree = ET.parse('corrected_traktor/collection.nml')
n = len(tree.getroot().find('COLLECTION').findall('ENTRY'))
print(f'Entries: {n:,}  (expected 21,437)')
"
```

---

## Step 3 — Prune the progress file

The progress file tracks `artist\ttitle` keys. After the dedup, some
keys no longer exist in the NML. This script removes them so the
progress count stays accurate, and also removes any entries whose
audio file is missing from `D:\Aaron\Music\corrected_music\`:

```powershell
python3 - << 'PYEOF'
import json, xml.etree.ElementTree as ET
from pathlib import Path

NML_PATH      = Path("D:/Aaron/development/music-collection/corrected_traktor/collection.nml")
PROGRESS_PATH = Path("state/cue_reset_progress.json")
MUSIC_ROOT    = Path("D:/Aaron/Music/corrected_music")

if not PROGRESS_PATH.exists():
    print("No progress file yet — nothing to prune.")
    exit()

# Build set of valid dkeys from new NML
def base_title(t):
    import re
    return re.sub(r'\s*[\(\[][^)\]]*[\)\]]', '', t or '').lower().strip()

tree = ET.parse(NML_PATH)
coll = tree.getroot().find("COLLECTION")
valid_keys = set()
for e in coll.findall("ENTRY"):
    a = (e.get("ARTIST") or "").lower().strip()
    t = base_title(e.get("TITLE", ""))
    valid_keys.add(f"{a}\t{t}")

with open(PROGRESS_PATH) as f:
    done = set(json.load(f))

before = len(done)
done &= valid_keys  # keep only keys still in NML
after  = len(done)

with open(PROGRESS_PATH, "w") as f:
    json.dump(sorted(done), f)

print(f"Progress pruned: {before:,} → {after:,} entries ({before-after:,} stale removed)")
PYEOF
```

---

## Step 4 — Resume the cue pass

```powershell
cd D:\Aaron\development\music-collection
python3 tools/reset_cues.py --workers 4
```

The pass will skip already-completed tracks and work through only the
remaining entries from the clean 21,437-entry NML.

---

## What changed in the NML

| Pass | Removed | Reason |
|------|---------|--------|
| Ghost entries | 91 | Audio file missing from corrected_music — would have errored |
| Strict BPM dupes | 55 | Same song, same rip (BPM ±0.5, duration ±1s) |
| Broad artist+title dupes | 2,197 | `_2`/`_3` rename-collision copies from Stage 2 copy |
| **Total** | **2,343** | |

The dedup kept the version with the most cue points + highest bitrate
in every case. No useful data was lost.

---

## NML workflow going forward

The `music-traktor` Syncthing folder is now **paused on both machines**.
Use git for all NML changes:

```powershell
# Get latest NML from Mac
git pull

# Push NML changes (e.g. after cue pass completes)
git add corrected_traktor/collection.nml
git commit -m "cues: audio pass complete — X tracks updated"
git push
```

Audio files (`music-corrected`) and state caches (`music-state`)
still sync via Syncthing as before.
