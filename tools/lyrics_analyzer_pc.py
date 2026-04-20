#!/usr/bin/env python3
"""
PC-side lyrics analyzer. Runs on the powerful analysis machine.

Reads:  state/tracklist.json    — full collection (artist + title)
Reads:  state/lyrics_dedup.json — existing cache (skip already done)
Writes: state/lyrics_dedup.json — new summaries appended
Writes: state/lyrics_raw.json   — raw fetched lyrics (local cache, not committed)

Then commits lyrics_dedup.json and pushes to GitHub.
The Mac pulls and the server hot-reloads.

Usage:
    python3 tools/lyrics_analyzer_pc.py
    python3 tools/lyrics_analyzer_pc.py --model qwen2.5:15b
    python3 tools/lyrics_analyzer_pc.py --workers 5 --limit 200
    python3 tools/lyrics_analyzer_pc.py --report          # stats only, no processing
    python3 tools/lyrics_analyzer_pc.py --skip-fetch      # summarize only (lyrics already fetched)
    python3 tools/lyrics_analyzer_pc.py --skip-summarize  # fetch only
    python3 tools/lyrics_analyzer_pc.py --push            # git commit + push when done

Requirements:
    pip install requests  (or use stdlib urllib — no external deps required)
    Ollama running at localhost:11434 with a suitable model installed.
    Recommended: qwen2.5:15b  (install: ollama pull qwen2.5:15b)
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen, Request

# ── Paths ───────────────────────────────────────────────────────────────────
BASE         = Path(__file__).parent.parent
TRACKLIST    = BASE / "state" / "tracklist.json"
LYRICS_RAW   = BASE / "state" / "lyrics_raw.json"
LYRICS_DEDUP = BASE / "state" / "lyrics_dedup.json"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_MODEL   = "qwen2.5:15b"
FETCH_WORKERS   = 3      # concurrent lyrics.ovh requests
SUMMARIZE_WORKERS = 4    # concurrent Ollama calls (adjust to GPU VRAM)
FETCH_SLEEP     = 0.35   # seconds between lyrics.ovh requests (per thread)
OLLAMA_TIMEOUT  = 120    # seconds per Ollama call

FLAG_DESCRIPTIONS = {
    "racism":          "Promotes or glorifies racism or racial hatred",
    "antisemitism":    "Antisemitic content",
    "homophobia":      "Homophobic or anti-LGBT content",
    "transphobia":     "Transphobic content",
    "misogyny":        "Promotes or glorifies violence against women or misogyny",
    "sexual_violence": "Glorifies or trivializes sexual violence",
    "child_abuse":     "References or glorifies abuse of children",
    "extreme_violence":"Explicit glorification of real-world murder or torture",
}

THEMES = [
    "loss",        # grief, heartbreak, mourning
    "isolation",   # loneliness, alienation, disconnection
    "love",        # longing, desire, romance, devotion
    "anger",       # rage, defiance, frustration
    "darkness",    # occultism, horror, the void, dread
    "death",       # mortality, decay, suicide, the afterlife
    "identity",    # self, transformation, existential questioning
    "euphoria",    # ecstasy, transcendence, dancing, release
    "spirituality",# religion, mysticism, faith, ritual
    "rebellion",   # anti-authority, punk ethos, resistance
    "alienation",  # feeling inhuman, outcast, estranged from society
    "nostalgia",   # memory, the past, longing for what was
    "power",       # control, domination, submission, strength
    "surreal",     # abstract, dreamlike, imagery-driven, no clear narrative
]

SYSTEM_PROMPT = f"""You are a music content analyst for a DJ who plays goth, darkwave, industrial, and post-punk.

Given song lyrics, produce three things:

1. summary — ONE concise sentence describing what the song is actually about lyrically.

2. theme — ONE word from this list that best captures the dominant emotional/thematic territory:
   {", ".join(THEMES)}

3. flags — list any content that conflicts with progressive community values. Valid flags:
   {", ".join(FLAG_DESCRIPTIONS.keys())}

IMPORTANT — do NOT flag:
- Dark themes (death, decay, depression, occultism, vampires, horror, BDSM, fetish, nihilism)
- Dark emotions (despair, isolation, obsession, rage)
These are normal goth content and should NOT be flagged.
ONLY flag genuinely bigoted, hateful content or content that promotes real-world harm to marginalized groups.

