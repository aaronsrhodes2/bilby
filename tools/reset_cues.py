#!/usr/bin/env python3
"""
reset_cues.py — Full cue point reset for all tracks in corrected_traktor/collection.nml.

Wipes every existing CUE_V2 and sets 8 slots from scratch using librosa audio
analysis + Traktor's own BPM/beat-anchor from the NML.

Slot → HOTCUE → Type  → What
  1  →    0   →  load  → First audio sound
  2  →    1   →  cue   → First beat (NML anchor, or librosa fallback)
  3  →    2   →  cue   → Bar START before first vocal entry
  4  →    3   →  loop  → First large beat (highest onset energy, 1-bar loop)
  5  →    4   →  cue   → Start of largest intermission (longest RMS dip)
  6  →    5   →  cue   → Bar END after last vocal
  7  →    6   →  —     → Unassigned (skipped)
  8  →    7   →  fade  → 8 bars before track goes silent

Traktor CUE_V2 TYPE values:
  0 = Hotcue   1 = Fade-in   2 = Fade-out   3 = Load   4 = Grid   5 = Loop

Usage:
  python3 tools/reset_cues.py [--dry-run] [--limit N] [--nml PATH]

  --dry-run      Print what would be written; don't touch the NML
  --limit N      Stop after N tracks (resumable — skips already-done tracks)
  --nml PATH     Override NML path (default: corrected_traktor/collection.nml)
  --workers N    Parallel audio workers (default: 4)
"""

from __future__ import annotations
import argparse
import json
import math
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

BASE        = Path(__file__).resolve().parent.parent
# If running from a git worktree, the synced data lives in the main repo
_MAIN_REPO  = Path("D:/Aaron/development/music-collection")
NML_DEFAULT = (
    _MAIN_REPO / "corrected_traktor" / "collection.nml"
    if (_MAIN_REPO / "corrected_traktor" / "collection.nml").exists()
    else BASE / "corrected_traktor" / "collection.nml"
)
PROGRESS    = BASE / "state" / "cue_reset_progress.json"
MUSIC_ROOT  = Path("D:/Aaron/Music/VERAS SONGS")
AUDIO_EXTS  = {".mp3", ".flac", ".aiff", ".m4a", ".wav", ".ogg"}

# Librosa SR
SR = 22050

# ── Cue slot constants ────────────────────────────────────────────────────────
SLOT_LOAD        = 0   # TYPE=3
SLOT_FIRST_BEAT  = 1   # TYPE=0 (hotcue)
SLOT_VOCAL_IN    = 2   # TYPE=0 — bar START before first vocal
SLOT_DROP        = 3   # TYPE=5 (loop)
SLOT_INTERMISSION = 4  # TYPE=0
SLOT_VOCAL_OUT   = 5   # TYPE=0 — bar END after last vocal
# SLOT 6 = unassigned
SLOT_OUTRO       = 7   # TYPE=2 (fade-out)

SLOT_TYPE = {
    SLOT_LOAD:        3,
    SLOT_FIRST_BEAT:  0,
    SLOT_VOCAL_IN:    0,
    SLOT_DROP:        5,
    SLOT_INTERMISSION: 0,
    SLOT_VOCAL_OUT:   0,
    SLOT_OUTRO:       2,
}

SLOT_NAME = {
    SLOT_LOAD:        "Load",
    SLOT_FIRST_BEAT:  "Beat",
    SLOT_VOCAL_IN:    "Vocal In",
    SLOT_DROP:        "Drop",
    SLOT_INTERMISSION: "Break",
    SLOT_VOCAL_OUT:   "Vocal Out",
    SLOT_OUTRO:       "Outro",
}

OUTRO_BARS     = 8
DROP_LOOP_BARS = 1      # loop length for slot 4
SILENCE_RMS_DB = -50.0  # below this = silence for slot 8 detection

