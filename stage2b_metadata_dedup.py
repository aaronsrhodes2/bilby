#!/usr/bin/env python3
"""
Stage 2b — Metadata-Based Deduplication (Dry-Run)

After Stage 3 corrects metadata, some tracks that have DIFFERENT checksums
(different rips/bitrates/formats) may end up with EXACTLY the same
artist + title. This stage identifies those near-duplicates.

Dry-run by default: outputs a report only.
Use --apply to update state so Stage 4 skips the near-duplicate copies.

Winner selection priority (same philosophy as Stage 5 entry merging):
  1. Traktor DJ metadata richness (cue points, ratings, play count — highest wins)
  2. Format quality: FLAC > WAV > AIFF > M4A > OGG > MP3
  3. Bitrate (higher = better)
  4. Not in a Backups folder
  5. Shortest path

Normalization: lowercase + strip whitespace on both artist and title.
Exact match only — no fuzzy matching.

Reads:  state/metadata.json, state/dedup.json,
        ~/Documents/Native Instruments/Traktor 4.0.2/collection.nml
Writes: state/metadata_dedup_report.json
        state/dedup_final.json  (only with --apply)

Usage:
    python3 stage2b_metadata_dedup.py            # dry-run, show report
    python3 stage2b_metadata_dedup.py --apply    # write dedup_final.json
    python3 stage2b_metadata_dedup.py --top 20   # show top 20 groups only
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from mutagen import File as MutagenFile
from tqdm import tqdm

STATE_DIR = Path(__file__).parent / "state"
METADATA_JSON = STATE_DIR / "metadata.json"
DEDUP_JSON = STATE_DIR / "dedup.json"
REPORT_JSON = STATE_DIR / "metadata_dedup_report.json"
FINAL_JSON = STATE_DIR / "dedup_final.json"

TRAKTOR_COLLECTION = (
    Path.home() / "Documents" / "Native Instruments" / "Traktor 4.0.2" / "collection.nml"
)

FORMAT_RANK = {
    ".flac": 6, ".wav": 5, ".aiff": 4, ".aif": 4,
    ".m4a": 3, ".ogg": 2, ".mp3": 1, ".wma": 0, ".opus": 2,
}


def normalize_key(artist: str, title: str) -> tuple[str, str]:
    """Normalize artist+title for comparison."""
    artist_n = (artist or "").lower().strip()
    title_n  = (title  or "").lower().strip()
    for article in ("the ", "a ", "an "):
        if artist_n.startswith(article):
            artist_n = artist_n[len(article):]
            break
    return artist_n, title_n


def load_traktor_richness() -> dict[str, int]:
    """
    Parse Traktor collection and return a map of abs_path → richness_score.

    Richness score (same logic as stage5_traktor.py entry_score):
      +1 per named hotcue (HOTCUE != -1 and NAME != 'n.n.')
      +min(total cue count, 5)
      +2 if RANKING > 0
      +1 if PLAYCOUNT > 0
      +1 if LAST_PLAYED present
      +1 if COLOR != 0
    """
    richness: dict[str, int] = {}
    if not TRAKTOR_COLLECTION.exists():
        print(f"  [WARN] Traktor collection not found — richness scores will be 0")
        return richness

    print(f"  Loading Traktor richness scores from collection.nml...")
    try:
        tree = ET.parse(TRAKTOR_COLLECTION)
    except ET.ParseError as e:
        print(f"  [WARN] Could not parse collection: {e}")
        return richness

    root = tree.getroot()
    for entry in root.iter("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None:
            continue
        dir_str  = loc.get("DIR", "")
        filename = loc.get("FILE", "")
        if not filename:
            continue

        # Decode Traktor path
        stripped = dir_str.strip()
        if stripped.startswith("/:"):
            stripped = stripped[2:]
        if stripped.endswith("/:"):
            stripped = stripped[:-2]
        parts = stripped.split("/:") if stripped else []
        abs_path = "/" + "/".join(parts) + "/" + filename if parts else "/" + filename

        # Score this entry
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

        cues = entry.findall("CUE_V2")
        for cue in cues:
            if cue.get("HOTCUE", "-1") != "-1" and cue.get("NAME", "n.n.") != "n.n.":
                score += 1
        score += min(len(cues), 5)

        # Keep highest score if a path appears multiple times
        richness[abs_path] = max(richness.get(abs_path, 0), score)

    print(f"  Loaded richness scores for {len(richness):,} Traktor entries")
    return richness


def get_bitrate(path: str) -> int:
    try:
        f = MutagenFile(path, easy=False)
        if f is None:
            return 0
        info = getattr(f, "info", None)
        return int(getattr(info, "bitrate", 0)) if info else 0
    except Exception:
        return 0


def winner_score(
    winner_path: str,
    winner_format: str,
    winner_bitrate: int,
    all_source_paths: list[str],
    traktor_richness: dict[str, int],
) -> tuple:
    """
    Score a winner candidate. Returns a tuple — higher = keep this one.

    Priority order (most important first):
      1. Traktor richness (highest score across this winner + all its checksum-losers)
      2. Format quality
      3. Bitrate
      4. Not in Backups
      5. Shorter path
    """
    # Richness = best Traktor score across all paths that resolve to this file
    richness = max((traktor_richness.get(p, 0) for p in all_source_paths), default=0)

    fmt_rank   = FORMAT_RANK.get(winner_format.lower(), 0)
    bitrate    = winner_bitrate or get_bitrate(winner_path)
    not_backup = 0 if any("/Backups/" in p or "/backups/" in p for p in all_source_paths) else 1
    path_len   = -len(winner_path)

    return (richness, fmt_rank, bitrate, not_backup, path_len)


def main():
    parser = argparse.ArgumentParser(description="Metadata-based near-duplicate detection")
    parser.add_argument("--apply", action="store_true",
                        help="Write dedup_final.json so Stage 4 uses updated dedup")
    parser.add_argument("--top", type=int, default=0,
                        help="Print only the top N duplicate groups to console (0 = all)")
    args = parser.parse_args()

    if not METADATA_JSON.exists():
        print("metadata.json not found — run stage3_fingerprint.py first")
        sys.exit(1)
    if not DEDUP_JSON.exists():
        print("dedup.json not found — run stage2_dedup.py first")
        sys.exit(1)

    print("Stage 2b: Loading state...")
    meta_data  = json.loads(METADATA_JSON.read_text())
    dedup_data = json.loads(DEDUP_JSON.read_text())

    tracks     = meta_data["tracks"]        # sha → {artist, title, ...}
    groups     = dedup_data["groups"]       # sha → {winner, winner_format, winner_bitrate, losers}
    old_to_sha = dedup_data["old_to_winner_sha"]

    traktor_richness = load_traktor_richness()

    print(f"  {len(tracks):,} unique tracks with metadata")

    # Group winners by normalized (artist, title)
    key_to_shas: dict[tuple, list[str]] = defaultdict(list)
    skipped = 0

    for sha, track in tracks.items():
        artist = (track.get("artist") or "").strip()
        title  = (track.get("title")  or "").strip()

        if not artist or not title:
            skipped += 1
            continue
        if artist.lower() in ("unknown artist", "artist") or \
           title.lower() in ("unknown", "track", "untitled"):
            skipped += 1
            continue

        key = normalize_key(artist, title)
        key_to_shas[key].append(sha)

    near_dup_groups = {k: shas for k, shas in key_to_shas.items() if len(shas) > 1}
    tracks_to_remove = sum(len(v) - 1 for v in near_dup_groups.values())
    print(f"  {skipped:,} tracks skipped (missing/placeholder metadata)")
    print(f"  {len(near_dup_groups):,} near-duplicate groups → {tracks_to_remove:,} tracks would be removed")

    # Score and sort each group
    report_groups = []
    for (artist_n, title_n), shas in sorted(near_dup_groups.items(),
                                             key=lambda x: -len(x[1])):
        candidates = []
        for sha in shas:
            group  = groups.get(sha, {})
            track  = tracks.get(sha, {})
            path   = group.get("winner", "")
            fmt    = group.get("winner_format", "")
            bitrate = group.get("winner_bitrate", 0)
            # All paths that checksum-dedup mapped to this SHA
            all_paths = [path] + group.get("losers", [])
            score = winner_score(path, fmt, bitrate, all_paths, traktor_richness)
            richness = score[0]  # first element is Traktor richness
            candidates.append({
                "sha256":          sha,
                "path":            path,
                "format":          fmt,
                "bitrate":         bitrate or get_bitrate(path),
                "artist":          track.get("artist", ""),
                "title":           track.get("title",  ""),
                "source":          track.get("source", ""),
                "traktor_richness": richness,
                "score":           score,
                "checksum_losers": group.get("losers", []),
            })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        super_winner    = candidates[0]
        near_dup_losers = candidates[1:]

        report_groups.append({
            "normalized_key": f"{artist_n} — {title_n}",
            "super_winner": {k: super_winner[k] for k in
                             ("sha256", "path", "format", "bitrate",
                              "artist", "title", "source", "traktor_richness")},
            "near_dup_losers": [
                {k: c[k] for k in
                 ("sha256", "path", "format", "bitrate",
                  "artist", "title", "traktor_richness")}
                for c in near_dup_losers
            ],
        })

    # Console output
    limit = args.top if args.top > 0 else len(report_groups)
    print(f"\n{'='*70}")
    print(f"Top {min(limit, len(report_groups))} near-duplicate groups:")
    print(f"{'='*70}\n")
    for g in report_groups[:limit]:
        w = g["super_winner"]
        print(f"  KEEP  [{w['format'].upper():5s} {w['bitrate']//1000:3d}k "
              f"cues={w['traktor_richness']:2d}]  {w['artist']} — {w['title']}")
        print(f"        {w['path']}")
        for loser in g["near_dup_losers"]:
            print(f"  DROP  [{loser['format'].upper():5s} {loser['bitrate']//1000:3d}k "
                  f"cues={loser['traktor_richness']:2d}]  {loser['artist']} — {loser['title']}")
            print(f"        {loser['path']}")
        print()

    # Write report
    report = {
        "total_near_dup_groups":  len(report_groups),
        "total_tracks_to_remove": tracks_to_remove,
        "skipped_no_metadata":    skipped,
        "groups": report_groups,
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Full report → {REPORT_JSON}")

    if not args.apply:
        print("\nDry-run complete. Review the report, then run with --apply to commit.")
        return

    # --apply: merge near-duplicate groups into dedup_final.json
    print("\nApplying near-duplicate deduplication...")
    final_groups   = dict(groups)
    final_old_to_sha = dict(old_to_sha)

    for g in tqdm(report_groups, desc="Merging"):
        super_sha = g["super_winner"]["sha256"]

        for loser in g["near_dup_losers"]:
            loser_sha = loser["sha256"]
            loser_group = groups.get(loser_sha, {})
            all_loser_paths = [loser_group.get("winner", "")] + loser_group.get("losers", [])

            # Redirect all loser paths to the super-winner SHA
            for p in all_loser_paths:
                if p:
                    final_old_to_sha[p] = super_sha

            # Absorb loser paths into super-winner's losers list
            if super_sha in final_groups:
                final_groups[super_sha]["losers"].extend(p for p in all_loser_paths if p)

            # Remove the absorbed group
            final_groups.pop(loser_sha, None)

    total_dup_files = sum(len(g.get("losers", [])) for g in final_groups.values())
    final = {
        "total_unique_hashes":      len(final_groups),
        "total_duplicate_files":    total_dup_files,
        "near_dup_groups_merged":   len(report_groups),
        "near_dup_tracks_removed":  tracks_to_remove,
        "groups": final_groups,
        "old_to_winner_sha": final_old_to_sha,
    }

    FINAL_JSON.write_text(json.dumps(final, ensure_ascii=False, indent=2))
    print(f"  {tracks_to_remove:,} near-duplicate winners absorbed")
    print(f"  {len(final_groups):,} truly unique tracks remain")
    print(f"  Written to {FINAL_JSON}")
    print("\nRun Stage 4 next — it auto-detects dedup_final.json.")


if __name__ == "__main__":
    main()
