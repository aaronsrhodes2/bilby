#!/usr/bin/env python3
"""
Stage 8b — Artist Name Normalization + Genre Sync

Two independent operations controlled by flags:

ARTIST NORMALIZATION (--report / --apply-artists)
  Groups artist names that differ only by case or leading article ("The", "A", "An").
  Picks a canonical name per group using:
    1. MusicBrainz canonical artist name (from mb_cache.json + metadata.json)
    2. Fall back: variant with the most tracks
  Applies changes to:
    - corrected_music/ folder names (Artist-level folder rename)
    - Audio file ARTIST tags (mutagen)
    - corrected_traktor/collection.nml ENTRY ARTIST attributes + LOCATION paths
    - corrected_traktor/*.nml playlist files (LOCATION + PRIMARYKEY paths)
    - state/path_map.json (path references)

GENRE SYNC (--fetch-genres / --apply-genres)
  Fetches genre data from MusicBrainz for tracks that have a MB recording ID.
  Uses a separate cache (state/mb_genre_cache.json) so existing mb_cache.json
  is untouched. The fetch is resumable — re-run safely at any time.
  Applies top-voted genre to:
    - Audio file GENRE tags
    - corrected_traktor/collection.nml INFO GENRE attributes

Usage:
    python3 stage8b_normalize.py --report                # artist normalization dry-run
    python3 stage8b_normalize.py --apply-artists         # apply artist normalization
    python3 stage8b_normalize.py --fetch-genres          # fetch genres from MB (slow, resumable)
    python3 stage8b_normalize.py --fetch-genres --limit 500  # fetch only 500 this run
    python3 stage8b_normalize.py --apply-genres          # apply genres to tags + NML
"""

import argparse
import asyncio
import json
import os
import shutil
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TPE1, TCON
from mutagen.mp4 import MP4
from tqdm import tqdm

import sys
sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs, abs_to_traktor_location, abs_to_primarykey, primarykey_to_abs

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT    = Path(__file__).parent
STATE_DIR  = PROJECT / "state"
TRAKTOR_DIR = PROJECT / "corrected_traktor"
NML_SOURCE  = TRAKTOR_DIR / "collection.nml"
CORRECTED   = PROJECT / "corrected_music"
PATH_MAP_JSON     = STATE_DIR / "path_map.json"
METADATA_JSON     = STATE_DIR / "metadata.json"
MB_CACHE_JSON     = STATE_DIR / "mb_cache.json"
MB_GENRE_CACHE    = STATE_DIR / "mb_genre_cache.json"
ARTIST_REPORT_JSON = STATE_DIR / "artist_normalization_report.json"
GENRE_REPORT_JSON  = STATE_DIR / "genre_sync_report.json"

MB_BASE    = "https://musicbrainz.org/ws/2"
USER_AGENT = "MusicOrganizePipeline/1.0 (aaron.s.rhodes@gmail.com)"
RATE_LIMIT = 1.1   # seconds between MB requests

ET.register_namespace("", "")


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def fix_xml_declaration(path: Path):
    content = path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(content)


def normalize_artist(name: str) -> str:
    """Lowercase only — articles are kept as-is (Traktor's search handles them)."""
    return (name or "").lower().strip()


def _best_variant(variants: set, artist_counts: Counter) -> str:
    """
    Pick best canonical from a set of variants when MB gives no clean answer.
    Priority:
      1. Variants where the first letter is uppercase (filters all-lowercase junk)
      2. Among those: most tracks
    This prevents 'the verve' beating 'The Verve' just because it has more
    badly-tagged files.
    """
    starts_upper = [v for v in variants if v and v[0].isupper()]
    pool = starts_upper if starts_upper else list(variants)
    return max(pool, key=lambda v: artist_counts.get(v, 0))


def entry_abs_path(entry: ET.Element) -> str | None:
    loc = entry.find("LOCATION")
    if loc is None:
        return None
    return traktor_to_abs(
        loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
    )


# ---------------------------------------------------------------------------
# ARTIST NORMALIZATION
# ---------------------------------------------------------------------------