# Vocal detection
VOICE_LOW  = 300.0
VOICE_HIGH = 3500.0
MIN_VOCAL_SEEK_SEC  = 4.0
VOCAL_SUSTAIN_FRAMES = 10

# Intermission detection
MIN_INTERMISSION_SEEK_SEC = 15.0
MIN_INTERMISSION_LEN_SEC  = 2.0
INTERMISSION_WINDOW_SEC   = 0.5

# Drop detection
DROP_ENERGY_PCT   = 75
DROP_SUSTAIN_BEATS = 4
MIN_DROP_SEEK_SEC  = 15.0


# ── Grid helpers ──────────────────────────────────────────────────────────────

def bar_length_ms(beat_period_ms: float) -> float:
    return beat_period_ms * 4.0


def snap_bar_start_before(ms: float, anchor_ms: float, beat_period_ms: float) -> float:
    """Snap ms to the bar boundary at or before ms."""
    bar_ms = bar_length_ms(beat_period_ms)
    if bar_ms <= 0:
        return ms
    offset = (ms - anchor_ms) % bar_ms
    return ms - offset


def snap_bar_end_after(ms: float, anchor_ms: float, beat_period_ms: float) -> float:
    """Snap ms to the bar boundary strictly after ms."""
    bar_ms = bar_length_ms(beat_period_ms)
    if bar_ms <= 0:
        return ms
    offset = (ms - anchor_ms) % bar_ms
    if offset == 0:
        return ms + bar_ms
    return ms - offset + bar_ms


def snap_to_beat(ms: float, anchor_ms: float, beat_period_ms: float) -> float:
    if beat_period_ms <= 0:
        return ms
    offset = (ms - anchor_ms) % beat_period_ms
    if offset < beat_period_ms / 2:
        return ms - offset
    return ms - offset + beat_period_ms


# ── NML helpers ───────────────────────────────────────────────────────────────

def decode_traktor_dir(volume: str, dir_str: str, filename: str) -> str:
    """
    Convert Traktor LOCATION (VOLUME + DIR + FILE) to a plain path string.
    DIR uses /: as separator. Returns a Mac-style absolute path string —
    we only use this for the filename, not to open the file on Windows.
    """
    parts = [p for p in dir_str.split("/:") if p]
    return "/".join(parts) + "/" + filename


def nml_artist_folder(dir_str: str) -> str:
    """
    Extract the artist folder name from a Traktor DIR string.
    corrected_music is the root — the component after it is the artist folder.
    """
    parts = [p for p in dir_str.split("/:") if p]
    try:
        idx = next(i for i, p in enumerate(parts)
                   if "corrected_music" in p.lower() or "veras songs" in p.lower())
        if idx + 1 < len(parts):
            return parts[idx + 1]
    except StopIteration:
        pass
    return parts[-2] if len(parts) >= 2 else ""


def parse_nml_entries(nml_path: Path) -> list[dict]:
    """
    Parse NML. Return list of dicts with keys:
      dkey, artist, title, bpm, beat_anchor_ms, duration_ms,
      loc_artist_folder, loc_filename, entry_index
    """
    tree  = ET.parse(str(nml_path))
    root  = tree.getroot()
    coll  = root.find("COLLECTION")
    if coll is None:
        return []

    entries = []
    for idx, entry in enumerate(coll.findall("ENTRY")):
        artist = entry.get("ARTIST", "")
        title  = entry.get("TITLE", "")

        loc   = entry.find("LOCATION")
        tempo = entry.find("TEMPO")
        info  = entry.find("INFO")

        if loc is None or tempo is None:
            continue

        bpm = float(tempo.get("BPM", 0) or 0)
        if bpm <= 0:
            continue

        beat_period_ms = 60000.0 / bpm

        # Beat anchor: prefer AutoGrid CUE_V2 (HOTCUE="-1")
        beat_anchor_ms = None
        for cue in entry.findall("CUE_V2"):
            if cue.get("HOTCUE") == "-1":
                beat_anchor_ms = float(cue.get("START", 0))
                break
        if beat_anchor_ms is None:
            beat_anchor_ms = 0.0

        duration_ms = None
        if info is not None:
            pt = info.get("PLAYTIME_FLOAT")
            if pt:
                duration_ms = float(pt) * 1000.0

        dir_str  = loc.get("DIR", "")
        filename = loc.get("FILE", "")

        entries.append({
            "dkey":              f"{artist.lower().strip()}\t{base_title(title)}",
            "artist":            artist,
            "title":             title,
            "bpm":               bpm,
            "beat_period_ms":    beat_period_ms,
            "beat_anchor_ms":    beat_anchor_ms,
            "duration_ms":       duration_ms,
            "loc_artist_folder": nml_artist_folder(dir_str),
            "loc_filename":      filename,
            "entry_index":       idx,
        })

    return entries


