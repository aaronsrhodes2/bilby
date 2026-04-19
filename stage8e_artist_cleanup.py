#!/usr/bin/env python3
r"""
Stage 8e — Artist Name Cleanup (Four Passes + Title Audit)

Pass 1 — Decade prefix stripping:
  "80's-Debbie Palacios" → "Debbie Palacios"
  Pattern: ^\d{2}'?s[-]\s*(.+)  (decade tag glued to artist with hyphen)

Pass 2 — Feat/collab suffix stripping:
  "Artist feat. Guest" → "Artist"
  "Artist & Guest"     → "Artist"
  "Artist w/ Guest"    → "Artist"
  Condition: primary artist already in NML with MORE tracks than the collab entry.
  Collabs filed under the mother band per user preference.
  Exception: Peaches & Herb (distinct 1970s R&B duo, not the artist Peaches).

Pass 3 — Confirmed typo/garbage merges:
  Hardcoded wrong → right table for typos and album-name-in-artist-field garbage.
  Does NOT auto-apply dist-2 pairs that might be different artists
  (e.g. "Soft Kill" vs "Soft Cell").

Pass 4 — Case normalization (runs FIRST internally, so Pass 2 sees normalized counts):
  "chemical brothers" → "Chemical Brothers"
  "recoil" → "Recoil"
  Excludes intentionally lowercase/stylized names (ohGr, of Montreal, a-ha, etc.)

Pass 5 — "The X" ↔ "X" article normalization:
  "Cure" → "The Cure"  (whichever variant has more tracks wins)
  "Beatles" → "The Beatles"
  Merges minority variant into majority. Both must already exist in the NML.
  Artists starting with a lowercase letter get smart title-cased.
  Excludes known intentionally-lowercase / stylized artist names.
  Rule: capitalize first letter of each word; preserve existing capitalization
  within words (so "ohGr" would be excluded, not munged to "OhGr").

Title audit (--report only):
  Finds tracks where the TITLE attribute starts with a lowercase letter and
  the artist uses normal casing — flags for manual review. Does NOT auto-fix
  titles (too many valid lowercase exceptions like "of Montreal" song titles).

Reads:  corrected_traktor/collection.nml
Writes: state/artist_cleanup_report.json
        corrected_traktor/collection.nml  (with --apply)
        audio file tags                   (with --apply)

Usage:
    python3 stage8e_artist_cleanup.py --report           # full dry-run
    python3 stage8e_artist_cleanup.py --apply            # write NML + file tags
    python3 stage8e_artist_cleanup.py --report --titles  # include title audit
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

try:
    import mutagen
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
REPORT_JSON = STATE_DIR / "artist_cleanup_report.json"

ET.register_namespace("", "")

# ---------------------------------------------------------------------------
# Pass 2 — Collab exclusions
# Only entries where the "& X" or "and X" IS the artist name, not a collab.
# User preference: all other collabs go under the mother band.
# ---------------------------------------------------------------------------

COLLAB_EXCLUSIONS: set[str] = {
    # Peaches & Herb: 1970s R&B duo (Shake Your Groove Thing, Reunited) —
    # completely different act from the Canadian artist Peaches.
    "Peaches & Herb",
    "Peaches and Herb",
}

# ---------------------------------------------------------------------------
# Pass 3 — Hardcoded typo / garbage corrections
# Format: wrong_name → correct_name
# ---------------------------------------------------------------------------

TYPO_CORRECTIONS: dict[str, str] = {
    # ---------- Spelling typos ----------
    '"Weird Al" Yankovich':              '"Weird Al" Yankovic',
    'Psycho le Cemu':                    'Psycho le Cému',
    'Seefeel 1':                         'Seefeel',
    'Mulu 1':                            'Mulu',
    'Creedence Clearwater Reviv':        'Creedence Clearwater Revival',
    'Big Audio Dynamtie II':             'Big Audio Dynamite II',
    'Frank Tovey & the Pryos':           'Frank Tovey & the Pyros',
    'Candi Stanton':                     'Candi Staton',  # her real name
    'amercian music club':               'American Music Club',
    'flock of seagulls':                 'A Flock of Seagulls',

    # ---------- Lowercased imports (must be in Pass 3 to win over Pass 4) ----------
    'sinead o connor':                   "Sinead O'Connor",
    'higher intellengence agency':       'Higher Intelligence Agency',

    # ---------- Band name separator / encoding variants ----------
    'a ha':                              'a-ha',
    'a\u2010ha':                         'a-ha',   # non-breaking hyphen → regular hyphen

    # ---------- Acronym casing that title() gets wrong ----------
    'faded sf':                          'Faded SF',
    'faded sf.mp3':                      'Faded SF',    # literal filename in artist field
    'final- faded sf':                   'Faded SF',    # track name crept into artist field
    'afro-celt sound system':            'Afro-Celt Sound System',

    # ---------- Album/soundtrack info crept into artist field ----------
    'coldplay (x&y)':                    'Coldplay',
    'coldplay (a rush of blood.)':       'Coldplay',
    'coldplay (parachutes)':             'Coldplay',
    'jimmy eat world (clarity)':         'Jimmy Eat World',
    'toad the wet sprocket (fear)':      'Toad the Wet Sprocket',
    'dave gahan (depeche mode)':         'Dave Gahan',
    'nine inch nails - edit':            'Nine Inch Nails',
    'cat stevens CORRUPTED':             'Cat Stevens',
    'angelo badalmenti - twin peaks':    'Angelo Badalamenti',  # also typo in surname
    'front line assembly & destinantion goa': 'Front Line Assembly',
    'the crow soundtrack (gramme revell)':    'Graeme Revell',   # also fixing name typo

    # ---------- Article direction overrides for Pass 5 ----------
    # Forces correct direction when count-based heuristic would pick wrong form.
    'Art of Noise':                      'The Art of Noise',
    'Prodigy':                           'The Prodigy',
    'The Red Hot Chili Peppers':         'Red Hot Chili Peppers',  # official name has no "The"

    # ---------- Collab forms of intentionally-lowercase artists ----------
    # Pass 2 can't strip these (standalone form not in NML with more tracks).
    'deadmau5 feat. Gerard Way':         'deadmau5',
    'tINI feat. Amiture':                'tINI',
    'ease. feat. WYS':                   'ease.',
}

# ---------------------------------------------------------------------------
# Pass 4 — Intentionally lowercase / stylized artists (skip case normalization)
# ---------------------------------------------------------------------------

LOWERCASE_KEEP: set[str] = {
    # Stylized mixed-case
    "ohGr",            # Skinny Puppy spinoff (Kevin Ogilvie)
    "esOterica",       # intentional
    "kAlte fArben",    # intentional
    "goJA moon ROCKAH",
    "n0nplus Vs Angelspit",
    "tINI",
    # Intentionally all-lowercase band names
    "of Montreal",     # indie pop band
    "múm",             # Icelandic
    "mind.in.a.box",   # EBM duo
    "a-ha",            # Norwegian pop trio (official name is lowercase)
    "deadmau5",        # DJ/producer
    "abingdon boys school",  # Japanese
    "sukekiyo",        # Japanese visual kei
    "kannivalism",     # Japanese
    "coldrain",        # Japanese
    "din_fiv",         # Intentional underscore style
    "nolongerhuman",   # Intentional
    "downset.",        # Band name includes period
    "t.A.T.u.",        # Russian pop duo
    "the GazettE",     # Japanese visual kei
    "cali\u2260gari",  # Japanese (cali≠gari)
    "gibkiy gibkiy gibkiy",  # Russian band
    "\u00falvur",      # Faroese band (úlvur)
    "space",           # Could be intentional for this small act
}

# ---------------------------------------------------------------------------
# Pass 1 — Decade prefix regex
# ---------------------------------------------------------------------------
_DECADE_PREFIX_RE = re.compile(r"^(\d{2}'?s)[-]\s*(.+)$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Pass 2 — Feat/collab regex
# ---------------------------------------------------------------------------
_FEAT_RE = re.compile(
    r'^(.+?)\s+(?:feat\.?|ft\.?|featuring|with|w/)\s+.+$',
    re.IGNORECASE,
)
_AMP_RE = re.compile(r'^(.+?)\s+(?:&|and)\s+.+$', re.IGNORECASE)


def _strip_collab(artist: str) -> str | None:
    if artist in COLLAB_EXCLUSIONS:
        return None
    m = _FEAT_RE.match(artist)
    if m:
        return m.group(1).strip()
    m = _AMP_RE.match(artist)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Pass 4 — Smart title case
# Capitalises first letter of each space-separated word; preserves existing
# caps within words (so "McCartney" stays "McCartney", not "Mccartney").
# ---------------------------------------------------------------------------

def smart_title_case(name: str) -> str:
    words = name.split(" ")
    result = []
    for word in words:
        if word and word[0].islower():
            word = word[0].upper() + word[1:]
        result.append(word)
    return " ".join(result)


def needs_case_fix(artist: str) -> bool:
    """True if artist starts with a lowercase letter and isn't in LOWERCASE_KEEP."""
    if not artist:
        return False
    if artist in LOWERCASE_KEEP:
        return False
    # Any word starts lowercase → candidate
    return artist[0].islower()


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


