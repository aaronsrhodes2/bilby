#!/usr/bin/env python3
"""
Fix 'Other' genre tracks using Last.fm artist tag lookup.

Reads:  corrected_traktor/collection.nml
Writes: state/lastfm_genre_cache.json    — per-artist lookup cache
        state/other_genre_report.json    — proposed changes
        corrected_traktor/collection.nml — (with --apply)
        audio file GENRE tags            — (with --apply)

Usage:
    python3 tools/fix_other_genres.py --report
    python3 tools/fix_other_genres.py --report --verbose
    python3 tools/fix_other_genres.py --apply
"""

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent.parent
NML_PATH    = BASE / "corrected_traktor" / "collection.nml"
CACHE_FILE  = BASE / "state" / "lastfm_genre_cache.json"
REPORT_FILE = BASE / "state" / "other_genre_report.json"
CREDS_FILE  = BASE / "lastfm_creds.txt"

# ── Last.fm ───────────────────────────────────────────────────────────────────
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"

def load_api_key() -> str:
    if not CREDS_FILE.exists():
        print("ERROR: lastfm_creds.txt not found")
        sys.exit(1)
    for line in CREDS_FILE.read_text().splitlines():
        if line.startswith("api_key="):
            return line.split("=", 1)[1].strip()
    print("ERROR: api_key not found in lastfm_creds.txt")
    sys.exit(1)


def lastfm_top_tags(artist: str, api_key: str) -> list[str]:
    """Return list of lowercase tag names for the artist (up to 10)."""
    url = (f"{LASTFM_URL}?method=artist.gettoptags"
           f"&artist={quote(artist)}&api_key={api_key}&format=json")
    try:
        with urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        tags = data.get("toptags", {}).get("tag", [])
        return [t["name"].lower().strip() for t in tags if int(t.get("count", 0)) >= 5]
    except Exception:
        return []


# ── Genre mapping ─────────────────────────────────────────────────────────────
# Ordered priority list: earlier entries win when multiple tags match
TAXONOMY = [
    "Deathrock", "Gothic Rock", "Darkwave", "Coldwave", "Post-Punk",
    "EBM", "Industrial", "New Wave", "Synthpop", "Ambient", "IDM",
    "Electronic", "Alternative Rock", "Indie Rock", "Punk", "Metal",
    "Hard Rock", "Classic Rock", "Rock", "Folk", "Pop", "Hip-Hop",
    "Soundtrack", "Comedy", "Other",
]
TAXONOMY_INDEX = {g: i for i, g in enumerate(TAXONOMY)}