def build_canonical_map(
    nml_path: Path,
    metadata: dict,
    mb_cache: dict,
) -> dict[str, str]:
    """
    Returns old_artist → canonical_artist for all groups with 2+ variants.

    Canonical selection priority:
      1. MusicBrainz canonical name (artist field from mb_cache, for the MB
         recording with the most tracks using that normalized key)
      2. Most-tracks variant
    """
    # Parse NML to get artist per path
    tree = ET.parse(nml_path)
    root = tree.getroot()
    collection = root.find("COLLECTION")

    # path → tag artist
    path_to_artist: dict[str, str] = {}
    artist_counts: Counter = Counter()
    for entry in collection.findall("ENTRY"):
        path = entry_abs_path(entry)
        artist = entry.get("ARTIST", "").strip()
        if path and artist:
            path_to_artist[path] = artist
            artist_counts[artist] += 1

    # Build a global map: tag_artist (normalized) → MB canonical artist votes
    # Uses ALL tracks in metadata.json, not just those currently in the NML,
    # so MB data from dropped paths (Backups, etc.) still informs the choice.
    tracks = metadata.get("tracks", {})
    tag_norm_to_mb_votes: dict[str, Counter] = defaultdict(Counter)
    for t in tracks.values():
        tag_artist = (t.get("artist") or "").strip()
        mb_id = t.get("musicbrainz_id")
        if not tag_artist or not mb_id:
            continue
        cached = mb_cache.get(mb_id)
        if cached and cached.get("artist"):
            tag_norm_to_mb_votes[normalize_artist(tag_artist)][cached["artist"].strip()] += 1

    # normalized key → set of artist name variants (from NML)
    norm_to_variants: dict[str, set] = defaultdict(set)
    for artist in artist_counts:
        norm_to_variants[normalize_artist(artist)].add(artist)

    # For each group with 2+ variants, pick a canonical name
    canonical_map: dict[str, str] = {}
    for norm_key, variants in norm_to_variants.items():
        if len(variants) < 2:
            continue

        mb_artist_votes = tag_norm_to_mb_votes.get(norm_key, Counter())

        if mb_artist_votes:
            # Use the most-voted MB canonical name — but only if it normalizes
            # to the same key (guards against MB returning "feat." variants etc.)
            for mb_name, _ in mb_artist_votes.most_common():
                if normalize_artist(mb_name) == norm_key:
                    canonical = mb_name
                    break
            else:
                # MB names all have different normalized keys (e.g. feat. suffix)
                canonical = _best_variant(variants, artist_counts)
        else:
            canonical = _best_variant(variants, artist_counts)

        for variant in variants:
            if variant != canonical:
                canonical_map[variant] = canonical

    return canonical_map


def print_artist_report(canonical_map: dict[str, str], artist_counts: Counter):
    print(f"\n  {len(canonical_map)} artist names will be normalized:")
    # Group by canonical
    by_canonical: dict[str, list] = defaultdict(list)
    for old, new in canonical_map.items():
        by_canonical[new].append(old)
    total = sum(artist_counts.get(old, 0) for old in canonical_map)
    print(f"  Tracks affected: {total:,}\n")
    for canonical, olds in sorted(by_canonical.items(),
                                  key=lambda x: -sum(artist_counts.get(o,0) for o in x[1])):
        affected = sum(artist_counts.get(o, 0) for o in olds)
        print(f"  CANONICAL: {canonical!r}  ({affected} tracks)")
        for old in sorted(olds, key=lambda o: -artist_counts.get(o, 0)):
            print(f"    RENAME:  {old!r}  ({artist_counts.get(old,0)} tracks)")


def rewrite_artist_tag(file_path: str, new_artist: str) -> bool:
    """Rewrite the ARTIST tag in an audio file. Returns True on success."""
    try:
        f = MutagenFile(file_path, easy=False)
        if f is None:
            return False
        ext = Path(file_path).suffix.lower()
        if ext == ".mp3":
            tags = ID3(file_path)
            tags["TPE1"] = TPE1(encoding=3, text=new_artist)
            tags.save()
        elif ext in (".m4a", ".aac", ".mp4"):
            tags = MP4(file_path)
            tags["\xa9ART"] = [new_artist]
            tags.save()
        elif ext == ".flac":
            from mutagen.flac import FLAC
            tags = FLAC(file_path)
            tags["artist"] = [new_artist]
            tags.save()
        elif ext in (".ogg", ".opus"):
            from mutagen.oggvorbis import OggVorbis
            tags = OggVorbis(file_path)
            tags["artist"] = [new_artist]
            tags.save()
        else:
            # Try easy=True generic
            tags = MutagenFile(file_path, easy=True)
            if tags is not None:
                tags["artist"] = [new_artist]
                tags.save()
        return True
    except Exception as e:
        print(f"    [WARN] Tag write failed for {file_path}: {e}")
        return False


