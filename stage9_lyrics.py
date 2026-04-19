#!/usr/bin/env python3
"""
Stage 9 — Lyrics Indexer

For every track in the library:
  1. Fetch lyrics from lyrics.ovh (free, no key)
  2. Send to Claude Haiku for a one-sentence summary + content flag check
  3. Cache everything to state/lyrics_index.json

One-sentence summary tells the DJ what the song is ABOUT lyrically.
Content flags warn about material that conflicts with goth community values
(racism, bigotry, glorified sexual violence, child abuse content).

Dark themes — death, occultism, horror, depression, BDSM — are NOT flagged.
That's just goth.

Usage:
  python3 stage9_lyrics.py --fetch       # fetch lyrics only (no LLM)
  python3 stage9_lyrics.py --summarize   # summarize already-fetched lyrics
  python3 stage9_lyrics.py --run         # fetch + summarize (full pipeline)
  python3 stage9_lyrics.py --report      # coverage stats
  python3 stage9_lyrics.py --run --limit 500   # process first 500 (test)

Requirements:
  ANTHROPIC_API_KEY in environment (for --summarize / --run)
  internet access (for lyrics.ovh)
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE          = Path(__file__).parent
STATE_DIR     = BASE / "state"
LYRICS_RAW    = STATE_DIR / "lyrics_raw.json"       # {path: lyrics_text | null}
LYRICS_INDEX  = STATE_DIR / "lyrics_index.json"     # {path: {summary, flags, error}}
LYRICS_DEDUP  = STATE_DIR / "lyrics_dedup.json"     # {"artist\ttitle_base": {summary, flags}}
ACTIVITY_FILE = STATE_DIR / "activity.json"         # live progress for server UI

SUMMARIZE_WORKERS = 3   # concurrent Ollama requests — safe for llama3.1:8b
TRAKTOR_NML   = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"

# ── Flag definitions ──────────────────────────────────────────────────────────
#
# These are the only things we flag. Everything else is just dark music.
# Occultism, death, horror, depression, anger, BDSM → NOT flagged.

FLAG_DESCRIPTIONS = {
    "racism":           "Racist or white supremacist lyrical content",
    "bigotry":          "Homophobic, transphobic, or similar hate content",
    "sexual_violence":  "Glorification or celebration of sexual assault/rape",
    "child_abuse":      "Sexualization or abuse of minors",
    "extreme_violence": "Glorification of real-world violence against specific groups",
}

# ── LLM prompt ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a lyrics analyst for a goth/industrial DJ tool.
Your job: read song lyrics and return a JSON object with exactly two fields.

Rules:
- "summary": ONE sentence describing what the song is about lyrically (its theme or meaning, not a track listing of events). Be specific and concrete. Max 20 words.
- "flags": a list containing zero or more of these exact strings, only if clearly present in the lyrics:
    "racism"          → racial slurs or white supremacist ideology
    "bigotry"         → homophobia, transphobia, explicit group hatred
    "sexual_violence" → glorification/celebration of rape or sexual assault
    "child_abuse"     → sexualization or abuse of minors
    "extreme_violence"→ glorification of real-world targeted violence

Do NOT flag: death, grief, depression, occultism, Satanism, horror imagery,
consensual BDSM, drug references, political commentary, anger, revenge fantasies,
or any other dark theme. Those are normal goth music subjects.

Respond ONLY with valid JSON. No markdown, no explanation."""

USER_PROMPT_TMPL = """Artist: {artist}
Title: {title}

Lyrics:
{lyrics}

Respond with JSON only: {{"summary": "...", "flags": []}}"""

# ── NML loader ────────────────────────────────────────────────────────────────

_VERSION_SUFFIX = re.compile(r'\s*[\(\[].{0,40}[\)\]]\s*$')

def base_title(title: str) -> str:
    """Strip version suffixes: '(Radio Edit)', '[Remaster]', etc."""
    return _VERSION_SUFFIX.sub("", title).strip().lower()

def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"

def load_all_tracks(nml_path: Path) -> list[dict]:
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    results = []
    for e in coll.findall("ENTRY"):
        loc = e.find("LOCATION")
        if loc is None:
            continue
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        artist = e.get("ARTIST", "").strip()
        title  = e.get("TITLE",  "").strip()
        if not artist and not title:
            continue
        results.append({
            "path":   path,
            "artist": artist,
            "title":  title,
            "dkey":   dedup_key(artist, title),
        })
    return results

# ── lyrics.ovh ────────────────────────────────────────────────────────────────