def rewrite_tags(filepath: str, new_artist: str | None = None, new_title: str | None = None) -> bool:
    if not MUTAGEN_OK:
        return False
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()
            if new_artist is not None:
                from mutagen.id3 import TPE1
                tags["TPE1"] = TPE1(encoding=3, text=new_artist)
            if new_title is not None:
                from mutagen.id3 import TIT2
                tags["TIT2"] = TIT2(encoding=3, text=new_title)
            tags.save(filepath)
        elif ext in (".m4a", ".mp4", ".aac"):
            audio = MP4(filepath)
            if new_artist is not None:
                audio["\xa9ART"] = [new_artist]
            if new_title is not None:
                audio["\xa9nam"] = [new_title]
            audio.save()
        elif ext == ".flac":
            audio = FLAC(filepath)
            if new_artist is not None:
                audio["artist"] = new_artist
            if new_title is not None:
                audio["title"] = new_title
            audio.save()
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(filepath)
            if new_artist is not None:
                audio["artist"] = new_artist
            if new_title is not None:
                audio["title"] = new_title
            audio.save()
        else:
            try:
                audio = mutagen.File(filepath)
                if audio is not None:
                    if new_artist is not None:
                        audio["artist"] = new_artist
                    if new_title is not None:
                        audio["title"] = new_title
                    audio.save()
                else:
                    return False
            except Exception:
                return False
        return True
    except Exception as e:
        print(f"  [WARN] tag write failed for {os.path.basename(filepath)}: {e}")
        return False


