#!/usr/bin/env python3
"""
fix_80s_prefixes.py
Fix "80's-" prefix naming issues in corrected_music and Traktor NML.

Category 1: Files in correct artist folders but with "80's-" prefix in filename.
Category 2: Files in wrong "80's-X" artist folders that need to move to clean artist folders.
"""

import os
import re
import shutil
import subprocess
import sys

NML_PATH = "/Users/aaronrhodes/Documents/Native Instruments/Traktor 4.0.2/collection.nml"
MUSIC_ROOT = "/Users/aaronrhodes/development/music organize/corrected_music"
BAD_TRAKTOR_NML = "/Users/aaronrhodes/development/music organize/corrected_traktor/collection.nml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def nml_dir_to_path(nml_dir: str) -> str:
    """Convert NML DIR format  '/:Users/:aaronrhodes/...' to a filesystem path."""
    # Each path component is prefixed with ':' — strip them all
    # Format: /: separated → join without the colons
    # e.g. '/:Users/:aaronrhodes/:foo/:bar/:'  → '/Users/aaronrhodes/foo/bar/'
    parts = nml_dir.split("/:")
    # First element after split will be '' (before the leading '/:')
    return "/" + "/".join(p for p in parts if p) + "/"


def path_to_nml_dir(fs_path: str) -> str:
    """Convert a filesystem directory path to NML DIR format."""
    # Ensure trailing slash
    if not fs_path.endswith("/"):
        fs_path += "/"
    parts = fs_path.strip("/").split("/")
    return "/:" + "/:" .join(parts) + "/:"


def clean_filename_cat1(filename: str) -> str:
    """
    Strip the '80's-' prefix from a Category 1 filename.

    Rules (in order):
    1. 'NN - 80's-ArtistName-Rest.ext'  → 'NN - Rest.ext'
    2. '80's-ArtistName-Rest.ext'       → 'Rest.ext'
       (where ArtistName contains no '-' that separates it from title)
    3. '80's-Stacey Q - I Love You.ext' → 'I Love You.ext'
       (artist uses ' - ' separator)
    4. Fallback '80's-Whatever.ext'     → 'Whatever.ext'
    """
    # Pattern 1: track-number prefix  "NN - 80's-Artist-Title.ext"
    m = re.match(r'^(\d+\s*-\s*)80\'s-[^-]+-(.+)$', filename)
    if m:
        return m.group(1) + m.group(2)

    # Pattern 3: '80's-Artist Name - Title.ext'  (space-dash-space separator)
    m = re.match(r"^80's-[^-]+\s+-\s+(.+)$", filename)
    if m:
        return m.group(1)

    # Pattern 2: '80's-Artist-Title.ext'  (hyphen separator, no spaces around it)
    m = re.match(r"^80's-[^-]+-(.+)$", filename)
    if m:
        return m.group(1)

    # Fallback: strip just the '80's-' prefix
    if filename.startswith("80's-"):
        return filename[len("80's-"):]

    return filename  # shouldn't reach here


