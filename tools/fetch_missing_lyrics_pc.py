#!/usr/bin/env python3
"""
PC-side missing lyrics fetcher — Stage 2 of the lyrics pipeline.

Picks up where lyrics_analyzer_pc.py left off. For tracks still lacking
summaries in lyrics_dedup.json, it tries:

  Phase 1 — Genius API        (requires GENIUS_TOKEN)
  Phase 2 — Whisper STT       (requires: pip install openai-whisper  OR  faster-whisper)

Then feeds new lyrics through Qwen2.5 for summarization (same as stage 1).

Usage:
    python3 tools/fetch_missing_lyrics_pc.py --report
    python3 tools/fetch_missing_lyrics_pc.py --genius-only --token YOUR_TOKEN
    python3 tools/fetch_missing_lyrics_pc.py --whisper-only --model large-v3
    python3 tools/fetch_missing_lyrics_pc.py --all --token YOUR_TOKEN --push

Windows path assumption: repo at D:/Aaron/development/music-collection
Audio files at:          D:/Aaron/Music/  (set --audio-root if different)

Requirements:
    pip install requests lyricsgenius openai-whisper
    Ollama running with qwen2.5:14b  (or pass --model)
    CUDA GPU for Whisper (CPU is 10-50× slower but works)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE         = Path(__file__).parent.parent
TRACKLIST    = BASE / "state" / "tracklist.json"
LYRICS_DEDUP = BASE / "state" / "lyrics_dedup.json"
LYRICS_RAW   = BASE / "state" / "lyrics_raw.json"         # from stage 1 (may not exist)
GENIUS_CACHE = BASE / "state" / "lyrics_genius_cache.json" # per-dkey genius results
WHISPER_CACHE= BASE / "state" / "lyrics_whisper_cache.json"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_MODEL    = "qwen2.5:14b"
OLLAMA_URL       = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT   = 120
SUMMARIZE_WORKERS= 4
GENIUS_SLEEP     = 0.4   # seconds between Genius requests

THEMES = [
    "loss", "isolation", "love", "anger", "darkness", "death",
    "identity", "euphoria", "spirituality", "rebellion", "alienation",
    "nostalgia", "power", "surreal",
]

FLAG_DESCRIPTIONS = {
    "racism":           "Promotes or glorifies racism or racial hatred",
    "antisemitism":     "Antisemitic content",
    "homophobia":       "Homophobic or anti-LGBT content",
    "transphobia":      "Transphobic content",
    "misogyny":         "Promotes or glorifies violence against women or misogyny",
    "sexual_violence":  "Glorifies or trivializes sexual violence",
    "child_abuse":      "References or glorifies abuse of children",
    "extreme_violence": "Explicit glorification of real-world murder or torture",
}

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
"""

# ── Utilities ──────────────────────────────────────────────────────────────────
def base_title(title: str) -> str:
    return re.sub(r'\s*[\(\[].{0,40}[\)\]]\s*$', "", title).strip().lower()

