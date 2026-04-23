#!/usr/bin/env python3
"""
stage10_autocue.py — Automatic cue point setter for Traktor NML collections.

Places four hotcue points on every track that has no user-placed cues:

  Cue 2 (HOTCUE 1)  First beat          — from AutoGrid or onset detection
  Cue 3 (HOTCUE 2)  First vocal/melodic — spectral voice-band onset
  Cue 4 (HOTCUE 3)  Main drop (loop)    — highest-energy beat, 1-bar stored loop
  Cue 8 (HOTCUE 7)  Outro anchor        — 16 beats before track end

Strategy
────────
NML-only pass (fast, no audio, Mac-friendly):
  Cue 1 → AutoGrid START (Load cue — deck snaps here on load; first beat approx)
  Cue 2 → AutoGrid START (First Beat regular cue, same position in fast mode)
  Cue 8 → PLAYTIME_FLOAT − 16 × beat_period_ms (fade-out marker)

Audio pass (librosa, run on PC overnight):
  Cue 1 → true first audio onset (may be before the beat grid anchor)
  Cue 2 → AutoGrid beat anchor (first beat, grid-aligned for BPM mixing)
  Cue 3 → first sustained onset in 300–3500 Hz harmonic band (vocal/melodic)
  Cue 4 → beat with highest onset energy in 2-bar window; snapped to 4-beat grid
  Cue 8 → PLAYTIME_FLOAT − 16 × beat_period_ms (same math, no audio needed)

  Note: 97 tracks skipped in fast pass (no BPM) will be handled by audio pass.

Usage
─────
  # Preview stats — no changes
  python3 tools/stage10_autocue.py --report

  # Fast NML-only pass: write Cues 2 and 8 to both NMLs
  python3 tools/stage10_autocue.py --fast --apply

  # Full audio pass: write all four cues (requires librosa)
  python3 tools/stage10_autocue.py --all --apply

  # Audio pass, 500 tracks at a time, resumable
  python3 tools/stage10_autocue.py --all --apply --limit 500

  # Dry-run audio pass (print what would be written, no file changes)
  python3 tools/stage10_autocue.py --all --dry-run --limit 50

  # Single NML only
  python3 tools/stage10_autocue.py --all --apply --nml corrected_traktor/collection.nml

Requirements
────────────
  Fast pass:   no extra dependencies
  Audio pass:  pip install librosa soundfile numpy  (pip install numba for speed)

Windows path note: if running on PC, pass --audio-root to override default.
"""

import argparse
import json
import multiprocessing
import queue as _queue
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE       = Path(__file__).parent.parent
NML_CORR   = BASE / "corrected_traktor" / "collection.nml"
NML_LIVE   = Path.home() / "Documents" / "Native Instruments" / "Traktor 4.0.2" / "collection.nml"
STATE_FILE = BASE / "state" / "autocue_progress.json"

# ── Cue slot mapping ──────────────────────────────────────────────────────────
# Traktor's HOTCUE attribute is 0-indexed (HOTCUE=N → button N+1)
#
# Button │ HOTCUE │ Type   │ Purpose
# ───────┼────────┼────────┼──────────────────────────────────────
#   1    │   0    │  Load  │ First beat — track cues here on load
#   2    │   1    │  Cue   │ First beat (redundant quick-select)
#   3    │   2    │  Cue   │ First vocal / melodic onset
#   4    │   3    │  Loop  │ Main drop — 1-bar stored loop
#   5    │   4    │  —     │ RESERVED (future use)
#   6    │   5    │  —     │ RESERVED (future use)
#   7    │   6    │  —     │ RESERVED (future: last vocals)
#   8    │   7    │ Fade   │ 16 beats before end (fade-out marker)
#
SLOT_LOAD        = 0   # button 1  TYPE=3 (Load cue — deck snaps here on load)
SLOT_FIRST_BEAT  = 1   # button 2  TYPE=1 (Fade-in — mixing entry point)
SLOT_VOCAL       = 2   # button 3  TYPE=0
SLOT_DROP        = 3   # button 4  TYPE=5 (stored loop)
SLOT_OUTRO       = 7   # button 8  TYPE=2 (fade-out marker)

# Number of beats before end of track for the outro cue
OUTRO_BEATS_FROM_END = 16

# Loop length for the drop cue (in beats, = 1 bar at 4/4)
DROP_LOOP_BEATS = 4

