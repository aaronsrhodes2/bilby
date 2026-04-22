#!/usr/bin/env python3
"""
write_nml_comments.py — Write lyric summaries and flags into Traktor NML COMMENT / COMMENT2 fields.

COMMENT  → one-sentence lyric summary (always overwritten)
COMMENT2 → theme | lyric flags | rep tier | song-specific flag (always overwritten)

Sources:
  state/lyrics_dedup.json     — {artist\ttitle: {summary, theme, flags}}
  misc/reputation_flags.json  — {flags: [...], song_flags: [...]}

Usage:
  python3 tools/write_nml_comments.py [--dry-run] [--nml PATH]

  --dry-run   Print stats without writing anything
  --nml PATH  Target a specific NML (default: both corrected + live)

IMPORTANT: Close Traktor before running.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE          = Path(__file__).resolve().parent.parent
STATE_DIR     = BASE / "state"
MISC_DIR      = BASE / "misc"
LYRICS_DEDUP  = STATE_DIR / "lyrics_dedup.json"
REP_FLAGS     = MISC_DIR / "reputation_flags.json"
NML_CORR      = BASE / "corrected_traktor" / "collection.nml"
NML_LIVE      = Path.home() / "Documents" / "Native Instruments" / "Traktor 4.0.2" / "collection.nml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalise artist/title for dict lookup."""
    return s.lower().strip()


def _dkey(artist: str, title: str) -> str:
    return f"{_norm(artist)}\t{_norm(title)}"


def load_lyrics(path: Path) -> dict:
    """Return {dkey: {summary, theme, flags}}."""
    if not path.exists():
        print(f"  [WARN] {path} not found — no lyrics data")
        return {}
    with open(path) as f:
        return json.load(f)


def build_rep_index(path: Path) -> tuple[dict, dict]:
    """
    Returns:
      artist_rep  — {lowercase_artist_name: {tier, name}}
      song_rep    — {(lowercase_artist, lowercase_title): reason}
    """
    artist_rep: dict[str, dict] = {}
    song_rep:   dict[tuple, str] = {}
    if not path.exists():
        print(f"  [WARN] {path} not found — no reputation data")
        return artist_rep, song_rep

    with open(path) as f:
        data = json.load(f)

    for entry in data.get("flags", []):
        tier   = entry.get("tier", "")
        name   = entry.get("name", "")
        for artist_name in entry.get("artists", []):
            key = _norm(artist_name)
            # Keep the most severe tier if an artist appears more than once
            existing = artist_rep.get(key)
            tier_rank = {"convicted": 3, "accused": 2, "settled": 1}
            if not existing or tier_rank.get(tier, 0) > tier_rank.get(existing["tier"], 0):
                artist_rep[key] = {"tier": tier, "name": name}

    for sf in data.get("song_flags", []):
        key = (_norm(sf["artist"]), _norm(sf["title"]))
        song_rep[key] = sf.get("reason", "flagged")

    return artist_rep, song_rep


def build_comment2(
    theme: str,
    lyric_flags: list[str],
    artist_tier: str | None,   # e.g. "convicted"
    artist_name: str | None,   # e.g. "Ian Watkins"
    song_reason: str | None,   # e.g. "Contains a racial slur"
) -> str:
    """
    Build COMMENT2 string from available data.
    Format: theme | ⚑ flag1 ⚑ flag2 | ⚑ rep:convicted (Ian Watkins) | ⚑ song: reason
    Returns empty string if nothing to write.
    """
    parts: list[str] = []

    if theme:
        parts.append(theme)

    if lyric_flags:
        parts.append(" ".join(f"⚑{f}" for f in lyric_flags))

    if artist_tier and artist_name:
        parts.append(f"⚑rep:{artist_tier} ({artist_name})")

    if song_reason:
        # Keep reason short — truncate at 60 chars
        short = song_reason[:60] + ("…" if len(song_reason) > 60 else "")
        parts.append(f"⚑song:{short}")

    return " | ".join(parts)


def get_entry_artist_title(entry: ET.Element) -> tuple[str, str]:
    """Extract artist and title from a Traktor ENTRY element."""
    artist = entry.get("ARTIST", "")
    title  = entry.get("TITLE", "")
    return artist, title


# ── Main NML processing ───────────────────────────────────────────────────────