# ── Audio file matching ───────────────────────────────────────────────────────

def base_title(title: str) -> str:
    return re.sub(r"\s*[\(\[].*", "", title).strip().lower()


def parse_filename_candidates(stem: str, dir_artist: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    s = re.sub(r"^\d+[-\s]\d+\s*[-.]?\s*", "", stem)
    s = re.sub(r"^\d+\s*[-.]?\s*", "", s)
    if " - " in s:
        parts = s.split(" - ", 2)
        if len(parts) >= 2:
            candidates.append((parts[0].strip(), parts[-1].strip()))
    if "-" in s:
        idx = s.index("-")
        candidates.append((s[:idx].strip(), s[idx + 1:].strip()))
    candidates.append((dir_artist, s))
    return candidates


def build_audio_index(nml_entries: list[dict]) -> dict[str, Path]:
    """
    Walk MUSIC_ROOT, build {dkey: path}.
    Also try matching via NML artist-folder + filename stem.
    """
    dkeys = {e["dkey"] for e in nml_entries}

    # dkey → set of possible artist folders from NML
    artist_folders: dict[str, str] = {
        e["dkey"]: e["loc_artist_folder"].lower()
        for e in nml_entries
    }

    index: dict[str, Path] = {}
    for fpath in MUSIC_ROOT.rglob("*"):
        if fpath.suffix.lower() not in AUDIO_EXTS:
            continue
        if fpath.name.startswith("._"):
            continue

        dir_artist = fpath.parent.parent.name

        # Try dkey matching via filename parse
        matched = False
        for artist, title in parse_filename_candidates(fpath.stem, dir_artist):
            dk = f"{artist.lower().strip()}\t{base_title(title)}"
            if dk in dkeys and dk not in index:
                index[dk] = fpath
                matched = True
                break

        # Try matching via NML artist folder name vs local dir
        if not matched:
            local_artist_lower = dir_artist.lower()
            for dk, af in artist_folders.items():
                if dk in index:
                    continue
                if af and (af in local_artist_lower or local_artist_lower in af):
                    # Check filename stem similarity
                    nml_entry = next(e for e in nml_entries if e["dkey"] == dk)
                    nml_stem = Path(nml_entry["loc_filename"]).stem.lower()
                    local_stem = re.sub(r"^\d+[-\s]\d+\s*[-.]?\s*", "", fpath.stem)
                    local_stem = re.sub(r"^\d+\s*[-.]?\s*", "", local_stem).lower()
                    # Simple: check if NML stem contains local stem or vice versa
                    if nml_stem and local_stem and (
                        nml_stem in local_stem or local_stem in nml_stem
                    ):
                        index[dk] = fpath
                        break

    return index


# ── Audio analysis ────────────────────────────────────────────────────────────

def _db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def compute_all_cues(args: tuple) -> tuple[str, dict]:
    """
    Worker function (runs in subprocess). Returns (dkey, cue_dict).
    cue_dict: {slot_int: start_ms} plus "drop_len_ms" for the loop slot.
    """
    dkey, audio_path_str, bpm, beat_anchor_ms, duration_ms = args

    try:
        import librosa
        import numpy as np
    except ImportError:
        return dkey, {"error": "librosa not installed"}

    beat_period_ms = 60000.0 / bpm
    bar_ms         = beat_period_ms * 4.0

    try:
        y, sr = librosa.load(audio_path_str, sr=SR, mono=True)
    except Exception as e:
        return dkey, {"error": str(e)}

    dur_sec = len(y) / sr
    if dur_sec < 30:
        return dkey, {"error": "too short"}

    cues: dict = {}

    # ── Beat grid (use NML anchor — most accurate) ────────────────────────────
    anchor_sec     = beat_anchor_ms / 1000.0
    beat_period_sec = beat_period_ms / 1000.0
    n_beats        = int(dur_sec / beat_period_sec) + 2
    beat_times     = np.array([anchor_sec + i * beat_period_sec for i in range(n_beats)])
    beat_times     = beat_times[(beat_times >= 0) & (beat_times < dur_sec)]

    # ── Slot 1 — First sound (load cue) ──────────────────────────────────────
    try:
        onset_env   = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        onset_times = librosa.times_like(onset_env, sr=sr, hop_length=512)
        frames      = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=sr, units="frames",
            pre_max=3, post_max=3, pre_avg=5, post_avg=5, delta=0.03, wait=10
        )
        first_sound_ms = float(onset_times[frames[0]]) * 1000.0 if len(frames) else beat_anchor_ms
    except Exception:
        first_sound_ms = beat_anchor_ms
    cues[SLOT_LOAD] = first_sound_ms

    # ── Slot 2 — First beat (NML anchor) ─────────────────────────────────────
    cues[SLOT_FIRST_BEAT] = beat_anchor_ms

    # ── Slot 3 — Bar start before first vocal ────────────────────────────────
    try:
        y_harm   = librosa.effects.harmonic(y, margin=4)
        n_fft    = 2048
        hop      = 512
        freqs    = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        mask     = (freqs >= VOICE_LOW) & (freqs <= VOICE_HIGH)
        D        = librosa.stft(y_harm, n_fft=n_fft, hop_length=hop)
        D_band   = np.abs(D)
        D_band[~mask, :] = 0
        band_rms = np.sqrt((D_band ** 2).mean(axis=0))
        times_v  = librosa.times_like(band_rms, sr=sr, hop_length=hop, n_fft=n_fft)

        intro_f  = int(5.0 * sr / hop)
        baseline = np.median(band_rms[:intro_f]) if intro_f > 0 else 0
        thresh   = max(baseline * 3.0, np.percentile(band_rms, 40))
        min_f    = int(MIN_VOCAL_SEEK_SEC * sr / hop)
        above = 0
        vocal_in_frame = None
        for i in range(min_f, len(band_rms)):
            if band_rms[i] >= thresh:
                above += 1
                if above >= VOCAL_SUSTAIN_FRAMES:
                    vocal_in_frame = i - VOCAL_SUSTAIN_FRAMES + 1
                    break
            else:
                above = 0

        if vocal_in_frame is not None:
            vocal_in_ms = float(times_v[vocal_in_frame]) * 1000.0
            slot3_ms    = snap_bar_start_before(vocal_in_ms, beat_anchor_ms, beat_period_ms)
            if slot3_ms > beat_anchor_ms:
                cues[SLOT_VOCAL_IN] = slot3_ms
                # Store raw for slot 6 too
                cues["_vocal_in_ms"]  = vocal_in_ms
                cues["_band_rms"]     = band_rms.tolist()
                cues["_times_v"]      = times_v.tolist()
    except Exception:
        pass

    # ── Slot 4 — First large beat (loop) ─────────────────────────────────────
    try:
        onset_env   = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        onset_times = librosa.times_like(onset_env, sr=sr, hop_length=512)
        beat_frames = np.searchsorted(onset_times, beat_times)
        beat_frames = np.clip(beat_frames, 0, len(onset_env) - 1)

        s_start = int(MIN_DROP_SEEK_SEC / beat_period_sec)
        s_end   = max(s_start + 8, len(beat_times) - int(30 / beat_period_sec))
        search  = beat_frames[s_start:s_end]

        if len(search) >= DROP_SUSTAIN_BEATS:
            w         = DROP_SUSTAIN_BEATS
            strengths = np.array([
                onset_env[search[i:i+w]].mean()
                for i in range(len(search) - w + 1)
            ])
            threshold  = np.percentile(onset_env[beat_frames], DROP_ENERGY_PCT)
            candidates = np.where(strengths >= threshold)[0]
            if len(candidates):
                drop_idx    = s_start + int(candidates[0])
                drop_sec    = float(beat_times[drop_idx])
                drop_ms     = snap_bar_start_before(drop_sec * 1000.0,
                                                    beat_anchor_ms, beat_period_ms)
                cues[SLOT_DROP]          = drop_ms
                cues["_drop_len_ms"]     = DROP_LOOP_BARS * bar_ms
    except Exception:
        pass

    # ── Slot 5 — Largest intermission (longest RMS dip) ──────────────────────
    try:
        hop_i    = 1024
        rms_full = librosa.feature.rms(y=y, hop_length=hop_i)[0]
        t_full   = librosa.times_like(rms_full, sr=sr, hop_length=hop_i)
        # Silence threshold: 20% of median RMS
        med      = np.median(rms_full[rms_full > 0]) if rms_full.any() else 1e-6
        sil_thr  = med * 0.20

        seek_start_f = int(MIN_INTERMISSION_SEEK_SEC * sr / hop_i)
        min_len_f    = int(MIN_INTERMISSION_LEN_SEC * sr / hop_i)

        best_len   = 0
        best_start = None
        run_start  = None
        run_len    = 0

        for i in range(seek_start_f, len(rms_full)):
            if rms_full[i] < sil_thr:
                if run_start is None:
                    run_start = i
                run_len += 1
            else:
                if run_start is not None and run_len >= min_len_f and run_len > best_len:
                    best_len   = run_len
                    best_start = run_start
                run_start = None
                run_len   = 0

        if best_start is not None and best_len >= min_len_f:
            interm_ms = float(t_full[best_start]) * 1000.0
            snapped   = snap_bar_start_before(interm_ms, beat_anchor_ms, beat_period_ms)
            if snapped > beat_anchor_ms:
                cues[SLOT_INTERMISSION] = snapped
    except Exception:
        pass

    # ── Slot 6 — Bar end after last vocal ────────────────────────────────────
    try:
        if "_band_rms" in cues:
            band_rms = np.array(cues.pop("_band_rms"))
            times_v  = np.array(cues.pop("_times_v"))
            thresh   = max(np.median(band_rms[band_rms > 0]) * 0.5,
                           np.percentile(band_rms, 40)) if band_rms.any() else 0

            last_vocal_frame = None
            # Search from end backwards
            for i in range(len(band_rms) - 1, int(MIN_VOCAL_SEEK_SEC * sr / 512), -1):
                if band_rms[i] >= thresh:
                    last_vocal_frame = i
                    break

            if last_vocal_frame is not None:
                last_vocal_ms = float(times_v[last_vocal_frame]) * 1000.0
                slot6_ms      = snap_bar_end_after(last_vocal_ms, beat_anchor_ms,
                                                   beat_period_ms)
                if slot6_ms < (dur_sec * 1000.0 - bar_ms):
                    cues[SLOT_VOCAL_OUT] = slot6_ms
    except Exception:
        pass
    finally:
        cues.pop("_vocal_in_ms", None)
        cues.pop("_band_rms", None)
        cues.pop("_times_v", None)

    # ── Slot 8 — 8 bars before silence end ───────────────────────────────────
    try:
        hop_s    = 1024
        rms_s    = librosa.feature.rms(y=y, hop_length=hop_s)[0]
        t_s      = librosa.times_like(rms_s, sr=sr, hop_length=hop_s)
        med_rms  = np.median(rms_s[rms_s > 0]) if rms_s.any() else 1e-6
        sil_amp  = _db_to_amp(SILENCE_RMS_DB)
        sil_thr  = max(med_rms * 0.05, sil_amp)

        # Find last frame above silence threshold
        last_sound_frame = None
        for i in range(len(rms_s) - 1, -1, -1):
            if rms_s[i] > sil_thr:
                last_sound_frame = i
                break

        if last_sound_frame is not None:
            silence_start_ms = float(t_s[last_sound_frame]) * 1000.0
            outro_ms         = silence_start_ms - OUTRO_BARS * bar_ms
            outro_snapped    = snap_bar_start_before(outro_ms, beat_anchor_ms,
                                                     beat_period_ms)
            if outro_snapped > beat_anchor_ms + bar_ms:
                cues[SLOT_OUTRO] = outro_snapped
    except Exception:
        pass

    return dkey, cues