Respond with valid JSON only. Examples:
{{"summary": "A meditation on mortality and the decay of the body.", "theme": "death", "flags": []}}
{{"summary": "Desperate longing for a lost lover who will never return.", "theme": "loss", "flags": []}}
{{"summary": "Glorifies violence against a specific ethnic group using slurs.", "theme": "anger", "flags": ["racism", "extreme_violence"]}}
"""


# ── Utilities ─────────────────────────────────────────────────────────────────
def base_title(title: str) -> str:
    return re.sub(r'\s*[\(\[].{0,40}[\)\]]\s*$', "", title).strip().lower()


def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"


# ── Lyrics fetch ──────────────────────────────────────────────────────────────
def fetch_lyrics_ovh(artist: str, title: str) -> str | None:
    url = f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}"
    try:
        with urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("lyrics", "").strip() or None
    except (HTTPError, URLError, json.JSONDecodeError):
        return None


# ── Ollama summarize ──────────────────────────────────────────────────────────
def summarize_with_ollama(model: str, artist: str, title: str, lyrics: str) -> dict | None:
    payload = {
        "model":  model,
        "system": SYSTEM_PROMPT,
        "prompt": f'Song: "{title}" by {artist}\n\nLyrics:\n{lyrics[:3000]}',
        "stream": False,
        "options": {"temperature": 0.2},
    }
    body = json.dumps(payload).encode()
    req  = Request("http://localhost:11434/api/generate",
                   data=body, method="POST",
                   headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            resp   = json.loads(r.read())
            raw    = resp.get("response", "").strip()
            # Strip markdown code fences if present
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)
            # Validate flags and theme
            parsed["flags"] = [f for f in parsed.get("flags", []) if f in FLAG_DESCRIPTIONS]
            parsed["theme"] = parsed.get("theme", "").lower().strip()
            if parsed["theme"] not in THEMES:
                parsed["theme"] = ""
            return parsed
    except Exception as e:
        print(f"    [ollama error] {artist} — {title}: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PC-side lyrics analyzer")
    parser.add_argument("--model",           default=DEFAULT_MODEL,     help="Ollama model name")
    parser.add_argument("--workers",         type=int, default=SUMMARIZE_WORKERS)
    parser.add_argument("--fetch-workers",   type=int, default=FETCH_WORKERS)
    parser.add_argument("--limit",           type=int, default=0,       help="Max tracks to process (0=all)")
    parser.add_argument("--skip-fetch",      action="store_true",       help="Skip lyrics fetch phase")
    parser.add_argument("--skip-summarize",  action="store_true",       help="Skip summarization phase")
    parser.add_argument("--report",          action="store_true",       help="Show stats and exit")
    parser.add_argument("--retag-themes",   action="store_true",       help="Add theme tags to existing summaries that lack them")
    parser.add_argument("--push",            action="store_true",       help="Git commit + push when done")
    args = parser.parse_args()

    # Load tracklist
    if not TRACKLIST.exists():
        print(f"ERROR: {TRACKLIST} not found. Run tools/export_tracklist.py on the Mac first.")
        sys.exit(1)
    tracklist = json.loads(TRACKLIST.read_text())
    print(f"Tracklist: {len(tracklist):,} unique tracks")

    # Load caches
    raw   = json.loads(LYRICS_RAW.read_text())   if LYRICS_RAW.exists()   else {}
    dedup = json.loads(LYRICS_DEDUP.read_text()) if LYRICS_DEDUP.exists() else {}

    # Report mode
    if args.report:
        with_lyrics    = sum(1 for v in raw.values() if v)
        with_summaries = len(dedup)
        need_fetch     = sum(1 for t in tracklist if t["dkey"] not in raw or not raw[t["dkey"]])
        need_summary   = sum(1 for t in tracklist if raw.get(t["dkey"]) and t["dkey"] not in dedup)
        flagged        = sum(1 for v in dedup.values() if v.get("flags"))
        print(f"Raw lyrics cached:    {with_lyrics:,}")
        print(f"Summaries in dedup:   {with_summaries:,}")
        print(f"Tracks needing fetch: {need_fetch:,}")
        print(f"Tracks needing summ:  {need_summary:,}")
        print(f"Flagged:              {flagged:,}")
        return

    # ── Phase 1: Fetch lyrics ──────────────────────────────────────────────
    if not args.skip_fetch:
        to_fetch = [t for t in tracklist if not raw.get(t["dkey"])]
        if args.limit:
            to_fetch = to_fetch[:args.limit]
        print(f"\nPhase 1: Fetching lyrics for {len(to_fetch):,} tracks "
              f"({args.fetch_workers} workers)…")

        raw_lock    = threading.Lock()
        raw_done    = 0
        raw_found   = 0
        start_time  = time.time()

        def fetch_one(track):
            nonlocal raw_done, raw_found
            lyrics = fetch_lyrics_ovh(track["artist"], track["title"])
            with raw_lock:
                raw[track["dkey"]] = lyrics or ""
                raw_done += 1
                if lyrics:
                    raw_found += 1
                if raw_done % 50 == 0:
                    elapsed  = time.time() - start_time
                    rate     = raw_done / elapsed if elapsed else 0
                    remaining = (len(to_fetch) - raw_done) / rate if rate else 0
                    LYRICS_RAW.write_text(json.dumps(raw, ensure_ascii=False))
                    print(f"  [{raw_done}/{len(to_fetch)}] "
                          f"{raw_found} found — "
                          f"{remaining/60:.0f}m remaining")
            time.sleep(FETCH_SLEEP)
            return track

        with ThreadPoolExecutor(max_workers=args.fetch_workers) as ex:
            list(ex.map(fetch_one, to_fetch))

        LYRICS_RAW.write_text(json.dumps(raw, ensure_ascii=False))
        print(f"Fetch complete: {raw_found:,}/{len(to_fetch):,} found")

    # ── Phase 2: Summarize ─────────────────────────────────────────────────
    if not args.skip_summarize:
        to_summarize = [
            t for t in tracklist
            if raw.get(t["dkey"]) and t["dkey"] not in dedup
        ]
        if args.limit:
            to_summarize = to_summarize[:args.limit]
        print(f"\nPhase 2: Summarizing {len(to_summarize):,} tracks "
              f"with {args.model} ({args.workers} workers)…")

        dedup_lock   = threading.Lock()
        summ_done    = 0
        summ_flagged = 0
        start_time   = time.time()

        def summarize_one(track):
            nonlocal summ_done, summ_flagged
            result = summarize_with_ollama(
                args.model, track["artist"], track["title"], raw[track["dkey"]]
            )
            if not result:
                return
            with dedup_lock:
                dedup[track["dkey"]] = {
                    "summary": result["summary"],
                    "theme":   result.get("theme", ""),
                    "flags":   result.get("flags", []),
                }
                summ_done += 1
                if result.get("flags"):
                    summ_flagged += 1
                if summ_done % 25 == 0:
                    elapsed   = time.time() - start_time
                    rate      = summ_done / elapsed if elapsed else 0
                    remaining = (len(to_summarize) - summ_done) / rate if rate else 0
                    LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
                    print(f"  [{summ_done}/{len(to_summarize)}] "
                          f"{summ_flagged} flagged — "
                          f"{remaining/3600:.1f}h remaining")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(summarize_one, to_summarize))

        LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
        print(f"Summarize complete: {summ_done:,} done, {summ_flagged} flagged")

    # ── Phase 2b: Retag themes on existing summaries ──────────────────────
    if args.retag_themes:
        to_retag = [dkey for dkey, v in dedup.items() if v.get("summary") and not v.get("theme")]
        print(f"\nPhase 2b: Tagging themes for {len(to_retag):,} existing summaries "
              f"({args.workers} workers)…")

        THEME_PROMPT = (
            "You are tagging songs with an emotional theme for a DJ.\n"
            f"Choose exactly ONE word from this list: {', '.join(THEMES)}\n"
            "Respond with valid JSON only. Example: {{\"theme\": \"loss\"}}"
        )

        retag_lock = threading.Lock()
        retag_done = 0
        start_time = time.time()

        def retag_one(dkey):
            nonlocal retag_done
            summary = dedup[dkey]["summary"]
            payload = {
                "model":  args.model,
                "system": THEME_PROMPT,
                "prompt": f'Song summary: "{summary}"',
                "stream": False,
                "options": {"temperature": 0.1},
            }
            body = json.dumps(payload).encode()
            req  = Request("http://localhost:11434/api/generate",
                           data=body, method="POST",
                           headers={"Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read())
                    raw  = resp.get("response", "").strip()
                    raw  = re.sub(r'^```(?:json)?\s*', '', raw)
                    raw  = re.sub(r'\s*```$', '', raw)
                    parsed = json.loads(raw)
                    theme  = parsed.get("theme", "").lower().strip()
                    if theme not in THEMES:
                        theme = ""
                    with retag_lock:
                        dedup[dkey]["theme"] = theme
                        retag_done += 1
                        if retag_done % 100 == 0:
                            elapsed   = time.time() - start_time
                            rate      = retag_done / elapsed if elapsed else 0
                            remaining = (len(to_retag) - retag_done) / rate if rate else 0
                            LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
                            print(f"  [{retag_done}/{len(to_retag)}] — "
                                  f"{remaining/60:.0f}m remaining")
            except Exception as e:
                pass

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(retag_one, to_retag))

        LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
        tagged = sum(1 for v in dedup.values() if v.get("theme"))
        print(f"Retag complete: {retag_done:,} tagged ({tagged:,} total with themes)")

    # ── Phase 3: Git commit + push ─────────────────────────────────────────
    if args.push:
        print("\nCommitting results to git…")
        try:
            subprocess.run(["git", "-C", str(BASE), "add",
                            "state/lyrics_dedup.json"], check=True)
            msg = f"lyrics: add {len(dedup):,} summaries from PC analysis"
            subprocess.run(["git", "-C", str(BASE), "commit", "-m", msg], check=True)
            subprocess.run(["git", "-C", str(BASE), "push"], check=True)
            print("Pushed. Pull on Mac and reload server.")
        except subprocess.CalledProcessError as e:
            print(f"Git error: {e}")
            print("Commit manually: git add state/lyrics_dedup.json && git commit && git push")


if __name__ == "__main__":
    main()
