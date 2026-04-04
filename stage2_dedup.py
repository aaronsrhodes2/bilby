#!/usr/bin/env python3
"""
Stage 2 — Deduplication & Winner Selection

Groups files by SHA-256, selects the best copy of each duplicate set,
and builds an old_path → winner_path mapping for all files.

Winner selection priority:
  1. Format quality: FLAC > WAV > AIFF > M4A > OGG > MP3
  2. Highest bitrate (via mutagen)
  3. Referenced in Traktor collection.nml (has analyzed BPM/cues)
  4. Not in a Backups folder
  5. Shortest path

Reads:   state/scan.json
Writes:  state/dedup.json
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from mutagen import File as MutagenFile
from tqdm import tqdm

STATE_DIR = Path(__file__).parent / "state"
SCAN_JSON = STATE_DIR / "scan.json"
OUTPUT = STATE_DIR / "dedup.json"

TRAKTOR_COLLECTION = (
    Path.home() / "Documents" / "Native Instruments" / "Traktor 4.0.2" / "collection.nml"
)

# Format quality ranking (higher = better)
FORMAT_RANK = {
    ".flac": 6,
    ".wav": 5,
    ".aiff": 4,
    ".aif": 4,
    ".m4a": 3,
    ".ogg": 2,
    ".mp3": 1,
    ".wma": 0,
    ".opus": 2,
}


def load_traktor_paths() -> set[str]:
    """Load all file paths referenced in the main Traktor collection."""
    paths = set()
    if not TRAKTOR_COLLECTION.exists():
        print(f"  [WARN] Traktor collection not found at {TRAKTOR_COLLECTION}")
        return paths
    print(f"  Loading Traktor collection from {TRAKTOR_COLLECTION}...")
    try:
        tree = ET.parse(TRAKTOR_COLLECTION)
        root = tree.getroot()
        for entry in root.iter("ENTRY"):
            loc = entry.find("LOCATION")
            if loc is None:
                continue
            volume = loc.get("VOLUME", "")
            dir_str = loc.get("DIR", "")
            filename = loc.get("FILE", "")
            if not filename:
                continue
            # Decode Traktor path format
            stripped = dir_str.strip()
            if stripped.startswith("/:"):
                stripped = stripped[2:]
            if stripped.endswith("/:"):
                stripped = stripped[:-2]
            parts = stripped.split("/:") if stripped else []
            abs_path = "/" + "/".join(parts) + "/" + filename if parts else "/" + filename
            paths.add(abs_path)
    except ET.ParseError as e:
        print(f"  [WARN] Could not parse Traktor collection: {e}")
    print(f"  Loaded {len(paths):,} paths from Traktor collection")
    return paths


def get_bitrate(path: str, ext: str) -> int:
    """Read bitrate from file tags. Returns 0 on failure."""
    try:
        f = MutagenFile(path, easy=False)
        if f is None:
            return 0
        info = getattr(f, "info", None)
        if info is None:
            return 0
        return int(getattr(info, "bitrate", 0))
    except Exception:
        return 0


def score_file(path: str, ext: str, size: int, traktor_paths: set[str]) -> tuple:
    """
    Return a sort key tuple (higher = better winner candidate).
    Tuples compare lexicographically — first element is most important.
    """
    fmt_rank = FORMAT_RANK.get(ext, 0)
    bitrate = get_bitrate(path, ext)
    in_traktor = 1 if path in traktor_paths else 0
    not_backup = 0 if "/Backups/" in path or "/backups/" in path else 1
    path_len = -len(path)  # shorter = better (negate for descending sort)
    return (fmt_rank, bitrate, in_traktor, not_backup, path_len)


def main():
    if not SCAN_JSON.exists():
        print("scan.json not found — run stage1_scan.py first")
        sys.exit(1)

    if OUTPUT.exists():
        print(f"dedup.json already exists. Delete {OUTPUT} to re-run Stage 2.")
        return

    print("Stage 2: Loading scan results...")
    data = json.loads(SCAN_JSON.read_text())
    files = data["files"]
    print(f"  {len(files):,} files loaded")

    traktor_paths = load_traktor_paths()

    # Group by SHA-256
    print("  Grouping by SHA-256...")
    groups: dict[str, list[dict]] = {}
    for f in files:
        groups.setdefault(f["sha256"], []).append(f)

    unique = sum(1 for g in groups.values() if len(g) == 1)
    dupes = sum(1 for g in groups.values() if len(g) > 1)
    print(f"  {unique:,} unique hashes, {dupes:,} duplicate groups")

    # Select winner for each group
    print("  Selecting winners (reading bitrates)...")
    group_results = {}
    old_to_winner: dict[str, str] = {}
    total_duplicate_files = 0

    for sha, group in tqdm(groups.items(), desc="Selecting"):
        if len(group) == 1:
            winner_path = group[0]["path"]
            group_results[sha] = {
                "winner": winner_path,
                "winner_format": group[0]["ext"],
                "winner_bitrate": 0,
                "losers": [],
            }
            old_to_winner[winner_path] = sha
            continue

        # Score each candidate
        scored = []
        for f in group:
            key = score_file(f["path"], f["ext"], f["size_bytes"], traktor_paths)
            scored.append((key, f))

        scored.sort(key=lambda x: x[0], reverse=True)
        winner = scored[0][1]
        losers = [s[1]["path"] for s in scored[1:]]
        total_duplicate_files += len(losers)

        group_results[sha] = {
            "winner": winner["path"],
            "winner_format": winner["ext"],
            "winner_bitrate": scored[0][0][1],  # bitrate from score tuple
            "losers": losers,
        }

        old_to_winner[winner["path"]] = sha
        for loser_path in losers:
            old_to_winner[loser_path] = sha

    print(f"  {total_duplicate_files:,} duplicate files will be deduplicated")

    output = {
        "total_unique_hashes": len(group_results),
        "total_duplicate_files": total_duplicate_files,
        "groups": group_results,
        "old_to_winner_sha": old_to_winner,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"  Written to {OUTPUT}")


if __name__ == "__main__":
    main()
