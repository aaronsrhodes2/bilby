#!/usr/bin/env python3
"""
strip_nml_lyrics.py — Remove KEY_LYRICS attributes from NML before committing.

Full lyrics text is copyrighted and should not be stored in a public git repo.
This script strips KEY_LYRICS from every INFO element in the NML, producing a
"public-safe" version suitable for committing.

The working copy (with full KEY_LYRICS) is left intact — only the committed
version is stripped. The pre-commit hook calls this with --in-place.

Usage:
  python3 tools/strip_nml_lyrics.py            # dry-run: print stats
  python3 tools/strip_nml_lyrics.py --in-place # strip and overwrite NML
  python3 tools/strip_nml_lyrics.py --nml PATH # target a specific NML

After --in-place, restore full lyrics at any time with:
  python3 tools/write_nml_lyrics.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

BASE    = Path(__file__).resolve().parent.parent
NML_CORR = BASE / "corrected_traktor" / "collection.nml"


def strip_lyrics(nml_path: Path, in_place: bool) -> int:
    """Strip KEY_LYRICS from all INFO elements. Returns count stripped."""
    if not nml_path.exists():
        print(f"[SKIP] {nml_path} not found")
        return 0

    tree = ET.parse(nml_path)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    if coll is None:
        print("[ERROR] No <COLLECTION> element")
        return 0

    stripped = 0
    for entry in coll.findall("ENTRY"):
        info = entry.find("INFO")
        if info is not None and info.get("KEY_LYRICS"):
            if in_place:
                del info.attrib["KEY_LYRICS"]
            stripped += 1

    if in_place and stripped:
        tree.write(nml_path, encoding="utf-8", xml_declaration=True)

    return stripped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in-place", action="store_true",
                    help="Strip and overwrite the NML file (default: dry-run)")
    ap.add_argument("--nml", metavar="PATH",
                    help="Target NML (default: corrected_traktor/collection.nml)")
    args = ap.parse_args()

    nml_path = Path(args.nml) if args.nml else NML_CORR
    mode = "STRIPPING" if args.in_place else "DRY RUN"
    print(f"[{mode}] {nml_path.name}")

    count = strip_lyrics(nml_path, args.in_place)

    if args.in_place:
        print(f"  Stripped KEY_LYRICS from {count:,} tracks")
        print("  Restore with: python3 tools/write_nml_lyrics.py")
    else:
        print(f"  Would strip KEY_LYRICS from {count:,} tracks (pass --in-place to write)")


if __name__ == "__main__":
    main()
