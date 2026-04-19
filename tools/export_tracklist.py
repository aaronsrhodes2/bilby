#!/usr/bin/env python3
"""
Export the full unique track list (artist + title) from the Traktor NML
into state/tracklist.json so the PC can fetch lyrics and process them
without needing local access to the music files or the NML.

Usage:
    python3 tools/export_tracklist.py

Output: state/tracklist.json
Format: [{"dkey": "artist\\ttitle_base", "artist": "...", "title": "..."}, ...]
"""

import json, re, sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE     = Path(__file__).parent.parent
OUT_FILE = BASE / "state" / "tracklist.json"
TRAKTOR_NML = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"

sys.path.insert(0, str(BASE))
from lib.nml_parser import traktor_to_abs


def base_title(title: str) -> str:
    return re.sub(r'\s*[\(\[].{0,40}[\)\]]\s*$', "", title).strip().lower()


def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"


def main():
    if not TRAKTOR_NML.exists():
        print(f"NML not found: {TRAKTOR_NML}")
        sys.exit(1)

    print("Parsing NML…")
    tree = ET.parse(TRAKTOR_NML)
    coll = tree.getroot().find("COLLECTION")

    seen   = set()
    tracks = []

    for e in coll.findall("ENTRY"):
        artist = e.get("ARTIST", "").strip()
        title  = e.get("TITLE",  "").strip()
        if not title:
            continue
        dkey = dedup_key(artist, title)
        if dkey in seen:
            continue
        seen.add(dkey)
        tracks.append({"dkey": dkey, "artist": artist, "title": title})

    OUT_FILE.write_text(json.dumps(tracks, ensure_ascii=False, indent=2))
    print(f"Exported {len(tracks):,} unique tracks → {OUT_FILE}")


if __name__ == "__main__":
    main()