def read_nml(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_nml(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def backup_nml(path: str) -> str:
    bak = path + ".bak_80s_fix"
    shutil.copy2(path, bak)
    print(f"  [BACKUP] {bak}")
    return bak


def xmllint_check(path: str) -> bool:
    result = subprocess.run(
        ["xmllint", "--noout", path],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  [XMLLINT] OK — NML is valid XML")
        return True
    else:
        print(f"  [XMLLINT] WARNING — XML errors:\n{result.stderr}")
        return False


# ---------------------------------------------------------------------------
# Category 1 operations
# ---------------------------------------------------------------------------

# Each entry: (current_disk_path, expected_nml_file_attr)
# We derive everything from the MUSIC_ROOT scan + NML grep.
# Build the list dynamically from the NML for robustness.

def find_cat1_entries(nml_text: str) -> list[dict]:
    """
    Find all LOCATION elements where:
      - DIR does NOT contain '/:80\'s-'  (already in correct artist folder)
      - FILE starts with "80's-"
    Returns list of dicts with keys: line_idx, old_file, new_file, dir_path, disk_path
    """
    entries = []
    lines = nml_text.splitlines()
    for i, line in enumerate(lines):
        # Look for LOCATION lines with FILE starting with 80's-
        m = re.search(r'<LOCATION\s+DIR="([^"]+)"\s+FILE="(80\'s-[^"]+)"', line)
        if m:
            nml_dir = m.group(1)
            old_file = m.group(2)
            # Skip if dir itself contains '80's-' (that's category 2)
            if "/:80's-" in nml_dir:
                continue
            new_file = clean_filename_cat1(old_file)
            disk_dir = nml_dir_to_path(nml_dir)
            disk_old = os.path.join(disk_dir.rstrip("/"), old_file)
            disk_new = os.path.join(disk_dir.rstrip("/"), new_file)
            entries.append({
                "line_idx": i,
                "nml_dir": nml_dir,
                "old_file": old_file,
                "new_file": new_file,
                "disk_old": disk_old,
                "disk_new": disk_new,
            })
    return entries


# ---------------------------------------------------------------------------
# Category 2 operations
# ---------------------------------------------------------------------------

def find_cat2_entries(nml_text: str) -> list[dict]:
    """
    Find all LOCATION elements where DIR contains '/:80's-X/:'.
    The physical file is in the wrong folder.
    We need to:
      - Move file from 80's-X/album/ to X/album/
      - Determine new filename (strip NN - 80's-X- prefix if present, else keep as-is)
      - Update NML DIR and FILE

    There are TWO kinds of NML entries for cat2 artists:
      A) Old entries: DIR still has '/:80's-X/:'  → need full DIR + FILE update
      B) New entries: DIR already has clean '/:X/:'  FILE has "NN - 80's-X-Title.mp3"
         → only need FILE update (DIR is correct)

    So we handle:
      A) DIR contains '/:80's-':  move file, update DIR + FILE
      B) DIR is clean but FILE contains '80's-': only update FILE (file rename only)
    """
    entries = []
    lines = nml_text.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r'<LOCATION\s+DIR="([^"]+)"\s+FILE="([^"]+)"', line)
        if not m:
            continue
        nml_dir = m.group(1)
        file_attr = m.group(2)

        # Type A: DIR has bad 80's- folder
        if "/:80's-" in nml_dir:
            # Extract the bad artist name  e.g. '80's-Henrik' → 'Henrik'
            bad_artist_match = re.search(r"/:80's-([^/]+)/:", nml_dir)
            if not bad_artist_match:
                continue
            bad_artist = "80's-" + bad_artist_match.group(1)
            clean_artist = bad_artist_match.group(1)

            clean_nml_dir = nml_dir.replace(f"/:80's-{clean_artist}/:", f"/:{clean_artist}/:")
            disk_old_dir = nml_dir_to_path(nml_dir)
            disk_new_dir = nml_dir_to_path(clean_nml_dir)
            disk_old = os.path.join(disk_old_dir.rstrip("/"), file_attr)
            # filename stays the same (these files don't have 80's- prefix in the name)
            disk_new = os.path.join(disk_new_dir.rstrip("/"), file_attr)
            entries.append({
                "type": "A",
                "line_idx": i,
                "old_nml_dir": nml_dir,
                "new_nml_dir": clean_nml_dir,
                "old_file": file_attr,
                "new_file": file_attr,  # filename unchanged for type A
                "disk_old": disk_old,
                "disk_new": disk_new,
            })

        # Type B: DIR is clean but FILE has 80's- prefix
        elif re.match(r"^\d+\s*-\s*80's-", file_attr) or file_attr.startswith("80's-"):
            new_file = clean_filename_cat1(file_attr)
            disk_dir = nml_dir_to_path(nml_dir)
            disk_old = os.path.join(disk_dir.rstrip("/"), file_attr)
            disk_new = os.path.join(disk_dir.rstrip("/"), new_file)
            entries.append({
                "type": "B",
                "line_idx": i,
                "old_nml_dir": nml_dir,
                "new_nml_dir": nml_dir,  # dir unchanged
                "old_file": file_attr,
                "new_file": new_file,
                "disk_old": disk_old,
                "disk_new": disk_new,
            })

    return entries


# ---------------------------------------------------------------------------
# NML patching
# ---------------------------------------------------------------------------

def patch_nml_line(line: str, new_file: str, new_dir: str = None) -> str:
    """Replace FILE (and optionally DIR) attributes in a LOCATION line."""
    # Replace FILE attribute
    line = re.sub(r'(FILE=")[^"]+"', lambda m: f'{m.group(1)}{new_file}"', line)
    # Replace DIR attribute if provided
    if new_dir:
        line = re.sub(r'(DIR=")[^"]+"', lambda m: f'{m.group(1)}{new_dir}"', line)
    return line


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False):
    print("=" * 70)
    print("fix_80s_prefixes.py — 80's prefix naming fixer")
    print(f"  NML: {NML_PATH}")
    print(f"  Music root: {MUSIC_ROOT}")
    print(f"  DRY RUN: {dry_run}")
    print("=" * 70)

    # Safety: never touch the git-managed corrected_traktor NML
    assert os.path.abspath(NML_PATH) != os.path.abspath(BAD_TRAKTOR_NML), \
        "ERROR: NML path points to corrected_traktor/collection.nml — aborting!"

    nml_text = read_nml(NML_PATH)
    lines = nml_text.splitlines(keepends=True)

    # --- CATEGORY 1 ---
    print("\n--- CATEGORY 1: Files in correct folders, bad filename prefix ---\n")
    cat1 = find_cat1_entries(nml_text)
    if not cat1:
        print("  (none found)")
    for e in cat1:
        disk_exists = os.path.exists(e["disk_old"])
        print(f"  DISK  : {e['disk_old']}")
        print(f"  →       {e['disk_new']}")
        print(f"  NML   : FILE  {e['old_file']!r} → {e['new_file']!r}")
        print(f"  EXISTS: {disk_exists}")
        if e["old_file"] == e["new_file"]:
            print("  WARNING: old == new, skipping")
        print()

    # --- CATEGORY 2 ---
    print("\n--- CATEGORY 2: Files in wrong 80's-X folders ---\n")
    cat2 = find_cat2_entries(nml_text)
    if not cat2:
        print("  (none found)")
    for e in cat2:
        disk_exists = os.path.exists(e["disk_old"])
        print(f"  TYPE  : {e['type']}")
        print(f"  DISK  : {e['disk_old']}")
        print(f"  →       {e['disk_new']}")
        if e["type"] == "A":
            print(f"  NML   : DIR   {e['old_nml_dir']!r}")
            print(f"  →             {e['new_nml_dir']!r}")
        print(f"  NML   : FILE  {e['old_file']!r} → {e['new_file']!r}")
        print(f"  EXISTS: {disk_exists}")
        if e["disk_old"] == e["disk_new"]:
            print("  (no disk change needed)")
        print()

    if dry_run:
        print("\nDRY RUN complete — no changes made.")
        return

    # --- EXECUTE ---
    print("\n" + "=" * 70)
    print("EXECUTING changes...")
    print("=" * 70)

    # Back up NML first
    backup_nml(NML_PATH)

    # Work on lines list for NML patching
    lines = nml_text.splitlines(keepends=True)

    # Apply Category 1
    print("\n[CAT 1] Renaming files + patching NML FILE attributes...")
    for e in cat1:
        if e["old_file"] == e["new_file"]:
            print(f"  SKIP (no change): {e['old_file']}")
            continue
        # Disk rename
        if os.path.exists(e["disk_old"]):
            if not os.path.exists(e["disk_new"]):
                os.rename(e["disk_old"], e["disk_new"])
                print(f"  RENAMED: {os.path.basename(e['disk_old'])} → {os.path.basename(e['disk_new'])}")
            else:
                print(f"  SKIP DISK (target exists): {e['disk_new']}")
        else:
            print(f"  WARN: source not found on disk: {e['disk_old']}")
        # NML patch
        idx = e["line_idx"]
        lines[idx] = patch_nml_line(lines[idx], e["new_file"])
        print(f"  NML[{idx}]: FILE patched to {e['new_file']!r}")

    # Apply Category 2
    print("\n[CAT 2] Moving files + patching NML DIR/FILE attributes...")
    moved_type_a_dirs = set()

    for e in cat2:
        # Disk operation
        if e["disk_old"] != e["disk_new"]:
            if os.path.exists(e["disk_old"]):
                new_dir = os.path.dirname(e["disk_new"])
                os.makedirs(new_dir, exist_ok=True)
                if not os.path.exists(e["disk_new"]):
                    shutil.move(e["disk_old"], e["disk_new"])
                    print(f"  MOVED : {e['disk_old']}")
                    print(f"       → {e['disk_new']}")
                    if e["type"] == "A":
                        moved_type_a_dirs.add(os.path.dirname(e["disk_old"]))
                else:
                    print(f"  SKIP DISK (target exists): {e['disk_new']}")
            else:
                print(f"  WARN: source not found on disk: {e['disk_old']}")
        else:
            print(f"  NO DISK MOVE needed for: {e['old_file']}")

        # NML patch
        idx = e["line_idx"]
        new_dir_attr = e["new_nml_dir"] if e["type"] == "A" else None
        lines[idx] = patch_nml_line(lines[idx], e["new_file"], new_dir_attr)
        action = f"DIR+FILE" if e["type"] == "A" else "FILE"
        print(f"  NML[{idx}]: {action} patched — FILE={e['new_file']!r}")

    # Remove now-empty 80's-X directories (type A only)
    print("\n[CAT 2] Cleaning up empty 80's- directories...")
    # Collect parent dirs of moved files (the 80's-X/album dirs)
    # Then try to remove album dir, then artist dir
    artist_dirs_to_try = set()
    for e in cat2:
        if e["type"] == "A" and e["disk_old"] != e["disk_new"]:
            album_dir = os.path.dirname(e["disk_old"])
            artist_dir = os.path.dirname(album_dir)
            artist_dirs_to_try.add(artist_dir)
            # Try album dir first
            if os.path.isdir(album_dir):
                remaining = os.listdir(album_dir)
                if not remaining:
                    os.rmdir(album_dir)
                    print(f"  RMDIR: {album_dir}")
                else:
                    print(f"  SKIP RMDIR (not empty, {len(remaining)} items): {album_dir}")

    for artist_dir in sorted(artist_dirs_to_try):
        if os.path.isdir(artist_dir):
            remaining = os.listdir(artist_dir)
            if not remaining:
                os.rmdir(artist_dir)
                print(f"  RMDIR: {artist_dir}")
            else:
                print(f"  SKIP RMDIR (not empty, {len(remaining)} items): {artist_dir}")

    # Write NML
    print("\n[NML] Writing updated NML...")
    new_nml_text = "".join(lines)
    write_nml(NML_PATH, new_nml_text)
    print(f"  Written: {NML_PATH}")

    # Validate
    print("\n[NML] Validating XML...")
    xmllint_check(NML_PATH)

    print("\nDone.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    if not dry and "--execute" not in sys.argv:
        # Default: dry run first, then ask
        print("Running dry-run first (pass --execute to apply, or --dry-run to keep dry)...\n")
        main(dry_run=True)
        print("\n" + "=" * 70)
        ans = input("Apply changes? [y/N] ").strip().lower()
        if ans == "y":
            main(dry_run=False)
        else:
            print("Aborted — no changes made.")
    else:
        main(dry_run=dry)