_TAG_MAP = {
    # EBM / Industrial
    "ebm":                     "EBM",
    "e.b.m.":                  "EBM",
    "electronic body music":   "EBM",
    "aggrotech":               "EBM",
    "futurepop":               "EBM",
    "dark electro":            "EBM",
    "electro-industrial":      "EBM",
    "electro industrial":      "EBM",
    "harsh ebm":               "EBM",
    "industrial":              "Industrial",
    "industrial rock":         "Industrial",
    "industrial metal":        "Industrial",
    "power electronics":       "Industrial",
    "power noise":             "Industrial",
    "rhythmic noise":          "Industrial",
    "harsh noise":             "Industrial",
    "noise":                   "Industrial",
    "noise industrial":        "Industrial",
    "dark industrial":         "Industrial",
    # Darkwave / Goth
    "darkwave":                "Darkwave",
    "dark wave":               "Darkwave",
    "gothic rock":             "Gothic Rock",
    "goth rock":               "Gothic Rock",
    "gothic":                  "Gothic Rock",
    "goth":                    "Gothic Rock",
    "gothic metal":            "Gothic Rock",
    "deathrock":               "Deathrock",
    "death rock":              "Deathrock",
    "coldwave":                "Coldwave",
    "cold wave":               "Coldwave",
    "minimal wave":            "Coldwave",
    # Post-Punk / New Wave
    "post-punk":               "Post-Punk",
    "post punk":               "Post-Punk",
    "post-punk revival":       "Post-Punk",
    "new wave":                "New Wave",
    # Synthpop
    "synth-pop":               "Synthpop",
    "synthpop":                "Synthpop",
    "synth pop":               "Synthpop",
    "electropop":              "Synthpop",
    "electro-pop":             "Synthpop",
    "synthwave":               "Synthpop",
    "retrowave":               "Synthpop",
    "darksynth":               "Synthpop",
    # Electronic
    "electronic":              "Electronic",
    "electronica":             "Electronic",
    "electro":                 "Electronic",
    "idm":                     "IDM",
    "intelligent dance music": "IDM",
    "ambient techno":          "IDM",
    "breakbeat":               "Electronic",
    "downtempo":               "Electronic",
    "trip-hop":                "Electronic",
    "trip hop":                "Electronic",
    "disco":                   "Electronic",
    "house":                   "Electronic",
    "techno":                  "Electronic",
    "trance":                  "Electronic",
    "drum and bass":           "Electronic",
    "drum & bass":             "Electronic",
    # Ambient
    "ambient":                 "Ambient",
    "dark ambient":            "Ambient",
    "space ambient":           "Ambient",
    "drone":                   "Ambient",
    "experimental":            "Ambient",
    "neo-classical":           "Ambient",
    "neoclassical":            "Ambient",
    "new age":                 "Ambient",
    "meditation":              "Ambient",
    "chillout":                "Ambient",
    "downtempo":               "Ambient",
    "psybient":                "Ambient",
    "soundtrack":              "Soundtrack",
    "score":                   "Soundtrack",
    "film score":              "Soundtrack",
    "ost":                     "Soundtrack",
    "instrumental":            "Soundtrack",
    # Rock family
    "alternative rock":        "Alternative Rock",
    "alternative":             "Alternative Rock",
    "indie rock":              "Indie Rock",
    "indie":                   "Indie Rock",
    "shoegaze":                "Alternative Rock",
    "dream pop":               "Alternative Rock",
    "grunge":                  "Alternative Rock",
    "punk":                    "Punk",
    "punk rock":               "Punk",
    "hardcore punk":           "Punk",
    "hardcore":                "Punk",
    "ska punk":                "Punk",
    "metal":                   "Metal",
    "heavy metal":             "Metal",
    "death metal":             "Metal",
    "black metal":             "Metal",
    "thrash metal":            "Metal",
    "doom metal":              "Metal",
    "power metal":             "Metal",
    "progressive metal":       "Metal",
    "symphonic metal":         "Metal",
    "gothic metal":            "Gothic Rock",
    "hard rock":               "Hard Rock",
    "classic rock":            "Classic Rock",
    "rock":                    "Rock",
    # Folk / neofolk
    "folk":                    "Folk",
    "neofolk":                 "Folk",
    "neo-folk":                "Folk",
    "folk rock":               "Folk",
    "acoustic":                "Folk",
    "medieval":                "Folk",
    # Pop
    "pop":                     "Pop",
    "dance pop":               "Pop",
    "j-pop":                   "Pop",
    "k-pop":                   "Pop",
    "funk":                    "Pop",
    "r&b":                     "Pop",
    "soul":                    "Pop",
    "jazz":                    "Pop",
    # Hip-Hop
    "hip-hop":                 "Hip-Hop",
    "hip hop":                 "Hip-Hop",
    "rap":                     "Hip-Hop",
    # Comedy
    "comedy":                  "Comedy",
    "parody":                  "Comedy",
    "novelty":                 "Comedy",
}

TAG_MAP = {k.lower().strip(): v for k, v in _TAG_MAP.items()}


def tags_to_genre(tags: list[str]) -> str | None:
    """Map a list of Last.fm tags → best canonical genre.

    Strategy: Last.fm returns tags ordered by community vote count, so the
    first tag that maps to a canonical genre is typically the most accurate.
    We use first-match on the top 5 tags rather than lowest TAXONOMY_INDEX,
    which would incorrectly let a niche secondary tag like 'darkwave' override
    a primary tag like 'industrial' or 'ebm'.
    """
    for tag in tags[:5]:  # only consider top 5 — beyond that accuracy drops
        canon = TAG_MAP.get(tag.lower().strip())
        if canon:
            return canon
    return None


# ── NML helpers ───────────────────────────────────────────────────────────────
ET.register_namespace("", "")

def traktor_to_abs(volume: str, dir_str: str, filename: str) -> str:
    """Convert Traktor DIR path (/:foo/:bar/:) to absolute path."""
    parts = [p for p in dir_str.split("/:") if p]
    return str(Path("/") / Path(*parts) / filename) if parts else filename


def fix_xml_declaration(path: Path):
    content = path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(content)


