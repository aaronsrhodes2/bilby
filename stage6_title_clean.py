#!/usr/bin/env python3
"""
Stage 6 — Title Cleaning & File/NML Congruency

Fixes tracks where the title contains the artist name, genre prefix,
file extension, or download artifacts. Applies changes to:
  1. File tags in corrected_music/
  2. Filenames in corrected_music/
  3. state/path_map.json (updates remapped paths)
  4. corrected_traktor/collection.nml (LOCATION FILE + PRIMARYKEY KEY)
  5. corrected_traktor/*.nml playlist files (PRIMARYKEY KEY)

Dry-run by default. Use --apply to execute.

Reads:  state/metadata.json, state/path_map.json
        corrected_traktor/collection.nml + playlist NMLs
Writes: corrected_music/ (renames + retags)
        state/path_map.json (updated)
        state/title_clean_log.json
        corrected_traktor/collection.nml (updated)
        corrected_traktor/*.nml (updated)
"""

import argparse
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from tqdm import tqdm

STATE_DIR      = Path(__file__).parent / "state"
METADATA_JSON  = STATE_DIR / "metadata.json"
PATH_MAP_JSON  = STATE_DIR / "path_map.json"
LOG_JSON       = STATE_DIR / "title_clean_log.json"
DEST_ROOT      = Path(__file__).parent / "corrected_music"
TRAKTOR_DIR    = Path(__file__).parent / "corrected_traktor"

UNSAFE_CHARS = r'\/:*?"<>|'

GENRE_PREFIXES = {
    "ebm", "goth", "industrial", "darkwave", "synth", "synthpop",
    "punk", "metal", "rock", "pop", "rave", "techno", "trance",
    "house", "electronic", "alternative", "new wave", "post-punk",
    "cold wave", "noise",
}

DOWNLOAD_ARTIFACT_PATTERNS = [
    r'\[YoutubeConverter\.Me\]',
    r'\[YoutubeConverter\.me\]',
    r'\[FREE DOWNLOAD\]',
    r'\[Full Album\]',
    r'\[Full Compilation\]',
    r'\(320\s+kbps\)',
    r'\(320kbps\)',
    r'\(HD\)',
    r'\(Official\s+Video\)',
    r'\(Official\s+Audio\)',
]
ARTIFACT_RE = re.compile('|'.join(DOWNLOAD_ARTIFACT_PATTERNS), re.I)


def sanitize(name: str, max_len: int = 100) -> str:
    name = unicodedata.normalize("NFKC", name.strip())
    for ch in UNSAFE_CHARS:
        name = name.replace(ch, "_")
    name = re.sub(r"[ _]{2,}", " ", name)
    name = name.strip(" ._")
    return name[:max_len] if name else "Unknown"


def clean_title(title: str, artist: str) -> str | None:
    """
    Return a cleaned title, or None if no cleaning needed.
    Only fixes provably wrong patterns — never touches legitimate paren/bracket titles.
    """
    original = title
    changed = False

    # 1. Strip file extension from end
    ext_match = re.search(r'\.(mp3|flac|m4a|wav|ogg|aiff|aif|wma|opus)$', title, re.I)
    if ext_match:
        title = title[:ext_match.start()].strip()
        changed = True

    # 2. Strip download artifacts
    cleaned = ARTIFACT_RE.sub('', title).strip()
    if cleaned != title:
        title = cleaned
        changed = True

    # 3. Genre-Artist-Title prefix: "EBM-Wolfsheim-Now I Fall" → "Now I Fall"
    genre_artist_match = re.match(
        r'^(' + '|'.join(re.escape(g) for g in GENRE_PREFIXES) + r')\s*[-_]\s*(.+?)\s*[-_]\s*(.+)$',
        title, re.I
    )
    if genre_artist_match:
        # Only strip if the middle part looks like the artist name
        possible_artist = genre_artist_match.group(2).strip()
        rest = genre_artist_match.group(3).strip()
        if artist and possible_artist.lower() == artist.lower():
            title = rest
            changed = True

    # 4. Artist-Title prefix: "Danzig - She Rides" or "Danzig-She Rides"
    if artist and len(artist) > 2:
        # Try "Artist - Title" (with spaces)
        prefix_spaced = re.escape(artist) + r'\s*[-–]\s*'
        m = re.match(prefix_spaced, title, re.I)
        if m:
            title = title[m.end():].strip()
            changed = True
        else:
            # Try "Artist-Title" (no spaces, tight hyphen)
            prefix_tight = re.escape(artist) + r'[-_]'
            m = re.match(prefix_tight, title, re.I)
            if m:
                title = title[m.end():].strip()
                changed = True

    # 5. Final cleanup
    title = title.strip(' -–_')

    return title if changed and title else None


