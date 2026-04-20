#!/usr/bin/env python3
"""
Stage 9 — STT Lyrics Extractor

For every track in lyrics_raw.json with null lyrics:
  1. Resolve file path via Traktor NML
  2. Separate vocals with spleeter
  3. Transcribe with faster-whisper large-v3
  4. Store result in lyrics_raw.json

Usage:
  python3 stage9_stt.py --report          # show gap stats
  python3 stage9_stt.py --run             # process all gaps
  python3 stage9_stt.py --run --limit 20  # test on first 20
"""

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE       = Path(__file__).parent
STATE_DIR  = BASE / "state"
LYRICS_RAW = STATE_DIR / "lyrics_raw.json"

TRAKTOR_NML = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"

STT_WORKERS    = 2
SAVE_EVERY     = 10
WHISPER_MODEL  = "large-v3"
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE = "float16"

# ── NML → filepath map ────────────────────────────────────────────────────────

_VERSION_RE   = __import__("re").compile(r'\s*[\(\[].{0,40}[\)\]]\s*$')
_INSTRUMENTAL = __import__("re").compile(r'\b(instrumental|inst\.?|no[ -]?vocals?)\b', __import__("re").I)


def base_title(title: str) -> str:
    return _VERSION_RE.sub("", title).strip().lower()


def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"


