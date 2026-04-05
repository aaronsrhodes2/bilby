#!/usr/bin/env python3
"""
Stage 5 — Update Traktor NML Files

For each NML file (main collection + playlist NMLs):
  1. Parse ENTRY elements
  2. Decode Traktor LOCATION → absolute path
  3. Look up in path_map.json to get new corrected_music/ path
  4. For duplicate entries: keep the richest ENTRY (most cue points,
     ratings, play counts), update its LOCATION to the new path

Three-way outcome per entry:
  A) File was scanned and copied → update LOCATION to new corrected_music/ path
  B) File is in an excluded directory (~/Music/Traktor, etc.) → keep ORIGINAL path unchanged
  C) File was truly missing before this pipeline → REMOVE the entry (log to unmappable.json)

Outcome B is critical: DJ recordings and samples live in ~/Music/Traktor and are
not being moved, so their original paths remain valid and must be preserved.
Outcome C prevents "file not found" errors from pre-existing broken entries.

Reads:  state/path_map.json, state/dedup.json
        ~/Documents/Native Instruments/Traktor 4.0.2/collection.nml
        ~/Documents/*.nml
Writes: corrected_traktor/, unmappable.json
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tqdm import tqdm

from lib.nml_parser import (
    traktor_to_abs,
    abs_to_traktor_location,
    primarykey_to_abs,
    abs_to_primarykey,
)

STATE_DIR = Path(__file__).parent / "state"
PATH_MAP_JSON = STATE_DIR / "path_map.json"
# Use metadata-dedup result if available (from stage2b --apply), else base dedup
DEDUP_JSON = STATE_DIR / "dedup_final.json" if (STATE_DIR / "dedup_final.json").exists() \
             else STATE_DIR / "dedup.json"
UNMAPPABLE_JSON = Path(__file__).parent / "unmappable.json"

TRAKTOR_COLLECTION = (
    Path.home() / "Documents" / "Native Instruments" / "Traktor 4.0.2" / "collection.nml"
)
DOCS_DIR = Path.home() / "Documents"
MUSIC_ROOT = Path.home() / "Music"

DEST_ROOT = Path(__file__).parent / "corrected_traktor"

# Directories excluded from Stage 1 scan — files here are not being moved,
# so their original paths must be preserved as-is in the NML.
EXCLUDED_DIRS = {
    str(MUSIC_ROOT / "Traktor"),
    str(MUSIC_ROOT / "Spotify"),
    str(MUSIC_ROOT / "Sounds"),
}

ET.register_namespace("", "")


def fix_xml_declaration(dest_path: Path):
    """
    Replace Python ET's single-quoted declaration with Traktor's expected format.
    ET writes:  <?xml version='1.0' encoding='UTF-8'?>
    Traktor expects: <?xml version="1.0" encoding="UTF-8" standalone="no" ?>
    """
    content = dest_path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    dest_path.write_bytes(content)


def is_in_excluded_dir(abs_path: str) -> bool:
    """Return True if this path lives in one of our excluded (unscanned) directories."""
    return any(
        abs_path.startswith(d + os.sep) or abs_path.startswith(d + "/")
        for d in EXCLUDED_DIRS
    )


def entry_score(entry: ET.Element) -> int:
    """Score an ENTRY by richness of DJ metadata. Higher = keep this one."""
    score = 0
    info = entry.find("INFO")
    if info is not None:
        if int(info.get("PLAYCOUNT", 0)) > 0:
            score += 1
        if int(info.get("RANKING", 0)) > 0:
            score += 2
        if info.get("LAST_PLAYED"):
            score += 1
        try:
            if int(info.get("COLOR", "0")) != 0:
                score += 1
        except ValueError:
            pass
    for cue in entry.findall("CUE_V2"):
        if cue.get("HOTCUE", "-1") != "-1" and cue.get("NAME", "n.n.") != "n.n.":
            score += 1
    score += min(len(entry.findall("CUE_V2")), 5)
    return score


def entry_abs_path(entry: ET.Element) -> str | None:
    loc = entry.find("LOCATION")
    if loc is None:
        return None
    return traktor_to_abs(loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", ""))


def update_entry_location(entry: ET.Element, new_abs_path: str):
    loc = entry.find("LOCATION")
    if loc is None:
        loc = ET.SubElement(entry, "LOCATION")
    for k, v in abs_to_traktor_location(new_abs_path).items():
        loc.set(k, v)


def classify_entry(old_path: str | None, path_map: dict, old_to_sha: dict) -> tuple[str, str | None]:
    """
    Classify an entry as 'remap', 'keep_original', or 'drop'.

    Returns (action, new_path_or_none).
    """
    if old_path is None:
        return "drop", None

    # Case A: file was scanned and copied — remap to new path
    if old_path in path_map:
        return "remap", path_map[old_path]

    # Case B: file is in an excluded directory — keep original path intact
    if is_in_excluded_dir(old_path):
        return "keep_original", old_path

    # Case B also: check if file actually exists on disk (might be outside Music entirely)
    if os.path.exists(old_path):
        return "keep_original", old_path

    # Case C: file was already missing — drop the entry
    return "drop", None


def process_collection_nml(
    nml_path: Path,
    path_map: dict,
    old_to_sha: dict,
    dest_path: Path,
    unmappable: list,
):
    print(f"  Parsing {nml_path.name} ({nml_path.stat().st_size // 1024 // 1024} MB)...")
    try:
        tree = ET.parse(nml_path)
    except ET.ParseError as e:
        print(f"  [ERROR] Could not parse {nml_path}: {e}")
        return

    root = tree.getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        print(f"  [WARN] No COLLECTION element — copying as-is")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil; shutil.copy2(nml_path, dest_path)
        return

    entries = list(collection.findall("ENTRY"))
    print(f"    {len(entries):,} ENTRY elements found")

    # Group entries by SHA so duplicates can be merged
    sha_to_entries: dict[str, list[tuple[ET.Element, str | None]]] = {}
    ungrouped: list[tuple[ET.Element, str | None, str]] = []  # (entry, old_path, action)

    for entry in entries:
        old_path = entry_abs_path(entry)
        sha = old_to_sha.get(old_path) if old_path else None
        if sha:
            sha_to_entries.setdefault(sha, []).append((entry, old_path))
        else:
            action, new_path = classify_entry(old_path, path_map, old_to_sha)
            ungrouped.append((entry, old_path, action))

    new_entries: list[ET.Element] = []
    remapped = kept = dropped = deduped = 0

    # Process SHA-grouped entries (dedup + remap)
    for sha, entry_list in tqdm(sha_to_entries.items(), desc="  Merging", leave=False):
        # Find new path for this SHA
        new_path = None
        old_path_sample = None
        for _, old_path in entry_list:
            if old_path and old_path in path_map:
                new_path = path_map[old_path]
                old_path_sample = old_path
                break

        if new_path is None:
            # Check if any of the paths is in an excluded dir or exists on disk
            for _, old_path in entry_list:
                if old_path and (is_in_excluded_dir(old_path) or os.path.exists(old_path)):
                    new_path = old_path  # keep original
                    old_path_sample = old_path
                    break

        best_entry = max(entry_list, key=lambda x: entry_score(x[0]))[0]
        deduped += len(entry_list) - 1

        if new_path:
            update_entry_location(best_entry, new_path)
            new_entries.append(best_entry)
            remapped += 1
        else:
            # Drop — was already missing
            dropped += 1
            unmappable.append({
                "title": best_entry.get("TITLE", ""),
                "artist": best_entry.get("ARTIST", ""),
                "old_path": old_path_sample,
                "reason": "file_missing_before_pipeline",
            })

    # Process ungrouped entries
    for entry, old_path, action in ungrouped:
        if action == "remap":
            update_entry_location(entry, path_map[old_path])
            new_entries.append(entry)
            remapped += 1
        elif action == "keep_original":
            # Leave LOCATION unchanged — file is in excluded dir or exists at original path
            new_entries.append(entry)
            kept += 1
        else:  # drop
            dropped += 1
            unmappable.append({
                "title": entry.get("TITLE", ""),
                "artist": entry.get("ARTIST", ""),
                "old_path": old_path,
                "reason": "file_missing_before_pipeline",
            })

    # Replace collection entries
    for entry in entries:
        collection.remove(entry)
    for entry in new_entries:
        collection.append(entry)
    collection.set("ENTRIES", str(len(new_entries)))

    print(f"    Remapped: {remapped:,} | Kept original path: {kept:,} | "
          f"Deduped away: {deduped:,} | Dropped (pre-existing missing): {dropped:,}")
    print(f"    Output entries: {len(new_entries):,}")

    # Fix PLAYLISTS section — update PRIMARYKEY KEY attributes to new paths.
    # These are the playlist track references embedded in collection.nml itself.
    playlists = root.find("PLAYLISTS")
    pk_remapped = pk_kept = 0
    if playlists is not None:
        for node in playlists.iter("ENTRY"):
            pk_el = node.find("PRIMARYKEY")
            if pk_el is None:
                continue
            old_path = primarykey_to_abs(pk_el.get("KEY", ""))
            action, new_path = classify_entry(old_path, path_map, old_to_sha)
            if action == "remap":
                pk_el.set("KEY", abs_to_primarykey(new_path))
                pk_remapped += 1
            else:
                pk_kept += 1  # keep_original or drop — leave KEY as-is
        print(f"    Playlist PRIMARYKEY: {pk_remapped:,} remapped, {pk_kept:,} left as-is")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(dest_path), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(dest_path)
    print(f"    Written to {dest_path}")


def process_playlist_nml(
    nml_path: Path,
    path_map: dict,
    old_to_sha: dict,
    dest_path: Path,
    unmappable: list,
):
    try:
        tree = ET.parse(nml_path)
    except ET.ParseError as e:
        print(f"  [ERROR] Could not parse {nml_path}: {e}")
        return

    root = tree.getroot()
    updated = kept = dropped = 0

    # Update COLLECTION ENTRY LOCATION elements
    collection = root.find("COLLECTION")
    if collection is not None:
        entries_to_remove = []
        for entry in collection.findall("ENTRY"):
            old_path = entry_abs_path(entry)
            action, new_path = classify_entry(old_path, path_map, old_to_sha)
            if action == "remap":
                update_entry_location(entry, new_path)
                updated += 1
            elif action == "keep_original":
                kept += 1
            else:
                entries_to_remove.append(entry)
                dropped += 1
        for entry in entries_to_remove:
            collection.remove(entry)

    # Update PLAYLISTS PRIMARYKEY paths.
    # PRIMARYKEY is a child *element* of ENTRY, not an attribute:
    #   <ENTRY><PRIMARYKEY TYPE="TRACK" KEY="Macintosh HD/:Users/..."></PRIMARYKEY></ENTRY>
    playlists = root.find("PLAYLISTS")
    pk_remapped = 0
    if playlists is not None:
        for node in playlists.iter("ENTRY"):
            pk_el = node.find("PRIMARYKEY")
            if pk_el is None:
                continue
            old_path = primarykey_to_abs(pk_el.get("KEY", ""))
            action, new_path = classify_entry(old_path, path_map, old_to_sha)
            if action == "remap":
                pk_el.set("KEY", abs_to_primarykey(new_path))
                pk_remapped += 1
            # keep_original or drop: leave KEY as-is
            # (missing entries show as unresolved in Traktor — acceptable)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(dest_path), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(dest_path)
    print(f"  {nml_path.name}: {updated} remapped, {kept} kept, {dropped} dropped, "
          f"{pk_remapped} playlist keys updated → {dest_path.name}")


def main():
    if not PATH_MAP_JSON.exists():
        print("path_map.json not found — run stage4_copy.py first")
        sys.exit(1)
    if not DEDUP_JSON.exists():
        print("dedup.json not found — run stage2_dedup.py first")
        sys.exit(1)

    if DEST_ROOT.exists() and any(DEST_ROOT.iterdir()):
        print(f"corrected_traktor/ already has files. Delete {DEST_ROOT} to re-run Stage 5.")
        return

    print("Stage 5: Loading path map and dedup data...")
    path_map = json.loads(PATH_MAP_JSON.read_text())
    dedup = json.loads(DEDUP_JSON.read_text())
    old_to_sha = dedup["old_to_winner_sha"]
    print(f"  {len(path_map):,} path mappings loaded")
    print(f"  Excluded dirs (files kept at original paths): {[str(d) for d in EXCLUDED_DIRS]}")

    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    unmappable: list = []

    # Process main collection
    if TRAKTOR_COLLECTION.exists():
        process_collection_nml(
            TRAKTOR_COLLECTION,
            path_map,
            old_to_sha,
            DEST_ROOT / "collection.nml",
            unmappable,
        )
    else:
        print(f"  [WARN] Main collection not found at {TRAKTOR_COLLECTION}")

    # Process playlist NML files
    print("\n  Processing playlist NML files...")
    playlist_nmls = [f for f in DOCS_DIR.glob("*.nml") if f.is_file()]
    print(f"  Found {len(playlist_nmls)} playlist NML files")
    for nml_path in sorted(playlist_nmls):
        process_playlist_nml(nml_path, path_map, old_to_sha, DEST_ROOT / nml_path.name, unmappable)

    # Write unmappable log
    UNMAPPABLE_JSON.write_text(json.dumps(unmappable, ensure_ascii=False, indent=2))
    print(f"\n  {len(unmappable)} entries removed (were already missing before pipeline) → unmappable.json")

    print(f"\nStage 5 complete. Updated NML files in {DEST_ROOT}")
    print("\nTo test in Traktor (SAFE — does not replace your library):")
    print("  File → Import Another Collection → corrected_traktor/collection.nml")
    print("  Check that tracks load. If happy, follow switch_library.md to replace.")


if __name__ == "__main__":
    main()
