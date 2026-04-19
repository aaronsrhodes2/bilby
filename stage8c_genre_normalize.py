#!/usr/bin/env python3
"""
Stage 8c — Genre Normalization

Reduces ~1,043 genre strings (many multi-value slash-separated) down to a
canonical taxonomy of ~25 genres appropriate for a goth/industrial/alternative
collection. Each track gets exactly one canonical genre.

Resolution priority per track:
  1. MusicBrainz genre (from state/mb_genre_cache.json) → mapped to canonical
  2. Existing tag if already a known canonical → keep
  3. Existing multi-value tag → split on "/" → map each → pick highest-priority
  4. Existing single non-canonical tag → map via GENRE_MAP
  5. No match → keep existing unchanged

Dry-run by default.

Reads:  corrected_traktor/collection.nml
        state/mb_genre_cache.json  (from stage8b --fetch-genres, may be partial)
        state/metadata.json        (path → mb_recording_id)
Writes: state/genre_normalization_report.json
        corrected_traktor/collection.nml  (modified in-place, with --apply)
        audio file GENRE tags             (with --apply)

Usage:
    .venv/bin/python stage8c_genre_normalize.py --report
    .venv/bin/python stage8c_genre_normalize.py --apply
"""

import argparse
import json
import os
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TCON
from mutagen.mp4 import MP4
from tqdm import tqdm

import sys
sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

PROJECT        = Path(__file__).parent
STATE_DIR      = PROJECT / "state"
TRAKTOR_DIR    = PROJECT / "corrected_traktor"
NML_SOURCE     = TRAKTOR_DIR / "collection.nml"
METADATA_JSON  = STATE_DIR / "metadata.json"
MB_GENRE_CACHE = STATE_DIR / "mb_genre_cache.json"
REPORT_JSON    = STATE_DIR / "genre_normalization_report.json"

ET.register_namespace("", "")

# ---------------------------------------------------------------------------
# TAXONOMY — ordered most-specific → least-specific.
# When a multi-value genre maps to multiple canonicals, the one with the
# LOWEST index wins (most specific).
# ---------------------------------------------------------------------------
TAXONOMY = [
    "Deathrock",
    "Gothic Rock",
    "Darkwave",
    "Coldwave",
    "Post-Punk",
    "EBM",
    "Industrial",
    "New Wave",
    "Synthpop",
    "Ambient",
    "IDM",
    "Electronic",
    "Alternative Rock",
    "Indie Rock",
    "Punk",
    "Metal",
    "Hard Rock",
    "Classic Rock",
    "Rock",
    "Folk",
    "Pop",
    "Hip-Hop",
    "Soundtrack",
    "Comedy",
    "Other",
]

TAXONOMY_SET   = set(TAXONOMY)
TAXONOMY_INDEX = {g: i for i, g in enumerate(TAXONOMY)}

