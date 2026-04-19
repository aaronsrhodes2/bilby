#!/usr/bin/env python3
"""
Stage 8i — Genre Consolidation (68 → 23 canonical genres)

Collapses redundant / low-count / word-sharing genres into 23 canonicals
chosen around the user's DJ focus: goth, industrial, synthpop, darkwave,
rock, 80s.

Applies to:
  ~/Documents/Native Instruments/Traktor 4.0.2/collection.nml  (Traktor's live NML)
  corrected_traktor/collection.nml                              (our corrected copy)

Optionally updates audio file GENRE tags via mutagen (--tags).

Usage:
    python3 stage8i_genre_consolidate.py            # dry-run report
    python3 stage8i_genre_consolidate.py --apply    # write NMLs
    python3 stage8i_genre_consolidate.py --apply --tags  # write NMLs + file tags
"""

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

TRAKTOR_NML = (
    Path.home()
    / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
)
OUR_NML = Path(__file__).parent / "corrected_traktor" / "collection.nml"

# ── Canonical genre set (23 total) ────────────────────────────────────────────

CANONICAL = {
    # Goth / Industrial / Dark (7)
    "Gothic Rock", "Darkwave", "Post-Punk", "EBM", "Industrial",
    "New Wave", "Synthpop",
    # Electronic (2)
    "Electronic", "Ambient",
    # Rock (7)
    "Rock", "Alternative Rock", "Indie Rock", "Classic Rock",
    "Hard Rock", "Punk", "Metal",
    # Specialty (7)
    "Pop", "Folk", "Soundtrack", "Hip-Hop", "Comedy", "Classical", "Other",
}

# ── Consolidation map (source → canonical) ────────────────────────────────────
# Case-sensitive: strings must match exactly as they appear in the NML.

CONSOLIDATION_MAP: dict[str, str] = {
    # ── Goth / Industrial collapses ───────────────────────────────────────────
    "Deathrock":            "Gothic Rock",
    "Coldwave":             "Darkwave",
    "Ethereal Wave":        "Darkwave",
    "New Romantic":         "New Wave",
    "Synth Rock":           "Synthpop",
    "Dance-Pop":            "Synthpop",

    # ── Electronic collapses ─────────────────────────────────────────────────
    "IDM":                  "Electronic",
    "Dance":                "Electronic",
    "Electro-Techno":       "Electronic",
    "Acid House":           "Electronic",
    "Big Beat":             "Electronic",
    "Alternative Dance":    "Electronic",
    "Leftfield":            "Electronic",
    "Progressive House":    "Electronic",
    "Dub":                  "Electronic",
    "Illbient":             "Ambient",

    # ── Rock family collapses ─────────────────────────────────────────────────
    "Britpop":              "Indie Rock",
    "Jangle Pop":           "Indie Rock",
    "Indie Folk":           "Folk",
    "Alternative Pop":      "Alternative Rock",
    "Dance-Rock":           "Alternative Rock",
    "Soft Rock":            "Rock",
    "Acoustic Rock":        "Rock",
    "Arena Rock":           "Hard Rock",
    "Psychobilly":          "Punk",
    "Pop Punk":             "Punk",
    "Post-Hardcore":        "Punk",
    "Cowpunk":              "Punk",
    "Melodic Death Metal":                       "Metal",
    "Groove Metal":                              "Metal",
    "Funk Metal":                                "Metal",
    "Progressive/Technical Sludge/Groove Metal": "Metal",

    # ── Specialty collapses ───────────────────────────────────────────────────
    "Easy Listening":       "Pop",
    "Contemporary R&B":     "Pop",
    "Celtic":               "Folk",
    "Alternative Country":  "Folk",
    "Musical":              "Soundtrack",
    "Conscious Hip Hop":    "Hip-Hop",
    "Worldwide":            "Other",
    "Japanese":             "Other",
    "Instrumental":         "Other",
    "Christian":            "Other",
    "Medieval":             "Other",
    "Children'S Music":     "Other",
    "Crossover Jazz":       "Other",
}

# Sanity check at module load time
for src, dst in CONSOLIDATION_MAP.items():
    assert dst in CANONICAL, f"Target {dst!r} is not in CANONICAL set"
    assert src not in CANONICAL, f"Source {src!r} is already canonical — remove it"


# ── Helpers ───────────────────────────────────────────────────────────────────

def fix_xml_declaration(path: Path) -> None:
    content = path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(content)


def genre_counts(nml_path: Path) -> Counter:
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    return Counter(
        (e.find("INFO").get("GENRE", "") if e.find("INFO") is not None else "")
        for e in coll.findall("ENTRY")
    )


