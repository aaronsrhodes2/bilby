# Safe Library Switch Procedure

## The Goal
Switch Traktor from the old messy library to the corrected one,
with a guaranteed rollback path if anything looks wrong.

## Before You Start

The original Traktor collection lives here — **do not touch it**:
```
~/Documents/Native Instruments/Traktor 4.0.2/collection.nml
```
Our corrected collection is here:
```
~/development/music organize/corrected_traktor/collection.nml
```

---

## Step 1 — Test First (Non-Destructive)

Open Traktor. Do NOT replace anything yet.

1. In Traktor: **File → Import Another Collection**
2. Navigate to `~/development/music organize/corrected_traktor/collection.nml`
3. Traktor imports it as a separate sub-library you can browse
4. Spot-check 10–20 tracks:
   - Do they load and play?
   - Are cue points present?
   - Is the BPM correct?
   - Search for a known track — does it appear once (not duplicated)?
5. Look at the bottom of the browser — are there any red/grey "file not found" tracks?

**If you see file not found errors → STOP. Do not proceed to Step 2. File a bug.**

---

## Step 2 — Back Up the Original (One-Time)

Only do this if Step 1 looked clean.

```bash
# Back up the entire Traktor app data
cp -R ~/Documents/Native\ Instruments/Traktor\ 4.0.2 \
      ~/Documents/Native\ Instruments/Traktor_BACKUP_$(date +%Y%m%d)
```

This creates a timestamped backup of everything:
`~/Documents/Native Instruments/Traktor_BACKUP_20260404/`

Takes about 30 seconds. Do not skip this.

---

## Step 3 — Switch to the Corrected Library

```bash
# Replace collection.nml with our corrected version
cp ~/development/music\ organize/corrected_traktor/collection.nml \
   ~/Documents/Native\ Instruments/Traktor\ 4.0.2/collection.nml
```

Then copy any corrected playlist NMLs you want to keep:
```bash
cp ~/development/music\ organize/corrected_traktor/*.nml \
   ~/Documents/Native\ Instruments/Traktor\ 4.0.2/
```

Relaunch Traktor. Your library should now be the corrected one.

---

## Step 4 — Tell Traktor Where the Music Is

After replacing the collection, Traktor needs to know about `corrected_music/`:

1. **Preferences → File Management → Music Folders**
2. Add `~/development/music organize/corrected_music/` to the list
3. Optionally remove old `~/Music` paths if you no longer want them scanned

---

## Rollback (If Something Goes Wrong)

```bash
# Restore the original collection
cp ~/Documents/Native\ Instruments/Traktor_BACKUP_20260404/collection.nml \
   ~/Documents/Native\ Instruments/Traktor\ 4.0.2/collection.nml
```

Relaunch Traktor — you're back to the original library, exactly as it was.

---

## What Is NOT Changed by This Procedure

- `~/Music/` — all original audio files untouched
- `~/Documents/Native Instruments/Traktor 4.0.2/` — untouched until Step 3
- Traktor settings, preferences, MIDI mappings — untouched
- DJ recordings in `~/Music/Traktor/` — untouched, paths preserved in corrected NML
