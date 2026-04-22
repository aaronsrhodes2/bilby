#!/usr/bin/env python3
"""
compute_cues_pc.py — PC-side audio cue computation (Cues 3 + 4).

Walks D:/Aaron/Music/VERAS SONGS, matches files to tracklist.json dkeys,
runs librosa to compute:
  - vocal_ms  : first sustained vocal/melodic onset (Cue 3)
  - drop_ms   : highest-energy beat (Cue 4, stored loop)
  - drop_len_ms : 4-beat loop length at track BPM

Output: state/cue_data.json  {dkey: {vocal_ms, drop_ms, drop_len_ms, path}}

Pair with tools/apply_cues_nml.py on Mac to write into NML.

Usage:
  python3 tools/compute_cues_pc.py [--limit N] [--dry-run]

  --limit N   Process at most N tracks then stop (resume next run)
  --dry-run   Match files and print stats without loading audio
"""

from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

BASE        = Path(__file__).resolve().parent.parent
TRACKLIST   = BASE / "state" / "tracklist.json"
CUE_DATA    = BASE / "state" / "cue_data.json"
MUSIC_ROOT  = Path("D:/Aaron/Music/VERAS SONGS")
AUDIO_EXTS  = {".mp3", ".flac", ".aiff", ".m4a", ".wav", ".ogg"}

# Librosa load sample rate
SR = 22050

# Vocal detection constants (mirrors stage10_autocue.py)
VOICE_BAND_LOW      = 300.0    # Hz
VOICE_BAND_HIGH     = 3500.0   # Hz
MIN_VOCAL_SEEK_SEC  = 4.0      # skip first N seconds (avoid count-ins)
VOCAL_SUSTAIN_FRAMES = 10      # consecutive frames required

# Drop detection constants
DROP_ENERGY_PERCENTILE = 75
DROP_SUSTAIN_BEATS     = 4
DROP_LOOP_BEATS        = 4
MIN_DROP_SEEK_SEC      = 20.0  # skip first N seconds


# ── dkey helpers ──────────────────────────────────────────────────────────────

def base_title(title: str) -> str:
    return re.sub(r'\s*[\(\[].*', '', title).strip().lower()


