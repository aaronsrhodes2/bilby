#!/usr/bin/env python3
"""
write_nml_lyrics.py — Write full lyrics text into Traktor NML KEY_LYRICS fields.

KEY_LYRICS → full lyrics text (plain UTF-8, newline-delimited lines)

Source:
  state/lyrics_raw.json     — {artist\ttitle: "full lyrics text"}

Usage:
  python3 tools/write_nml_lyrics.py [--dry-run] [--nml PATH]

  --dry-run   Print stats without writing anything
  --nml PATH  Target a specific NML (default: both corrected + live)

Match strategy: artist\\ttitle key, case-insensitive, stripped.
Tracks with no match (instrumentals, not-yet-transcribed) are left unchanged.

IMPORTANT: Close Traktor before running.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE         = Path(__file__).resolve().parent.parent
STATE_DIR    = BASE / "state"
LYRICS_RAW   = STATE_DIR / "lyrics_raw.json"
NML_CORR     = BASE / "corrected_traktor" / "collection.nml"
NML_LIVE     = Path.home() / "Documents" / "Native Instruments" / "Traktor 4.0.2" / "collection.nml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return s.lower().strip()


def _dkey(artist: str, title: str) -> str:
    return f"{_norm(artist)}\t{_norm(title)}"


def load_lyrics_raw(path: Path) -> dict[str, str]:
    """Return {dkey: lyrics_text}. Normalise keys on load."""
    if not path.exists():
        print(f"  [WARN] {path} not found — no lyrics data")
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    # Keys in lyrics_raw.json are already lowercased artist\ttitle
    return {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}


# ── Main NML processing ───────────────────────────────────────────────────────

def process_nml(nml_path: Path, lyrics: dict[str, str], dry_run: bool) -> dict:
    """Process one NML file. Returns stats dict."""
    if not nml_path.exists():
        print(f"  [SKIP] {nml_path} — not found")
        return {}

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Processing: {nml_path}")

    tree = ET.parse(nml_path)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    if coll is None:
        print("  [ERROR] No <COLLECTION> element found")
        return {}

    stats = {
        "total":    0,
        "written":  0,
        "skipped":  0,   # no lyrics found for track
        "already":  0,   # KEY_LYRICS was already populated (overwritten)
    }

    for entry in coll.findall("ENTRY"):
        stats["total"] += 1
        artist = entry.get("ARTIST", "").strip()
        title  = entry.get("TITLE",  "").strip()
        if not artist and not title:
            continue

        info = entry.find("INFO")
        if info is None:
            info = ET.SubElement(entry, "INFO")

        # Track if it had lyrics already (for stats)
        had_lyrics = bool(info.get("KEY_LYRICS", "").strip())

        dk = _dkey(artist, title)
        text = lyrics.get(dk)
        if not text:
            stats["skipped"] += 1
            continue

        if had_lyrics:
            stats["already"] += 1

        if not dry_run:
            info.set("KEY_LYRICS", text)
        stats["written"] += 1

    if not dry_run:
        backup = nml_path.with_suffix(".nml.bak_lyrics")
        shutil.copy2(nml_path, backup)
        print(f"  Backup → {backup.name}")
        tree.write(nml_path, encoding="utf-8", xml_declaration=True)
        print(f"  Written → {nml_path.name}")

    print(f"  Tracks total:        {stats['total']:,}")
    print(f"  KEY_LYRICS written:  {stats['written']:,}")
    print(f"  Already had lyrics:  {stats['already']:,}  (overwritten)")
    print(f"  No match (skipped):  {stats['skipped']:,}")

    return stats


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print stats without writing anything")
    ap.add_argument("--nml", metavar="PATH",
                    help="Target a specific NML file (default: both corrected + live)")
    args = ap.parse_args()

    print("Loading lyrics_raw.json …")
    lyrics = load_lyrics_raw(LYRICS_RAW)
    print(f"  {len(lyrics):,} lyric entries loaded")

    if args.nml:
        nml_paths = [Path(args.nml)]
    else:
        nml_paths = [p for p in [NML_CORR, NML_LIVE] if p.exists()]

    if not nml_paths:
        print("ERROR: No NML files found")
        sys.exit(1)

    for nml_path in nml_paths:
        process_nml(nml_path, lyrics, args.dry_run)

    print("\nDone." if not args.dry_run else "\nDry run complete — no files written.")


if __name__ == "__main__":
    main()