# Minimum distance from start before we look for the drop/vocal (seconds)
MIN_VOCAL_SEEK_SEC   = 4.0
MIN_DROP_SEEK_SEC    = 2.0

# Guard: track needs at least this many seconds to be worth cueing
MIN_TRACK_DURATION   = 30.0

# Voice band for spectral analysis (Hz)
VOICE_BAND_LOW  = 300
VOICE_BAND_HIGH = 3500

# Onset strength percentile that counts as "loud" for drop detection
DROP_ENERGY_PERCENTILE = 75

# How many consecutive "loud" beats before we call it the drop
DROP_SUSTAIN_BEATS = 4


# ── Platform utilities ─────────────────────────────────────────────────────────

def keep_awake() -> None:
    """Prevent Windows from sleeping while the process runs."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS      = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def set_low_priority() -> None:
    """Run at below-normal priority so the machine stays usable."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        BELOW_NORMAL = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL)
    except Exception:
        pass


# ── NML helpers ───────────────────────────────────────────────────────────────

def traktor_to_abs(volume: str, dir_: str, file_: str) -> str:
    """Reconstruct absolute path from Traktor LOCATION attributes."""
    parts = dir_.strip("/").split("/:")
    parts = [p for p in parts if p]
    return str(Path("/") / Path(*parts) / file_) if parts else str(Path(file_))


def parse_grid(entry) -> tuple[float | None, float | None]:
    """
    Return (first_beat_ms, bpm) from an ENTRY's AutoGrid CUE_V2 or TEMPO element.
    Returns (None, None) if no valid BPM is found.
    """
    bpm = None
    first_beat_ms = None

    # Prefer AutoGrid CUE_V2 (HOTCUE="-1") — most authoritative
    for cue in entry.findall("CUE_V2"):
        if cue.get("HOTCUE") == "-1":
            try:
                first_beat_ms = float(cue.get("START", 0))
            except (ValueError, TypeError):
                pass
            grid = cue.find("GRID")
            if grid is not None:
                try:
                    bpm = float(grid.get("BPM", 0) or 0)
                except (ValueError, TypeError):
                    pass
            break

    # Fall back to TEMPO element for BPM
    if bpm is None or bpm == 0:
        tempo = entry.find("TEMPO")
        if tempo is not None:
            try:
                bpm = float(tempo.get("BPM", 0) or 0)
            except (ValueError, TypeError):
                pass

    if bpm == 0:
        bpm = None
    return first_beat_ms, bpm


def get_duration(entry) -> float | None:
    """Return track duration in seconds from INFO PLAYTIME_FLOAT."""
    info = entry.find("INFO")
    if info is None:
        return None
    try:
        v = float(info.get("PLAYTIME_FLOAT", 0) or 0)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def existing_user_hotcues(entry) -> set[int]:
    """
    Return the set of HOTCUE slot integers already placed by the user (slots 1-7).
    Slot 0 is excluded — Traktor always places it as the AutoGrid duplicate and
    we will update its TYPE to Load rather than treat it as a user-placed cue.
    """
    slots = set()
    for cue in entry.findall("CUE_V2"):
        try:
            h = int(cue.get("HOTCUE", -999))
            if h >= 1:    # 0 = AutoGrid in-cue (we manage it); -1 = grid marker
                slots.add(h)
        except (ValueError, TypeError):
            pass
    return slots


def snap_to_grid(pos_ms: float, first_beat_ms: float, beat_period_ms: float) -> float:
    """Snap a millisecond position to the nearest beat on the Traktor grid."""
    offset = pos_ms - first_beat_ms
    beat_n = round(offset / beat_period_ms)
    return first_beat_ms + beat_n * beat_period_ms


def snap_to_4beat(pos_ms: float, first_beat_ms: float, beat_period_ms: float) -> float:
    """Snap a position to the nearest 4-beat (bar) boundary."""
    offset = pos_ms - first_beat_ms
    bar_period = 4 * beat_period_ms
    bar_n = round(offset / bar_period)
    return first_beat_ms + bar_n * bar_period


def make_cue(hotcue: int, start_ms: float, name: str = "n.n.",
             type_: int = 0, len_ms: float = 0.0, color: str | None = None) -> ET.Element:
    """Build a CUE_V2 XML element."""
    attrs = {
        "NAME":         name,
        "DISPL_ORDER":  "0",
        "TYPE":         str(type_),
        "START":        f"{start_ms:.6f}",
        "LEN":          f"{len_ms:.6f}",
        "REPEATS":      "-1",
        "HOTCUE":       str(hotcue),
    }
    if color:
        attrs["COLOR"] = color
    el = ET.Element("CUE_V2", attrs)
    el.tail = "\n      "
    return el