def build_filepath_map(nml_path: Path) -> dict[str, str]:
    """Return {dkey: abs_filepath} for every entry in the NML."""
    if not nml_path.exists():
        print(f"  NML not found at {nml_path}")
        return {}

    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    result: dict[str, str] = {}

    for entry in coll.findall("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None:
            continue
        filepath = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        artist = entry.get("ARTIST", "").strip()
        title  = entry.get("TITLE",  "").strip()
        if not artist and not title:
            continue
        dkey = dedup_key(artist, title)
        result[dkey] = filepath

    return result


# ── Vocal separation ──────────────────────────────────────────────────────────

def separate_vocals(filepath: str, tmp_dir: str) -> str | None:
    """
    Run spleeter on filepath, outputting into tmp_dir.
    Returns path to vocals.wav, or None on failure.
    """
    cmd = [
        "spleeter", "separate",
        "-p", "spleeter:2stems",
        "-o", tmp_dir,
        filepath,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    stem_name = Path(filepath).stem
    vocals_path = Path(tmp_dir) / stem_name / "vocals.wav"
    return str(vocals_path) if vocals_path.exists() else None


# ── Whisper transcription ─────────────────────────────────────────────────────

_whisper_model = None
_whisper_lock  = threading.Lock()


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
        )
    return _whisper_model


def transcribe(vocals_path: str) -> str | None:
    """Transcribe vocals.wav. Returns full text or None on failure."""
    try:
        model = get_whisper_model()
        segments, _ = model.transcribe(
            vocals_path,
            beam_size=5,
            vad_filter=True,
        )
        text = "\n".join(seg.text.strip() for seg in segments if seg.text.strip())
        return text if text else None
    except Exception:
        return None


# ── Core processing ───────────────────────────────────────────────────────────

def process_track(dkey: str, filepath: str) -> tuple[str, str | None]:
    """
    Separate + transcribe one track.
    Returns (dkey, lyrics_text_or_None).
    """
    tmp_dir = tempfile.mkdtemp(prefix="stt_")
    try:
        vocals_path = separate_vocals(filepath, tmp_dir)
        if not vocals_path:
            return dkey, None
        lyrics = transcribe(vocals_path)
        return dkey, lyrics
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Run phase ─────────────────────────────────────────────────────────────────

def run_stt(filepath_map: dict[str, str], limit: int = 0) -> None:
    STATE_DIR.mkdir(exist_ok=True)

    raw: dict = {}
    if LYRICS_RAW.exists():
        try:
            raw = json.loads(LYRICS_RAW.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}

    todo = [
        dkey for dkey, val in raw.items()
        if val is None and dkey in filepath_map
    ]

    if limit:
        todo = todo[:limit]

    total_gaps  = sum(1 for v in raw.values() if v is None)
    in_filepath = sum(1 for dkey in (k for k, v in raw.items() if v is None)
                      if dkey in filepath_map)

    print(f"STT Lyrics Extraction — faster-whisper {WHISPER_MODEL}")
    print(f"  Total null entries:    {total_gaps:,}")
    print(f"  Have file path:        {in_filepath:,}")
    print(f"  To process:            {len(todo):,}")
    if not todo:
        print("  Nothing to do.")
        return
    print(f"  Workers:               {STT_WORKERS}")
    print()

    newly_filled = 0
    done_count   = 0
    lock         = threading.Lock()
    start        = time.time()

    def do_one(dkey: str):
        nonlocal newly_filled, done_count

        artist_part, title_part = dkey.split("\t", 1)
        label = f"{artist_part.title()} — {title_part.title()}"
        filepath = filepath_map[dkey]

        result_dkey, lyrics = process_track(dkey, filepath)

        with lock:
            if lyrics:
                raw[result_dkey] = lyrics
                newly_filled += 1
                word_count = len(lyrics.split())
                print(f">> {label} | [transcribed {word_count} words]")
            else:
                print(f">> {label} | [no transcription]")

            done_count += 1
            if done_count % SAVE_EVERY == 0 or done_count == len(todo):
                LYRICS_RAW.write_text(
                    json.dumps(raw, ensure_ascii=False), encoding="utf-8"
                )
                elapsed = time.time() - start
                rate    = done_count / max(elapsed, 0.1)
                remain  = (len(todo) - done_count) / rate / 60 if rate else 0
                print(f"  [{done_count}/{len(todo)}] saved — ~{remain:.0f} min remaining")

    with concurrent.futures.ThreadPoolExecutor(max_workers=STT_WORKERS) as ex:
        list(ex.map(do_one, todo))

    LYRICS_RAW.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    elapsed = time.time() - start
    print(f"\nSTT complete in {elapsed/60:.1f} min")
    print(f"  Newly filled: {newly_filled:,}")
    print(f"  Still null:   {len(todo) - newly_filled:,}")


# ── Report ────────────────────────────────────────────────────────────────────

def run_report(filepath_map: dict[str, str]) -> None:
    if not LYRICS_RAW.exists():
        print("No lyrics_raw.json found.")
        return

    raw = json.loads(LYRICS_RAW.read_text(encoding="utf-8"))

    total       = len(raw)
    with_lyrics = sum(1 for v in raw.values() if v is not None)
    null_total  = sum(1 for v in raw.values() if v is None)
    actionable  = sum(1 for dkey, v in raw.items() if v is None and dkey in filepath_map)
    no_path     = null_total - actionable

    print(f"STT report")
    print(f"  Total tracks cached:   {total:,}")
    print(f"  Have lyrics:           {with_lyrics:,} ({with_lyrics/max(total,1)*100:.0f}%)")
    print(f"  Null (no lyrics):      {null_total:,}")
    print(f"    Have file path:      {actionable:,}  (STT-eligible)")
    print(f"    No file path in NML: {no_path:,}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Stage 9 — STT lyrics via faster-whisper")
    parser.add_argument("--run",    action="store_true", help="Run transcription on all null-lyrics tracks")
    parser.add_argument("--report", action="store_true", help="Show gap statistics")
    parser.add_argument("--limit",  type=int, default=0, help="Process at most N tracks (for testing)")
    args = parser.parse_args()

    if not any([args.run, args.report]):
        parser.print_help()
        sys.exit(0)

    print("Loading NML filepath map…")
    filepath_map = build_filepath_map(TRAKTOR_NML)
    print(f"  {len(filepath_map):,} tracks in NML\n")

    if args.report:
        run_report(filepath_map)
        return

    if args.run:
        run_stt(filepath_map, limit=args.limit)


if __name__ == "__main__":
    main()
