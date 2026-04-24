# PC Dispatch — STT Re-run with Word Timestamps

Auto-generated 2026-04-23.

## Purpose

Re-run faster-whisper on the music library with `word_timestamps=True` to produce
per-word timing data. This enables karaoke-style scrolling lyrics in the DJ Block
Planner — each lyric line advances in sync with Traktor's deck playback position.

Output: `state/lyrics_timed.json`  
Format: `{"artist\ttitle": [{"t": 2.34, "line": "First lyric line"}, ...]}`

Estimated run time: **12–18 hours** on the PC GPU.

## Context

The Mac has now populated `INFO KEY_LYRICS` in collection.nml for 14,728 tracks
(plain text, no timestamps). stage9 can display static lyrics today. With timed
data, stage9 will scroll lyrics line by line in sync with deck position.

- Mac Tailscale: `100.93.161.25`
- PC Tailscale: `100.122.71.14`
- Syncthing state/ folder: synced between both machines

## Step 1 — Pull latest from git

```bash
cd D:\Aaron\dev\music-organizer-manydeduptrak
git pull
```

Expected: picks up `nml: populate KEY_LYRICS for 14,728 tracks` commit.

## Step 2 — Write `tools/stage11_stt_words.py`

Create this script (does not exist yet — write it):

```python
#!/usr/bin/env python3
"""
stage11_stt_words.py — Re-run faster-whisper with word_timestamps=True.

Reads lyrics_raw.json to find which tracks were already transcribed,
loads each corresponding audio file, re-transcribes with word timestamps,
groups words into lines (max ~60 chars, split at punctuation/silence),
writes state/lyrics_timed.json.

Usage:
    python3 tools/stage11_stt_words.py [--limit N] [--resume]

    --limit N   Process only N tracks (for testing)
    --resume    Skip tracks already in lyrics_timed.json
"""
import json, re, sys
from pathlib import Path

BASE        = Path(__file__).resolve().parent.parent
STATE_DIR   = BASE / "state"
LYRICS_RAW  = STATE_DIR / "lyrics_raw.json"
TIMED_OUT   = STATE_DIR / "lyrics_timed.json"
NML_PATH    = BASE / "corrected_traktor" / "collection.nml"

# Audio root on Windows — adjust if needed
AUDIO_ROOT  = Path(r"D:\Aaron\Music\VERAS SONGS\corrected_music")

MODEL_SIZE  = "medium"    # or "large-v3" for higher accuracy (slower)
DEVICE      = "cuda"      # "cpu" if no CUDA
MAX_LINE    = 60          # max chars per lyric line

def group_words_to_lines(words: list[dict]) -> list[dict]:
    """Group word-level timestamps into display lines."""
    lines = []
    current_words = []
    current_len   = 0
    line_start_t  = None

    for w in words:
        word = w.get("word", "").strip()
        t    = w.get("start", 0.0)
        if not word:
            continue
        if line_start_t is None:
            line_start_t = t

        candidate = (current_words + [word])
        candidate_str = " ".join(candidate)

        # Break on punctuation end or length exceeded
        split_here = (
            current_len + len(word) + 1 > MAX_LINE
            or (current_words and current_words[-1][-1] in ".!?,;:")
        )

        if split_here and current_words:
            lines.append({
                "t":    line_start_t,
                "line": " ".join(current_words),
            })
            current_words = [word]
            current_len   = len(word)
            line_start_t  = t
        else:
            current_words.append(word)
            current_len += len(word) + 1

    if current_words:
        lines.append({"t": line_start_t, "line": " ".join(current_words)})

    return lines


def find_audio(artist: str, title: str, audio_root: Path) -> Path | None:
    """Walk audio_root to find file for artist/title."""
    from xml.etree import ElementTree as ET
    import os
    # Use NML to find the exact path
    # (Parsed in main; passed as index)
    return None   # placeholder — see main() for real lookup


def main():
    import argparse
    from faster_whisper import WhisperModel

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit",  type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    # Load existing timed output (for resume)
    timed: dict[str, list] = {}
    if args.resume and TIMED_OUT.exists():
        timed = json.loads(TIMED_OUT.read_text(encoding="utf-8"))
        print(f"Resuming — {len(timed):,} tracks already done")

    # Build NML path index: dkey → absolute audio path
    from xml.etree import ElementTree as ET
    nml_index: dict[str, str] = {}
    tree = ET.parse(NML_PATH)
    for entry in tree.getroot().find("COLLECTION").findall("ENTRY"):
        artist = entry.get("ARTIST", "").strip()
        title_ = entry.get("TITLE",  "").strip()
        loc = entry.find("LOCATION")
        if loc is None: continue
        # Convert NML path to Windows audio path
        nml_dir  = loc.get("DIR", "").replace("/:", "\\").replace("/", "\\")
        nml_file = loc.get("FILE", "")
        # Strip Mac prefix: find corrected_music anchor
        parts = nml_dir.replace("\\", "/").split("/")
        try:
            idx = next(i for i, p in enumerate(parts) if p.lower() == "corrected_music")
            rel = "\\".join(parts[idx + 1:] + [nml_file])
        except StopIteration:
            continue
        abs_path = str(AUDIO_ROOT / rel)
        dk = f"{artist.lower().strip()}\t{title_.lower().strip()}"
        nml_index[dk] = abs_path

    # Load lyrics_raw to know which tracks have transcriptions
    lyrics_raw = json.loads(LYRICS_RAW.read_text(encoding="utf-8"))
    keys = [k for k in lyrics_raw if k in nml_index]
    if args.resume:
        keys = [k for k in keys if k not in timed]
    if args.limit:
        keys = keys[:args.limit]

    print(f"Loading Whisper {MODEL_SIZE} …")
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type="float16")
    print(f"Processing {len(keys):,} tracks …")

    for i, dk in enumerate(keys, 1):
        audio_path = nml_index[dk]
        if not Path(audio_path).exists():
            continue
        try:
            segs, _ = model.transcribe(
                audio_path,
                word_timestamps=True,
                language="en",
                beam_size=5,
            )
            words = []
            for seg in segs:
                if seg.words:
                    words.extend([
                        {"word": w.word, "start": w.start}
                        for w in seg.words
                    ])
            if words:
                timed[dk] = group_words_to_lines(words)
        except Exception as e:
            print(f"  [ERR] {dk}: {e}", file=sys.stderr)
            continue

        if i % 100 == 0:
            TIMED_OUT.write_text(
                json.dumps(timed, ensure_ascii=False, indent=None),
                encoding="utf-8"
            )
            print(f"  {i:,}/{len(keys):,} done …")

    TIMED_OUT.write_text(
        json.dumps(timed, ensure_ascii=False, indent=None),
        encoding="utf-8"
    )
    print(f"\nDone. {len(timed):,} tracks in {TIMED_OUT}")

if __name__ == "__main__":
    main()
```