# ── NML writing ───────────────────────────────────────────────────────────────

def make_cue_element(hotcue: int, start_ms: float, name: str,
                     type_: int, len_ms: float = 0.0) -> ET.Element:
    el = ET.Element("CUE_V2")
    el.set("NAME",         name)
    el.set("DISPL_ORDER",  str(hotcue))
    el.set("TYPE",         str(type_))
    el.set("START",        f"{start_ms:.6f}")
    el.set("LEN",          f"{len_ms:.6f}")
    el.set("REPEATS",      "-1")
    el.set("HOTCUE",       str(hotcue))
    return el


def apply_cues_to_entry(entry: ET.Element, cue_dict: dict,
                         beat_period_ms: float) -> int:
    """
    Remove all existing CUE_V2 (including AutoGrid), write new ones.
    Returns number of cue slots written.
    """
    # Remove all existing CUE_V2
    for cue in list(entry.findall("CUE_V2")):
        entry.remove(cue)

    written = 0
    drop_len_ms = cue_dict.pop("_drop_len_ms", beat_period_ms * 4)

    for slot, start_ms in sorted(cue_dict.items()):
        if not isinstance(slot, int):
            continue
        type_ = SLOT_TYPE[slot]
        name  = SLOT_NAME[slot]
        len_ms = drop_len_ms if slot == SLOT_DROP else 0.0
        el = make_cue_element(slot, start_ms, name, type_, len_ms)
        entry.append(el)
        written += 1

    return written