def fetch_lyrics(artist: str, title: str) -> str | None:
    """Fetch from lyrics.ovh. Returns text or None if not found."""
    url = f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}"
    req = Request(url, headers={"User-Agent": "dj-planner/1.0"})
    try:
        with urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        lyrics = data.get("lyrics", "").strip()
        return lyrics if lyrics else None
    except HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 429:
            time.sleep(5)
            return fetch_lyrics(artist, title)
        return None
    except (URLError, Exception):
        return None

# ── Claude Haiku summarizer ───────────────────────────────────────────────────

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.1:8b"   # fast, good general reasoning; swap to gemma3:27b for higher quality

def summarize_lyrics(artist: str, title: str, lyrics: str) -> dict:
    """
    Call local Ollama model to get one-sentence summary + content flags.
    Returns {"summary": str, "flags": list[str]} or {"error": str}.
    """
    # Truncate very long lyrics to keep context window reasonable
    lyrics_trunc = lyrics[:3000] + ("\n[truncated]" if len(lyrics) > 3000 else "")

    prompt = f"""{SYSTEM_PROMPT}

{USER_PROMPT_TMPL.format(artist=artist, title=title, lyrics=lyrics_trunc)}"""

    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0.1, "num_predict": 200},
    }).encode()

    req = Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except Exception as ex:
        return {"error": str(ex)}

    raw = resp.get("response", "").strip()
    # Strip markdown code fences if model added them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
        if "summary" not in result:
            return {"error": f"bad response: {raw[:80]}"}
        result["flags"] = [f for f in result.get("flags", []) if f in FLAG_DESCRIPTIONS]
        return result
    except json.JSONDecodeError:
        return {"error": f"bad JSON: {raw[:80]}"}

# ── Fetch phase ───────────────────────────────────────────────────────────────

def run_fetch(tracks: list[dict], limit: int = 0) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    raw: dict = {}
    if LYRICS_RAW.exists():
        try:
            raw = json.loads(LYRICS_RAW.read_text())
        except json.JSONDecodeError:
            raw = {}

    todo = [t for t in tracks if t["path"] not in raw]
    if limit:
        todo = todo[:limit]

    print(f"Lyrics fetch — lyrics.ovh")
    print(f"  Total tracks:    {len(tracks):,}")
    print(f"  Already cached:  {len(tracks) - len(todo):,}")
    print(f"  To fetch:        {len(todo):,}")
    if not todo:
        print("  Nothing to do.")
        return

    found = not_found = 0
    start = time.time()

    for i, track in enumerate(todo, 1):
        lyrics = fetch_lyrics(track["artist"], track["title"])
        raw[track["path"]] = lyrics
        if lyrics:
            found += 1
        else:
            not_found += 1

        if i % 100 == 0 or i == len(todo):
            LYRICS_RAW.write_text(json.dumps(raw, ensure_ascii=False))
            elapsed = time.time() - start
            rate    = i / max(elapsed, 0.1)
            remain  = (len(todo) - i) / rate / 60 if rate else 0
            pct     = found / i * 100
            write_activity("Lyrics indexer", "fetch", i, len(todo), start, rate)
            print(f"  {i:,}/{len(todo):,} — found {found:,} ({pct:.0f}%), "
                  f"not found {not_found:,} — ~{remain:.0f} min left")

        time.sleep(0.35)  # ~2.8 req/sec, polite

    LYRICS_RAW.write_text(json.dumps(raw, ensure_ascii=False))
    elapsed = time.time() - start
    print(f"\nFetch complete in {elapsed/60:.1f} min")
    print(f"  With lyrics: {found:,}  ({found/len(todo)*100:.0f}%)")
    print(f"  Not found:   {not_found:,}")

# ── Summarize phase ───────────────────────────────────────────────────────────

