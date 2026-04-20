#!/usr/bin/env python3
"""
stage9_stt_mac.py — STT lyrics extraction for Apple Silicon (mlx-whisper)

Transcribes the ~4.4k tracks in corrected_traktor/collection.nml that have
no entry in lyrics_dedup.json, using mlx-whisper large-v3-turbo (no spleeter,
no CUDA — runs on Apple Silicon Neural Engine).

After transcription, sends to Claude Haiku for a one-sentence summary + flags
and stores in lyrics_dedup.json (the authoritative summary store).

Usage:
  python3 stage9_stt_mac.py --report          # show gap stats
  python3 stage9_stt_mac.py --run             # full pass (resumable)
  python3 stage9_stt_mac.py --run --limit 20  # test 20 tracks
  python3 stage9_stt_mac.py --run --transcribe-only  # skip Claude summary
"""

from __future__ import annotations
import argparse, json, os, re, sys, time, threading
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE          = Path(__file__).parent
STATE         = BASE / "state"
RAW_JSON      = STATE / "lyrics_raw.json"
DEDUP_JSON    = STATE / "lyrics_dedup.json"
PROGRESS_JSON = STATE / "stt_mac_progress.json"
NML_PATH      = BASE / "corrected_traktor" / "collection.nml"
CORRECTED     = (BASE / "corrected_music").resolve()

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
SAVE_EVERY    = 10

# ── Helpers ────────────────────────────────────────────────────────────────────
_VER = re.compile(r'\s*[\(\[].{0,40}[\)\]]\s*$')
_INSTR = re.compile(r'\b(instrumental|inst\.?|no[ -]?vocals?)\b', re.I)

def dkey(artist: str, title: str) -> str:
    t = _VER.sub("", title or "").strip().lower()
    return f"{(artist or '').lower().strip()}\t{t}"

def load_json(p: Path, default):
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default

def save_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

_save_lock = threading.Lock()

# ── Build eligible track list ──────────────────────────────────────────────────
def build_eligible() -> list[dict]:
    """Return list of {artist, title, path, dkey} needing STT."""
    dedup   = load_json(DEDUP_JSON, {})
    raw     = load_json(RAW_JSON, {})
    nml     = ET.parse(NML_PATH)
    coll    = nml.getroot().find("COLLECTION")

    eligible = []
    for e in coll.findall("ENTRY"):
        k = dkey(e.get("ARTIST",""), e.get("TITLE",""))
        if k in dedup:
            continue  # already summarised
        loc = e.find("LOCATION")
        if loc is None:
            continue
        p = traktor_to_abs(loc.get("VOLUME",""), loc.get("DIR",""), loc.get("FILE",""))
        if not Path(p).exists() or not str(p).startswith(str(CORRECTED)):
            continue
        # Already has raw lyrics from web scrape — just needs summary
        if p in raw and raw[p] is not None:
            eligible.append({"artist": e.get("ARTIST",""), "title": e.get("TITLE",""),
                              "path": p, "dkey": k, "has_raw": True})
        elif p not in raw or raw[p] is None:
            if _INSTR.search(e.get("TITLE","") + " " + e.get("ARTIST","")):
                continue  # skip obvious instrumentals
            eligible.append({"artist": e.get("ARTIST",""), "title": e.get("TITLE",""),
                              "path": p, "dkey": k, "has_raw": False})
    return eligible

# ── Transcription ──────────────────────────────────────────────────────────────
_whisper = None
def get_whisper():
    global _whisper
    if _whisper is None:
        print(f"  Loading mlx-whisper model {WHISPER_MODEL}...")
        import mlx_whisper as _mw
        _whisper = _mw
    return _whisper

def transcribe(path: str) -> str | None:
    """Transcribe audio file, return text or None if silent/failed."""
    try:
        mw = get_whisper()
        result = mw.transcribe(
            path,
            path_or_hf_repo=WHISPER_MODEL,
            language="en",          # most tracks are English
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
        )
        text = result.get("text","").strip()
        # Filter out Whisper hallucinations on silent/instrumental tracks
        if len(text) < 20:
            return None
        # Common hallucination patterns
        if re.match(r'^(Thank you\.?\s*)+$', text, re.I):
            return None
        return text
    except Exception as ex:
        print(f"    [transcribe error] {ex}")
        return None

# ── Claude summary ─────────────────────────────────────────────────────────────
_anthropic = None
def get_anthropic():
    global _anthropic
    if _anthropic is None:
        import anthropic
        _anthropic = anthropic.Anthropic()
    return _anthropic

SYSTEM_PROMPT = """\
You analyse song lyrics for a DJ. For each set of lyrics respond with JSON only:
{
  "summary": "<one sentence — what the song is ABOUT lyrically, 10-20 words>",
  "theme": "<single word or short phrase: e.g. love, grief, defiance, paranoia>",
  "flags": ["racism"|"bigotry"|"sexual_violence"|"child_abuse"|"extreme_violence"]
}
flags is an empty list unless the lyrics contain that specific content.
Dark themes (death, occultism, horror, BDSM, depression) are NOT flagged.
If the text has too little content to summarise, return {"summary":null,"theme":null,"flags":[]}.
Return ONLY the JSON object, no markdown."""