# ── Fast NML-only cue computation ─────────────────────────────────────────────

def compute_fast_cues(entry) -> dict[int, ET.Element] | None:
    """
    Compute Cue 2 (first beat) and Cue 8 (outro) from NML data only.
    Returns {slot: Element} or None if not enough data.
    """
    first_beat_ms, bpm = parse_grid(entry)
    duration = get_duration(entry)

    if bpm is None or bpm == 0:
        return None
    if first_beat_ms is None:
        return None
    if duration is None or duration < MIN_TRACK_DURATION:
        return None

    beat_period_ms = 60_000.0 / bpm
    cues: dict[int, ET.Element] = {}

    # Cue 1 — Load cue (TYPE=3) at first beat: track snaps here on deck load
    cues[SLOT_LOAD]       = make_cue(SLOT_LOAD, first_beat_ms, "Load",
                                     type_=3, color="#FFFFFF")
    # Cue 2 — first beat (regular cue, quick-access duplicate)
    cues[SLOT_FIRST_BEAT] = make_cue(SLOT_FIRST_BEAT, first_beat_ms, "Fade In", type_=1)

    # Cue 8 — 16 beats before end (TYPE=2 = fade-out marker)
    end_ms        = duration * 1000.0
    outro_ms      = end_ms - OUTRO_BEATS_FROM_END * beat_period_ms
    outro_snapped = snap_to_grid(outro_ms, first_beat_ms, beat_period_ms)
    if outro_snapped > first_beat_ms + beat_period_ms:
        cues[SLOT_OUTRO] = make_cue(SLOT_OUTRO, outro_snapped, "Outro",
                                    type_=2)   # TYPE=2 = fade-out

    return cues if cues else None


# ── Audio analysis cue computation ────────────────────────────────────────────
#
# _compute_cue_data() is the subprocess-safe core: returns plain dicts (picklable).
# compute_audio_cues() wraps it for callers that need ET.Elements directly.

