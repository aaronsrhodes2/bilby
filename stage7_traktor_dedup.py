#!/usr/bin/env python3
"""
Stage 7 — Traktor-Based Duplicate Identification

Identifies tracks in the curated library that are duplicates based on all
four musical criteria simultaneously:

  1. Title + Artist   — normalized exact match
  2. Duration         — within 1 second (INFO PLAYTIME)
  3. BPM              — within 0.5 BPM (TEMPO BPM)
  4. Key              — exact match (MUSICAL_KEY VALUE, fallback INFO KEY)

Entries missing BPM or key data are excluded — we only flag what we can
confirm. Within a matched group the entry with the richest DJ metadata
(cue points, ratings, play count) is kept; the rest are flagged for removal.

Dry-run by default. Use --apply to:
  - Remove loser ENTRYs from corrected_traktor/collection.nml
  - Redirect playlist PRIMARYKEY references from losers → winner
  - Update all playlist NML files in corrected_traktor/ the same way
  - Write a deletion manifest to state/traktor_dedup_deletions.json

Use --delete-files (requires --apply) to also delete loser files from disk.
NOTE: --delete-files is irreversible. Review the report first.

Reads:  corrected_traktor/collection.nml, corrected_traktor/*.nml
Writes: state/traktor_dedup_report.json
        state/traktor_dedup_deletions.json  (with --apply)
        corrected_traktor/collection.nml    (modified in-place, with --apply)
        corrected_traktor/*.nml             (modified in-place, with --apply)

Usage:
    python3 stage7_traktor_dedup.py                        # full report
    python3 stage7_traktor_dedup.py --top 20               # show top 20 groups
    python3 stage7_traktor_dedup.py --min-score N          # filter by winner richness
    python3 stage7_traktor_dedup.py --apply                # apply to NML files
    python3 stage7_traktor_dedup.py --apply --delete-files # apply + delete files
"""

import argparse
import json
import os
import shutil
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from lib.nml_parser import traktor_to_abs, abs_to_traktor_location, abs_to_primarykey, primarykey_to_abs

STATE_DIR        = Path(__file__).parent / "state"
REPORT_JSON      = STATE_DIR / "traktor_dedup_report.json"
DELETIONS_JSON   = STATE_DIR / "traktor_dedup_deletions.json"
TRAKTOR_DIR      = Path(__file__).parent / "corrected_traktor"
NML_SOURCE       = TRAKTOR_DIR / "collection.nml"

FORMAT_RANK = {
    ".flac": 6, ".wav": 5, ".aiff": 4, ".aif": 4,
    ".m4a": 3, ".ogg": 2, ".mp3": 1, ".wma": 0, ".opus": 2,
}

BPM_TOLERANCE      = 0.5   # BPM units
DURATION_TOLERANCE = 1     # seconds


# ---------------------------------------------------------------------------
# Scoring — same logic as stage5_traktor.py / stage2b_metadata_dedup.py
# ---------------------------------------------------------------------------

def entry_score(entry: ET.Element) -> int:
    """DJ-metadata richness score. Higher = keep this one."""
    score = 0
    info = entry.find("INFO")
    if info is not None:
        try:
            if int(info.get("PLAYCOUNT", 0)) > 0:
                score += 1
        except ValueError:
            pass
        try:
            if int(info.get("RANKING", 0)) > 0:
                score += 2
        except ValueError:
            pass
        if info.get("LAST_PLAYED"):
            score += 1
        try:
            if int(info.get("COLOR", "0")) != 0:
                score += 1
        except ValueError:
            pass

    cues = entry.findall("CUE_V2")
    for cue in cues:
        if cue.get("HOTCUE", "-1") != "-1" and cue.get("NAME", "n.n.") != "n.n.":
            score += 1
    score += min(len(cues), 5)
    return score


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    t = (text or "").lower().strip()
    for article in ("the ", "a ", "an "):
        if t.startswith(article):
            t = t[len(article):]
            break
    return t


# ---------------------------------------------------------------------------
# Metadata extraction from a single ENTRY element
# ---------------------------------------------------------------------------