# ---------------------------------------------------------------------------
# Build rename map (all four passes)
# ---------------------------------------------------------------------------

def build_rename_map(root: ET.Element) -> dict[str, dict]:
    """
    Build the full rename map in four passes.

    Pass order: 4 → 1 → 2 → 3
    Pass 4 (case fix) runs FIRST so that normalized artist names are used when
    Pass 2 (collab strip) checks whether the primary artist has more tracks.
    Example: "chemical brothers" (1) + "The Chemical Brothers" (35) — without
    normalizing case first, Pass 2 wouldn't find the primary.
    """
    collection = root.find("COLLECTION")
    if collection is None:
        return {}

    # Raw counts from NML
    artist_counts: dict[str, int] = defaultdict(int)
    for e in collection.findall("ENTRY"):
        a = (e.get("ARTIST") or "").strip()
        if a:
            artist_counts[a] += 1

    renames: dict[str, dict] = {}

    # ---- Pass 3 runs FIRST: catch specific known-bad names before generic case fix ----
    # This ensures "sinead o connor" → "Sinead O'Connor" (not "Sinead O Connor"),
    # "faded sf" → "Faded SF" (not "Faded Sf"), etc.
    for wrong, correct in TYPO_CORRECTIONS.items():
        if wrong not in artist_counts:
            continue
        count = artist_counts[wrong]
        renames[wrong] = {
            "new_artist": correct,
            "pass": "typo",
            "old_count": count,
            "new_count": artist_counts.get(correct, 0),
            "note": "confirmed typo/garbage correction",
        }

    # ---- Pass 4: case normalization (runs after Pass 3 to avoid clobbering typos) ----
    for artist, count in list(artist_counts.items()):
        if artist in renames:
            continue  # already handled by Pass 3
        if not needs_case_fix(artist):
            continue
        new_name = smart_title_case(artist)
        if new_name == artist:
            continue
        renames[artist] = {
            "new_artist": new_name,
            "pass": "case_fix",
            "old_count": count,
            "new_count": artist_counts.get(new_name, 0),
            "note": "lowercase → title case",
        }

    # Build normalized artist counts (applying Pass 3+4 renames virtually).
    # Pass 2 (collab) and Pass 5 (article) use these counts so they can find
    # primaries that were previously lowercase or misspelled.
    norm_counts: dict[str, int] = defaultdict(int)
    for artist, count in artist_counts.items():
        effective = renames.get(artist, {}).get("new_artist", artist)
        norm_counts[effective] += count

    # ---- Pass 1: decade prefix ----
    for artist, count in list(artist_counts.items()):
        if artist in renames:
            continue
        m = _DECADE_PREFIX_RE.match(artist)
        if m:
            new_name = m.group(2).strip()
            if new_name and new_name != artist:
                renames[artist] = {
                    "new_artist": new_name,
                    "pass": "decade_prefix",
                    "old_count": count,
                    "new_count": norm_counts.get(new_name, 0),
                    "note": f"stripped decade prefix '{m.group(1)}-'",
                }

    # ---- Pass 2: feat/collab stripping ----
    # Use the effective (post-Pass-4) name when looking up primary counts.
    for artist, count in list(artist_counts.items()):
        if artist in renames:
            continue
        # Effective name after case fix (may be same as artist if no change)
        effective = artist  # no case fix → effective == artist
        primary = _strip_collab(effective)
        if primary is None:
            continue
        primary_count = norm_counts.get(primary, 0)
        my_count = norm_counts.get(effective, count)
        if primary_count > my_count:
            renames[artist] = {
                "new_artist": primary,
                "pass": "collab_strip",
                "old_count": count,
                "new_count": primary_count,
                "note": f"collab suffix stripped → '{primary}' ({primary_count} tracks)",
            }

    # ---- Pass 5: "The X" ↔ "X" article normalization ----
    # If "Cure" (2) exists and "The Cure" (360) also exists → merge to "The Cure".
    # Whichever variant has MORE tracks in norm_counts wins.
    # Operates on the post-Pass-4 normalized names.
    processed_article: set[str] = set()
    for artist, count in list(artist_counts.items()):
        if artist in renames:
            continue
        effective = renames.get(artist, {}).get("new_artist", artist)

        # Candidate pairs: try adding/removing "The"
        if effective.startswith("The ") and len(effective) > 4:
            without_the = effective[4:]
            pairs = [(effective, without_the)]
        else:
            pairs = [(effective, "The " + effective)]

        for canonical_candidate, alternate in pairs:
            pair_key = frozenset([canonical_candidate, alternate])
            if pair_key in processed_article:
                continue
            processed_article.add(pair_key)

            count_canonical = norm_counts.get(canonical_candidate, 0)
            count_alternate = norm_counts.get(alternate, 0)

            if count_canonical == 0 or count_alternate == 0:
                continue  # one side doesn't exist — nothing to merge

            # Merge minority into majority
            if count_canonical >= count_alternate:
                # alternate → canonical_candidate
                # Find the NML artist key that matches alternate
                for orig_artist, orig_count in artist_counts.items():
                    orig_eff = renames.get(orig_artist, {}).get("new_artist", orig_artist)
                    if orig_eff == alternate and orig_artist not in renames:
                        renames[orig_artist] = {
                            "new_artist": canonical_candidate,
                            "pass": "article_norm",
                            "old_count": orig_count,
                            "new_count": count_canonical,
                            "note": f"article variant → '{canonical_candidate}'",
                        }
            else:
                # canonical_candidate → alternate (alternate has more tracks)
                for orig_artist, orig_count in artist_counts.items():
                    orig_eff = renames.get(orig_artist, {}).get("new_artist", orig_artist)
                    if orig_eff == canonical_candidate and orig_artist not in renames:
                        renames[orig_artist] = {
                            "new_artist": alternate,
                            "pass": "article_norm",
                            "old_count": orig_count,
                            "new_count": count_alternate,
                            "note": f"article variant → '{alternate}'",
                        }

    return renames