def dkey(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"

# ── Genius fetch ───────────────────────────────────────────────────────────────
def genius_fetch(artist: str, title: str, token: str) -> str | None:
    """Fetch lyrics from Genius API. Returns text or None."""
    try:
        import lyricsgenius
        genius = getattr(genius_fetch, "_client", None)
        if genius is None:
            genius = lyricsgenius.Genius(token, verbose=False, remove_section_headers=True)
            genius_fetch._client = genius
        song = genius.search_song(title, artist, get_full_info=False)
        if song and song.lyrics:
            lyrics = song.lyrics.strip()
            # Strip the "Contributor" header Genius adds
            lyrics = re.sub(r'^\d+ Contributors.*?\n', '', lyrics, flags=re.DOTALL)
            return lyrics if len(lyrics) > 50 else None
        return None
    except Exception as e:
        return None

# ── Whisper STT ───────────────────────────────────────────────────────────────
def find_audio_file(artist: str, title: str, audio_root: Path) -> Path | None:
    """Best-effort scan for an audio file matching artist+title under audio_root."""
    # Try common patterns
    patterns = [
        f"**/{artist}*/**/*{title[:20]}*",
        f"**/*{artist[:15]}*/*{title[:20]}*",
    ]
    for pat in patterns:
        try:
            matches = list(audio_root.glob(pat))
            if matches:
                return matches[0]
        except Exception:
            pass
    return None


def whisper_transcribe(audio_path: Path, whisper_model: str = "large-v3") -> str | None:
    """Transcribe audio using faster-whisper (preferred) or openai-whisper."""
    try:
        # Prefer faster-whisper (much faster on GPU)
        from faster_whisper import WhisperModel
        model = WhisperModel(whisper_model, device="cuda", compute_type="float16")
        segments, _ = model.transcribe(str(audio_path), beam_size=5)
        return " ".join(s.text for s in segments).strip() or None
    except ImportError:
        pass
    try:
        import whisper
        model = whisper.load_model(whisper_model)
        result = model.transcribe(str(audio_path))
        return result.get("text", "").strip() or None
    except ImportError:
        print("  [whisper] Neither faster-whisper nor openai-whisper installed.")
        print("  Install: pip install faster-whisper   (recommended)")
        print("       or: pip install openai-whisper")
        return None


# ── Ollama summarize ───────────────────────────────────────────────────────────
def summarize(model: str, artist: str, title: str, lyrics: str) -> dict | None:
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": f'Song: "{title}" by {artist}\n\nLyrics:\n{lyrics[:3000]}',
        "stream": False,
        "options": {"temperature": 0.2},
    }
    body = json.dumps(payload).encode()
    req  = Request(OLLAMA_URL, data=body, method="POST",
                   headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            resp = json.loads(r.read())
            raw  = resp.get("response", "").strip()
            raw  = re.sub(r'^```(?:json)?\s*', '', raw)
            raw  = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)
            parsed["flags"] = [f for f in parsed.get("flags", []) if f in FLAG_DESCRIPTIONS]
            parsed["theme"] = parsed.get("theme", "").lower().strip()
            if parsed["theme"] not in THEMES:
                parsed["theme"] = ""
            return parsed
    except Exception as e:
        print(f"  [ollama error] {artist} — {title}: {e}")
        return None


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Fetch missing lyrics (Genius + Whisper)")
    parser.add_argument("--report",       action="store_true", help="Show stats and exit")
    parser.add_argument("--genius-only",  action="store_true", help="Run Genius phase only")
    parser.add_argument("--whisper-only", action="store_true", help="Run Whisper phase only")
    parser.add_argument("--all",          action="store_true", help="Run all phases")
    parser.add_argument("--token",        default=os.environ.get("GENIUS_TOKEN",""),
                        help="Genius API token (or set GENIUS_TOKEN env var)")
    parser.add_argument("--model",        default=DEFAULT_MODEL)
    parser.add_argument("--workers",      type=int, default=SUMMARIZE_WORKERS)
    parser.add_argument("--whisper-model",default="large-v3")
    parser.add_argument("--audio-root",   default="D:/Aaron/Music",
                        help="Root directory of your audio files (for Whisper STT)")
    parser.add_argument("--limit",        type=int, default=0)
    parser.add_argument("--push",         action="store_true")
    args = parser.parse_args()

    if not (args.report or args.genius_only or args.whisper_only or args.all):
        parser.print_help()
        return

    if not TRACKLIST.exists():
        print(f"ERROR: {TRACKLIST} not found. Run export_tracklist.py first.")
        sys.exit(1)

    tracklist = json.loads(TRACKLIST.read_text())
    dedup     = json.loads(LYRICS_DEDUP.read_text()) if LYRICS_DEDUP.exists() else {}
    genius_cache  = json.loads(GENIUS_CACHE.read_text())  if GENIUS_CACHE.exists()  else {}
    whisper_cache = json.loads(WHISPER_CACHE.read_text()) if WHISPER_CACHE.exists() else {}

    missing = [t for t in tracklist if t["dkey"] not in dedup]
    if args.limit:
        missing = missing[:args.limit]

    # ── Report ──────────────────────────────────────────────────────────────
    if args.report:
        genius_hits  = sum(1 for t in missing if genius_cache.get(t["dkey"]))
        whisper_hits = sum(1 for t in missing if whisper_cache.get(t["dkey"]))
        print(f"Total tracks:      {len(tracklist):,}")
        print(f"Have summaries:    {len(dedup):,}")
        print(f"Missing:           {len(missing):,}")
        print(f"Genius cache hits: {genius_hits:,}")
        print(f"Whisper cache:     {whisper_hits:,}")
        return

    # ── Phase 1: Genius ──────────────────────────────────────────────────────
    if args.genius_only or args.all:
        if not args.token:
            print("ERROR: --token required for Genius. Get one at https://genius.com/api-clients")
            sys.exit(1)

        to_fetch = [t for t in missing if t["dkey"] not in genius_cache]
        print(f"\nPhase 1 — Genius: {len(to_fetch):,} tracks to query…")
        found = 0
        for i, t in enumerate(to_fetch, 1):
            lyrics = genius_fetch(t["artist"], t["title"], args.token)
            genius_cache[t["dkey"]] = lyrics or ""
            if lyrics:
                found += 1
            if i % 50 == 0 or i == len(to_fetch):
                GENIUS_CACHE.write_text(json.dumps(genius_cache, ensure_ascii=False))
                print(f"  [{i}/{len(to_fetch)}] {found} found so far")
            time.sleep(GENIUS_SLEEP)

        GENIUS_CACHE.write_text(json.dumps(genius_cache, ensure_ascii=False))
        print(f"Genius complete: {found:,}/{len(to_fetch):,} found")

    # ── Phase 2: Whisper ─────────────────────────────────────────────────────
    if args.whisper_only or args.all:
        audio_root = Path(args.audio_root)
        if not audio_root.exists():
            print(f"WARNING: audio-root {audio_root} not found — skipping Whisper phase")
        else:
            # Tracks still missing after Genius
            still_missing = [
                t for t in missing
                if not genius_cache.get(t["dkey"])
                and t["dkey"] not in whisper_cache
            ]
            print(f"\nPhase 2 — Whisper STT: {len(still_missing):,} tracks…")
            found = 0
            for i, t in enumerate(still_missing, 1):
                audio = find_audio_file(t["artist"], t["title"], audio_root)
                if audio is None:
                    whisper_cache[t["dkey"]] = ""
                    continue
                transcript = whisper_transcribe(audio, args.whisper_model)
                whisper_cache[t["dkey"]] = transcript or ""
                if transcript:
                    found += 1
                    print(f"  [{i}] ✓ {t['artist']} — {t['title']}")
                if i % 50 == 0:
                    WHISPER_CACHE.write_text(json.dumps(whisper_cache, ensure_ascii=False))
                    print(f"  [{i}/{len(still_missing)}] {found} transcribed")

            WHISPER_CACHE.write_text(json.dumps(whisper_cache, ensure_ascii=False))
            print(f"Whisper complete: {found:,} transcribed")

    # ── Phase 3: Summarize new lyrics ────────────────────────────────────────
    to_summarize = []
    for t in missing:
        dk = t["dkey"]
        if dk in dedup:
            continue
        lyrics = genius_cache.get(dk) or whisper_cache.get(dk)
        if lyrics:
            to_summarize.append((t, lyrics))

    if not to_summarize:
        print("\nNo new lyrics to summarize.")
    else:
        print(f"\nPhase 3 — Summarize: {len(to_summarize):,} tracks with {args.model}…")
        dedup_lock = threading.Lock()
        done = flagged = 0
        start_time = time.time()

        def summarize_one(item):
            nonlocal done, flagged
            track, lyrics = item
            result = summarize(args.model, track["artist"], track["title"], lyrics)
            if not result:
                return
            with dedup_lock:
                dedup[track["dkey"]] = {
                    "summary": result["summary"],
                    "theme":   result.get("theme", ""),
                    "flags":   result.get("flags", []),
                }
                done += 1
                if result.get("flags"):
                    flagged += 1
                if done % 25 == 0 or done == len(to_summarize):
                    elapsed   = time.time() - start_time
                    rate      = done / max(elapsed, 1)
                    remaining = (len(to_summarize) - done) / max(rate, 0.01) / 3600
                    LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
                    print(f"  [{done}/{len(to_summarize)}] {flagged} flagged — "
                          f"~{remaining:.1f}h remaining")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(summarize_one, to_summarize))

        LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
        print(f"\nSummarize complete: {done:,} done, {flagged} flagged")

    # ── Phase 4: Git push ────────────────────────────────────────────────────
    if args.push:
        print("\nCommitting to git…")
        try:
            subprocess.run(["git", "-C", str(BASE), "add", "state/lyrics_dedup.json"],
                           check=True)
            msg = (f"lyrics: add {len(dedup):,} summaries "
                   f"(Genius + Whisper + qwen2.5)")
            subprocess.run(["git", "-C", str(BASE), "commit", "-m", msg], check=True)
            subprocess.run(["git", "-C", str(BASE), "push"], check=True)
            print("Pushed. Pull on Mac and run: curl -X POST http://localhost:7334/api/reload-lyrics")
        except subprocess.CalledProcessError as e:
            print(f"Git error: {e}")


if __name__ == "__main__":
    main()