def extract_entry_data(entry: ET.Element) -> dict | None:
    """
    Return a dict of all fields needed for matching, or None if the entry
    lacks the minimum data required (BPM or key missing → skip).
    """
    title  = entry.get("TITLE", "").strip()
    artist = entry.get("ARTIST", "").strip()
    if not title or not artist:
        return None

    loc = entry.find("LOCATION")
    if loc is None:
        return None
    path = traktor_to_abs(loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", ""))
    if not path:
        return None

    # Duration
    info = entry.find("INFO")
    playtime = None
    if info is not None:
        try:
            playtime = int(info.get("PLAYTIME", 0))
        except ValueError:
            pass
    if not playtime:
        return None  # can't check duration → skip

    # BPM
    tempo = entry.find("TEMPO")
    bpm = None
    if tempo is not None:
        try:
            bpm = float(tempo.get("BPM", 0))
        except ValueError:
            pass
    if not bpm:
        return None  # can't check BPM → skip

    # Key — prefer MUSICAL_KEY VALUE, fall back to INFO KEY string
    key = None
    mk = entry.find("MUSICAL_KEY")
    if mk is not None:
        key = mk.get("VALUE", "").strip()
    if not key and info is not None:
        key = info.get("KEY", "").strip()
    if not key:
        return None  # can't check key → skip

    ext = Path(path).suffix.lower()

    return {
        "title":    title,
        "artist":   artist,
        "path":     path,
        "ext":      ext,
        "playtime": playtime,
        "bpm":      bpm,
        "key":      key,
        "richness": entry_score(entry),
        "entry":    entry,   # keep reference for reporting detail
    }


# ---------------------------------------------------------------------------
# Cluster matching within a title+artist group
# ---------------------------------------------------------------------------

def tracks_match(a: dict, b: dict) -> bool:
    """Return True if a and b satisfy duration, BPM, and key criteria."""
    if abs(a["playtime"] - b["playtime"]) > DURATION_TOLERANCE:
        return False
    if abs(a["bpm"] - b["bpm"]) > BPM_TOLERANCE:
        return False
    if a["key"] != b["key"]:
        return False
    return True


def find_dup_clusters(tracks: list[dict]) -> list[list[dict]]:
    """
    Given a list of tracks with the same normalized title+artist, find
    clusters where every pair satisfies the match criteria.

    Uses connected-components on the match graph — if A matches B and B
    matches C, all three form one cluster even if A doesn't directly match C
    (BPM drift chain). This is intentional: the chain means they're all
    versions of the same song.
    """
    n = len(tracks)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if tracks_match(tracks[i], tracks[j]):
                union(i, j)

    # Group by root
    clusters: dict[int, list[dict]] = defaultdict(list)
    for i, track in enumerate(tracks):
        clusters[find(i)].append(track)

    # Only return clusters with 2+ members
    return [c for c in clusters.values() if len(c) > 1]


# ---------------------------------------------------------------------------
# Winner selection
# ---------------------------------------------------------------------------

def candidate_sort_key(t: dict) -> tuple:
    """Higher tuple = better candidate. Sort descending."""
    fmt_rank = FORMAT_RANK.get(t["ext"], 0)
    not_backup = 0 if "/Backups/" in t["path"] or "/backups/" in t["path"] else 1
    return (t["richness"], fmt_rank, t["playtime"], not_backup, -len(t["path"]))


# ---------------------------------------------------------------------------
# Apply helpers
# ---------------------------------------------------------------------------

def fix_xml_declaration(path: Path):
    """Match Traktor's expected XML declaration format."""
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


def apply_to_collection(nml_path: Path, loser_to_winner: dict[str, str]) -> tuple[int, int]:
    """
    Remove loser ENTRYs from the COLLECTION section and redirect playlist
    PRIMARYKEY references from losers to their winners.

    Returns (entries_removed, keys_redirected).
    """
    ET.register_namespace("", "")
    tree = ET.parse(nml_path)
    root = tree.getroot()

    # --- COLLECTION: remove loser ENTRYs ---
    collection = root.find("COLLECTION")
    entries_removed = 0
    if collection is not None:
        to_remove = []
        for entry in collection.findall("ENTRY"):
            path = entry_abs_path(entry)
            if path and path in loser_to_winner:
                to_remove.append(entry)
        for entry in to_remove:
            collection.remove(entry)
        entries_removed = len(to_remove)
        collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))

    # --- PLAYLISTS: redirect loser PRIMARYKEY → winner ---
    keys_redirected = 0
    playlists = root.find("PLAYLISTS")
    if playlists is not None:
        for node in playlists.iter("ENTRY"):
            pk_el = node.find("PRIMARYKEY")
            if pk_el is None:
                continue
            old_path = primarykey_to_abs(pk_el.get("KEY", ""))
            if old_path in loser_to_winner:
                pk_el.set("KEY", abs_to_primarykey(loser_to_winner[old_path]))
                keys_redirected += 1

    tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(nml_path)
    return entries_removed, keys_redirected