def apply_artist_normalization(canonical_map: dict[str, str], dry_run: bool = True):
    """
    Apply artist renaming:
      1. Rename Artist-level folders in corrected_music/
      2. Rewrite ARTIST tags in audio files
      3. Update NML ENTRY ARTIST + LOCATION paths
      4. Update state/path_map.json
    """
    if not canonical_map:
        print("  Nothing to normalize.")
        return

    # -----------------------------------------------------------------------
    # Step 1: Rename artist folders in corrected_music/
    # Build old_folder_path → new_folder_path map
    # -----------------------------------------------------------------------
    folder_renames: dict[str, str] = {}  # old abs → new abs
    for old_artist, new_artist in canonical_map.items():
        old_dir = CORRECTED / old_artist
        new_dir = CORRECTED / new_artist
        if old_dir.exists():
            folder_renames[str(old_dir)] = str(new_dir)

    print(f"\n  Step 1: Folder renames in corrected_music/ ({len(folder_renames)} artists)")
    for old, new in sorted(folder_renames.items()):
        old_name = Path(old).name
        new_name = Path(new).name
        print(f"    {old_name!r}  →  {new_name!r}")
        if not dry_run:
            new_path = Path(new)
            if new_path.exists():
                # Merge: move contents of old into existing new
                for item in Path(old).iterdir():
                    dest = new_path / item.name
                    if dest.exists():
                        # Album folder collision — merge album contents
                        if item.is_dir():
                            for track in item.iterdir():
                                track_dest = dest / track.name
                                if not track_dest.exists():
                                    shutil.move(str(track), str(track_dest))
                            try:
                                item.rmdir()
                            except OSError:
                                pass
                    else:
                        shutil.move(str(item), str(dest))
                try:
                    Path(old).rmdir()
                except OSError:
                    pass
            else:
                Path(old).rename(new_path)

    # -----------------------------------------------------------------------
    # Step 2: Build path remapping (old file path → new file path)
    # Any file under an old artist folder now lives under the new artist folder
    # -----------------------------------------------------------------------
    def remap_path(p: str) -> str:
        for old_folder, new_folder in folder_renames.items():
            if p.startswith(old_folder + "/") or p == old_folder:
                return new_folder + p[len(old_folder):]
        return p

    # -----------------------------------------------------------------------
    # Step 3: Rewrite audio file ARTIST tags
    # -----------------------------------------------------------------------
    total_files = 0
    for old_artist, new_artist in canonical_map.items():
        artist_dir = Path(folder_renames.get(
            str(CORRECTED / old_artist),
            str(CORRECTED / new_artist)
        ))
        if not artist_dir.exists():
            continue
        for f in artist_dir.rglob("*"):
            if f.suffix.lower() in (".mp3", ".m4a", ".flac", ".aiff", ".aif", ".ogg", ".opus", ".wav"):
                total_files += 1
                if not dry_run:
                    rewrite_artist_tag(str(f), new_artist)

    print(f"\n  Step 2: Rewrite ARTIST tag in {total_files} audio files" +
          (" (skipped — dry run)" if dry_run else ""))

    # -----------------------------------------------------------------------
    # Step 4: Update NML files
    # -----------------------------------------------------------------------
    nml_files = [NML_SOURCE] + sorted(
        f for f in TRAKTOR_DIR.glob("*.nml")
        if f.name != "collection.nml" and f.is_file()
    )
    print(f"\n  Step 3: Update {len(nml_files)} NML files")

    total_artist_updates = 0
    total_path_updates = 0

    for nml_path in nml_files:
        try:
            tree = ET.parse(nml_path)
        except ET.ParseError as e:
            print(f"    [WARN] Could not parse {nml_path.name}: {e}")
            continue
        root = tree.getroot()
        artist_updates = path_updates = 0

        collection = root.find("COLLECTION")
        if collection is not None:
            for entry in collection.findall("ENTRY"):
                # Update ARTIST attribute
                old_artist = entry.get("ARTIST", "").strip()
                if old_artist in canonical_map:
                    if not dry_run:
                        entry.set("ARTIST", canonical_map[old_artist])
                    artist_updates += 1

                # Update LOCATION path
                old_path = entry_abs_path(entry)
                if old_path:
                    new_path = remap_path(old_path)
                    if new_path != old_path:
                        if not dry_run:
                            loc = entry.find("LOCATION")
                            for k, v in abs_to_traktor_location(new_path).items():
                                loc.set(k, v)
                        path_updates += 1

        playlists = root.find("PLAYLISTS")
        if playlists is not None:
            for node in playlists.iter("ENTRY"):
                pk_el = node.find("PRIMARYKEY")
                if pk_el is None:
                    continue
                old_path = primarykey_to_abs(pk_el.get("KEY", ""))
                new_path = remap_path(old_path)
                if new_path != old_path:
                    if not dry_run:
                        pk_el.set("KEY", abs_to_primarykey(new_path))
                    path_updates += 1

        total_artist_updates += artist_updates
        total_path_updates   += path_updates

        if not dry_run:
            tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
            fix_xml_declaration(nml_path)

        if artist_updates or path_updates:
            print(f"    {nml_path.name}: {artist_updates} ARTIST attrs, {path_updates} paths")

    print(f"    Totals: {total_artist_updates} ARTIST attributes, {total_path_updates} paths updated")

    # -----------------------------------------------------------------------
    # Step 5: Update path_map.json
    # -----------------------------------------------------------------------
    if PATH_MAP_JSON.exists():
        path_map = json.loads(PATH_MAP_JSON.read_text())
        new_path_map = {}
        pm_updates = 0
        for src, dst in path_map.items():
            new_src = remap_path(src)
            new_dst = remap_path(dst)
            new_path_map[new_src] = new_dst
            if new_src != src or new_dst != dst:
                pm_updates += 1
        print(f"\n  Step 4: path_map.json — {pm_updates} entries updated")
        if not dry_run:
            PATH_MAP_JSON.write_text(json.dumps(new_path_map, ensure_ascii=False))

    if dry_run:
        print("\n  Dry-run complete — no changes made.")
        print("  Run with --apply-artists to apply.")
    else:
        print("\n  Artist normalization applied.")