def summarise(artist: str, title: str, lyrics: str) -> dict | None:
    try:
        client = get_anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=120,
            system=SYSTEM_PROMPT,
            messages=[{"role":"user","content":
                f"Artist: {artist}\nTitle: {title}\n\nLyrics:\n{lyrics[:3000]}"}]
        )
        raw = msg.content[0].text.strip()
        return json.loads(raw)
    except Exception as ex:
        print(f"    [claude error] {ex}")
        return None

# ── Report ─────────────────────────────────────────────────────────────────────
def report():
    eligible = build_eligible()
    progress = load_json(PROGRESS_JSON, [])
    done_set = set(progress)
    remaining = [t for t in eligible if t["path"] not in done_set]
    has_raw  = sum(1 for t in remaining if t["has_raw"])
    needs_stt = sum(1 for t in remaining if not t["has_raw"])
    dedup = load_json(DEDUP_JSON, {})
    print(f"lyrics_dedup.json entries: {len(dedup):,}")
    print(f"Eligible tracks:           {len(eligible):,}")
    print(f"  Already done:            {len(done_set):,}")
    print(f"  Remaining:               {len(remaining):,}")
    print(f"    Has raw lyrics:        {has_raw:,}  (just need Claude summary)")
    print(f"    Needs STT:             {needs_stt:,}")
    if needs_stt:
        avg_sec = 210  # ~3.5 min average
        rate    = 3.0  # ~3x real-time on Apple Silicon
        est_hrs = (needs_stt * avg_sec / rate) / 3600
        print(f"\n  Estimated runtime: ~{est_hrs:.0f}h (Apple Silicon, 3x real-time)")

# ── Main run ───────────────────────────────────────────────────────────────────
def run(limit: int = 0, transcribe_only: bool = False):
    eligible = build_eligible()
    progress = set(load_json(PROGRESS_JSON, []))
    raw      = load_json(RAW_JSON, {})
    dedup    = load_json(DEDUP_JSON, {})

    todo = [t for t in eligible if t["path"] not in progress]
    if limit:
        todo = todo[:limit]

    print(f"Tracks to process: {len(todo):,}  (of {len(eligible):,} eligible)")
    if not todo:
        print("Nothing to do.")
        return

    done_this_run = 0
    for i, track in enumerate(todo):
        artist, title, path, dk = track["artist"], track["title"], track["path"], track["dkey"]
        print(f"[{i+1}/{len(todo)}] {artist} — {title}")

        # Step 1: get lyrics text
        if track["has_raw"]:
            lyrics = raw[path]
            print(f"  Using cached raw lyrics ({len(lyrics)} chars)")
        else:
            t0 = time.time()
            lyrics = transcribe(path)
            elapsed = time.time() - t0
            if lyrics:
                print(f"  Transcribed in {elapsed:.1f}s — {len(lyrics)} chars")
                raw[path] = lyrics
            else:
                print(f"  No speech detected ({elapsed:.1f}s) — marking instrumental")
                raw[path] = None
                dedup[dk] = {"summary": "Instrumental — no vocals detected.",
                              "theme": "instrumental", "flags": []}
                progress.add(path)
                done_this_run += 1
                if done_this_run % SAVE_EVERY == 0:
                    with _save_lock:
                        save_json(RAW_JSON, raw)
                        save_json(PROGRESS_JSON, sorted(progress))
                continue

        # Step 2: Claude summary
        if not transcribe_only:
            summary = summarise(artist, title, lyrics)
            if summary and summary.get("summary"):
                dedup[dk] = summary
                print(f"  Summary: {summary['summary']}")
            else:
                dedup[dk] = {"summary": None, "theme": None, "flags": []}
                print(f"  No summary generated")
        else:
            print(f"  (transcribe-only — skipping Claude)")

        progress.add(path)
        done_this_run += 1

        if done_this_run % SAVE_EVERY == 0:
            with _save_lock:
                save_json(RAW_JSON, raw)
                save_json(DEDUP_JSON, dedup)
                save_json(PROGRESS_JSON, sorted(progress))
                print(f"  [saved progress: {done_this_run} done this run]")

    # Final save
    with _save_lock:
        save_json(RAW_JSON, raw)
        save_json(DEDUP_JSON, dedup)
        save_json(PROGRESS_JSON, sorted(progress))
    print(f"\nDone. {done_this_run} tracks processed this run.")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mac STT lyrics pass (mlx-whisper)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--run",    action="store_true")
    ap.add_argument("--limit",  type=int, default=0)
    ap.add_argument("--transcribe-only", action="store_true",
                    help="Skip Claude summary step (transcribe text only)")
    args = ap.parse_args()

    if args.report:
        report()
    elif args.run:
        run(limit=args.limit, transcribe_only=args.transcribe_only)
    else:
        ap.print_help()
