#!/usr/bin/env python3
"""
dedup_corrected_music.py — Remove duplicate audio files from corrected_music/.

Reads the corrected NML to find all artist+title groups with multiple files.
For each group, keeps the HIGHEST bitrate version and moves the rest to
a _dedup_removed/ staging folder (NOT permanent deletion).

After running:
  1. Review _dedup_removed/ — spot-check a few tracks
  2. python3 tools/dedup_corrected_music.py --apply-nml  (remove from NML too)
  3. If happy: rm -rf corrected_music/_dedup_removed/

Usage:
  python3 tools/dedup_corrected_music.py            # dry run — shows what would move
  python3 tools/dedup_corrected_music.py --move     # move dupes to _dedup_removed/
  python3 tools/dedup_corrected_music.py --move --apply-nml  # also remove from NML

IMPORTANT: Keep Traktor closed if using --apply-nml.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

BASE       = Path(__file__).resolve().parent.parent
NML_CORR   = BASE / "corrected_traktor" / "collection.nml"
MUSIC_ROOT = BASE / "corrected_music"
REMOVED    = MUSIC_ROOT / "_dedup_removed"


def traktor_to_abs(volume: str, dir_: str, file_: str) -> Path:
    """Convert Traktor loc fields to absolute path."""
    # DIR is like /:Users/:name/:path/:  — strip leading /: and replace /:
    dir_clean = dir_.lstrip("/").replace("/:", "/").rstrip("/")
    return Path("/") / dir_clean / file_


def load_entries(nml_path: Path):
    """Yield (element, artist, title, bitrate, abs_path) for every ENTRY."""
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    for e in coll.findall("ENTRY"):
        artist = e.get("ARTIST", "").strip()
        title  = e.get("TITLE",  "").strip()
        if not artist and not title:
            continue
        info = e.find("INFO")
        loc  = e.find("LOCATION")
        if info is None or loc is None:
            continue
        try:
            bitrate = int(info.get("BITRATE", 0) or 0)
        except (ValueError, TypeError):
            bitrate = 0
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        key = f"{artist.lower().strip()}\t{title.lower().strip()}"
        yield e, key, bitrate, path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--move",      action="store_true",
                    help="Move duplicate files to _dedup_removed/ (default: dry run)")
    ap.add_argument("--apply-nml", action="store_true",
                    help="Also remove duplicate entries from corrected NML")
    args = ap.parse_args()

    print(f"Reading NML: {NML_CORR}")
    entries = list(load_entries(NML_CORR))
    print(f"  {len(entries):,} total entries")

    # Group by song key
    groups: dict[str, list[tuple]] = defaultdict(list)
    for e, key, bitrate, path in entries:
        groups[key].append((bitrate, path, e))

    # Identify dupes: all but best-bitrate per group
    to_remove: list[tuple[Path, object]] = []   # (path, element)
    for key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda x: -x[0])   # highest bitrate first
        # Keep index 0; remove the rest
        for bitrate, path, el in members[1:]:
            to_remove.append((path, el))

    print(f"  {len(to_remove):,} duplicate entries to remove")
    print(f"  {len(groups):,} unique artist+title pairs")
    missing_files = [p for p, _ in to_remove if not p.exists()]
    print(f"  {len(missing_files):,} paths already missing (will skip)")

    if not to_remove:
        print("Nothing to do.")
        return

    # Print sample
    print("\nSample removals (up to 20):")
    for path, _ in to_remove[:20]:
        exists = "✓" if path.exists() else "✗"
        print(f"  {exists}  {path.name}  ({path.parent.name})")

    if not args.move:
        print(f"\nDry run — {len(to_remove):,} files would be moved to _dedup_removed/")
        print("Run with --move to execute.")
        return

    # ── Move files ────────────────────────────────────────────────────────────
    REMOVED.mkdir(exist_ok=True)
    moved = 0
    skipped = 0
    for path, _ in to_remove:
        if not path.exists():
            skipped += 1
            continue
        # Preserve subdirectory structure inside _dedup_removed/
        try:
            rel = path.relative_to(MUSIC_ROOT)
        except ValueError:
            rel = Path(path.name)
        dest = REMOVED / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        moved += 1

    print(f"\nMoved {moved:,} files to {REMOVED}")
    print(f"Skipped {skipped:,} (already missing)")

    # ── Update NML ────────────────────────────────────────────────────────────
    if args.apply_nml:
        print(f"\nUpdating NML: {NML_CORR}")
        tree = ET.parse(NML_CORR)
        root = tree.getroot()
        coll = root.find("COLLECTION")

        remove_els = {id(el) for _, el in to_remove}
        before = len(coll.findall("ENTRY"))
        for el in [e for e in coll.findall("ENTRY") if id(e) in remove_els]:
            coll.remove(el)
        after = len(coll.findall("ENTRY"))

        backup = NML_CORR.with_suffix(".nml.bak_dedup")
        shutil.copy2(NML_CORR, backup)
        tree.write(NML_CORR, encoding="utf-8", xml_declaration=True)
        print(f"  Removed {before - after:,} entries ({before:,} → {after:,})")
        print(f"  Backup: {backup.name}")
        print("  Validate with: xmllint --noout corrected_traktor/collection.nml")

    print("\nDone. Review _dedup_removed/ before permanent deletion.")


if __name__ == "__main__":
    main()