# ---------------------------------------------------------------------------
# GENRE SYNC — Fetch phase
# ---------------------------------------------------------------------------

async def fetch_genres_async(limit: int | None):
    """Fetch genre data from MB for unique recording IDs. Saves to mb_genre_cache.json."""

    # Load what we need
    metadata = json.loads(METADATA_JSON.read_text())
    mb_genre_cache: dict = {}
    if MB_GENRE_CACHE.exists():
        try:
            mb_genre_cache = json.loads(MB_GENRE_CACHE.read_text())
        except Exception:
            pass

    # Collect unique MB recording IDs not yet in genre cache
    tracks = metadata.get("tracks", {})
    unique_ids = {
        t["musicbrainz_id"]
        for t in tracks.values()
        if t.get("musicbrainz_id") and t["musicbrainz_id"] not in mb_genre_cache
    }

    to_fetch = sorted(unique_ids)
    if limit:
        to_fetch = to_fetch[:limit]

    already_cached = sum(1 for t in tracks.values()
                        if t.get("musicbrainz_id") and t["musicbrainz_id"] in mb_genre_cache)
    print(f"  MB recording IDs needing genre fetch: {len(to_fetch):,}")
    print(f"  Already cached: {already_cached:,}")
    if not to_fetch:
        print("  Nothing to fetch.")
        return

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_request = [0.0]
    fetched = errors = 0

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as session:
        for mb_id in tqdm(to_fetch, desc="  Fetching genres"):
            # Rate limit
            elapsed = time.monotonic() - last_request[0]
            if elapsed < RATE_LIMIT:
                await asyncio.sleep(RATE_LIMIT - elapsed)
            last_request[0] = time.monotonic()

            url = f"{MB_BASE}/recording/{mb_id}"
            params = {"inc": "genres+tags", "fmt": "json"}

            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 60))
                        print(f"\n  Rate limited — sleeping {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 404:
                        mb_genre_cache[mb_id] = None
                        continue
                    if resp.status != 200:
                        errors += 1
                        continue
                    data = await resp.json()
                    # Extract genres (curated) — fall back to tags (folksonomy)
                    genres = data.get("genres") or []
                    tags   = data.get("tags") or []
                    # genres: [{"name": "...", "count": N}, ...]
                    # tags:   [{"name": "...", "count": N}, ...]
                    genre_name = None
                    if genres:
                        best = max(genres, key=lambda g: g.get("count", 0))
                        genre_name = best["name"].title()
                    elif tags:
                        # Filter out non-genre tags (decades, countries, etc.)
                        SKIP = {"seen live", "albums i own", "favorite", "favourites",
                                "check in", "loved", "wishlist"}
                        music_tags = [t for t in tags
                                      if t["name"].lower() not in SKIP
                                      and not t["name"].isdigit()
                                      and len(t["name"]) > 2]
                        if music_tags:
                            best = max(music_tags, key=lambda t: t.get("count", 0))
                            genre_name = best["name"].title()
                    mb_genre_cache[mb_id] = genre_name
                    fetched += 1

            except (aiohttp.ClientError, asyncio.TimeoutError):
                errors += 1

            # Save cache periodically
            if (fetched + errors) % 500 == 0 and fetched + errors > 0:
                MB_GENRE_CACHE.write_text(json.dumps(mb_genre_cache, ensure_ascii=False, indent=2))

    MB_GENRE_CACHE.write_text(json.dumps(mb_genre_cache, ensure_ascii=False, indent=2))
    print(f"\n  Fetched: {fetched:,} | Errors: {errors:,} | Cache size: {len(mb_genre_cache):,}")
    print(f"  Genre cache → {MB_GENRE_CACHE}")


# ---------------------------------------------------------------------------
# GENRE SYNC — Apply phase
# ---------------------------------------------------------------------------

def rewrite_genre_tag(file_path: str, genre: str) -> bool:
    try:
        f = MutagenFile(file_path, easy=False)
        if f is None:
            return False
        ext = Path(file_path).suffix.lower()
        if ext == ".mp3":
            tags = ID3(file_path)
            tags["TCON"] = TCON(encoding=3, text=genre)
            tags.save()
        elif ext in (".m4a", ".aac", ".mp4"):
            tags = MP4(file_path)
            tags["\xa9gen"] = [genre]
            tags.save()
        elif ext == ".flac":
            from mutagen.flac import FLAC
            tags = FLAC(file_path)
            tags["genre"] = [genre]
            tags.save()
        elif ext in (".ogg", ".opus"):
            from mutagen.oggvorbis import OggVorbis
            tags = OggVorbis(file_path)
            tags["genre"] = [genre]
            tags.save()
        else:
            tags = MutagenFile(file_path, easy=True)
            if tags is not None:
                tags["genre"] = [genre]
                tags.save()
        return True
    except Exception as e:
        print(f"    [WARN] Genre tag write failed for {file_path}: {e}")
        return False


def apply_genres(dry_run: bool = True):
    """Apply genre data from mb_genre_cache.json to files and NML."""
    if not MB_GENRE_CACHE.exists():
        print("  No mb_genre_cache.json found. Run --fetch-genres first.")
        return

    mb_genre_cache = json.loads(MB_GENRE_CACHE.read_text())
    metadata = json.loads(METADATA_JSON.read_text())
    tracks = metadata.get("tracks", {})

    # Build path → genre
    path_to_genre: dict[str, str] = {}
    for t in tracks.values():
        mb_id = t.get("musicbrainz_id")
        if not mb_id:
            continue
        genre = mb_genre_cache.get(mb_id)
        if genre:
            path_to_genre[t["path"]] = genre

    # Build path_map new→genre for corrected_music files
    path_map = json.loads(PATH_MAP_JSON.read_text()) if PATH_MAP_JSON.exists() else {}
    # path_map: old_path → new corrected_music path
    new_path_to_genre: dict[str, str] = {}
    for old_path, new_path in path_map.items():
        genre = path_to_genre.get(old_path)
        if genre:
            new_path_to_genre[new_path] = genre

    # Also handle tracks whose path IS already the corrected_music path
    for path, genre in path_to_genre.items():
        if str(CORRECTED) in path:
            new_path_to_genre[path] = genre

    print(f"  Tracks with genre data: {len(new_path_to_genre):,}")

    # Count genre distribution
    genre_counts = Counter(new_path_to_genre.values())
    print(f"  Top 15 genres:")
    for genre, count in genre_counts.most_common(15):
        print(f"    {count:5d}  {genre}")

    if dry_run:
        print(f"\n  Dry-run — no changes. Run with --apply-genres to apply.")

        # Write report
        report = {
            "total_tracks_with_genre": len(new_path_to_genre),
            "genre_distribution": dict(genre_counts.most_common()),
        }
        GENRE_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"  Report → {GENRE_REPORT_JSON}")
        return

    # Apply: rewrite file tags
    print(f"\n  Writing genre tags to audio files...")
    tagged = errors = 0
    for path, genre in tqdm(new_path_to_genre.items(), desc="  Tagging"):
        if os.path.exists(path):
            if rewrite_genre_tag(path, genre):
                tagged += 1
            else:
                errors += 1
    print(f"  Tagged: {tagged:,} | Errors: {errors:,}")

    # Apply: update NML ENTRY INFO GENRE attributes
    print(f"\n  Updating NML GENRE attributes in {NML_SOURCE.name}...")
    tree = ET.parse(NML_SOURCE)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    nml_updated = 0
    if collection is not None:
        for entry in collection.findall("ENTRY"):
            path = entry_abs_path(entry)
            if not path:
                continue
            genre = new_path_to_genre.get(path)
            if not genre:
                continue
            info = entry.find("INFO")
            if info is None:
                info = ET.SubElement(entry, "INFO")
            info.set("GENRE", genre)
            nml_updated += 1
    tree.write(str(NML_SOURCE), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(NML_SOURCE)
    print(f"  {nml_updated:,} NML entries updated with genre")

    report = {
        "total_tracks_with_genre": len(new_path_to_genre),
        "files_tagged": tagged,
        "nml_entries_updated": nml_updated,
        "genre_distribution": dict(genre_counts.most_common()),
    }
    GENRE_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  Genre sync complete → {GENRE_REPORT_JSON}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Artist normalization + genre sync")
    parser.add_argument("--report",        action="store_true",
                        help="Artist normalization dry-run report")
    parser.add_argument("--apply-artists", action="store_true",
                        help="Apply artist normalization (folder renames + tags + NML)")
    parser.add_argument("--fetch-genres",  action="store_true",
                        help="Fetch genre data from MusicBrainz (slow, resumable)")
    parser.add_argument("--limit",         type=int, default=0,
                        help="Max MB recordings to fetch this run (0 = all)")
    parser.add_argument("--apply-genres",  action="store_true",
                        help="Apply genres to file tags + NML (dry-run unless --apply-genres-write)")
    parser.add_argument("--write",         action="store_true",
                        help="Combined with --apply-genres: actually write changes")
    args = parser.parse_args()

    if not any([args.report, args.apply_artists, args.fetch_genres, args.apply_genres]):
        parser.print_help()
        return

    # Load shared data
    metadata = json.loads(METADATA_JSON.read_text()) if METADATA_JSON.exists() else {}
    mb_cache = json.loads(MB_CACHE_JSON.read_text()) if MB_CACHE_JSON.exists() else {}

    # Artist normalization
    if args.report or args.apply_artists:
        print("Stage 8b: Building artist canonical map...")
        canonical_map = build_canonical_map(NML_SOURCE, metadata, mb_cache)

        # Get artist counts from NML for report
        tree = ET.parse(NML_SOURCE)
        artist_counts: Counter = Counter(
            entry.get("ARTIST", "").strip()
            for entry in tree.getroot().find("COLLECTION").findall("ENTRY")
            if entry.get("ARTIST", "").strip()
        )

        print_artist_report(canonical_map, artist_counts)

        # Save report
        by_canonical: dict[str, list] = defaultdict(list)
        for old, new in canonical_map.items():
            by_canonical[new].append(old)
        report = {
            "total_variant_groups": len(by_canonical),
            "total_names_to_rename": len(canonical_map),
            "total_tracks_affected": sum(artist_counts.get(o, 0) for o in canonical_map),
            "groups": [
                {
                    "canonical": canon,
                    "variants": sorted(olds, key=lambda o: -artist_counts.get(o, 0)),
                    "track_counts": {o: artist_counts.get(o, 0) for o in olds},
                }
                for canon, olds in sorted(by_canonical.items(),
                                          key=lambda x: -sum(artist_counts.get(o,0) for o in x[1]))
            ],
        }
        STATE_DIR.mkdir(exist_ok=True)
        ARTIST_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n  Full report → {ARTIST_REPORT_JSON}")

        if args.apply_artists:
            print("\n  Applying artist normalization...")
            apply_artist_normalization(canonical_map, dry_run=False)
        else:
            apply_artist_normalization(canonical_map, dry_run=True)

    # Genre fetch
    if args.fetch_genres:
        print("Stage 8b: Fetching genres from MusicBrainz...")
        limit = args.limit if args.limit > 0 else None
        asyncio.run(fetch_genres_async(limit))

    # Genre apply
    if args.apply_genres:
        print("Stage 8b: Genre sync...")
        apply_genres(dry_run=not args.write)


if __name__ == "__main__":
    main()