def process_nml(
    nml_path: Path,
    lyrics: dict,
    artist_rep: dict,
    song_rep: dict,
    dry_run: bool,
) -> dict:
    """Process one NML file. Returns stats dict."""
    if not nml_path.exists():
        print(f"  [SKIP] {nml_path} — not found")
        return {}

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Processing: {nml_path}")

    ET.register_namespace("", "")
    tree = ET.parse(nml_path)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    if coll is None:
        print("  [ERROR] No <COLLECTION> element found")
        return {}

    stats = {
        "total":         0,
        "comment_set":   0,
        "comment2_set":  0,
        "comment_blank": 0,  # had no summary
        "rep_flagged":   0,
        "song_flagged":  0,
        "lyric_flagged": 0,
    }

    for entry in coll.findall("ENTRY"):
        stats["total"] += 1
        artist, title = get_entry_artist_title(entry)

        info = entry.find("INFO")
        if info is None:
            info = ET.SubElement(entry, "INFO")

        # ── Lyrics lookup ─────────────────────────────────────────────────────
        dk = _dkey(artist, title)
        lyric_data = lyrics.get(dk, {})
        summary     = lyric_data.get("summary") or ""
        theme       = lyric_data.get("theme")   or ""
        lyric_flags = lyric_data.get("flags")   or []

        # ── Reputation lookup ─────────────────────────────────────────────────
        rep = artist_rep.get(_norm(artist))
        artist_tier = rep["tier"]  if rep else None
        artist_name = rep["name"]  if rep else None

        song_reason = song_rep.get((_norm(artist), _norm(title)))

        # ── Build values ──────────────────────────────────────────────────────
        comment  = summary  # always overwrite (Captain's order)
        comment2 = build_comment2(theme, lyric_flags, artist_tier, artist_name, song_reason)

        # ── Write ─────────────────────────────────────────────────────────────
        if comment:
            if not dry_run:
                info.set("COMMENT", comment)
            stats["comment_set"] += 1
        else:
            stats["comment_blank"] += 1

        if comment2:
            if not dry_run:
                info.set("COMMENT2", comment2)
            stats["comment2_set"] += 1

        if artist_tier:
            stats["rep_flagged"] += 1
        if song_reason:
            stats["song_flagged"] += 1
        if lyric_flags:
            stats["lyric_flagged"] += 1

    if not dry_run:
        # Backup + write
        backup = nml_path.with_suffix(".nml.bak_comments")
        shutil.copy2(nml_path, backup)
        print(f"  Backup → {backup.name}")

        # Write with xml declaration, preserving encoding
        tree.write(nml_path, encoding="utf-8", xml_declaration=True)
        print(f"  Written → {nml_path.name}")

    # Print stats
    print(f"  Tracks:         {stats['total']:,}")
    print(f"  COMMENT set:    {stats['comment_set']:,}  (blank/no summary: {stats['comment_blank']:,})")
    print(f"  COMMENT2 set:   {stats['comment2_set']:,}")
    print(f"    ↳ lyric flags:  {stats['lyric_flagged']:,}")
    print(f"    ↳ rep flags:    {stats['rep_flagged']:,}")
    print(f"    ↳ song flags:   {stats['song_flagged']:,}")

    return stats


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print stats without writing anything")
    ap.add_argument("--nml", metavar="PATH",
                    help="Target a specific NML file (default: both corrected + live)")
    args = ap.parse_args()

    print("Loading lyrics data …")
    lyrics = load_lyrics(LYRICS_DEDUP)
    print(f"  {len(lyrics):,} lyric entries loaded")

    print("Loading reputation flags …")
    artist_rep, song_rep = build_rep_index(REP_FLAGS)
    print(f"  {len(artist_rep):,} artist reputation entries")
    print(f"  {len(song_rep):,} song-specific flags")

    if args.nml:
        nml_paths = [Path(args.nml)]
    else:
        nml_paths = [p for p in [NML_CORR, NML_LIVE] if p.exists()]

    if not nml_paths:
        print("ERROR: No NML files found")
        sys.exit(1)

    for nml_path in nml_paths:
        process_nml(nml_path, lyrics, artist_rep, song_rep, args.dry_run)

    print("\nDone." if not args.dry_run else "\nDry run complete — no files written.")


if __name__ == "__main__":
    main()