# ---------------------------------------------------------------------------
# GENRE_MAP — case-insensitive lookup: raw string → canonical
# Covers both multi-value compound strings (exact match takes priority over
# split-and-resolve) and single-value strings.
# ---------------------------------------------------------------------------
_RAW_MAP = {
    # --- EBM family ---
    "ebm": "EBM",
    "e.b.m.": "EBM",
    "aggrotech": "EBM",
    "futurepop": "EBM",
    "dark electro": "EBM",
    "dark electro / ebm": "EBM",
    "electro-industrial": "EBM",
    "electro industrial": "EBM",
    "harsh ebm": "EBM",
    "neo-folk / ebm": "EBM",
    "ebm / electronic": "EBM",
    "ebm / electro": "EBM",
    "ebm / electro / electronic": "EBM",
    "ebm / electro / electronic / industrial": "EBM",
    "ebm / electro / electronic / industrial / synth-pop": "EBM",
    "ebm / electronic / industrial": "EBM",
    "ebm / electronic": "EBM",
    "ebm / electronic / synth-pop": "EBM",
    "ebm / synth-pop": "EBM",
    # --- Industrial family ---
    "industrial": "Industrial",
    "industrial rock": "Industrial",
    "power electronics": "Industrial",
    "noise": "Industrial",
    "power noise": "Industrial",
    "rhythmic noise": "Industrial",
    "harsh noise": "Industrial",
    "noise rock": "Industrial",
    "industrial / rock": "Industrial",
    "electronic / industrial": "Industrial",
    "electro / electronic / industrial": "Industrial",
    "electronic / industrial / rock": "Industrial",
    "breakbeat / electronic / hard rock / industrial / rock": "Industrial",
    "electronic / industrial / metal": "Industrial",
    "industrial / metal": "Industrial",
    "industrial metal": "Industrial",
    "industrial / electronic": "Industrial",
    "noise / industrial": "Industrial",
    # --- Darkwave / Gothic ---
    "darkwave": "Darkwave",
    "dark wave": "Darkwave",
    "dark wave / post-punk": "Darkwave",
    "dark wave / synth-pop": "Darkwave",
    "dark wave / electronic": "Darkwave",
    "dark wave / gothic rock": "Gothic Rock",
    "gothic": "Gothic Rock",
    "goth": "Gothic Rock",
    "gothic rock": "Gothic Rock",
    "goth rock": "Gothic Rock",
    "gothic metal": "Gothic Rock",
    "gothic / darkwave": "Gothic Rock",
    "deathrock": "Deathrock",
    "death rock": "Deathrock",
    "coldwave": "Coldwave",
    "cold wave": "Coldwave",
    "minimal wave": "Coldwave",
    # --- Post-Punk ---
    "post-punk": "Post-Punk",
    "post punk": "Post-Punk",
    "post-punk revival": "Post-Punk",
    "post-punk / new wave": "Post-Punk",
    "post-punk / rock": "Post-Punk",
    "post-punk / gothic rock": "Gothic Rock",
    "post-punk / darkwave": "Darkwave",
    # --- New Wave ---
    "new wave": "New Wave",
    "nw/rock": "New Wave",
    "new wave / rock": "New Wave",
    "new wave / synth-pop": "New Wave",
    "new wave / pop": "New Wave",
    "new wave / electronic": "New Wave",
    "new wave / post-punk": "Post-Punk",
    "new wave / punk": "Punk",
    "new wave / alternative rock": "Alternative Rock",
    # --- Synthpop ---
    "synth-pop": "Synthpop",
    "synthpop": "Synthpop",
    "synth pop": "Synthpop",
    "electronic / synth-pop": "Synthpop",
    "electro-pop": "Synthpop",
    "electropop": "Synthpop",
    "synthwave": "Synthpop",
    "retrowave": "Synthpop",
    "darksynth": "Synthpop",
    "ebm / electronic / synth-pop": "EBM",
    "electro / electronic / industrial / synth-pop": "EBM",
    # --- Electronic ---
    "electronic": "Electronic",
    "electro": "Electronic",
    "electronica": "Electronic",
    "breakbeat": "Electronic",
    "breaks": "Electronic",
    "chiptune": "Electronic",
    "electronic / dance": "Electronic",
    "disco": "Electronic",
    "house": "Electronic",
    "techno": "Electronic",
    "trance": "Electronic",
    "drum and bass": "Electronic",
    "jungle": "Electronic",
    "trip-hop": "Electronic",
    "trip hop": "Electronic",
    "downtempo": "Ambient",
    "psybient": "Ambient",
    "chillout": "Ambient",
    "chill": "Ambient",
    # --- Ambient ---
    "ambient": "Ambient",
    "dark ambient": "Ambient",
    "space ambient": "Ambient",
    "ambient / electronic": "Ambient",
    "ambient / drone": "Ambient",
    "drone": "Ambient",
    "experimental": "Ambient",
    "neo-classical": "Ambient",
    "neoclassical": "Ambient",
    "spoken word": "Other",
    # --- IDM ---
    "idm": "IDM",
    "intelligent dance music": "IDM",
    "glitch": "IDM",
    "abstract": "IDM",
    # --- Alternative Rock ---
    "alternative": "Alternative Rock",
    "alternative rock": "Alternative Rock",
    "alt. rock": "Alternative Rock",
    "alt rock": "Alternative Rock",
    "alternative rock / rock": "Alternative Rock",
    "alternative rock / indie rock": "Alternative Rock",
    "alternative metal": "Alternative Rock",
    "art rock": "Alternative Rock",
    "alternative / rock": "Alternative Rock",
    "grunge": "Alternative Rock",
    "shoegaze": "Alternative Rock",
    "dream pop": "Alternative Rock",
    "sadcore": "Alternative Rock",
    "slowcore": "Alternative Rock",
    # --- Indie Rock ---
    "indie": "Indie Rock",
    "indie rock": "Indie Rock",
    "indie rock / rock": "Indie Rock",
    "indie rock / alternative rock": "Alternative Rock",
    "indie pop": "Indie Rock",
    "lo-fi": "Indie Rock",
    "post-rock": "Indie Rock",
    "math rock": "Indie Rock",
    "chamber pop": "Indie Rock",
    # --- Punk ---
    "punk": "Punk",
    "punk rock": "Punk",
    "hardcore punk": "Punk",
    "hardcore": "Punk",
    "emo": "Punk",
    "skate punk": "Punk",
    "electronic / punk": "Punk",
    "punk / rock": "Punk",
    # --- Metal ---
    "metal": "Metal",
    "heavy metal": "Metal",
    "death metal": "Metal",
    "black metal": "Metal",
    "thrash metal": "Metal",
    "metalcore": "Metal",
    "deathcore": "Metal",
    "doom metal": "Metal",
    "nu-metal": "Metal",
    "nu metal": "Metal",
    "prog metal": "Metal",
    "progressive metal": "Metal",
    "speed metal": "Metal",
    "symphonic metal": "Metal",
    # --- Hard Rock ---
    "hard rock": "Hard Rock",
    "glam rock": "Hard Rock",
    "glam metal": "Hard Rock",
    "hair metal": "Hard Rock",
    "southern rock": "Hard Rock",
    # --- Classic Rock ---
    "classic rock": "Classic Rock",
    "psychedelic rock": "Classic Rock",
    "psychedelic": "Classic Rock",
    "progressive rock": "Classic Rock",
    "prog rock": "Classic Rock",
    "blues rock": "Classic Rock",
    "blues": "Classic Rock",
    "rockabilly": "Classic Rock",
    # --- Rock ---
    "rock": "Rock",
    "pop/rock": "Rock",
    "pop rock": "Rock",
    "j-rock": "Rock",
    "j rock": "Rock",
    "visual kei": "Rock",
    "jrock": "Rock",
    # --- Folk ---
    "folk": "Folk",
    "folk rock": "Folk",
    "singer-songwriter": "Folk",
    "acoustic": "Folk",
    "americana": "Folk",
    "country": "Folk",
    "neo-folk": "Folk",
    "neofolk": "Folk",
    # --- Pop ---
    "pop": "Pop",
    "dance pop": "Pop",
    "teen pop": "Pop",
    "j-pop": "Pop",
    "jpop": "Pop",
    "k-pop": "Pop",
    "kpop": "Pop",
    "electropop": "Pop",
    # --- Hip-Hop ---
    "hip-hop": "Hip-Hop",
    "hip hop": "Hip-Hop",
    "rap": "Hip-Hop",
    "trap": "Hip-Hop",
    # --- Soundtrack ---
    "soundtrack": "Soundtrack",
    "score": "Soundtrack",
    "game": "Soundtrack",
    "video game": "Soundtrack",
    "anime": "Soundtrack",
    # --- Comedy ---
    "comedy": "Comedy",
    "parody": "Comedy",
    "novelty": "Comedy",
    # --- Other ---
    "other": "Other",
    "unknown": "Other",
    "miscellaneous": "Other",
    "dj mixes": "Other",
    "world": "Other",
    "world music": "Other",
    "reggae": "Other",
    "ska": "Other",
    "latin": "Other",
    "jazz": "Other",
    "soul": "Other",
    "r&b": "Other",
    "rnb": "Other",
    "funk": "Other",
    "gospel": "Other",
    "spiritual": "Other",
    "new age": "Ambient",
    "meditation": "Ambient",
}

