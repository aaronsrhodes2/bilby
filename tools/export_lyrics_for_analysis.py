#!/usr/bin/env python3
"""
Export lyrics that need summarization into a portable JSON batch file.
Run this on the Mac, then send the output file to the analysis PC.

Usage:
    python3 tools/export_lyrics_for_analysis.py
    # Writes:  state/lyrics_batch_export.json
    # Send that file to the PC, run lyrics_analyzer.py there,
    # bring back lyrics_batch_results.json, then run import_lyrics_results.py here.
"""

import json, re
from pathlib import Path

BASE        = Path(__file__).parent.parent
LYRICS_RAW  = BASE / "state" / "lyrics_raw.json"
LYRICS_DEDUP= BASE / "state" / "lyrics_dedup.json"
OUT_FILE    = BASE / "state" / "lyrics_batch_export.json"

def base_title(title: str) -> str:
    return re.sub(r'\s*[\(\[].{0,40}[\)\]]\s*$', "", title).strip().lower()

def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"

def main():
    if not LYRICS_RAW.exists():
        print("No lyrics_raw.json — run stage9_lyrics.py --fetch first.")
        return

    raw   = json.loads(LYRICS_RAW.read_text())
    dedup = json.loads(LYRICS_DEDUP.read_text()) if LYRICS_DEDUP.exists() else {}

    # Build export: only tracks with lyrics not yet summarized
    batch = []
    seen_dkeys = set()

    for path, lyrics in raw.items():
        if not lyrics:
            continue
        # Extract artist/title from path filename as fallback
        fname   = Path(path).stem
        # Try to parse "Artist - Title" from filename
        parts   = fname.split(" - ", 1)
        artist  = parts[0].strip() if len(parts) == 2 else ""
        title   = parts[1].strip() if len(parts) == 2 else fname.strip()

        dkey = dedup_key(artist, title)
        if dkey in dedup:
            continue   # already summarized
        if dkey in seen_dkeys:
            continue   # duplicate in this export
        seen_dkeys.add(dkey)

        batch.append({
            "dkey":   dkey,
            "artist": artist,
            "title":  title,
            "lyrics": lyrics[:3000],   # truncate to keep file size reasonable
        })

    OUT_FILE.write_text(json.dumps(batch, ensure_ascii=False, indent=2))
    print(f"Exported {len(batch):,} unique tracks needing analysis")
    print(f"Output: {OUT_FILE}")
    print()
    print("Next steps:")
    print(f"  1. Copy state/lyrics_batch_export.json to the analysis PC")
    print(f"  2. Run lyrics_analyzer.py on the PC")
    print(f"  3. Copy lyrics_batch_results.json back here")
    print(f"  4. Run: python3 tools/import_lyrics_results.py")

if __name__ == "__main__":
    main()
