#!/usr/bin/env python3
"""
Stage 8d — Unknown Artist Recovery + Typo Variant Detection

Two problems:
  A) Tracks where Traktor shows <unknown> artist — many have artist embedded in title
     e.g. "This is not the End - Faderhead" → artist = Faderhead
  B) Artist name typos that slipped past stage8b exact matching
     e.g. '"Weird Al" Yankovic' vs '"Weird Al" Yankovich' (edit distance 1)

Sub-problem A — Artist-from-title recovery:
  Reuses lib/tag_cleaner.clean_stem() to split title into (artist, title).
  Tries BOTH orientations (Artist - Title AND Title - Artist) and validates
  the candidate artist against the set of known artists already in the NML.
  Confidence levels:
    HIGH: candidate found in known-artists set → auto-apply with --apply
    MEDIUM: candidate not in known-artists but looks plausible → report only
    LOW: no splittable artist, or result looks like a placeholder → skip

Sub-problem B — Typo detection (report-only, no auto-apply):
  For pairs of artist names where:
    - First token (before first space) is identical
    - Levenshtein edit distance of the full normalized name ≤ 2
  Flag as likely typo. Suggest the higher-count or MB-canonical variant.

Reads:  corrected_traktor/collection.nml
        state/metadata.json  (for MB canonical names)
Writes: state/unknown_artist_report.json
        corrected_traktor/collection.nml  (modified in-place, with --apply)
        audio file tags                   (modified in-place, with --apply)

Usage:
    python3 stage8d_unknown_artists.py --report          # full dry-run report
    python3 stage8d_unknown_artists.py --apply           # apply HIGH-confidence recoveries
    python3 stage8d_unknown_artists.py --report --verbose  # show all candidates
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs, abs_to_traktor_location
from lib.tag_cleaner import clean_stem

try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TPE1, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

STATE_DIR   = Path(__file__).parent / "state"
TRAKTOR_DIR = Path(__file__).parent / "corrected_traktor"
NML_SOURCE  = TRAKTOR_DIR / "collection.nml"
META_JSON   = STATE_DIR / "metadata.json"
REPORT_JSON = STATE_DIR / "unknown_artist_report.json"

ET.register_namespace("", "")

# ---------------------------------------------------------------------------
# Levenshtein distance (pure Python, no external dependency)
# ---------------------------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j-1] + 1, prev[j-1] + (ca != cb)))
        prev = curr
    return prev[-1]


def normalize_for_fuzzy(name: str) -> str:
    """Lowercase, strip punctuation/articles for fuzzy comparison."""
    s = name.lower()
    # strip leading articles
    s = re.sub(r'^(the|a|an)\s+', '', s)
    # strip non-alphanumeric except spaces
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ---------------------------------------------------------------------------
# NML helpers
# ---------------------------------------------------------------------------

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


def load_nml(nml_path: Path) -> tuple[ET.ElementTree, ET.Element]:
    tree = ET.parse(nml_path)
    root = tree.getroot()
    return tree, root


# ---------------------------------------------------------------------------
# Tag rewriting
# ---------------------------------------------------------------------------

def rewrite_artist_tag(filepath: str, new_artist: str) -> bool:
    """Write artist tag to audio file. Returns True on success."""
    if not MUTAGEN_OK:
        return False
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()
            tags["TPE1"] = TPE1(encoding=3, text=new_artist)
            tags.save(filepath)
        elif ext in (".m4a", ".mp4", ".aac"):
            audio = MP4(filepath)
            audio["\xa9ART"] = [new_artist]
            audio.save()
        elif ext == ".flac":
            audio = FLAC(filepath)
            audio["artist"] = new_artist
            audio.save()
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(filepath)
            audio["artist"] = new_artist
            audio.save()
        else:
            # WAV and others — best effort via mutagen generic
            try:
                audio = mutagen.File(filepath)
                if audio is not None:
                    audio["artist"] = new_artist
                    audio.save()
                else:
                    return False
            except Exception:
                return False
        return True
    except Exception as e:
        print(f"  [WARN] Could not write artist tag to {filepath}: {e}")
        return False


# ---------------------------------------------------------------------------
# Sub-problem A: unknown-artist recovery
# ---------------------------------------------------------------------------

# Patterns that indicate a title is not a real artist-title combo
_PLACEHOLDER_TITLE_RE = re.compile(r'^(track\s*\d+|unknown|untitled|intro|outro|interlude)$', re.IGNORECASE)
_AC_PREFIX_RE = re.compile(r'^A&C', re.IGNORECASE)

# Minimum artist candidate length (single-char or pure-number "artists" are noise)
_MIN_ARTIST_LEN = 3


def _is_plausible_artist(candidate: str) -> bool:
    """Basic sanity filter for artist candidates."""
    if not candidate or len(candidate) < _MIN_ARTIST_LEN:
        return False
    if re.match(r'^\d+$', candidate):
        return False  # pure number
    if _PLACEHOLDER_TITLE_RE.match(candidate):
        return False
    if len(candidate) > 60:
        return False  # absurdly long
    return True


def try_recover_artist(title: str, known_artists: set[str]) -> dict | None:
    """
    Try to recover artist from a title string.

    Returns:
        None if recovery fails
        dict with keys: artist, clean_title, confidence, orientation
    """
    if not title or _AC_PREFIX_RE.match(title):
        return None

    stem = os.path.splitext(title)[0] if '.' in title else title

    # clean_stem defaults to "Artist - Title" (left side = artist)
    artist_l, title_r = clean_stem(stem)
    # Also try "Title - Artist" (right side = artist) by reversing the stem
    parts = stem.split(' - ', 1)
    if len(parts) == 2:
        artist_r_raw, title_l_raw = parts[1].strip(), parts[0].strip()
        # Apply same cleaning to the reversed version
        artist_r = artist_r_raw if _is_plausible_artist(artist_r_raw) else None
        title_l = title_l_raw
    else:
        artist_r, title_l = None, None

    # Normalize known artists to lowercase for case-insensitive lookup
    known_lower = {a.lower() for a in known_artists}

    candidates = []

    # Orientation 1: clean_stem's result (Artist - Title)
    if artist_l and _is_plausible_artist(artist_l):
        in_known = artist_l.lower() in known_lower
        candidates.append({
            "artist": artist_l,
            "clean_title": title_r or stem,
            "confidence": "HIGH" if in_known else "MEDIUM",
            "orientation": "artist_first",
            "in_known": in_known,
        })

    # Orientation 2: reversed (Title - Artist)
    if artist_r and _is_plausible_artist(artist_r) and (artist_r != artist_l):
        in_known = artist_r.lower() in known_lower
        candidates.append({
            "artist": artist_r,
            "clean_title": title_l or stem,
            "confidence": "HIGH" if in_known else "MEDIUM",
            "orientation": "title_first",
            "in_known": in_known,
        })

    if not candidates:
        return None

    # Prefer HIGH confidence over MEDIUM, then prefer the known-artist hit
    high = [c for c in candidates if c["confidence"] == "HIGH"]
    if high:
        # If both orientations are HIGH, prefer the one that appears more times
        # (can't easily check counts here, just pick first)
        return high[0]

    # Both MEDIUM — prefer artist_first (more common filename convention)
    return candidates[0]


def collect_unknown_entries(root: ET.Element) -> list[ET.Element]:
    """Return ENTRY elements with blank or missing ARTIST."""
    collection = root.find("COLLECTION")
    if collection is None:
        return []
    return [
        e for e in collection.findall("ENTRY")
        if not (e.get("ARTIST") or "").strip()
    ]


def collect_known_artists(root: ET.Element) -> set[str]:
    """Return set of all non-empty artist names in the NML."""
    collection = root.find("COLLECTION")
    if collection is None:
        return set()
    artists = set()
    for e in collection.findall("ENTRY"):
        a = (e.get("ARTIST") or "").strip()
        if a:
            artists.add(a)
    return artists


# ---------------------------------------------------------------------------
# Sub-problem B: typo/fuzzy duplicate detection
# ---------------------------------------------------------------------------

def find_typo_variants(
    all_artists: set[str],
    mb_canonical: dict[str, str],
    artist_counts: dict[str, int],
    max_dist: int = 2,
) -> list[dict]:
    """
    Find pairs of artist names that are likely typos of each other.

    Strategy:
    1. Group artists by their first token (word before first space)
    2. Within each group, compare all pairs with Levenshtein distance ≤ max_dist
    3. Report with suggestion (prefer MB canonical, else higher count)
    """
    # Build first-token → artists mapping (using normalized names)
    from collections import defaultdict
    token_groups: dict[str, list[str]] = defaultdict(list)
    for artist in all_artists:
        norm = normalize_for_fuzzy(artist)
        first_token = norm.split()[0] if norm.split() else norm
        token_groups[first_token].append(artist)

    results = []
    seen_pairs: set[frozenset] = set()

    for token, group in token_groups.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a1, a2 = group[i], group[j]
                pair = frozenset([a1, a2])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                n1 = normalize_for_fuzzy(a1)
                n2 = normalize_for_fuzzy(a2)
                dist = levenshtein(n1, n2)
                if dist == 0 or dist > max_dist:
                    continue

                # Suggest the canonical: prefer MB, else higher count
                mb1 = mb_canonical.get(a1)
                mb2 = mb_canonical.get(a2)
                count1 = artist_counts.get(a1, 0)
                count2 = artist_counts.get(a2, 0)

                if mb1 and not mb2:
                    suggested = a1
                elif mb2 and not mb1:
                    suggested = a2
                elif mb1 and mb2 and mb1 == mb2:
                    suggested = mb1  # same MB canonical → definite typo
                else:
                    suggested = a1 if count1 >= count2 else a2

                results.append({
                    "artist_a": a1,
                    "artist_b": a2,
                    "count_a": count1,
                    "count_b": count2,
                    "edit_distance": dist,
                    "normalized_a": n1,
                    "normalized_b": n2,
                    "mb_canonical_a": mb1,
                    "mb_canonical_b": mb2,
                    "suggested_canonical": suggested,
                })

    results.sort(key=lambda x: (x["edit_distance"], -x["count_a"] - x["count_b"]))
    return results


# ---------------------------------------------------------------------------
# A&C prefix investigation
# ---------------------------------------------------------------------------

def collect_ac_prefix_entries(root: ET.Element) -> list[dict]:
    """Collect entries whose title starts with A&C (likely an import artifact)."""
    collection = root.find("COLLECTION")
    if collection is None:
        return []
    results = []
    for e in collection.findall("ENTRY"):
        title = e.get("TITLE", "")
        if _AC_PREFIX_RE.match(title):
            path = entry_abs_path(e)
            results.append({
                "title": title,
                "artist": e.get("ARTIST", ""),
                "path": path or "",
            })
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Recover unknown artists + detect typo variants")
    parser.add_argument("--report", action="store_true",
                        help="Dry-run: show what would be changed")
    parser.add_argument("--apply", action="store_true",
                        help="Apply HIGH-confidence artist recoveries to NML + file tags")
    parser.add_argument("--verbose", action="store_true",
                        help="Show MEDIUM-confidence candidates too")
    parser.add_argument("--max-dist", type=int, default=2,
                        help="Max Levenshtein distance for typo detection (default: 2)")
    args = parser.parse_args()

    if not args.report and not args.apply:
        parser.print_help()
        return

    if not NML_SOURCE.exists():
        print(f"collection.nml not found at {NML_SOURCE}")
        return

    if not MUTAGEN_OK and args.apply:
        print("[WARN] mutagen not available — file tags will NOT be updated (NML only)")

    print("Stage 8d: Unknown Artist Recovery + Typo Detection")
    print("=" * 60)

    # Load NML
    print(f"\nLoading {NML_SOURCE.name}...")
    tree, root = load_nml(NML_SOURCE)
    collection = root.find("COLLECTION")

    # Build known-artists set and counts
    known_artists = collect_known_artists(root)
    artist_counts: dict[str, int] = {}
    if collection is not None:
        for e in collection.findall("ENTRY"):
            a = (e.get("ARTIST") or "").strip()
            if a:
                artist_counts[a] = artist_counts.get(a, 0) + 1

    print(f"  {len(known_artists):,} distinct known artists in NML")

    # Load MB canonical map from metadata.json
    mb_canonical: dict[str, str] = {}
    if META_JSON.exists():
        try:
            meta = json.loads(META_JSON.read_text())
            # metadata.json: path → {mb_artist, ...}
            # Build artist → mb_artist map
            for path_data in meta.values():
                if isinstance(path_data, dict):
                    mb_art = path_data.get("mb_artist") or path_data.get("artist")
                    tag_art = path_data.get("artist")
                    if mb_art and tag_art and mb_art != tag_art:
                        mb_canonical[tag_art] = mb_art
        except Exception as e:
            print(f"  [WARN] Could not load metadata.json: {e}")

    # -----------------------------------------------------------------------
    # Part A: Unknown artist recovery
    # -----------------------------------------------------------------------
    print(f"\n--- Part A: Unknown Artist Recovery ---")
    unknown_entries = collect_unknown_entries(root)
    print(f"  {len(unknown_entries):,} entries with no artist set")

    high_conf: list[dict] = []
    medium_conf: list[dict] = []
    low_conf: list[dict] = []

    for entry in unknown_entries:
        title = entry.get("TITLE", "").strip()
        path = entry_abs_path(entry)

        if not title:
            low_conf.append({"title": title, "path": path or "", "reason": "no title"})
            continue

        if _AC_PREFIX_RE.match(title):
            low_conf.append({"title": title, "path": path or "", "reason": "A&C prefix — skipped"})
            continue

        result = try_recover_artist(title, known_artists)
        if result is None:
            low_conf.append({"title": title, "path": path or "", "reason": "no splittable artist"})
            continue

        record = {
            "original_title": title,
            "recovered_artist": result["artist"],
            "clean_title": result["clean_title"],
            "confidence": result["confidence"],
            "orientation": result["orientation"],
            "path": path or "",
        }

        if result["confidence"] == "HIGH":
            high_conf.append(record)
        else:
            medium_conf.append(record)

    print(f"  HIGH confidence (auto-apply): {len(high_conf)}")
    print(f"  MEDIUM confidence (review):   {len(medium_conf)}")
    print(f"  LOW / unrecoverable:          {len(low_conf)}")

    if high_conf:
        print(f"\n  HIGH confidence recoveries:")
        for r in high_conf[:20]:
            print(f"    → artist: {r['recovered_artist']!r}")
            print(f"       title: {r['original_title']!r} → {r['clean_title']!r}")
            print(f"       path:  {os.path.basename(r['path'])}")
        if len(high_conf) > 20:
            print(f"    ... and {len(high_conf)-20} more (see report JSON)")

    if args.verbose and medium_conf:
        print(f"\n  MEDIUM confidence (not auto-applied):")
        for r in medium_conf[:20]:
            print(f"    → candidate artist: {r['recovered_artist']!r}")
            print(f"       title:           {r['original_title']!r}")
            print(f"       path:            {os.path.basename(r['path'])}")
        if len(medium_conf) > 20:
            print(f"    ... and {len(medium_conf)-20} more (see report JSON)")

    # -----------------------------------------------------------------------
    # Part B: A&C prefix entries
    # -----------------------------------------------------------------------
    print(f"\n--- Part B: A&C Prefix Entries ---")
    ac_entries = collect_ac_prefix_entries(root)
    print(f"  {len(ac_entries)} entries with A&C title prefix")
    if ac_entries:
        print("  (These appear to be an import artifact — investigate manually)")
        for e in ac_entries[:10]:
            print(f"    {e['title']!r}  [{e['artist']!r}]  {os.path.basename(e['path'])}")
        if len(ac_entries) > 10:
            print(f"  ... and {len(ac_entries)-10} more (see report JSON)")

    # -----------------------------------------------------------------------
    # Part C: Typo variants
    # -----------------------------------------------------------------------
    print(f"\n--- Part C: Typo Artist Variants (report only) ---")
    all_artists_with_counts = set(artist_counts.keys())
    typo_pairs = find_typo_variants(
        all_artists_with_counts, mb_canonical, artist_counts, args.max_dist
    )
    print(f"  {len(typo_pairs)} likely typo pairs found (edit distance ≤ {args.max_dist})")
    if typo_pairs:
        print()
        for p in typo_pairs[:30]:
            print(f"  dist={p['edit_distance']}  "
                  f"{p['artist_a']!r} ({p['count_a']} tracks) vs "
                  f"{p['artist_b']!r} ({p['count_b']} tracks)")
            print(f"         → suggest canonical: {p['suggested_canonical']!r}")
        if len(typo_pairs) > 30:
            print(f"  ... and {len(typo_pairs)-30} more (see report JSON)")

    # -----------------------------------------------------------------------
    # Write report
    # -----------------------------------------------------------------------
    report = {
        "unknown_entries_total": len(unknown_entries),
        "high_confidence": high_conf,
        "medium_confidence": medium_conf,
        "low_confidence": low_conf,
        "ac_prefix_entries": ac_entries,
        "typo_pairs": typo_pairs,
    }
    STATE_DIR.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nFull report → {REPORT_JSON}")

    if not args.apply:
        print("\nDry-run complete. Run with --apply to apply HIGH-confidence recoveries.")
        return

    # -----------------------------------------------------------------------
    # Apply HIGH-confidence recoveries
    # -----------------------------------------------------------------------
    print(f"\n--- Applying {len(high_conf)} HIGH-confidence recoveries ---")

    if not high_conf:
        print("  Nothing to apply.")
        return

    # Build path → recovery dict for fast lookup
    recovery_by_path: dict[str, dict] = {r["path"]: r for r in high_conf if r["path"]}
    recovery_by_title: dict[str, dict] = {}
    for r in high_conf:
        if not r["path"]:  # no path — match by original title
            recovery_by_title[r["original_title"]] = r

    nml_updated = 0
    file_updated = 0
    file_failed = 0

    for entry in unknown_entries:
        path = entry_abs_path(entry)
        title = entry.get("TITLE", "").strip()

        rec = recovery_by_path.get(path) if path else None
        if rec is None:
            rec = recovery_by_title.get(title)
        if rec is None:
            continue

        new_artist = rec["recovered_artist"]
        new_title = rec["clean_title"]

        # Update NML ENTRY attributes
        entry.set("ARTIST", new_artist)
        entry.set("TITLE", new_title)
        nml_updated += 1

        # Update file tags
        if path and os.path.exists(path):
            ok = rewrite_artist_tag(path, new_artist)
            if ok:
                # Also update title tag if we cleaned it
                if new_title != title:
                    try:
                        ext = os.path.splitext(path)[1].lower()
                        if ext == ".mp3":
                            from mutagen.id3 import TIT2
                            tags = ID3(path)
                            tags["TIT2"] = TIT2(encoding=3, text=new_title)
                            tags.save(path)
                        elif ext in (".m4a", ".mp4", ".aac"):
                            audio = MP4(path)
                            audio["\xa9nam"] = [new_title]
                            audio.save()
                        elif ext == ".flac":
                            audio = FLAC(path)
                            audio["title"] = new_title
                            audio.save()
                        elif ext in (".ogg", ".oga"):
                            audio = OggVorbis(path)
                            audio["title"] = new_title
                            audio.save()
                    except Exception as e:
                        print(f"  [WARN] Could not write title tag to {path}: {e}")
                file_updated += 1
            else:
                file_failed += 1

    print(f"  NML entries updated: {nml_updated}")
    print(f"  File tags updated:   {file_updated}")
    if file_failed:
        print(f"  File tag failures:   {file_failed}")

    # Write updated NML
    print(f"\n  Writing {NML_SOURCE.name}...")
    # Update ENTRIES count
    if collection is not None:
        collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))
    tree.write(str(NML_SOURCE), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(NML_SOURCE)

    report["applied"] = True
    report["nml_entries_updated"] = nml_updated
    report["file_tags_updated"] = file_updated
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nStage 8d complete. {nml_updated} unknown artists recovered.")
    if typo_pairs:
        print(f"Review {len(typo_pairs)} typo pairs in {REPORT_JSON} — no auto-apply for those.")


if __name__ == "__main__":
    main()