def get_track_prefix(filename: str) -> str:
    """Extract '02 - ' style prefix from filename if present."""
    m = re.match(r'^(\d{1,3}\s*[-–]\s*)', Path(filename).stem)
    return m.group(1) if m else ""


def fix_xml_declaration(dest_path: Path):
    content = dest_path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    dest_path.write_bytes(content)


def update_tags(path: Path, new_title: str):
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=new_title))
            tags.save(path)
        elif ext in (".m4a", ".mp4"):
            tags = MP4(path)
            tags["©nam"] = [new_title]
            tags.save()
        elif ext == ".flac":
            tags = FLAC(path)
            tags["title"] = [new_title]
            tags.save()
        else:
            f = MutagenFile(path, easy=True)
            if f is not None:
                f["title"] = [new_title]
                f.save()
    except Exception as e:
        print(f"  [WARN] Could not update tags on {path.name}: {e}")


def abs_to_traktor_location(abs_path: str) -> dict:
    parts = abs_path.split("/")
    filename = parts[-1]
    dir_parts = parts[1:-1]
    dir_str = "/:" + "/:" .join(dir_parts) + "/:" if dir_parts else "/:"
    return {"DIR": dir_str, "FILE": filename, "VOLUME": "Macintosh HD", "VOLUMEID": "Macintosh HD"}


def abs_to_primarykey(abs_path: str) -> str:
    parts = [p for p in abs_path.split("/") if p]
    return "Macintosh HD/:" + "/:" .join(parts)


def traktor_to_abs(volume: str, dir_str: str, filename: str) -> str:
    stripped = dir_str.strip()
    if stripped.startswith("/:"):
        stripped = stripped[2:]
    if stripped.endswith("/:"):
        stripped = stripped[:-2]
    parts = stripped.split("/:") if stripped else []
    return "/" + "/".join(parts) + "/" + filename if parts else "/" + filename