# ── Progress tracking ─────────────────────────────────────────────────────────

def load_progress() -> set[str]:
    if PROGRESS.exists():
        with open(PROGRESS, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_progress(done: set[str]) -> None:
    with open(PROGRESS, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--limit",    type=int, default=0)
    ap.add_argument("--nml",      default=str(NML_DEFAULT))
    ap.add_argument("--workers",  type=int, default=4)
    ap.add_argument("--reset-progress", action="store_true",
                    help="Clear progress file and reprocess everything")
    args = ap.parse_args()

    nml_path = Path(args.nml)
    if not nml_path.exists():
        print(f"ERROR: NML not found: {nml_path}")
        sys.exit(1)

    if args.reset_progress and PROGRESS.exists():
        PROGRESS.unlink()
        print("Progress cleared.")

    print(f"Parsing NML: {nml_path}")
    nml_entries = parse_nml_entries(nml_path)
    print(f"  {len(nml_entries):,} entries with BPM")

    print("Building audio file index…")
    audio_index = build_audio_index(nml_entries)
    print(f"  {len(audio_index):,} local files matched")

    done = load_progress()
    print(f"  {len(done):,} already processed")

    todo = [
        e for e in nml_entries
        if e["dkey"] in audio_index and e["dkey"] not in done
    ]
    if args.limit:
        todo = todo[:args.limit]

    print(f"  {len(todo):,} to process this run\n")

    if not todo:
        print("Nothing to do.")
    elif args.dry_run:
        print("Dry run — showing first 10 matches:")
        for e in todo[:10]:
            p = audio_index[e["dkey"]]
            print(f"  {e['artist']} — {e['title']}")
            print(f"    BPM={e['bpm']:.1f}  anchor={e['beat_anchor_ms']:.0f}ms")
            print(f"    audio: {p.name}")
        return

    # Load NML tree for writing
    ET.register_namespace("", "")
    tree  = ET.parse(str(nml_path))
    root  = tree.getroot()
    coll  = root.find("COLLECTION")
    entries_list = coll.findall("ENTRY")

    # Build dkey → entry element map
    entry_map: dict[str, ET.Element] = {}
    for entry in entries_list:
        artist = entry.get("ARTIST", "")
        title  = entry.get("TITLE", "")
        dk = f"{artist.lower().strip()}\t{base_title(title)}"
        entry_map[dk] = entry

    # Process in parallel
    work_args = [
        (
            e["dkey"],
            str(audio_index[e["dkey"]]),
            e["bpm"],
            e["beat_anchor_ms"],
            e["duration_ms"],
        )
        for e in todo
    ]

    slot_counts = {s: 0 for s in SLOT_TYPE}
    err_count   = 0
    saved_count = 0
    t0          = time.time()

    print(f"Processing with {args.workers} workers…")

    # Backup NML before first write
    backup = nml_path.with_suffix(".nml.cue_reset_bak")
    if not backup.exists():
        shutil.copy2(str(nml_path), str(backup))
        print(f"Backup → {backup.name}\n")

    batch_size = max(1, min(50, len(work_args)))

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(compute_all_cues, a): a[0] for a in work_args}

        for i, fut in enumerate(as_completed(futures), 1):
            dkey = futures[fut]
            try:
                dkey_out, cue_dict = fut.result()
            except Exception as exc:
                print(f"  [{i}/{len(todo)}] ERROR: {exc}")
                err_count += 1
                done.add(dkey)
                continue

            if "error" in cue_dict:
                print(f"  [{i}/{len(todo)}] SKIP ({cue_dict['error']}): {dkey[:50]}")
                done.add(dkey)
                err_count += 1
                continue

            # Apply to NML entry
            entry = entry_map.get(dkey_out)
            if entry is not None:
                entry_meta = next(e for e in todo if e["dkey"] == dkey_out)
                n = apply_cues_to_entry(entry, cue_dict, entry_meta["beat_period_ms"])
                for slot in cue_dict:
                    if isinstance(slot, int):
                        slot_counts[slot] = slot_counts.get(slot, 0) + 1

                slots_str = " ".join(str(s+1) for s in sorted(
                    k for k in cue_dict if isinstance(k, int)))
                print(f"  [{i}/{len(todo)}] slots [{slots_str}] {dkey_out[:55]}")
            else:
                print(f"  [{i}/{len(todo)}] WARN: no NML entry for {dkey_out[:55]}")

            done.add(dkey_out)
            saved_count += 1

            # Save NML + progress every batch_size tracks
            if i % batch_size == 0:
                tree.write(str(nml_path), encoding="utf-8", xml_declaration=True)
                save_progress(done)
                elapsed = time.time() - t0
                rate    = i / elapsed
                remain  = (len(todo) - i) / rate if rate > 0 else 0
                print(f"  → saved ({i}/{len(todo)}, ~{remain/60:.0f}min left)\n")

    # Final save
    tree.write(str(nml_path), encoding="utf-8", xml_declaration=True)
    save_progress(done)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed/60:.1f}min")
    print(f"  Processed: {saved_count}")
    print(f"  Errors:    {err_count}")
    print(f"  Cue slots written:")
    for slot, count in sorted(slot_counts.items()):
        print(f"    Slot {slot+1} ({SLOT_NAME[slot]}): {count:,}")
    print(f"\nNML written: {nml_path}")


if __name__ == "__main__":
    main()
