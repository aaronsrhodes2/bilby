# PC Dispatch — NML Sync + Autocue Stage 10

Auto-generated 2026-04-23T03:19:42Z.

## Baseline at dispatch time

| Metric | Count |
|---|---:|
| NML tracks (corrected) | 21,435 |
| HOTCUE=2 set (Cue 3 — first vocal onset) | 3,652 |
| HOTCUE=3 set (Cue 4 — main drop) | 4,031 |
| autocue_progress done list | 23,150 |

## Context

The Mac just installed an updated corrected_traktor/collection.nml from git.
Two unplayable tracks were removed (U2 — Drowning Man, Alternative Radio — You Won't
Put Me Down Again), and 14 new tracks were added (Noise Unit DECODER album, SPC ECO,
New Order — Exit, others). NML is now 21,435 entries.

Two files were also deleted from corrected_music/ and are propagating via Syncthing:
- corrected_music/U2/Best Ballads/Drowning Man.mp3
- corrected_music/Alternative Radio/Across the Universe (1995-01-05)/04 - You Won't Put Me Down Again (Electric version).mp3

## Run this

You are Claude on the Skippy PC. Do the following in order:

### Step 1 — Pull new NML from git
```
cd D:\Aaron\development\music-collection
git pull
```
Confirm `corrected_traktor/collection.nml` now shows 21,435 entries:
```
python3 -c "import xml.etree.ElementTree as ET; coll=ET.parse('corrected_traktor/collection.nml').getroot().find('COLLECTION'); print(f'Entries: {len(coll.findall(chr(69)+chr(78)+chr(84)+chr(82)+chr(89)))}') "
```
Expected: **21,435**. If the count is wrong, stop and report back.

### Step 2 — Wait for Syncthing to finish propagating deletes
Check that the two deleted files are gone from D:\Aaron\Music\corrected_music\:
```
Test-Path "D:\Aaron\Music\corrected_music\U2\Best Ballads\Drowning Man.mp3"
Test-Path "D:\Aaron\Music\corrected_music\Alternative Radio\Across the Universe (1995-01-05)\04 - You Won't Put Me Down Again (Electric version).mp3"
```
Both should return **False**. If either returns True, wait 5 minutes for Syncthing to sync and check again. Do not proceed until both are gone.

### Step 3 — Overwrite the PC's working NML with the corrected one
The corrected_traktor/collection.nml IS the working NML for stage10. Confirm it
is at the path stage10_autocue.py expects, or adjust the script's NML_PATH if needed.

### Step 4 — Run stage10 autocue on the updated library
```
python3 tools\stage10_autocue.py --all --apply
```
This is resumable. It will skip tracks already in autocue_progress.json.
The done list was at 23,150 when this dispatch was written — new tracks from
the 14 additions will be processed fresh; others will be skipped.

Commit state/autocue_progress.json and corrected_traktor/collection.nml every
2 hours or on clean exit. Push to origin.

### Step 5 — Report back
When done (or after first 2-hour checkpoint), report:
- Final NML entry count (must be 21,435)
- New HOTCUE=2 count (delta from 3,652)
- New HOTCUE=3 count (delta from 4,031)
- New autocue_progress done count (delta from 23,150)
- Any errors or tracks that failed

Permission: bypass mode. Resumable — safe to interrupt.