# ---------------------------------------------------------------------------
# Title audit
# ---------------------------------------------------------------------------

def audit_titles(root: ET.Element, renames: dict) -> list[dict]:
    """
    Find tracks where the NML TITLE starts with a lowercase letter.
    These are candidates for manual review — NOT auto-fixed.
    Excludes titles that start with a number, symbol, or are in
    collections from known lowercase-style artists.
    """
    collection = root.find("COLLECTION")
    if collection is None:
        return []

    # Build post-rename artist set
    lowercase_ok_artists = LOWERCASE_KEEP | {"of Montreal", "múm", "mind.in.a.box"}

    suspects = []
    for entry in collection.findall("ENTRY"):
        title = (entry.get("TITLE") or "").strip()
        artist = (entry.get("ARTIST") or "").strip()
        if not title or not title[0].islower():
            continue
        # Skip if artist is known lowercase-style (their titles may also be lowercase)
        if artist in lowercase_ok_artists:
            continue
        # Effective artist after renames
        effective_artist = renames.get(artist, {}).get("new_artist", artist)
        suspects.append({
            "title": title,
            "artist": artist,
            "effective_artist": effective_artist,
            "path": entry_abs_path(entry) or "",
        })
    return suspects


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Artist cleanup — four passes + title audit")
    parser.add_argument("--report", action="store_true", help="Dry-run: show proposed changes")
    parser.add_argument("--apply",  action="store_true", help="Apply changes to NML + file tags")
    parser.add_argument("--titles", action="store_true", help="Include title case audit in report")
    parser.add_argument("--pass1-only", action="store_true")
    parser.add_argument("--pass2-only", action="store_true")
    parser.add_argument("--pass3-only", action="store_true")
    parser.add_argument("--pass4-only", action="store_true")
    args = parser.parse_args()

    if not args.report and not args.apply:
        parser.print_help()
        return

    if not NML_SOURCE.exists():
        print(f"collection.nml not found at {NML_SOURCE}")
        return

    if not MUTAGEN_OK and args.apply:
        print("[WARN] mutagen not available — file tags will NOT be updated (NML only)")

    print("Stage 8e: Artist Name Cleanup")
    print("=" * 60)
    print(f"\nLoading {NML_SOURCE.name}...")
    tree = ET.parse(NML_SOURCE)
    root = tree.getroot()
    collection = root.find("COLLECTION")

    renames = build_rename_map(root)

    # Filter by pass flags
    pass_filter = None
    if args.pass1_only:  pass_filter = "decade_prefix"
    elif args.pass2_only: pass_filter = "collab_strip"
    elif args.pass3_only: pass_filter = "typo"
    elif args.pass4_only: pass_filter = "case_fix"
    if pass_filter:
        renames = {k: v for k, v in renames.items() if v["pass"] == pass_filter}

    by_pass: dict[str, list] = defaultdict(list)
    for old, info in renames.items():
        by_pass[info["pass"]].append((old, info))

    total_tracks = sum(v["old_count"] for v in renames.values())

    print(f"\n  Total artists to rename: {len(renames)} ({total_tracks} tracks affected)")
    print(f"  Pass 1 — Decade prefix:  {len(by_pass['decade_prefix'])}")
    print(f"  Pass 2 — Collab strip:   {len(by_pass['collab_strip'])}")
    print(f"  Pass 3 — Typo/garbage:   {len(by_pass['typo'])}")
    print(f"  Pass 4 — Case fix:       {len(by_pass['case_fix'])}")
    print(f"  Pass 5 — Article norm:   {len(by_pass['article_norm'])}")

    pass_display = [
        ("case_fix",      "PASS 4 — Case Normalization (applied first)"),
        ("decade_prefix", "PASS 1 — Decade Prefix Stripping"),
        ("collab_strip",  "PASS 2 — Feat/Collab Suffix Stripping"),
        ("typo",          "PASS 3 — Typo / Garbage Corrections"),
        ("article_norm",  "PASS 5 — Article (The X ↔ X) Normalization"),
    ]
    for pass_name, label in pass_display:
        items = by_pass[pass_name]
        if not items:
            continue
        print(f"\n  --- {label} ({len(items)}) ---")
        for old, info in sorted(items, key=lambda x: -x[1]["old_count"]):
            print(f"    [{info['old_count']:3d}→{info['new_count']:3d}]  "
                  f"{old!r}  →  {info['new_artist']!r}")

    # Title audit
    title_suspects = []
    if args.titles or args.report:
        title_suspects = audit_titles(root, renames)
        print(f"\n  --- TITLE AUDIT — Lowercase-starting titles: {len(title_suspects)} ---")
        if title_suspects:
            # Show up to 40 most suspicious
            shown = 0
            for s in title_suspects[:40]:
                print(f"    {s['effective_artist'] or '(unknown)':30s}  {s['title']!r}")
                shown += 1
            if len(title_suspects) > 40:
                print(f"    ... and {len(title_suspects)-40} more (see report JSON)")
            print(f"  (Title case audit is report-only — not auto-fixed)")

    # Write report
    report = {
        "total_artists_renamed": len(renames),
        "total_tracks_affected": total_tracks,
        "renames": [{"old": old, **info} for old, info in sorted(renames.items())],
        "title_audit": title_suspects,
    }
    STATE_DIR.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nFull report → {REPORT_JSON}")

    if not args.apply:
        print("\nDry-run complete. Run with --apply to write NML + file tags.")
        return

    # -----------------------------------------------------------------------
    # Apply
    # -----------------------------------------------------------------------
    print(f"\n--- Applying {len(renames)} artist renames ---")

    nml_updated = 0
    file_ok = 0
    file_skip = 0
    file_fail = 0

    if collection is None:
        print("  [ERROR] No COLLECTION element found!")
        return

    for entry in collection.findall("ENTRY"):
        old_artist = (entry.get("ARTIST") or "").strip()
        if old_artist not in renames:
            continue

        new_artist = renames[old_artist]["new_artist"]
        path = entry_abs_path(entry)

        entry.set("ARTIST", new_artist)
        nml_updated += 1

        if path and os.path.exists(path):
            ok = rewrite_tags(path, new_artist=new_artist)
            if ok:
                file_ok += 1
            else:
                file_fail += 1
        else:
            file_skip += 1

    print(f"  NML entries updated: {nml_updated}")
    print(f"  File tags written:   {file_ok}")
    print(f"  File tags skipped:   {file_skip}  (not in corrected_music/)")
    if file_fail:
        print(f"  File tag failures:   {file_fail}")

    print(f"\n  Writing {NML_SOURCE.name}...")
    if collection is not None:
        collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))
    tree.write(str(NML_SOURCE), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(NML_SOURCE)

    report["applied"] = True
    report["nml_entries_updated"] = nml_updated
    report["file_tags_written"] = file_ok
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\nStage 8e complete. {len(renames)} artist name patterns cleaned "
          f"across {nml_updated} NML entries.")


if __name__ == "__main__":
    main()
