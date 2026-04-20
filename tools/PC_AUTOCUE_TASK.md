# PC Task: Audio Cue Point Analysis (Stage 10 — Audio Pass)

## What this does

The Mac already ran a fast pass that set **Cues 1, 2, and 8** on 23,327 tracks
using beat-grid math (no audio loading). This task adds the remaining two cues
that require actual audio analysis:

| Cue | Button | Type | Description |
|-----|--------|------|-------------|
| **Cue 1** (refine) | 1 | Load | Update to true first-sound onset (not just beat anchor) |
| **Cue 3** | 3 | Cue | First vocal / melodic onset |
| **Cue 4** | 4 | Loop | Main drop — 1-bar stored loop at highest-energy beat |

Cues 2 and 8 are already in the NML; this pass writes 3 and 4.

---

## Setup (one time)

```bat
cd D:\Aaron\development\music-collection
pip install librosa soundfile numpy numba
```

`numba` is optional but makes librosa significantly faster on repeated runs.

---

## Run

```bat
python tools\stage10_autocue.py --all --apply --limit 500
```

- `--limit 500` processes 500 tracks per run. Run it repeatedly, or remove
  `--limit` to run the full collection overnight (~6–10 hours for 23k tracks).
- Progress is saved to `state/autocue_progress.json` after each batch.
  Re-running picks up exactly where it left off.
- The script skips any track that already has Cues 3 or 4 set.

### If audio files are on a different drive

```bat
python tools\stage10_autocue.py --all --apply --audio-root D:\Aaron\Music
```

---

## After it finishes

The NML files it modifies are:
- `corrected_traktor\collection.nml`
- `%USERPROFILE%\Documents\Native Instruments\Traktor 4.0.2\collection.nml`
  (adjust if your Traktor version differs)

**On the PC:**
```bat
git add corrected_traktor\collection.nml state\autocue_progress.json
git commit -m "cues: add vocal + drop cues (audio pass)"
git push
```

**On the Mac after pulling:**
```bash
git pull
# Then do the Traktor library swap (xmllint + direct file copy, see switch_library.md)
```

---

## What the cue points mean in Traktor

| Button | Cue | Color | Use during set |
|--------|-----|-------|----------------|
| 1 | Load | White | Track loads here — positioned at first sound |
| 2 | First Beat | — | Grid-aligned beat anchor — use for BPM-locked drops |
| 3 | Vocal | — | Jump to first lyrics / melodic entry |
| 4 | Drop Loop | — | 1-bar loop at the track's highest-energy section |
| 5–7 | (reserved) | — | Future use |
| 8 | Outro | — | Fade-out marker, 16 beats before end |

---

## Estimated runtime

| Phase | Time per track | Total (23k) |
|-------|---------------|-------------|
| Audio load (22kHz mono) | ~0.3–1s | 2–6h |
| Onset + HPSS analysis | ~0.5–2s | 3–8h |
| **Total** | **~1–3s/track** | **~6–18h** |

With a 4070 Ti: librosa runs on CPU regardless (it's not CUDA-accelerated by default),
but the machine will otherwise be idle so no throttling. Overnight job.