def primarykey_to_abs(primarykey: str) -> str:
    pk = primarykey
    if pk.startswith("Macintosh HD"):
        pk = pk[len("Macintosh HD"):]
    parts = [p for p in pk.split("/:") if p]
    return "/" + "/".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--top", type=int, default=0, help="Show only top N in dry-run")
    args = parser.parse_args()

    print("Stage 6: Loading metadata and path map...")
    meta_data = json.loads(METADATA_JSON.read_text())
    path_map  = json.loads(PATH_MAP_JSON.read_text())
    tracks    = meta_data["tracks"]

    # Build reverse map: corrected_path → sha
    corrected_to_sha: dict[str, str] = {}
    sha_to_corrected: dict[str, str] = {}
    for old_path, new_path in path_map.items():
        sha = tracks.get(old_path.split("/")[-1])  # not reliable
        if new_path.startswith(str(DEST_ROOT)):
            pass
    # Better: use sha from metadata
    for sha, t in tracks.items():
        corrected = path_map.get(t.get("path", ""))
        if corrected:
            corrected_to_sha[corrected] = sha
            sha_to_corrected[sha] = corrected

    # Find all changes needed
    changes: list[dict] = []
    used_paths: set[str] = set(path_map.values())

    for sha, t in tracks.items():
        artist = (t.get("artist") or "").strip()
        title  = (t.get("title")  or "").strip()
        corrected_path = sha_to_corrected.get(sha)
        if not corrected_path or not title:
            continue
        if not Path(corrected_path).exists():
            continue

        new_title = clean_title(title, artist)
        if not new_title or new_title == title:
            continue

        # Compute new filename
        old_path_obj = Path(corrected_path)
        ext = old_path_obj.suffix
        prefix = get_track_prefix(old_path_obj.name)
        new_stem = prefix + sanitize(new_title)
        new_filename = new_stem + ext

        # Handle collision
        new_path = old_path_obj.parent / new_filename
        if str(new_path) != corrected_path and str(new_path) in used_paths:
            suffix = 2
            while True:
                candidate = old_path_obj.parent / f"{new_stem}_{suffix}{ext}"
                if str(candidate) not in used_paths:
                    new_path = candidate
                    break
                suffix += 1

        if str(new_path) == corrected_path:
            continue  # filename unchanged (sanitize produced same result)

        used_paths.add(str(new_path))
        used_paths.discard(corrected_path)

        changes.append({
            "sha":          sha,
            "artist":       artist,
            "old_title":    title,
            "new_title":    new_title,
            "old_path":     corrected_path,
            "new_path":     str(new_path),
        })

    print(f"  {len(changes):,} titles to clean")
    if not changes:
        print("  Nothing to do.")
        return

    # Show preview
    limit = args.top if args.top > 0 else len(changes)
    print(f"\n{'─'*65}")
    for c in changes[:limit]:
        print(f"  ARTIST: {c['artist']}")
        print(f"  OLD:    {c['old_title']!r}")
        print(f"  NEW:    {c['new_title']!r}")
        print(f"  FILE:   {Path(c['old_path']).name}")
        print(f"       → {Path(c['new_path']).name}")
        print()

    if not args.apply:
        print(f"Dry-run complete. {len(changes):,} changes queued.")
        print("Run with --apply to execute.")
        return

    # ── APPLY ──────────────────────────────────────────────────────────────

    # Build rename map for NML update
    rename_map: dict[str, str] = {}  # old_corrected → new_corrected

    print("\nApplying changes...")
    errors = []
    applied = 0

    for c in tqdm(changes, desc="Renaming & retagging"):
        old_p = Path(c["old_path"])
        new_p = Path(c["new_path"])
        try:
            # Rename file
            old_p.rename(new_p)
            # Update title tag
            update_tags(new_p, c["new_title"])
            rename_map[c["old_path"]] = c["new_path"]
            applied += 1
        except OSError as e:
            errors.append({"path": c["old_path"], "error": str(e)})

    print(f"  {applied:,} files renamed and retagged, {len(errors)} errors")

    # Update path_map.json
    print("  Updating path_map.json...")
    new_path_map = {}
    for old_src, old_corrected in path_map.items():
        new_path_map[old_src] = rename_map.get(old_corrected, old_corrected)
    PATH_MAP_JSON.write_text(json.dumps(new_path_map, ensure_ascii=False, indent=2))

    # Update NML files
    nml_files = list(TRAKTOR_DIR.glob("*.nml"))
    print(f"  Updating {len(nml_files)} NML files...")

    for nml_path in nml_files:
        try:
            tree = ET.parse(nml_path)
        except ET.ParseError as e:
            print(f"  [ERROR] {nml_path.name}: {e}")
            continue

        root = tree.getroot()
        nml_changes = 0

        # Update COLLECTION ENTRY LOCATION elements
        collection = root.find("COLLECTION")
        if collection is not None:
            for entry in collection.findall("ENTRY"):
                loc = entry.find("LOCATION")
                if loc is None:
                    continue
                cur_path = traktor_to_abs(
                    loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
                )
                if cur_path in rename_map:
                    new_abs = rename_map[cur_path]
                    for k, v in abs_to_traktor_location(new_abs).items():
                        loc.set(k, v)
                    nml_changes += 1

        # Update PLAYLISTS PRIMARYKEY KEY elements
        playlists = root.find("PLAYLISTS")
        if playlists is not None:
            for node in playlists.iter("ENTRY"):
                pk_el = node.find("PRIMARYKEY")
                if pk_el is None:
                    continue
                cur_path = primarykey_to_abs(pk_el.get("KEY", ""))
                if cur_path in rename_map:
                    pk_el.set("KEY", abs_to_primarykey(rename_map[cur_path]))
                    nml_changes += 1

        tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
        fix_xml_declaration(nml_path)
        print(f"    {nml_path.name}: {nml_changes} entries updated")

    # Write log
    log = {
        "applied": applied,
        "errors": len(errors),
        "changes": changes,
        "error_detail": errors,
    }
    LOG_JSON.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    print(f"\n  Log → {LOG_JSON}")
    print(f"\nDone. Quit Traktor, copy corrected_traktor/collection.nml into place, relaunch.")


if __name__ == "__main__":
    main()
