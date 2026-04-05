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

## Step 1 — Validate the NML Before Touching Traktor

> ⚠️ Do NOT use **File → Import Another Collection** as your test.
> That pathway is known to grey out and silently fail on valid NML files,
> giving a false negative. See: NI Community thread #12721.
> We test by directly swapping the file and verifying, with a clean rollback path.

First, validate the XML is well-formed:

```bash
xmllint --noout ~/development/music\ organize/corrected_traktor/collection.nml \
  && echo "XML OK" || echo "XML BROKEN — do not proceed"
```

If that prints `XML BROKEN` → stop and file a bug before touching anything.

---

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

**Quit Traktor completely before doing this.**

```bash
# Replace collection.nml with our corrected version
cp ~/development/music\ organize/corrected_traktor/collection.nml \
   ~/Documents/Native\ Instruments/Traktor\ 4.0.2/collection.nml
```

Then copy any corrected playlist NMLs:
```bash
cp ~/development/music\ organize/corrected_traktor/*.nml \
   ~/Documents/Native\ Instruments/Traktor\ 4.0.2/
```

Relaunch Traktor. It will read the file directly as its primary collection —
no import step needed. Spot-check 10–20 tracks:
- Do they load and play?
- Are cue points present?
- Is the BPM correct?
- Search for a known track — does it appear once (not duplicated)?
- Any red/grey "file not found" indicators?

**If something looks wrong → rollback immediately (see below). Do not save or let Traktor re-write the collection.**

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