def make_dkey(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"


def parse_filename(stem: str, dir_artist: str) -> list[tuple[str, str]]:
    """Return candidate (artist, title) pairs from filename stem."""
    candidates: list[tuple[str, str]] = []
    s = stem

    # Strip leading disc-track prefix: "2-06 ", "09 ", "01. "
    s = re.sub(r'^\d+[-\s]\d+\s*[-.]?\s*', '', s)  # "2-06 " form
    s = re.sub(r'^\d+\s*[-.]?\s*', '', s)           # "09 " form

    if ' - ' in s:
        parts = s.split(' - ', 2)
        if len(parts) >= 2:
            candidates.append((parts[0].strip(), parts[-1].strip()))

    if '-' in s:
        idx = s.index('-')
        candidates.append((s[:idx].strip(), s[idx + 1:].strip()))

    # Fallback: use artist-folder name
    candidates.append((dir_artist, s))
    return candidates


def build_file_index(dkeys: set[str]) -> dict[str, Path]:
    """
    Walk MUSIC_ROOT and return {dkey: path} for all matchable audio files.
    When multiple files share a dkey, the first match wins.
    """
    index: dict[str, Path] = {}
    for fpath in MUSIC_ROOT.rglob("*"):
        if fpath.suffix.lower() not in AUDIO_EXTS:
            continue
        if fpath.name.startswith("._"):
            continue
        dir_artist = fpath.parent.parent.name
        for artist, title in parse_filename(fpath.stem, dir_artist):
            dk = make_dkey(artist, title)
            if dk in dkeys and dk not in index:
                index[dk] = fpath
                break
    return index


# ── Cue computation ───────────────────────────────────────────────────────────

def snap_to_grid(ms: float, anchor_ms: float, period_ms: float) -> float:
    if period_ms <= 0:
        return ms
    offset = (ms - anchor_ms) % period_ms
    if offset < period_ms / 2:
        return ms - offset
    return ms - offset + period_ms


def snap_to_4beat(ms: float, anchor_ms: float, period_ms: float) -> float:
    bar_ms = period_ms * 4
    if bar_ms <= 0:
        return ms
    offset = (ms - anchor_ms) % bar_ms
    if offset < bar_ms / 2:
        return ms - offset
    return ms - offset + bar_ms


def compute_cues(audio_path: Path) -> dict:
    """
    Run librosa on audio_path. Returns dict with any/all of:
      vocal_ms, drop_ms, drop_len_ms
    Returns {} on failure.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        print("ERROR: librosa not installed — pip install librosa soundfile")
        sys.exit(1)

    try:
        y, sr = librosa.load(str(audio_path), sr=SR, mono=True)
    except Exception as e:
        print(f"  [load error] {e}")
        return {}

    dur_sec = len(y) / sr
    if dur_sec < 30:
        return {}

    # Detect BPM and beat grid
    tempo_arr, beat_frames_arr = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    if hasattr(tempo_arr, '__len__'):
        bpm = float(tempo_arr[0]) if len(tempo_arr) else 120.0
    else:
        bpm = float(tempo_arr) if tempo_arr > 0 else 120.0
    beat_period_sec = 60.0 / bpm
    beat_period_ms  = beat_period_sec * 1000.0

    beat_times = librosa.frames_to_time(beat_frames_arr, sr=sr)  # array of beat times in seconds
    first_beat_ms = float(beat_times[0]) * 1000.0 if len(beat_times) else 0.0

    result: dict = {}

    # ── Vocal onset (Cue 3) ──────────────────────────────────────────────────
    try:
        y_harm   = librosa.effects.harmonic(y, margin=4)
        n_fft    = 2048
        hop      = 512
        freqs    = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        band_mask = (freqs >= VOICE_BAND_LOW) & (freqs <= VOICE_BAND_HIGH)

        D_harm   = librosa.stft(y_harm, n_fft=n_fft, hop_length=hop)
        D_band   = np.abs(D_harm)
        D_band[~band_mask, :] = 0

        band_rms = np.sqrt((D_band ** 2).mean(axis=0))
        times    = librosa.times_like(band_rms, sr=sr, hop_length=hop, n_fft=n_fft)

        intro_frames = int(5.0 * sr / hop)
        baseline     = np.median(band_rms[:intro_frames]) if intro_frames > 0 else 0
        threshold    = max(baseline * 3.0, np.percentile(band_rms, 40))

        min_frame = int(MIN_VOCAL_SEEK_SEC * sr / hop)
        above = 0
        vocal_frame = None
        for i in range(min_frame, len(band_rms)):
            if band_rms[i] >= threshold:
                above += 1
                if above >= VOCAL_SUSTAIN_FRAMES:
                    vocal_frame = i - VOCAL_SUSTAIN_FRAMES + 1
                    break
            else:
                above = 0

        if vocal_frame is not None:
            vocal_sec = float(times[vocal_frame])
            anchor_sec = first_beat_ms / 1000.0
            if vocal_sec > anchor_sec + beat_period_sec:
                vocal_ms = vocal_sec * 1000.0
                result["vocal_ms"] = snap_to_grid(vocal_ms, first_beat_ms, beat_period_ms)
    except Exception:
        pass

    # ── Drop (Cue 4, stored loop) ────────────────────────────────────────────
    try:
        onset_env   = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        onset_times = librosa.times_like(onset_env, sr=sr, hop_length=512)

        beat_frames = np.searchsorted(onset_times, beat_times)
        beat_frames = np.clip(beat_frames, 0, len(onset_env) - 1)

        search_start = int(MIN_DROP_SEEK_SEC / beat_period_sec)
        search_end   = max(search_start + 8,
                           len(beat_times) - int(30 / beat_period_sec))
        search_beats = beat_frames[search_start:search_end]

        if len(search_beats) >= DROP_SUSTAIN_BEATS:
            window    = DROP_SUSTAIN_BEATS
            strengths = np.array([
                onset_env[search_beats[i:i + window]].mean()
                for i in range(len(search_beats) - window + 1)
            ])
            threshold  = np.percentile(onset_env[beat_frames], DROP_ENERGY_PERCENTILE)
            candidates = np.where(strengths >= threshold)[0]
            if len(candidates):
                drop_beat_idx = search_start + int(candidates[0])
                drop_sec      = float(beat_times[drop_beat_idx])
                drop_ms       = snap_to_4beat(drop_sec * 1000.0, first_beat_ms, beat_period_ms)
                result["drop_ms"]      = drop_ms
                result["drop_len_ms"]  = DROP_LOOP_BEATS * beat_period_ms
    except Exception:
        pass

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit",   type=int, default=0,
                    help="Stop after N tracks (0 = unlimited)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Match files, print stats, skip audio loading")
    args = ap.parse_args()

    print("Loading tracklist…")
    with open(TRACKLIST, encoding="utf-8") as f:
        tracks = json.load(f)
    dkeys = {t["dkey"] for t in tracks}
    print(f"  {len(dkeys):,} dkeys")

    print("Loading existing cue data…")
    cue_data: dict = {}
    if CUE_DATA.exists():
        with open(CUE_DATA, encoding="utf-8") as f:
            cue_data = json.load(f)
    print(f"  {len(cue_data):,} already computed")

    print("Building file index…")
    file_index = build_file_index(dkeys)
    new_files  = {dk: p for dk, p in file_index.items() if dk not in cue_data}
    print(f"  {len(file_index):,} matched local files")
    print(f"  {len(new_files):,} not yet processed")

    if args.dry_run:
        print("\nDry run — no audio loaded.")
        return

    if not new_files:
        print("Nothing to do.")
        return

    todo = list(new_files.items())
    if args.limit:
        todo = todo[:args.limit]

    print(f"\nProcessing {len(todo):,} tracks…")
    vocal_count = 0
    drop_count  = 0
    err_count   = 0
    t0          = time.time()

    for i, (dk, fpath) in enumerate(todo, 1):
        rel = fpath.relative_to(MUSIC_ROOT)
        print(f"  [{i}/{len(todo)}] {rel}", end="", flush=True)

        cues = compute_cues(fpath)

        entry: dict = {"path": str(fpath)}
        if "vocal_ms" in cues:
            entry["vocal_ms"] = cues["vocal_ms"]
            vocal_count += 1
        if "drop_ms" in cues:
            entry["drop_ms"]     = cues["drop_ms"]
            entry["drop_len_ms"] = cues["drop_len_ms"]
            drop_count += 1
        if not cues:
            err_count += 1

        cue_data[dk] = entry

        elapsed = time.time() - t0
        print(f"  vocal={cues.get('vocal_ms','—'):.0f}ms  drop={cues.get('drop_ms','—'):.0f}ms"
              if cues else "  [no cues]")

        # Save every 50 tracks
        if i % 50 == 0:
            with open(CUE_DATA, "w", encoding="utf-8") as f:
                json.dump(cue_data, f, indent=2)
            rate = i / elapsed
            remaining = (len(todo) - i) / rate
            print(f"  → saved ({i} done, ~{remaining/60:.0f}min remaining)")

    with open(CUE_DATA, "w", encoding="utf-8") as f:
        json.dump(cue_data, f, indent=2)

    print(f"\nDone. {len(todo)} processed in {(time.time()-t0)/60:.1f}min")
    print(f"  Vocal cues: {vocal_count}")
    print(f"  Drop cues:  {drop_count}")
    print(f"  Errors:     {err_count}")
    print(f"  Saved to:   {CUE_DATA}")


if __name__ == "__main__":
    main()
