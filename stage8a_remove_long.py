#!/usr/bin/env python3
"""
Stage 8a — Remove Long Recordings (> 12 minutes)

Files longer than 12 minutes are live show recordings, DJ mixes, or full
album compilations — not individual tracks. Traktor cannot analyze them
properly (missing transients). They are not keepers.

For files in corrected_music/: removes ENTRY from NML and deletes the file.
For files at original ~/Music/ paths: removes ENTRY from NML only (not our files).

Dry-run by default.

Reads:  corrected_traktor/collection.nml, corrected_traktor/*.nml
Writes: state/long_file_report.json
        corrected_traktor/collection.nml  (modified in-place, with --apply)
        corrected_traktor/*.nml           (modified in-place, with --apply)

Usage:
    python3 stage8a_remove_long.py              # dry-run report
    python3 stage8a_remove_long.py --apply      # remove from NML + delete corrected_music files
    python3 stage8a_remove_long.py --minutes 15 # override threshold (default 12)
"""

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs, primarykey_to_abs

STATE_DIR    = Path(__file__).parent / "state"
TRAKTOR_DIR  = Path(__file__).parent / "corrected_traktor"
NML_SOURCE   = TRAKTOR_DIR / "collection.nml"
CORRECTED    = Path(__file__).parent / "corrected_music"
REPORT_JSON  = STATE_DIR / "long_file_report.json"

ET.register_namespace("", "")


def fix_xml_declaration(path: Path):
    content = path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(content)


def entry_abs_path(entry: ET.Element) -> str | None:
    loc = entry.find("LOCATION")
    if loc is None:
        return None
    return traktor_to_abs(loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", ""))


def collect_long_entries(nml_path: Path, threshold_sec: int) -> list[dict]:
    """Return list of dicts for entries exceeding threshold."""
    tree = ET.parse(nml_path)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        return []

    results = []
    for entry in collection.findall("ENTRY"):
        info = entry.find("INFO")
        if info is None:
            continue
        try:
            playtime = int(info.get("PLAYTIME", 0))
        except ValueError:
            continue
        if playtime <= threshold_sec:
            continue
        path = entry_abs_path(entry)
        if not path:
            continue
        in_corrected = path.startswith(str(CORRECTED))
        results.append({
            "path":         path,
            "artist":       entry.get("ARTIST", ""),
            "title":        entry.get("TITLE", ""),
            "playtime":     playtime,
            "in_corrected": in_corrected,
        })
    return results


def remove_from_nml(nml_path: Path, paths_to_remove: set[str]) -> tuple[int, int]:
    """
    Remove COLLECTION ENTRYs and drop PRIMARYKEY references for the given paths.
    Returns (entries_removed, keys_dropped).
    """
    try:
        tree = ET.parse(nml_path)
    except ET.ParseError as e:
        print(f"  [WARN] Could not parse {nml_path.name}: {e}")
        return 0, 0

    root = tree.getroot()
    entries_removed = 0

    collection = root.find("COLLECTION")
    if collection is not None:
        to_remove = [e for e in collection.findall("ENTRY")
                     if entry_abs_path(e) in paths_to_remove]
        for e in to_remove:
            collection.remove(e)
        entries_removed = len(to_remove)
        if hasattr(collection, 'set'):
            collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))

    keys_dropped = 0
    playlists = root.find("PLAYLISTS")
    if playlists is not None:
        for node in playlists.iter("ENTRY"):
            pk_el = node.find("PRIMARYKEY")
            if pk_el is None:
                continue
            path = primarykey_to_abs(pk_el.get("KEY", ""))
            if path in paths_to_remove:
                # Remove the playlist entry node itself
                parent = None
                for p in playlists.iter():
                    for child in list(p):
                        if child is node:
                            parent = p
                            break
                if parent is not None:
                    parent.remove(node)
                keys_dropped += 1

    tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(nml_path)
    return entries_removed, keys_dropped


def main():
    parser = argparse.ArgumentParser(description="Remove long recordings from library")
    parser.add_argument("--apply", action="store_true",
                        help="Remove entries from NML and delete corrected_music files")
    parser.add_argument("--minutes", type=int, default=12,
                        help="Duration threshold in minutes (default: 12)")
    args = parser.parse_args()

    threshold = args.minutes * 60

    if not NML_SOURCE.exists():
        print(f"collection.nml not found at {NML_SOURCE}")
        return

    print(f"Stage 8a: Scanning for tracks longer than {args.minutes} minutes...")
    long_entries = collect_long_entries(NML_SOURCE, threshold)
    long_entries.sort(key=lambda x: -x["playtime"])

    in_corrected = [e for e in long_entries if e["in_corrected"]]
    original_only = [e for e in long_entries if not e["in_corrected"]]

    print(f"  {len(long_entries)} entries exceed {args.minutes} minutes")
    print(f"  {len(in_corrected)} in corrected_music/ (will delete file + NML entry)")
    print(f"  {len(original_only)} at original paths (NML entry only)")

    print()
    for e in long_entries[:30]:
        mins = e["playtime"] // 60; secs = e["playtime"] % 60
        tag = "FILE+NML" if e["in_corrected"] else "NML only"
        print(f"  [{tag}] {mins}:{secs:02d}  {e['artist'] or '(no artist)'} — {e['title'] or '(no title)'}")
        print(f"           {e['path']}")
    if len(long_entries) > 30:
        print(f"  ... and {len(long_entries)-30} more (see report JSON)")

    # Write report
    report = {
        "threshold_minutes":   args.minutes,
        "total_entries":       len(long_entries),
        "in_corrected_music":  len(in_corrected),
        "original_paths_only": len(original_only),
        "entries":             long_entries,
    }
    STATE_DIR.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nFull report → {REPORT_JSON}")

    if not args.apply:
        print("\nDry-run complete. Run with --apply to remove entries and delete files.")
        return

    # Build set of paths to remove
    paths_to_remove = {e["path"] for e in long_entries}

    # Update collection.nml
    print(f"\n  Updating {NML_SOURCE.name}...")
    removed, dropped = remove_from_nml(NML_SOURCE, paths_to_remove)
    print(f"    {removed} entries removed, {dropped} playlist references dropped")

    # Update playlist NMLs
    playlist_nmls = sorted(f for f in TRAKTOR_DIR.glob("*.nml")
                           if f.name != "collection.nml" and f.is_file())
    print(f"\n  Updating {len(playlist_nmls)} playlist NML files...")
    total_removed = total_dropped = 0
    for nml_path in playlist_nmls:
        r, d = remove_from_nml(nml_path, paths_to_remove)
        total_removed += r; total_dropped += d
        if r or d:
            print(f"    {nml_path.name}: {r} removed, {d} dropped")
    print(f"    Totals: {total_removed} removed, {total_dropped} dropped")

    # Delete files in corrected_music/
    print(f"\n  Deleting {len(in_corrected)} files from corrected_music/...")
    deleted = missing = errors = 0
    for e in in_corrected:
        p = e["path"]
        if not os.path.exists(p):
            missing += 1
            continue
        try:
            os.remove(p)
            deleted += 1
        except OSError as ex:
            print(f"    [ERROR] {p}: {ex}")
            errors += 1
    print(f"    Deleted: {deleted} | Already missing: {missing} | Errors: {errors}")

    # Clean up empty dirs
    for dirpath, dirnames, filenames in os.walk(str(CORRECTED), topdown=False):
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    report["applied"] = True
    report["files_deleted"] = deleted
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nStage 8a complete. {len(long_entries)} long recordings removed from library.")


if __name__ == "__main__":
    main()