def process_nml(
    nml_path: Path,
    apply: bool,
    label: str,
) -> tuple[Counter, Counter]:
    """
    Apply CONSOLIDATION_MAP to NML entries.
    Returns (before_counts, after_counts).
    """
    ET.register_namespace("", "")
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    entries = coll.findall("ENTRY")

    before: Counter = Counter()
    after: Counter = Counter()
    change_tally: Counter = Counter()

    for e in entries:
        info = e.find("INFO")
        if info is None:
            continue
        g = info.get("GENRE", "").strip()
        before[g] += 1
        new_g = CONSOLIDATION_MAP.get(g, g)
        after[new_g] += 1
        if new_g != g:
            change_tally[f"  {g!r:<50} → {new_g}"] += 1
            if apply:
                info.set("GENRE", new_g)

    if apply:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = nml_path.parent / f"{nml_path.stem}_pre_consolidate_{stamp}.nml"
        shutil.copy2(nml_path, backup)
        print(f"  [{label}] Backup → {backup.name}")
        tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
        fix_xml_declaration(nml_path)
        print(f"  [{label}] Written → {nml_path}")

    return before, after, change_tally


def update_file_tags(nml_path: Path, apply: bool) -> int:
    """Update GENRE tag in audio files. Returns number of files changed."""
    try:
        import mutagen
        import mutagen.mp3
        import mutagen.mp4
        import mutagen.flac
        import mutagen.id3
    except ImportError:
        print("  [WARN] mutagen not available — skipping file tag updates")
        return 0

    ET.register_namespace("", "")
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    changed = skipped = errors = 0

    for e in coll.findall("ENTRY"):
        info = e.find("INFO")
        if info is None:
            continue
        nml_genre = info.get("GENRE", "").strip()
        if not nml_genre:
            continue

        loc = e.find("LOCATION")
        if loc is None:
            continue
        abs_path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        if not Path(abs_path).exists():
            skipped += 1
            continue

        try:
            f = mutagen.File(abs_path, easy=True)
            if f is None:
                skipped += 1
                continue
            current = (f.get("genre") or [""])[0]
            if current == nml_genre:
                continue
            if apply:
                f["genre"] = [nml_genre]
                f.save()
            changed += 1
        except Exception as ex:
            errors += 1
            if errors <= 5:
                print(f"  [ERROR] {abs_path}: {ex}")

    return changed, skipped, errors


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate genres to 23 canonicals")
    parser.add_argument("--apply", action="store_true",
                        help="Write changes to NML files (default: dry-run)")
    parser.add_argument("--tags", action="store_true",
                        help="Also update GENRE tags in audio files (requires mutagen)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Stage 8i — Genre Consolidation [{mode}]")
    print(f"  {len(CONSOLIDATION_MAP)} source genres → {len(CANONICAL)} canonicals\n")

    # ── Process NMLs ─────────────────────────────────────────────────────────
    results = {}
    for nml_path, label in [
        (TRAKTOR_NML, "Traktor NML"),
        (OUR_NML,     "Our NML  "),
    ]:
        if not nml_path.exists():
            print(f"  [{label}] NOT FOUND — skipping")
            continue
        before, after, changes = process_nml(nml_path, args.apply, label)
        results[label] = (before, after, changes)

    # ── Report ───────────────────────────────────────────────────────────────
    # Use Traktor NML as authoritative for the report
    label_ref = "Traktor NML"
    if label_ref not in results:
        label_ref = next(iter(results))
    before, after, changes = results[label_ref]

    non_empty_before = {g: c for g, c in before.items() if g}
    non_empty_after  = {g: c for g, c in after.items()  if g}

    print(f"\n{'─'*60}")
    print(f"Before: {len(non_empty_before)} unique genres  "
          f"({sum(non_empty_before.values())} tracks)")
    print(f"After:  {len(non_empty_after)} unique genres  "
          f"({sum(non_empty_after.values())} tracks)")
    print(f"\nConsolidations ({len(changes)} genre strings remapped):")
    for line, count in sorted(changes.items(), key=lambda x: -x[1]):
        print(f"  {count:5d} ×  {line.strip()}")

    print(f"\n{'─'*60}")
    print("Final genre distribution:")
    for g, c in sorted(non_empty_after.items(), key=lambda x: -x[1]):
        marker = "✓" if g in CANONICAL else "?"
        print(f"  {marker}  {c:6d}  {g}")

    non_canonical_after = {g for g in non_empty_after if g not in CANONICAL}
    if non_canonical_after:
        print(f"\n[WARN] {len(non_canonical_after)} non-canonical genres remain "
              f"(not in CONSOLIDATION_MAP):")
        for g in sorted(non_canonical_after):
            print(f"  ? {non_empty_after[g]:5d}  {g!r}")
    else:
        print(f"\n✓ All genres are canonical ({len(CANONICAL)} total)")

    # ── File tags ─────────────────────────────────────────────────────────────
    if args.tags:
        print(f"\n{'─'*60}")
        print("Updating audio file GENRE tags...")
        changed, skipped, errors = update_file_tags(TRAKTOR_NML, args.apply)
        action = "Updated" if args.apply else "Would update"
        print(f"  {action}: {changed} | Skipped (no file): {skipped} | Errors: {errors}")

    if not args.apply:
        print(f"\nDry-run complete. Run with --apply to write changes.")
    else:
        print(f"\nStage 8i complete. Reload collection in Traktor to verify.")


if __name__ == "__main__":
    main()