# Build case-insensitive lookup
GENRE_MAP: dict[str, str] = {k.lower().strip(): v for k, v in _RAW_MAP.items()}


def resolve_genre(raw: str) -> str | None:
    """
    Map a raw genre string (possibly multi-value) to a canonical genre.
    Returns None if no mapping found.
    """
    if not raw:
        return None
    clean = raw.strip()

    # 1. Direct lookup (handles exact multi-value strings too)
    mapped = GENRE_MAP.get(clean.lower())
    if mapped:
        return mapped

    # 2. Already a known canonical → keep
    if clean in TAXONOMY_SET:
        return clean

    # 3. Multi-value: split on "/" and resolve each component
    if "/" in clean:
        parts = [p.strip() for p in clean.split("/")]
        canonicals = []
        for part in parts:
            m = GENRE_MAP.get(part.lower())
            if m:
                canonicals.append(m)
            elif part in TAXONOMY_SET:
                canonicals.append(part)
        if canonicals:
            # Pick highest-priority (lowest index) canonical
            return min(canonicals, key=lambda g: TAXONOMY_INDEX.get(g, 999))

    # 4. Try stripping and re-mapping individual words
    # e.g. "Alternative Metal / Heavy Metal" might split into mapped pieces

    return None


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
    return traktor_to_abs(
        loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
    )