def _compute_cue_data(audio_path: str,
                      first_beat_ms: float | None,
                      bpm: float | None,
                      duration: float | None) -> dict[int, dict]:
    """
    Compute cue point data from audio.  Returns picklable dict:
      {slot: {"start": ms, "len": ms, "type": int, "name": str, "color": str|None}}

    This function runs in a subprocess (via TimeoutWorker) so it must not
    return any non-picklable objects.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        return {}

    try:
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
    except Exception as e:
        return {}

    dur_sec = len(y) / sr
    if dur_sec < MIN_TRACK_DURATION:
        return {}

    # ── Beat grid ────────────────────────────────────────────────────────────
    if bpm and bpm > 0:
        beat_period_sec = 60.0 / bpm
    else:
        tempo_est, _ = librosa.beat.beat_track(y=y, sr=sr, units="time")
        if hasattr(tempo_est, '__len__'):
            tempo_est = float(tempo_est[0]) if len(tempo_est) else 120.0
        bpm = float(tempo_est) if tempo_est > 0 else 120.0
        beat_period_sec = 60.0 / bpm

    beat_period_ms = beat_period_sec * 1000.0

    if first_beat_ms is not None:
        anchor_sec = first_beat_ms / 1000.0
    else:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        onsets    = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr,
                                               units="time", backtrack=True)
        anchor_sec    = float(onsets[0]) if len(onsets) else 0.0
        first_beat_ms = anchor_sec * 1000.0

    n_beats    = int(dur_sec / beat_period_sec) + 2
    beat_times = np.array([anchor_sec + i * beat_period_sec for i in range(n_beats)])
    beat_times = beat_times[(beat_times >= 0) & (beat_times < dur_sec)]

    data: dict[int, dict] = {}

    # ── Cue 1 — Load cue at FIRST SOUND ──────────────────────────────────────
    try:
        onset_env   = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        onset_times = librosa.times_like(onset_env, sr=sr, hop_length=512)
        onset_frames = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=sr, units="frames",
            pre_max=3, post_max=3, pre_avg=5, post_avg=5,
            delta=0.03, wait=10
        )
        first_sound_ms = (float(onset_times[onset_frames[0]]) * 1000.0
                          if len(onset_frames) else first_beat_ms)
    except Exception:
        first_sound_ms = first_beat_ms

    data[SLOT_LOAD] = {"start": first_sound_ms, "len": 0.0,
                       "type": 3, "name": "Load", "color": "#FFFFFF"}

    # ── Cue 2 — first beat (grid-aligned) ────────────────────────────────────
    data[SLOT_FIRST_BEAT] = {"start": first_beat_ms, "len": 0.0,
                             "type": 1, "name": "Fade In", "color": None}

    # ── Cue 8 — 16 beats before end ──────────────────────────────────────────
    end_ms        = dur_sec * 1000.0
    outro_ms      = end_ms - OUTRO_BEATS_FROM_END * beat_period_ms
    outro_snapped = snap_to_grid(outro_ms, first_beat_ms, beat_period_ms)
    if outro_snapped > first_beat_ms + beat_period_ms:
        data[SLOT_OUTRO] = {"start": outro_snapped, "len": 0.0,
                            "type": 2, "name": "Outro", "color": None}

    # ── Cue 4 — main drop (highest-energy beat) ───────────────────────────────
    try:
        onset_env   = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        onset_times = librosa.times_like(onset_env, sr=sr, hop_length=512)

        beat_frames  = np.searchsorted(onset_times, beat_times)
        beat_frames  = np.clip(beat_frames, 0, len(onset_env) - 1)

        search_start = int(MIN_DROP_SEEK_SEC / beat_period_sec)
        search_end   = max(search_start + 8,
                           len(beat_times) - int(30 / beat_period_sec))
        search_beats = beat_frames[search_start:search_end]

        if len(search_beats) >= DROP_SUSTAIN_BEATS:
            window    = DROP_SUSTAIN_BEATS
            strengths = np.array([
                onset_env[search_beats[i:i+window]].mean()
                for i in range(len(search_beats) - window + 1)
            ])
            threshold  = np.percentile(onset_env[beat_frames], DROP_ENERGY_PERCENTILE)
            candidates = np.where(strengths >= threshold)[0]
            if len(candidates):
                drop_beat_idx = search_start + int(candidates[0])
                drop_sec      = float(beat_times[drop_beat_idx])
                drop_ms       = snap_to_4beat(drop_sec * 1000.0, first_beat_ms, beat_period_ms)
                loop_len_ms   = DROP_LOOP_BEATS * beat_period_ms
                data[SLOT_DROP] = {"start": drop_ms, "len": loop_len_ms,
                                   "type": 5, "name": "Drop", "color": None}
    except Exception:
        pass

    # ── Cue 3 — first vocal / melodic onset ──────────────────────────────────
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

        min_frame   = int(MIN_VOCAL_SEEK_SEC * sr / hop)
        sustain     = 10
        vocal_frame = None
        above       = 0
        for i in range(min_frame, len(band_rms)):
            if band_rms[i] >= threshold:
                above += 1
                if above >= sustain:
                    vocal_frame = i - sustain + 1
                    break
            else:
                above = 0

        if vocal_frame is not None:
            vocal_sec = float(times[vocal_frame])
            if vocal_sec > anchor_sec + beat_period_sec:
                vocal_ms      = vocal_sec * 1000.0
                vocal_snapped = snap_to_grid(vocal_ms, first_beat_ms, beat_period_ms)
                data[SLOT_VOCAL] = {"start": vocal_snapped, "len": 0.0,
                                    "type": 0, "name": "Vocal", "color": None}
    except Exception:
        pass

    return data


def _data_to_element(slot: int, d: dict) -> ET.Element:
    return make_cue(slot, d["start"], d.get("name", "n.n."),
                    type_=d.get("type", 0), len_ms=d.get("len", 0.0),
                    color=d.get("color"))


def compute_audio_cues(audio_path: str,
                       first_beat_ms: float | None,
                       bpm: float | None,
                       duration: float | None) -> dict[int, ET.Element]:
    """Compute audio cues and return ET.Elements (wrapper around _compute_cue_data)."""
    data = _compute_cue_data(audio_path, first_beat_ms, bpm, duration)
    return {slot: _data_to_element(slot, d) for slot, d in data.items()}


# ── Subprocess worker with stall recovery ─────────────────────────────────────
#
# Audio analysis can hang on malformed files.  We run each track in a
# persistent worker process and kill+restart it if it exceeds the timeout.

def _persistent_worker(in_q: multiprocessing.Queue,
                       out_q: multiprocessing.Queue) -> None:
    """
    Long-running worker process: read (audio_path, first_beat_ms, bpm, duration)
    from in_q, write result dict (or None on error) to out_q.
    Send None on in_q to shut down cleanly.
    """
    while True:
        item = in_q.get()
        if item is None:
            break
        audio_path, first_beat_ms, bpm, duration = item
        try:
            result = _compute_cue_data(audio_path, first_beat_ms, bpm, duration)
        except Exception:
            result = None
        out_q.put(result)


class TimeoutWorker:
    """
    Single reusable worker process for audio analysis.
    Auto-restarts if the worker stalls (exceeds timeout).
    """

    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout
        self._proc: multiprocessing.Process | None = None
        self._in_q: multiprocessing.Queue | None = None
        self._out_q: multiprocessing.Queue | None = None
        self._start()

    def _start(self) -> None:
        self._in_q  = multiprocessing.Queue()
        self._out_q = multiprocessing.Queue()
        self._proc  = multiprocessing.Process(
            target=_persistent_worker,
            args=(self._in_q, self._out_q),
            daemon=True,
        )
        self._proc.start()

    def analyze(self, audio_path: str,
                first_beat_ms: float | None,
                bpm: float | None,
                duration: float | None) -> dict | None:
        """
        Run audio analysis in the worker.
        Returns cue data dict or None on stall/crash.
        """
        if not self._proc.is_alive():
            print("  [worker] dead worker - restarting", flush=True)
            self._start()

        self._in_q.put((audio_path, first_beat_ms, bpm, duration))
        try:
            return self._out_q.get(timeout=self.timeout)
        except _queue.Empty:
            print(f"  [worker] stall after {self.timeout}s - killing and restarting", flush=True)
            self._proc.kill()
            self._proc.join(5)
            self._start()
            return None

    def close(self) -> None:
        if self._proc and self._proc.is_alive():
            try:
                self._in_q.put(None)
                self._proc.join(5)
            except Exception:
                pass
            if self._proc.is_alive():
                self._proc.kill()


# ── Progress tracking ─────────────────────────────────────────────────────────

def load_progress() -> tuple[set[str], set[str]]:
    """Return (done, stalled) path sets from the state file."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("done", [])), set(data.get("stalled", []))
        except Exception:
            pass
    return set(), set()