def rewrite_file_genre(file_path: str, genre: str) -> bool:
    """Write genre to the audio file's embedded tag."""
    try:
        from mutagen.id3 import ID3, TCON
        from mutagen.mp4 import MP4
        from mutagen import File as MutagenFile

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
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Re-classify 'Other' genre tracks via Last.fm")
    parser.add_argument("--report",  action="store_true", help="Dry run — show proposed changes")
    parser.add_argument("--apply",   action="store_true", help="Apply changes to NML + file tags")
    parser.add_argument("--verbose", action="store_true", help="Show per-track detail")
    parser.add_argument("--keep-other", action="store_true",
                        help="Show tracks that stay as Other (unknown to Last.fm)")
    args = parser.parse_args()

    if not args.report and not args.apply:
        parser.print_help()
        return

    api_key = load_api_key()

    # Load Last.fm cache
    cache: dict[str, list[str]] = {}   # artist → tags list
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text())
    print(f"Last.fm cache: {len(cache):,} artists loaded")

    # Parse NML
    print(f"Parsing {NML_PATH.name}…")
    tree = ET.parse(NML_PATH)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    entries = list(collection.findall("ENTRY"))
    print(f"  {len(entries):,} total entries")

    # Find 'Other' entries
    other_entries = []
    for entry in entries:
        info = entry.find("INFO")
        if info is None: continue
        if (info.get("GENRE", "") or "").strip().lower() == "other":
            other_entries.append(entry)
    print(f"  {len(other_entries):,} entries with genre='Other'")

    # Collect unique artists needing lookup
    unique_artists = {entry.get("ARTIST", "").strip() for entry in other_entries if entry.get("ARTIST")}
    to_fetch = [a for a in unique_artists if a not in cache]
    print(f"  {len(unique_artists):,} unique artists — {len(to_fetch):,} need Last.fm lookup")

    # Fetch missing artists
    if to_fetch:
        print(f"\nQuerying Last.fm for {len(to_fetch)} artists…")
        for i, artist in enumerate(sorted(to_fetch), 1):
            tags = lastfm_top_tags(artist, api_key)
            cache[artist] = tags
            if i % 20 == 0 or i == len(to_fetch):
                CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"  [{i}/{len(to_fetch)}] fetched")
            time.sleep(0.25)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
        print("Fetch complete.")

    # Build proposed changes
    proposals: list[dict] = []   # {entry, artist, title, new_genre, tags}
    stays_other: list[tuple]  = []

    for entry in other_entries:
        artist = entry.get("ARTIST", "").strip()
        title  = entry.get("TITLE", "").strip()
        tags   = cache.get(artist, [])
        new_genre = tags_to_genre(tags)
        loc    = entry.find("LOCATION")
        path   = (traktor_to_abs(loc.get("VOLUME",""), loc.get("DIR",""), loc.get("FILE",""))
                  if loc is not None else "")
        if new_genre and new_genre != "Other":
            proposals.append({
                "entry":     entry,
                "artist":    artist,
                "title":     title,
                "new_genre": new_genre,
                "tags":      tags[:5],
                "path":      path,
            })
        else:
            stays_other.append((artist, title, tags[:5]))

    # ── Report ────────────────────────────────────────────────────────────────
    by_genre: dict[str, list] = defaultdict(list)
    for p in proposals:
        by_genre[p["new_genre"]].append(p)

    print(f"\n{'='*60}")
    print(f"PROPOSED CHANGES: {len(proposals)} tracks reclassified")
    print(f"STAYS OTHER:      {len(stays_other)} tracks")
    print(f"{'='*60}\n")

    for genre in TAXONOMY:
        tracks = by_genre.get(genre, [])
        if not tracks: continue
        print(f"  → {genre}  ({len(tracks)} tracks)")
        if args.verbose:
            for p in sorted(tracks, key=lambda x: x["artist"]):
                print(f"       {p['artist'][:30]:30s}  {p['title'][:35]:35s}")
                print(f"       tags: {', '.join(p['tags'][:4])}")
        else:
            # Show artist distribution
            artist_counts = Counter(p["artist"] for p in tracks)
            for a, c in artist_counts.most_common(8):
                print(f"       {c:3d}×  {a}")
        print()

    if args.keep_other and stays_other:
        print(f"\n  ── STAYS 'OTHER' ({len(stays_other)}) ──")
        for artist, title, tags in sorted(stays_other):
            tag_str = f"  [{', '.join(tags[:3])}]" if tags else "  [no tags]"
            print(f"       {artist[:30]:30s}  {title[:35]:35s}{tag_str}")

    # Save report JSON
    report = {
        "summary": {
            "total_other":   len(other_entries),
            "reclassified":  len(proposals),
            "stays_other":   len(stays_other),
        },
        "by_genre": {
            genre: [{"artist": p["artist"], "title": p["title"], "tags": p["tags"]}
                    for p in tracks]
            for genre, tracks in by_genre.items()
        },
        "stays_other": [
            {"artist": a, "title": t, "tags": tags}
            for a, t, tags in stays_other
        ],
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report saved → {REPORT_FILE.name}")

    # ── Apply ─────────────────────────────────────────────────────────────────
    if args.apply:
        print(f"\nApplying {len(proposals)} genre changes…")
        nml_ok = 0
        file_ok = 0
        file_fail = 0

        for p in proposals:
            # Update NML INFO element
            info = p["entry"].find("INFO")
            if info is not None:
                info.set("GENRE", p["new_genre"])
                nml_ok += 1

            # Update audio file tag
            if p["path"] and Path(p["path"]).exists():
                ok = rewrite_file_genre(p["path"], p["new_genre"])
                if ok:
                    file_ok += 1
                else:
                    file_fail += 1

        # Write NML
        ET.indent(tree, space="  ")
        tree.write(NML_PATH, encoding="UTF-8", xml_declaration=True)
        fix_xml_declaration(NML_PATH)

        print(f"\nDone.")
        print(f"  NML entries updated: {nml_ok}")
        print(f"  Audio files updated: {file_ok}")
        if file_fail:
            print(f"  Audio file failures: {file_fail}")
        print(f"\nRestart Traktor and re-import the collection to see changes.")


if __name__ == "__main__":
    main()
