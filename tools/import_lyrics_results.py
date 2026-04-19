#!/usr/bin/env python3
"""
Import analysis results from the PC back into the Mac's lyrics index.
Run this after receiving lyrics_batch_results.json from the analysis PC.

Usage:
    python3 tools/import_lyrics_results.py
    # Reads:   state/lyrics_batch_results.json   (from the PC)
    # Updates: state/lyrics_index.json  +  state/lyrics_dedup.json
    # Then notifies the running server to reload.
"""

import json
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET, sys, re

BASE         = Path(__file__).parent.parent
RESULTS_FILE = BASE / "state" / "lyrics_batch_results.json"
LYRICS_INDEX = BASE / "state" / "lyrics_index.json"
LYRICS_DEDUP = BASE / "state" / "lyrics_dedup.json"
TRAKTOR_NML  = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"

sys.path.insert(0, str(BASE))
from lib.nml_parser import traktor_to_abs

def base_title(title: str) -> str:
    return re.sub(r'\s*[\(\[].{0,40}[\)\]]\s*$', "", title).strip().lower()

def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"

def main():
    if not RESULTS_FILE.exists():
        print(f"Results file not found: {RESULTS_FILE}")
        print("Copy lyrics_batch_results.json from the analysis PC into state/")
        return

    results = json.loads(RESULTS_FILE.read_text())
    print(f"Loaded {len(results):,} results from PC")

    # Load existing caches
    index = json.loads(LYRICS_INDEX.read_text()) if LYRICS_INDEX.exists() else {}
    dedup = json.loads(LYRICS_DEDUP.read_text()) if LYRICS_DEDUP.exists() else {}

    # Load NML to build path → (artist, title) lookup
    print("Building track path index from NML…")
    tree = ET.parse(TRAKTOR_NML)
    coll = tree.getroot().find("COLLECTION")
    path_to_dkey = {}
    for e in coll.findall("ENTRY"):
        loc = e.find("LOCATION")
        if loc is None: continue
        path = traktor_to_abs(loc.get("VOLUME",""), loc.get("DIR",""), loc.get("FILE",""))
        artist = e.get("ARTIST","").strip()
        title  = e.get("TITLE","").strip()
        path_to_dkey[path] = dedup_key(artist, title)

    # Merge results into dedup cache
    merged   = 0
    flagged  = 0
    for dkey, entry in results.items():
        if not entry.get("summary"):
            continue
        dedup[dkey] = {"summary": entry["summary"], "flags": entry.get("flags", [])}
        if entry.get("flags"):
            flagged += 1
        merged += 1

    # Propagate to index by matching paths to dkeys
    indexed = 0
    for path, dkey in path_to_dkey.items():
        if path not in index and dkey in dedup:
            index[path] = dedup[dkey]
            indexed += 1

    LYRICS_DEDUP.write_text(json.dumps(dedup, ensure_ascii=False))
    LYRICS_INDEX.write_text(json.dumps(index, ensure_ascii=False))

    print(f"Merged {merged:,} summaries into dedup cache ({flagged} flagged)")
    print(f"Indexed {indexed:,} new track paths")

    # Hot-reload server
    try:
        req = Request("http://localhost:7334/api/reload-lyrics", method="POST")
        with urlopen(req, timeout=3) as r:
            d = json.loads(r.read())
        print(f"Server reloaded: {d['loaded']:,} entries, {d['flagged']} flagged")
    except Exception:
        print("(Server not running — reload manually after restart)")

if __name__ == "__main__":
    main()