def rewrite_genre_tag(file_path: str, genre: str) -> bool:
    try:
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
            f = MutagenFile(file_path, easy=True)
            if f is not None:
                f["genre"] = [genre]
                f.save()
        return True
    except Exception as e:
        return False


def main():
    parser = argparse.ArgumentParser(description="Genre normalization")
    parser.add_argument("--report", action="store_true",
                        help="Dry-run: show proposed changes")
    parser.add_argument("--apply", action="store_true",
                        help="Apply canonical genres to NML + file tags")
    args = parser.parse_args()

    if not args.report and not args.apply:
        parser.print_help()
        return

    # -----------------------------------------------------------------------
    # Load support data
    # -----------------------------------------------------------------------
    metadata = json.loads(METADATA_JSON.read_text()) if METADATA_JSON.exists() else {}
    tracks   = metadata.get("tracks", {})

    mb_genre_cache: dict = {}
    if MB_GENRE_CACHE.exists():
        mb_genre_cache = json.loads(MB_GENRE_CACHE.read_text())
        with_genre = sum(1 for v in mb_genre_cache.values() if v)
        print(f"  MB genre cache: {len(mb_genre_cache):,} entries, {with_genre:,} with genre")
    else:
        print("  No MB genre cache — using existing tags only")

    # Build path → mb_genre (best canonical available from MB)
    path_to_mb_genre: dict[str, str] = {}
    for t in tracks.values():
        mb_id = t.get("musicbrainz_id")
        if not mb_id:
            continue
        raw_mb_genre = mb_genre_cache.get(mb_id)
        if not raw_mb_genre:
            continue
        canonical = resolve_genre(raw_mb_genre)
        if canonical:
            path_to_mb_genre[t["path"]] = canonical

    print(f"  Tracks with MB-derived canonical genre: {len(path_to_mb_genre):,}")

    # -----------------------------------------------------------------------
    # Parse NML
    # -----------------------------------------------------------------------
    print(f"\nParsing {NML_SOURCE.name}...")
    tree = ET.parse(NML_SOURCE)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    entries = list(collection.findall("ENTRY"))
    print(f"  {len(entries):,} entries")

    # -----------------------------------------------------------------------
    # Resolve genre for each entry
    # -----------------------------------------------------------------------
    changes: list[dict] = []       # {path, old_genre, new_genre, source}
    no_change: int = 0
    unmapped: Counter = Counter()

    for entry in entries:
        path = entry_abs_path(entry)
        info = entry.find("INFO")
        old_genre = (info.get("GENRE", "") if info is not None else "").strip()

        # Priority 1: MB genre
        new_genre = path_to_mb_genre.get(path) if path else None
        source = "mb"

        # Priority 2+: existing tag
        if not new_genre:
            new_genre = resolve_genre(old_genre)
            source = "tag"

        if new_genre and new_genre != old_genre:
            changes.append({
                "path":      path or "",
                "artist":    entry.get("ARTIST", ""),
                "title":     entry.get("TITLE", ""),
                "old_genre": old_genre,
                "new_genre": new_genre,
                "source":    source,
            })
        elif not new_genre and old_genre:
            unmapped[old_genre] += 1
            no_change += 1
        else:
            no_change += 1

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------
    total_after: Counter = Counter()
    # tally what the final genre distribution would look like
    for entry in entries:
        path = entry_abs_path(entry)
        info = entry.find("INFO")
        old_genre = (info.get("GENRE", "") if info is not None else "").strip()
        new_genre = path_to_mb_genre.get(path) if path else None
        if not new_genre:
            new_genre = resolve_genre(old_genre)
        final = new_genre or old_genre or "(none)"
        total_after[final] += 1

    print(f"\n  Changes: {len(changes):,} tracks will get a new canonical genre")
    print(f"  No change: {no_change:,} tracks")
    print(f"  Unmapped (kept as-is): {len(unmapped):,} distinct genres across tracks\n")

    print(f"  Projected genre distribution after normalization:")
    for genre, count in sorted(total_after.items(), key=lambda x: -x[1])[:35]:
        marker = " ✓" if genre in TAXONOMY_SET else ""
        print(f"    {count:5d}  {genre}{marker}")

    if unmapped:
        print(f"\n  Top unmapped genres (will be kept unchanged):")
        for genre, count in unmapped.most_common(20):
            print(f"    {count:4d}x  {genre!r}")

    # Write report
    report = {
        "total_changes":   len(changes),
        "total_no_change": no_change,
        "unmapped_count":  sum(unmapped.values()),
        "genre_distribution_after": dict(total_after.most_common()),
        "top_unmapped":    dict(unmapped.most_common(50)),
        "changes":         changes[:500],   # cap for file size
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n  Report → {REPORT_JSON}")

    if not args.apply:
        print("\nDry-run complete. Run with --apply to write changes.")
        return

    # -----------------------------------------------------------------------
    # Apply: update NML GENRE attributes
    # -----------------------------------------------------------------------
    print(f"\nApplying genre changes to NML...")
    nml_updated = 0
    for entry in tqdm(entries, desc="  NML"):
        path = entry_abs_path(entry)
        info = entry.find("INFO")
        old_genre = (info.get("GENRE", "") if info is not None else "").strip()

        new_genre = path_to_mb_genre.get(path) if path else None
        if not new_genre:
            new_genre = resolve_genre(old_genre)

        if new_genre and new_genre != old_genre:
            if info is None:
                info = ET.SubElement(entry, "INFO")
            info.set("GENRE", new_genre)
            nml_updated += 1

    tree.write(str(NML_SOURCE), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(NML_SOURCE)
    print(f"  {nml_updated:,} NML entries updated → {NML_SOURCE.name}")

    # -----------------------------------------------------------------------
    # Apply: rewrite file tags
    # -----------------------------------------------------------------------
    print(f"\nApplying genre tags to audio files...")
    tagged = errors = skipped = 0
    for ch in tqdm(changes, desc="  Files"):
        path = ch["path"]
        if not path or not os.path.exists(path):
            skipped += 1
            continue
        if rewrite_genre_tag(path, ch["new_genre"]):
            tagged += 1
        else:
            errors += 1

    print(f"  Tagged: {tagged:,} | Skipped (not on disk): {skipped:,} | Errors: {errors:,}")
    print(f"\nStage 8c complete.")
    print(f"  Final genre count: {len(total_after)} unique genres in collection")


if __name__ == "__main__":
    main()
