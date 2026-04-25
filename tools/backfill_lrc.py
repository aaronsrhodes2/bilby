#!/usr/bin/env python3
"""
tools/backfill_lrc.py — Batch-fetch syncedLyrics from LRCLIB for all tracks
in the library that don't yet have LRC data in state/lyrics_lrc.json.

Usage:
    python3 tools/backfill_lrc.py [--limit N] [--force]

Options:
    --limit N   Only process N tracks (default: all)
    --force     Re-fetch even if an entry already exists in lyrics_lrc.json

Resume-safe: tracks already in lyrics_lrc.json (including null entries) are
skipped unless --force is given.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from stage9_lyrics import fetch_lyrics_lrclib, LYRICS_RAW, LYRICS_LRC


def main():
    ap = argparse.ArgumentParser(description="Backfill syncedLyrics LRC from LRCLIB")
    ap.add_argument("--limit", type=int, default=0, help="Max tracks to process (0 = all)")
    ap.add_argument("--force", action="store_true", help="Re-fetch already-seen entries")
    args = ap.parse_args()

    if not LYRICS_RAW.exists():
        print("state/lyrics_raw.json not found — run stage9_lyrics.py --fetch first.")
        sys.exit(1)

    raw: dict = json.loads(LYRICS_RAW.read_text(encoding="utf-8"))
    lrc_cache: dict = json.loads(LYRICS_LRC.read_text(encoding="utf-8")) if LYRICS_LRC.exists() else {}

    # Build deduplicated list of (artist, title) pairs from lyrics_raw keys
    # lyrics_raw uses "path" as key for per-file storage, but we need artist+title.
    # Keys are file paths like "Artist/Album/track.mp3" — not directly useful.
    # Better: parse keys that look like "artist\ttitle" (the LRC cache key format).
    # Since lyrics_raw uses paths, we derive artist/title pairs from the NML instead.
    try:
        import xml.etree.ElementTree as ET
        nml_path = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
        if not nml_path.exists():
            # fallback: try project corrected_traktor/
            nml_path = BASE / "corrected_traktor" / "collection.nml"
        tree = ET.parse(nml_path)
        coll = tree.getroot().find("COLLECTION")
        seen: set[str] = set()
        pairs: list[tuple[str, str]] = []
        for e in coll.findall("ENTRY"):
            artist = e.get("ARTIST", "").strip()
            title  = e.get("TITLE",  "").strip()
            if not artist or not title:
                continue
            dk = f"{artist.lower().strip()}\t{title.lower().strip()}"
            if dk in seen:
                continue
            seen.add(dk)
            if not args.force and dk in lrc_cache:
                continue  # already fetched (even if null)
            pairs.append((artist, title))
    except Exception as exc:
        print(f"Failed to read NML: {exc}")
        sys.exit(1)

    total = len(pairs)
    if args.limit:
        pairs = pairs[: args.limit]

    print(f"Backfill LRC — LRCLIB.net")
    print(f"  Library size:        {len(seen):,} unique tracks")
    print(f"  Already in cache:    {len(lrc_cache):,}")
    print(f"  Tracks to query:     {total:,}  (processing: {len(pairs):,})")
    if not pairs:
        print("  Nothing to do.")
        return

    found = not_found = done = 0
    start = time.time()

    for artist, title in pairs:
        dk = f"{artist.lower().strip()}\t{title.lower().strip()}"
        _, _, lrc = fetch_lyrics_lrclib(artist, title)
        lrc_cache[dk] = lrc  # store even if None (marks as queried)
        if lrc:
            found += 1
        else:
            not_found += 1
        done += 1
        # Rate-limit: LRCLIB is free but polite usage matters
        time.sleep(0.25)

        if done % 50 == 0 or done == len(pairs):
            LYRICS_LRC.write_text(json.dumps(lrc_cache, ensure_ascii=False), encoding="utf-8")
            elapsed = time.time() - start
            rate    = done / max(elapsed, 0.1)
            remain  = (len(pairs) - done) / rate / 60 if rate else 0
            print(f"  {done:,}/{len(pairs):,} — synced {found:,}, "
                  f"no LRC {not_found:,} — ~{remain:.0f} min left")

    LYRICS_LRC.write_text(json.dumps(lrc_cache, ensure_ascii=False), encoding="utf-8")
    elapsed = time.time() - start
    print(f"\nBackfill complete in {elapsed/60:.1f} min")
    print(f"  Synced LRC found: {found:,}")
    print(f"  No LRC available: {not_found:,}")
    print(f"  Total in cache:   {len(lrc_cache):,}")
    print(f"\nRestart Mac Bilby to load the new LRC data.")


if __name__ == "__main__":
    main()