## Step 3 — Test with 5 tracks

```bash
python3 tools/stage11_stt_words.py --limit 5
```

Check `state/lyrics_timed.json` — should have 5 entries with `[{"t": ..., "line": "..."}]` lists.
Inspect a few to confirm timestamps are sane (line `t` values are seconds from track start).

## Step 4 — Full run

```bash
python3 tools/stage11_stt_words.py --resume
```

`--resume` skips the 5 test tracks already done. This will run overnight.
Progress is saved every 100 tracks — safe to interrupt and resume.

## Step 5 — Push results

```bash
git add state/lyrics_timed.json tools/stage11_stt_words.py
git commit -m "feat: lyrics_timed.json — word-timestamp lines for karaoke sync"
git push
```

Mac will pull and stage9 will serve timed lyrics to the browser.

## Verification

After the full run, check:
```bash
python3 -c "
import json
from pathlib import Path
t = json.loads(Path('state/lyrics_timed.json').read_text())
print(f'{len(t):,} tracks with timed lines')
sample = next(iter(t.items()))
print(f'Sample: {sample[0]}')
for line in sample[1][:4]:
    print(f'  {line}')
"
```

Expected: 14,000–19,000 tracks (limited by audio availability), lines with
reasonable `t` values (0–600 seconds range depending on track position).

## Notes

- Audio root: `D:\Aaron\Music\VERAS SONGS\corrected_music`
- Model: faster-whisper `medium` (good balance of speed/accuracy for lyrics)
- Upgrade to `large-v3` if accuracy is poor on first test batch
- This dispatch writes `state/lyrics_timed.json` only — does NOT modify NML
- NML embedding of timed lyrics (as LRC format in KEY_LYRICS) is a Mac-side
  follow-up task once the timed data is validated