def run_summarize(tracks: list[dict], limit: int = 0) -> None:
    if not LYRICS_RAW.exists():
        print("No lyrics cache — run --fetch first.")
        return

    # Verify Ollama is reachable before starting
    try:
        with urlopen(Request("http://localhost:11434/api/tags", method="GET"), timeout=5) as r:
            pass
    except Exception:
        print(f"ERROR: Ollama not reachable at localhost:11434")
        print(f"  Start it with:  ollama serve")
        sys.exit(1)

    raw: dict = json.loads(LYRICS_RAW.read_text())

    index: dict = {}
    if LYRICS_INDEX.exists():
        try:
            index = json.loads(LYRICS_INDEX.read_text())
        except json.JSONDecodeError:
            index = {}

    # Dedup cache: keyed by "artist\tbase_title" → {summary, flags}
    # Lets us reuse summaries for remixes/edits without another API call
    dedup: dict = {}
    if LYRICS_DEDUP.exists():
        try:
            dedup = json.loads(LYRICS_DEDUP.read_text())
        except json.JSONDecodeError:
            dedup = {}

    path_map = {t["path"]: t for t in tracks}

    # Tracks that have lyrics and aren't yet summarized
    todo = [
        path_map[path] for path, lyrics in raw.items()
        if lyrics is not None
        and path not in index
        and path in path_map
    ]
    if limit:
        todo = todo[:limit]

    print(f"Lyrics summarize — Claude Haiku")
    print(f"  Tracks with lyrics:    {sum(1 for v in raw.values() if v):,}")
    print(f"  Already summarized:    {len(index):,}")
    print(f"  To process:            {len(todo):,}")
    if not todo:
        print("  Nothing to do.")
        return

    flagged = errors = dedup_hits = llm_calls = 0
    start   = time.time()
    lock    = threading.Lock()

    # Separate dedup-hits from tracks needing LLM calls
    dedup_todo = [(t, raw[t["path"]], t["dkey"]) for t in todo if t["dkey"] in dedup]
    llm_todo   = [(t, raw[t["path"]], t["dkey"]) for t in todo if t["dkey"] not in dedup]

    # Apply dedup hits instantly
    for track, _, dkey in dedup_todo:
        index[track["path"]] = dedup[dkey].copy()
        dedup_hits += 1

    print(f"  Dedup hits (no LLM needed): {dedup_hits:,}")
    print(f"  LLM calls needed:           {len(llm_todo):,}")
    print(f"  Workers:                    {SUMMARIZE_WORKERS}")

    def process_one(args):
        nonlocal flagged, errors, llm_calls
        track, lyrics, dkey = args
        result = summarize_lyrics(track["artist"], track["title"], lyrics)
        with lock:
            llm_calls += 1
            if "error" in result:
                index[track["path"]] = {"summary": None, "flags": [], "error": result["error"]}
                errors += 1
            else:
                entry = {"summary": result["summary"], "flags": result["flags"]}
                index[track["path"]] = entry
                dedup[dkey] = entry
                if result["flags"]:
                    flagged += 1
                    print(f"  ⚑ FLAGGED: {track['artist']} — {track['title']}: {result['flags']}")
            done_count = llm_calls + dedup_hits
            if llm_calls % 50 == 0 or llm_calls == len(llm_todo):
                LYRICS_INDEX.write_text(json.dumps(index, ensure_ascii=False))
                LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
                elapsed = time.time() - start
                rate    = llm_calls / max(elapsed, 1)
                remain  = (len(llm_todo) - llm_calls) / max(rate, 0.01) / 60
                write_activity("Lyrics indexer", "summarize", done_count,
                               len(todo), start, rate)
                print(f"  {done_count:,}/{len(todo):,} — "
                      f"{llm_calls} LLM calls, {dedup_hits} dedup, "
                      f"{flagged} flagged, {errors} errors — ~{remain:.0f} min left")

    with concurrent.futures.ThreadPoolExecutor(max_workers=SUMMARIZE_WORKERS) as ex:
        list(ex.map(process_one, llm_todo))

    LYRICS_INDEX.write_text(json.dumps(index, ensure_ascii=False))
    LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
    elapsed = time.time() - start
    print(f"\nSummarize complete in {elapsed/60:.1f} min")
    print(f"  LLM calls made: {llm_calls:,}")
    print(f"  Dedup reuses:   {dedup_hits:,}")
    print(f"  Flagged:        {flagged}")
    print(f"  Errors:         {errors}")

# ── Report ────────────────────────────────────────────────────────────────────

def run_report(tracks: list[dict]) -> None:
    raw   = json.loads(LYRICS_RAW.read_text())   if LYRICS_RAW.exists()   else {}
    index = json.loads(LYRICS_INDEX.read_text()) if LYRICS_INDEX.exists() else {}

    with_lyrics  = sum(1 for v in raw.values() if v)
    no_lyrics    = sum(1 for v in raw.values() if v is None)
    summarized   = sum(1 for v in index.values() if v.get("summary"))
    flagged      = [p for p, v in index.items() if v.get("flags")]
    errors       = sum(1 for v in index.values() if "error" in v)

    print(f"Lyrics index report")
    print(f"  Total tracks:       {len(tracks):,}")
    print(f"  Fetched:            {len(raw):,}")
    print(f"    With lyrics:      {with_lyrics:,} ({with_lyrics/max(len(raw),1)*100:.0f}%)")
    print(f"    Not found:        {no_lyrics:,}")
    print(f"  Summarized:         {summarized:,}")
    print(f"  Errors:             {errors:,}")
    print(f"  Content flagged:    {len(flagged):,}")
    if flagged:
        print()
        flag_counts: dict[str, int] = {}
        for path in flagged:
            for f in index[path].get("flags", []):
                flag_counts[f] = flag_counts.get(f, 0) + 1
        for flag, count in sorted(flag_counts.items(), key=lambda x: -x[1]):
            print(f"    {flag}: {count}")