def apply_to_playlist_nml(nml_path: Path, loser_to_winner: dict[str, str]) -> tuple[int, int]:
    """
    Update a playlist NML file: remove loser COLLECTION ENTRYs and redirect
    playlist PRIMARYKEY references.

    Returns (entries_removed, keys_redirected).
    """
    ET.register_namespace("", "")
    try:
        tree = ET.parse(nml_path)
    except ET.ParseError as e:
        print(f"  [WARN] Could not parse {nml_path.name}: {e}")
        return 0, 0

    root = tree.getroot()
    entries_removed = 0

    collection = root.find("COLLECTION")
    if collection is not None:
        to_remove = []
        for entry in collection.findall("ENTRY"):
            path = entry_abs_path(entry)
            if path and path in loser_to_winner:
                to_remove.append(entry)
        for entry in to_remove:
            collection.remove(entry)
        entries_removed = len(to_remove)

    keys_redirected = 0
    playlists = root.find("PLAYLISTS")
    if playlists is not None:
        for node in playlists.iter("ENTRY"):
            pk_el = node.find("PRIMARYKEY")
            if pk_el is None:
                continue
            old_path = primarykey_to_abs(pk_el.get("KEY", ""))
            if old_path in loser_to_winner:
                pk_el.set("KEY", abs_to_primarykey(loser_to_winner[old_path]))
                keys_redirected += 1

    tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(nml_path)
    return entries_removed, keys_redirected


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Traktor-based duplicate identification")
    parser.add_argument("--top", type=int, default=0,
                        help="Print only the top N groups (0 = all)")
    parser.add_argument("--min-score", type=int, default=0,
                        help="Only show groups where the winner richness >= N")
    parser.add_argument("--apply", action="store_true",
                        help="Apply dedup: update NML files, write deletion manifest")
    parser.add_argument("--delete-files", action="store_true",
                        help="Also delete loser files from disk (requires --apply)")
    args = parser.parse_args()

    if args.delete_files and not args.apply:
        parser.error("--delete-files requires --apply")

    if not NML_SOURCE.exists():
        print(f"collection.nml not found at {NML_SOURCE}")
        print("Run stages 1–5 first.")
        return

    print(f"Stage 7: Parsing {NML_SOURCE.name} ({NML_SOURCE.stat().st_size // 1024 // 1024} MB)...")
    try:
        tree = ET.parse(NML_SOURCE)
    except ET.ParseError as e:
        print(f"[ERROR] Could not parse NML: {e}")
        return

    root = tree.getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        print("[ERROR] No COLLECTION element found.")
        return

    entries = list(collection.findall("ENTRY"))
    print(f"  {len(entries):,} ENTRY elements")

    # Extract metadata from each entry
    valid: list[dict] = []
    skipped = 0
    for entry in entries:
        data = extract_entry_data(entry)
        if data is None:
            skipped += 1
        else:
            valid.append(data)

    print(f"  {len(valid):,} entries with full BPM/key/duration data")
    print(f"  {skipped:,} skipped (missing BPM, key, duration, title, or artist)")

    # Group by normalized title + artist
    by_title_artist: dict[tuple, list[dict]] = defaultdict(list)
    for t in valid:
        key = (normalize(t["artist"]), normalize(t["title"]))
        by_title_artist[key].append(t)

    # Find duplicate clusters
    dup_groups = []
    for (artist_n, title_n), tracks in by_title_artist.items():
        if len(tracks) < 2:
            continue
        clusters = find_dup_clusters(tracks)
        for cluster in clusters:
            cluster.sort(key=candidate_sort_key, reverse=True)
            winner = cluster[0]
            losers = cluster[1:]
            dup_groups.append({
                "normalized_key": f"{artist_n} — {title_n}",
                "match_criteria": {
                    "bpm":      round(winner["bpm"], 3),
                    "key":      winner["key"],
                    "playtime": winner["playtime"],
                },
                "winner": {
                    "path":     winner["path"],
                    "format":   winner["ext"],
                    "bpm":      round(winner["bpm"], 3),
                    "key":      winner["key"],
                    "playtime": winner["playtime"],
                    "richness": winner["richness"],
                    "artist":   winner["artist"],
                    "title":    winner["title"],
                },
                "losers": [
                    {
                        "path":     l["path"],
                        "format":   l["ext"],
                        "bpm":      round(l["bpm"], 3),
                        "key":      l["key"],
                        "playtime": l["playtime"],
                        "richness": l["richness"],
                        "artist":   l["artist"],
                        "title":    l["title"],
                    }
                    for l in losers
                ],
            })

    # Sort: most losers first, then by winner richness
    dup_groups.sort(key=lambda g: (-len(g["losers"]), -g["winner"]["richness"]))

    # Apply --min-score filter
    if args.min_score > 0:
        dup_groups = [g for g in dup_groups if g["winner"]["richness"] >= args.min_score]

    total_to_remove = sum(len(g["losers"]) for g in dup_groups)
    print(f"\n  {len(dup_groups):,} duplicate groups found")
    print(f"  {total_to_remove:,} tracks could be removed")

    # Console output
    limit = args.top if args.top > 0 else len(dup_groups)
    if limit > 0:
        print(f"\n{'='*72}")
        print(f"{'Top ' + str(limit) if args.top else 'All'} duplicate groups:")
        print(f"{'='*72}\n")
        for g in dup_groups[:limit]:
            w = g["winner"]
            mc = g["match_criteria"]
            print(f"  KEEP  [{w['format'].upper():5s} cues={w['richness']:2d}]  "
                  f"{w['artist']} — {w['title']}  "
                  f"({mc['bpm']} BPM, key={mc['key']}, {mc['playtime']}s)")
            print(f"        {w['path']}")
            for loser in g["losers"]:
                print(f"  DROP  [{loser['format'].upper():5s} cues={loser['richness']:2d}]  "
                      f"{loser['artist']} — {loser['title']}  "
                      f"({loser['bpm']} BPM, key={loser['key']}, {loser['playtime']}s)")
                print(f"        {loser['path']}")
            print()

    # Write report
    report = {
        "total_dup_groups":       len(dup_groups),
        "total_tracks_to_remove": total_to_remove,
        "skipped_missing_data":   skipped,
        "source_nml":             str(NML_SOURCE),
        "bpm_tolerance":          BPM_TOLERANCE,
        "duration_tolerance_sec": DURATION_TOLERANCE,
        "groups":                 dup_groups,
    }
    STATE_DIR.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Full report → {REPORT_JSON}")

    if not args.apply:
        print("\nDry-run complete. Run with --apply to update NML files.")
        print("Add --delete-files to also remove loser tracks from disk.")
        return

    # ------------------------------------------------------------------
    # Build loser → winner path map (all losers across all groups)
    # ------------------------------------------------------------------
    loser_to_winner: dict[str, str] = {}
    all_loser_paths: list[str] = []
    for g in dup_groups:
        winner_path = g["winner"]["path"]
        for loser in g["losers"]:
            loser_to_winner[loser["path"]] = winner_path
            all_loser_paths.append(loser["path"])

    print(f"\nApplying: {len(loser_to_winner):,} loser paths → winner redirects")

    # ------------------------------------------------------------------
    # Update collection.nml
    # ------------------------------------------------------------------
    print(f"\n  Updating {NML_SOURCE.name}...")
    removed, redirected = apply_to_collection(NML_SOURCE, loser_to_winner)
    print(f"    {removed:,} ENTRY elements removed, {redirected:,} playlist keys redirected")

    # ------------------------------------------------------------------
    # Update playlist NML files
    # ------------------------------------------------------------------
    playlist_nmls = sorted(
        f for f in TRAKTOR_DIR.glob("*.nml")
        if f.name != "collection.nml" and f.is_file()
    )
    print(f"\n  Updating {len(playlist_nmls)} playlist NML files...")
    total_pl_removed = total_pl_redirected = 0
    for nml_path in playlist_nmls:
        pl_removed, pl_redirected = apply_to_playlist_nml(nml_path, loser_to_winner)
        total_pl_removed    += pl_removed
        total_pl_redirected += pl_redirected
        if pl_removed or pl_redirected:
            print(f"    {nml_path.name}: {pl_removed} removed, {pl_redirected} redirected")
    print(f"    Totals: {total_pl_removed:,} removed, {total_pl_redirected:,} redirected")

    # ------------------------------------------------------------------
    # Write deletion manifest
    # ------------------------------------------------------------------
    deletions = {
        "total_files": len(all_loser_paths),
        "files_deleted": 0,
        "paths": all_loser_paths,
    }

    # ------------------------------------------------------------------
    # Optionally delete files from disk
    # ------------------------------------------------------------------
    if args.delete_files:
        print(f"\n  Deleting {len(all_loser_paths):,} loser files from disk...")
        deleted = 0
        missing = 0
        errors = 0
        for path in all_loser_paths:
            if not os.path.exists(path):
                missing += 1
                continue
            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                print(f"    [ERROR] {path}: {e}")
                errors += 1
        deletions["files_deleted"] = deleted
        print(f"    Deleted: {deleted:,} | Already missing: {missing:,} | Errors: {errors}")
    else:
        print(f"\n  Files NOT deleted (run with --delete-files to remove from disk).")

    DELETIONS_JSON.write_text(json.dumps(deletions, ensure_ascii=False, indent=2))
    print(f"\n  Deletion manifest → {DELETIONS_JSON}")
    print(f"\nStage 7 apply complete.")
    print(f"  Load corrected_traktor/collection.nml into Traktor to verify, then")
    print(f"  follow switch_library.md to replace your live library.")


if __name__ == "__main__":
    main()