def save_progress(done: set[str], stalled: set[str]) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"done": sorted(done), "stalled": sorted(stalled)},
                   ensure_ascii=False)
    )


# ── NML processing ────────────────────────────────────────────────────────────

def process_nml(nml_path: Path, mode: str, apply: bool, limit: int,
                audio_root: Path | None, done: set[str], stalled: set[str],
                worker: "TimeoutWorker | None" = None,
                verbose: bool = False) -> tuple[int, int, int]:
    """
    Process one NML file.

    mode:  "fast"  — NML-only (cues 1, 2, 8)
           "audio" — full audio analysis (cues 1, 2, 3, 4, 8)

    Returns (candidates, written, skipped_no_data).
    """
    print(f"\n{'-'*60}")
    print(f"  NML:  {nml_path}")
    print(f"  Mode: {mode}  |  Apply: {apply}  |  Limit: {limit or 'all'}")
    print(f"{'-'*60}")

    tree    = ET.parse(nml_path)
    root    = tree.getroot()
    coll    = root.find("COLLECTION")
    entries = coll.findall("ENTRY")

    candidates = 0
    written    = 0
    skipped_nd = 0
    modified   = False

    t_start = time.time()

    for entry in entries:
        artist = entry.get("ARTIST", "")
        title  = entry.get("TITLE",  "")

        loc  = entry.find("LOCATION")
        if loc is None:
            continue
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )

        # Skip already-processed tracks (done or stalled)
        if path in done or path in stalled:
            continue

        # Determine which slots are missing for this track
        existing_hotcues = {c.get("HOTCUE") for c in entry.findall("CUE_V2")}

        load_el = next((c for c in entry.findall("CUE_V2")
                        if c.get("HOTCUE") == "0"), None)
        fb_el   = next((c for c in entry.findall("CUE_V2")
                        if c.get("HOTCUE") == str(SLOT_FIRST_BEAT)), None)

        need_load       = load_el is None or load_el.get("TYPE") != "3"
        need_first_beat = fb_el   is None or fb_el.get("TYPE")   != "1"
        need_outro      = str(SLOT_OUTRO) not in existing_hotcues

        # Vocal and drop are only set in audio mode
        if mode == "audio":
            need_vocal = str(SLOT_VOCAL) not in existing_hotcues
            need_drop  = str(SLOT_DROP)  not in existing_hotcues
        else:
            need_vocal = False
            need_drop  = False

        if not any([need_load, need_first_beat, need_outro, need_vocal, need_drop]):
            continue   # all relevant slots already set

        candidates += 1
        if limit and written >= limit:
            break

        # Compute cues
        if mode == "fast":
            all_cues_el = compute_fast_cues(entry) or {}
            stalled_track = False
        else:
            first_beat_ms, bpm = parse_grid(entry)
            duration            = get_duration(entry)
            if audio_root:
                # Strip Mac-side path prefix up to the anchor component so that
                # D:/Aaron/Music/VERAS SONGS + corrected_music/Artist/Album/file
                # resolves correctly from NML paths like
                # /Users/.../corrected_music/Artist/Album/file
                p_parts = Path(path).parts
                anchor  = "corrected_music"
                try:
                    idx = next(i for i, p in enumerate(p_parts)
                               if p.lower() == anchor.lower())
                    rel = Path(*p_parts[idx + 1:])
                except StopIteration:
                    rel = Path(path).relative_to("/") if Path(path).is_absolute() else Path(path)
                audio_path = str(audio_root / rel)
            else:
                audio_path = path

            cue_data = worker.analyze(audio_path, first_beat_ms, bpm, duration)
            if cue_data is None:
                stalled_track = True
                all_cues_el   = {}
            else:
                stalled_track = False
                all_cues_el   = {slot: _data_to_element(slot, d)
                                 for slot, d in cue_data.items()}

        if stalled_track:
            stalled.add(path)
            print(f"  [stall]  {artist} - {title}", flush=True)
            if apply:
                save_progress(done, stalled)
            continue

        # Filter to only the slots this track actually needs
        new_cues: dict[int, ET.Element] = {}
        if need_load       and SLOT_LOAD        in all_cues_el: new_cues[SLOT_LOAD]       = all_cues_el[SLOT_LOAD]
        if need_first_beat and SLOT_FIRST_BEAT  in all_cues_el: new_cues[SLOT_FIRST_BEAT] = all_cues_el[SLOT_FIRST_BEAT]
        if need_vocal      and SLOT_VOCAL       in all_cues_el: new_cues[SLOT_VOCAL]      = all_cues_el[SLOT_VOCAL]
        if need_drop       and SLOT_DROP        in all_cues_el: new_cues[SLOT_DROP]       = all_cues_el[SLOT_DROP]
        if need_outro      and SLOT_OUTRO       in all_cues_el: new_cues[SLOT_OUTRO]      = all_cues_el[SLOT_OUTRO]

        if not new_cues:
            skipped_nd += 1
            if verbose:
                print(f"  [skip]   {artist} - {title}")
            continue

        # Apply — write CUE_V2 elements into the entry
        if apply:
            children      = list(entry)
            existing_cues = entry.findall("CUE_V2")

            # SLOT_LOAD (HOTCUE=0): update TYPE in-place rather than adding a duplicate
            if SLOT_LOAD in new_cues:
                updated_load = False
                for cue_el in existing_cues:
                    if cue_el.get("HOTCUE") == "0":
                        cue_el.set("TYPE", "3")
                        cue_el.set("NAME", "Load")
                        updated_load = True
                        break
                if not updated_load:
                    el = new_cues[SLOT_LOAD]
                    el.tail = "\n      "
                    idx = children.index(existing_cues[-1]) + 1 if existing_cues else len(children)
                    entry.insert(idx, el)

            # SLOT_FIRST_BEAT (HOTCUE=1): update in-place if already present
            if SLOT_FIRST_BEAT in new_cues:
                existing_fb = next((c for c in entry.findall("CUE_V2")
                                    if c.get("HOTCUE") == str(SLOT_FIRST_BEAT)), None)
                if existing_fb is not None:
                    existing_fb.set("TYPE", "1")
                    existing_fb.set("NAME", "Fade In")
                    new_cues.pop(SLOT_FIRST_BEAT)

            # All other slots: insert as new elements after the last existing CUE_V2
            existing_cues = entry.findall("CUE_V2")
            children      = list(entry)
            insert_after  = (children.index(existing_cues[-1]) + 1
                             if existing_cues else len(children))

            for slot in sorted(k for k in new_cues if k != SLOT_LOAD):
                el = new_cues[slot]
                el.tail = "\n      "
                entry.insert(insert_after, el)
                insert_after += 1

            children = list(entry)
            if children:
                children[-1].tail = "\n    "

            modified = True
            done.add(path)

        slots_placed = sorted(new_cues.keys())
        slot_labels  = {SLOT_LOAD: "1(L)", SLOT_FIRST_BEAT: "2",
                        SLOT_VOCAL: "3", SLOT_DROP: "4", SLOT_OUTRO: "8(F)"}
        placed_str   = "+".join(slot_labels[s] for s in slots_placed)
        if apply:
            print(f"  [ok] [{placed_str}]  {artist} - {title}", flush=True)
        else:
            c2 = new_cues.get(SLOT_FIRST_BEAT)
            c8 = new_cues.get(SLOT_OUTRO)
            c2ms = f"{float(c2.get('START')):.0f}ms" if c2 is not None else "-"
            c8ms = f"{float(c8.get('START')):.0f}ms" if c8 is not None else "-"
            print(f"  DRY [{placed_str}]  {artist} - {title}  |  C2={c2ms} C8={c8ms}")

        written += 1

        if written % 100 == 0:
            elapsed  = time.time() - t_start
            rate     = written / elapsed if elapsed > 0 else 0
            remaining = (candidates - written) / rate if rate > 0 else 0
            eta_h    = int(remaining // 3600)
            eta_m    = int((remaining % 3600) // 60)
            print(f"  ... {written:,} done | {candidates - written:,} remaining "
                  f"| {rate:.1f} t/s | ETA ~{eta_h}h{eta_m:02d}m", flush=True)
            if apply:
                save_progress(done, stalled)

    if apply and modified:
        backup = nml_path.with_suffix(".nml.autocue_bak")
        shutil.copy2(nml_path, backup)
        print(f"\n  Backup -> {backup.name}")

        tree.write(str(nml_path), encoding="utf-8", xml_declaration=False)
        content = nml_path.read_text(encoding="utf-8")
        nml_path.write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n' + content,
            encoding="utf-8"
        )
        print(f"  Saved  -> {nml_path}")
        save_progress(done, stalled)

    return candidates, written, skipped_nd


# ── Report ────────────────────────────────────────────────────────────────────

def report(nml_path: Path) -> None:
    tree  = ET.parse(nml_path)
    coll  = tree.getroot().find("COLLECTION")
    total = has_autogrid = has_user_cues = no_bpm = target_empty = 0
    slot_counts = {SLOT_LOAD: 0, SLOT_FIRST_BEAT: 0,
                   SLOT_VOCAL: 0, SLOT_DROP: 0, SLOT_OUTRO: 0}
    load_as_load_type = 0

    for e in coll.findall("ENTRY"):
        total += 1
        _, bpm = parse_grid(e)
        if bpm is None:
            no_bpm += 1
        cues     = e.findall("CUE_V2")
        hotcues  = {c.get("HOTCUE") for c in cues}
        if "-1" in hotcues:
            has_autogrid += 1
        for c in cues:
            if c.get("HOTCUE") == "0" and c.get("TYPE") == "3":
                load_as_load_type += 1
                break
        user = existing_user_hotcues(e)
        if user:
            has_user_cues += 1
        else:
            target_empty += 1
        for slot in slot_counts:
            if str(slot) in hotcues:
                slot_counts[slot] += 1

    done, stalled = load_progress()
    print(f"\n  {nml_path.name}")
    print(f"  Total entries:                  {total:>7,}")
    print(f"  Has AutoGrid (has BPM grid):    {has_autogrid:>7,}")
    print(f"  Has user cue points (skip):     {has_user_cues:>7,}")
    print(f"  Need auto-cueing (candidates):  {target_empty:>7,}")
    print(f"  No BPM (will be skipped):       {no_bpm:>7,}")
    print(f"  Cue 1 Load type already set:    {load_as_load_type:>7,}")
    print(f"  Cue 2 already set:              {slot_counts[SLOT_FIRST_BEAT]:>7,}")
    print(f"  Cue 3 (vocal) already set:      {slot_counts[SLOT_VOCAL]:>7,}")
    print(f"  Cue 4 (drop loop) already set:  {slot_counts[SLOT_DROP]:>7,}")
    print(f"  Cue 8 (fade-out) already set:   {slot_counts[SLOT_OUTRO]:>7,}")
    print(f"  Progress - done:                {len(done):>7,}")
    print(f"  Progress - stalled:             {len(stalled):>7,}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Auto cue point setter for Traktor NML collections",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--fast",     action="store_true",
                            help="NML-only: write Cue 2 (first beat) + Cue 8 (outro). Fast, no audio.")
    mode_group.add_argument("--audio",    action="store_true",
                            help="Audio analysis: write Cues 1, 2, 3, 4, 8. Requires librosa.")
    mode_group.add_argument("--all",      action="store_true",
                            help="All cues: same as --audio.")
    mode_group.add_argument("--report",   action="store_true",
                            help="Show stats only, no changes.")

    parser.add_argument("--apply",          action="store_true",
                        help="Write changes to NML files (default: dry-run only).")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Print what would be written without changing files (default).")
    parser.add_argument("--limit",          type=int, default=0,
                        help="Max number of tracks to process per NML (0 = all).")
    parser.add_argument("--nml",            type=str, default="",
                        help="Path to a specific NML file. Default: both corrected and live.")
    parser.add_argument("--audio-root",     type=str, default="",
                        help="Override audio file root directory (for cross-machine use).")
    parser.add_argument("--timeout",        type=int, default=120,
                        help="Per-track analysis timeout in seconds (default: 120). "
                             "Stalled tracks are skipped and logged.")
    parser.add_argument("--reset-progress", action="store_true",
                        help="Clear the progress state file and start fresh.")
    parser.add_argument("--retry-stalled",  action="store_true",
                        help="Retry tracks that previously stalled (clears stalled list).")
    parser.add_argument("--verbose",        action="store_true",
                        help="Print every skipped track.")
    args = parser.parse_args()

    if not any([args.fast, args.audio, args.all, args.report]):
        parser.print_help()
        return

    # Determine NML targets
    if args.nml:
        nml_paths = [Path(args.nml)]
    else:
        nml_paths = [p for p in [NML_CORR, NML_LIVE] if p.exists()]

    if not nml_paths:
        print("ERROR: No NML files found. Pass --nml or check paths.")
        sys.exit(1)

    # Report mode
    if args.report:
        print("\n=== Auto Cue Point Report ===")
        for p in nml_paths:
            report(p)
        print()
        return

    # Keep machine awake + run at low priority so it doesn't interfere with
    # other work on the machine
    keep_awake()
    set_low_priority()

    done, stalled = load_progress()

    if args.reset_progress:
        done    = set()
        stalled = set()
        print("Progress state cleared.")
    elif args.retry_stalled:
        print(f"Clearing {len(stalled):,} stalled tracks for retry.")
        stalled = set()

    print(f"Progress: {len(done):,} done, {len(stalled):,} stalled from previous runs.")

    audio_root = Path(args.audio_root) if args.audio_root else None
    apply      = args.apply and not args.dry_run

    if not apply:
        print("\n  *** DRY RUN - no files will be modified ***")
        print("  Pass --apply to write changes.\n")

    total_candidates = total_written = total_skipped = 0

    mode = "fast" if args.fast else "audio"

    # Start the worker process for audio mode
    worker = None
    if mode == "audio":
        worker = TimeoutWorker(timeout=args.timeout)
        print(f"  Worker started (PID {worker._proc.pid}) "
              f"| timeout={args.timeout}s per track\n")

    try:
        for nml_path in nml_paths:
            c, w, s = process_nml(
                nml_path, mode, apply, args.limit,
                audio_root, done, stalled,
                worker=worker, verbose=args.verbose,
            )
            total_candidates += c
            total_written    += w
            total_skipped    += s
    finally:
        if worker:
            worker.close()

    print(f"\n{'='*60}")
    print(f"  Candidates:            {total_candidates:,}")
    print(f"  Written/queued:        {total_written:,}")
    print(f"  Skipped (no BPM/data): {total_skipped:,}")
    print(f"  Stalled (timeout):     {len(stalled):,}")
    if apply:
        print(f"  Progress saved:        {len(done):,} tracks total")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    multiprocessing.freeze_support()   # needed if ever compiled to .exe on Windows
    main()