# ── Entry point ───────────────────────────────────────────────────────────────

def write_activity(task: str, phase: str, done: int, total: int,
                   started_at: float, rate: float = 0.0) -> None:
    """Write progress to state/activity.json so the server UI can display it."""
    pct     = round(done / max(total, 1) * 100)
    eta_min = round((total - done) / max(rate, 0.01) / 60) if rate else None
    STATE_DIR.mkdir(exist_ok=True)
    ACTIVITY_FILE.write_text(json.dumps({
        "task":       task,
        "phase":      phase,
        "done":       done,
        "total":      total,
        "pct":        pct,
        "rate":       round(rate, 2),
        "eta_min":    eta_min,
        "started_at": started_at,
        "updated_at": time.time(),
    }, ensure_ascii=False))

def clear_activity() -> None:
    """Remove the activity file when the task completes."""
    ACTIVITY_FILE.unlink(missing_ok=True)


def notify_server_reload() -> None:
    """Tell the running DJ server to hot-reload lyrics index."""
    try:
        from urllib.request import urlopen, Request
        req = Request("http://localhost:5001/api/reload-lyrics", method="POST")
        with urlopen(req, timeout=3) as r:
            result = json.loads(r.read())
        print(f"  Server reloaded: {result['loaded']} entries, {result['flagged']} flagged")
    except Exception:
        pass  # Server might not be running — that's fine


def watch_for_new_tracks(interval_sec: int = 60) -> None:
    """
    Watch the Traktor NML for modifications. When it changes, run the full
    pipeline on any new tracks and notify the server to reload.
    Called with --watch (runs until Ctrl-C).
    """
    print(f"Watching {TRAKTOR_NML} for new tracks (every {interval_sec}s)…")
    print("Press Ctrl-C to stop.\n")
    last_mtime = TRAKTOR_NML.stat().st_mtime if TRAKTOR_NML.exists() else 0

    while True:
        try:
            time.sleep(interval_sec)
            if not TRAKTOR_NML.exists():
                continue
            mtime = TRAKTOR_NML.stat().st_mtime
            if mtime <= last_mtime:
                continue
            last_mtime = mtime
            print(f"\n[{time.strftime('%H:%M:%S')}] NML changed — checking for new tracks…")
            tracks = load_all_tracks(TRAKTOR_NML)

            # Only process tracks not yet in fetch cache
            raw = json.loads(LYRICS_RAW.read_text()) if LYRICS_RAW.exists() else {}
            new = [t for t in tracks if t["path"] not in raw]
            if not new:
                print(f"  No new tracks to process.")
                continue
            print(f"  {len(new)} new tracks found — running pipeline…")
            run_fetch(tracks)
            run_summarize(tracks)
            notify_server_reload()
        except KeyboardInterrupt:
            print("\nWatcher stopped.")
            break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 9 — Lyrics indexer",
        epilog="New tracks workflow: run --run once for the full library, then --watch to pick up additions automatically."
    )
    parser.add_argument("--fetch",     action="store_true", help="Fetch lyrics from lyrics.ovh")
    parser.add_argument("--summarize", action="store_true", help="Summarize fetched lyrics with Claude Haiku")
    parser.add_argument("--run",       action="store_true", help="Fetch + summarize (full pipeline)")
    parser.add_argument("--report",    action="store_true", help="Show coverage statistics")
    parser.add_argument("--watch",     action="store_true", help="Watch NML for new tracks, process automatically")
    parser.add_argument("--limit",     type=int, default=0, help="Process at most N tracks (for testing)")
    args = parser.parse_args()

    if not any([args.fetch, args.summarize, args.run, args.report, args.watch]):
        parser.print_help()
        sys.exit(0)

    if not TRAKTOR_NML.exists():
        print(f"ERROR: NML not found at {TRAKTOR_NML}")
        sys.exit(1)

    if args.watch:
        watch_for_new_tracks()
        return

    print(f"Loading tracks from NML…")
    tracks = load_all_tracks(TRAKTOR_NML)
    print(f"  {len(tracks):,} tracks\n")

    if args.report:
        run_report(tracks)
        return

    if args.fetch or args.run:
        run_fetch(tracks, limit=args.limit)
        print()

    if args.summarize or args.run:
        run_summarize(tracks, limit=args.limit)
        print()
        clear_activity()
        notify_server_reload()

if __name__ == "__main__":
    main()
