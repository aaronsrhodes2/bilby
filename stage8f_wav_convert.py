#!/usr/bin/env python3
"""
Stage 8f — WAV → MP3 Conversion

Surveys all WAV entries in collection.nml and applies three rules:

  RULE 1 — Delete non-songs:
    Files ≤ 30s OR matching suspicious filename patterns (Loop Recorder,
    Recording YYYY-MM-DD, Closer to Spice, 01 Soylent Green, etc.).
    Removes NML entry + deletes file.

  RULE 2 — Deduplicate before converting:
    When multiple NML entries share the same (artist, normalized-title),
    keep the one with the largest file size (best quality source).
    Delete the others from NML + disk.

  RULE 3 — Convert survivors to MP3:
    ffmpeg -codec:a libmp3lame -q:a 0 (LAME V0 VBR, ~245 kbps average,
    transparent quality from lossless WAV source).
    Preserves all ID3 metadata via ffmpeg.
    Updates NML LOCATION to .mp3, deletes the original WAV.

Dry-run by default.

Usage:
    python3 stage8f_wav_convert.py          # dry-run report
    python3 stage8f_wav_convert.py --apply  # execute
    python3 stage8f_wav_convert.py --apply --quality 320k  # CBR 320 instead of V0
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs, abs_to_traktor_location

STATE_DIR   = Path(__file__).parent / "state"
TRAKTOR_DIR = Path(__file__).parent / "corrected_traktor"
NML_SOURCE  = TRAKTOR_DIR / "collection.nml"
CORRECTED   = Path(__file__).parent / "corrected_music"

ET.register_namespace("", "")

# ---------------------------------------------------------------------------
# Non-song detection
# ---------------------------------------------------------------------------

# Max duration (seconds) to classify as a non-song clip
NON_SONG_MAX_SECONDS = 30

# Filename patterns that identify recordings/fragments regardless of duration
NON_SONG_FILENAME_RE = re.compile(
    r'(loop.?recorder|recording\s+\d{4}[-_]\d{2}[-_]\d{2}|'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}-\d{2}-\d{2}|'
    r'closer.to.spice|soylent.green)',
    re.IGNORECASE,
)

# Title patterns that identify non-song entries
NON_SONG_TITLE_RE = re.compile(
    r'^(loop.?recorder|recording\s+\d{4}|'
    r'intro|track\s+\d+|track.by.track.*intro|'
    r'\(\d+\)\s+(synth|organ))',
    re.IGNORECASE,
)


def is_non_song(path: str, playtime: int, title: str) -> str | None:
    """Return reason string if this file should be deleted, else None."""
    fname = os.path.basename(path)
    stem = os.path.splitext(fname)[0]

    if playtime > 0 and playtime <= NON_SONG_MAX_SECONDS:
        return f"duration {playtime}s ≤ {NON_SONG_MAX_SECONDS}s"
    if NON_SONG_FILENAME_RE.search(stem):
        return f"filename matches non-song pattern"
    if title and NON_SONG_TITLE_RE.match(title):
        return f"title matches non-song pattern: {title!r}"
    return None


# ---------------------------------------------------------------------------
# Normalization for dedup matching
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """
    Lowercase, strip noise for dedup grouping.
    Strips:
      - Leading track numbers: "03 Song" → "song"
      - Trailing artist name (after " - ") appended to title: "Song - Artist" → "song"
      - Trailing version number: "Song 1" / "Song 2" → "song"
      - All punctuation
    """
    t = title.lower().strip()
    # Strip leading track number: "03 title" or "03 - title"
    t = re.sub(r'^\d{1,3}\s*[-.]?\s*', '', t).strip()
    # Strip trailing "- Artist Name" (artist appended to title in filename)
    t = re.sub(r'\s+-\s+\w[\w\s]*$', '', t).strip()
    # Strip trailing version/copy number: " 1", " 2", " 3"
    t = re.sub(r'\s+\d+$', '', t).strip()
    # Strip punctuation
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


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


def update_entry_location(entry: ET.Element, new_path: str):
    """Rewrite LOCATION sub-element to point at new_path."""
    loc = entry.find("LOCATION")
    if loc is None:
        return
    loc_parts = abs_to_traktor_location(new_path)
    loc.set("VOLUME", loc_parts["VOLUME"])
    loc.set("DIR", loc_parts["DIR"])
    loc.set("FILE", loc_parts["FILE"])


# ---------------------------------------------------------------------------
# Main analysis pass
# ---------------------------------------------------------------------------

def analyse(root: ET.Element) -> tuple[list, list, list]:
    """
    Returns (delete_entries, dup_losers, convert_entries)
    Each item: (entry, path, playtime, size, artist, title, reason)
    """
    collection = root.find("COLLECTION")
    if collection is None:
        return [], [], []

    # Collect all WAV entries
    wav_entries = []
    for entry in collection.findall("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None:
            continue
        if not loc.get("FILE", "").lower().endswith(".wav"):
            continue
        path = entry_abs_path(entry)
        if not path:
            continue
        info = entry.find("INFO")
        playtime = int((info.get("PLAYTIME", 0) if info is not None else 0) or 0)
        artist = (entry.get("ARTIST") or "").strip()
        title  = (entry.get("TITLE") or "").strip()
        size   = os.path.getsize(path) if os.path.exists(path) else -1
        wav_entries.append((entry, path, playtime, size, artist, title))

    # Rule 1: non-song detection
    delete_entries = []
    survivors = []
    for item in wav_entries:
        entry, path, playtime, size, artist, title = item
        reason = is_non_song(path, playtime, title)
        if reason:
            delete_entries.append((*item, reason))
        else:
            survivors.append(item)

    # Rule 2: deduplication — group by (artist, normalized_title)
    groups: dict[tuple, list] = {}
    for item in survivors:
        entry, path, playtime, size, artist, title = item
        key = (artist.lower(), normalize_title(title))
        groups.setdefault(key, []).append(item)

    dup_losers = []
    convert_entries = []
    for key, items in groups.items():
        if len(items) == 1:
            convert_entries.append((*items[0], "single copy"))
        else:
            # Keep the largest file (best quality WAV)
            items_valid = [i for i in items if i[3] >= 0]
            if not items_valid:
                continue
            winner = max(items_valid, key=lambda x: x[3])
            convert_entries.append((*winner, f"kept (largest of {len(items)})"))
            for item in items_valid:
                if item is not winner:
                    dup_losers.append((*item, f"duplicate of {os.path.basename(winner[1])}"))

    # Second dedup pass: cross-artist matching by (normalized_title, duration ±2s)
    # Catches cases like Abiotha/Noise Unit "Inner Chaos" where artist differs
    # but it's clearly the same recording.
    already_decided = {id(i[0]) for i in dup_losers} | {id(i[0]) for i in convert_entries}
    remaining_convert = list(convert_entries)
    convert_entries = []

    # Group remaining by (norm_title, rounded_duration)
    cross_groups: dict[tuple, list] = {}
    for item in remaining_convert:
        entry, path, playtime, size, artist, title = item[:6]
        key2 = (normalize_title(title), round(playtime / 5) * 5)  # duration bucket ±5s
        cross_groups.setdefault(key2, []).append(item)

    for key2, items in cross_groups.items():
        items_valid = [i for i in items if i[3] >= 0]
        if len(items_valid) == 1:
            convert_entries.append(items_valid[0])
        elif len(items_valid) > 1:
            # Check if sizes are within 1% of each other → same recording
            sizes = [i[3] for i in items_valid]
            max_sz, min_sz = max(sizes), min(sizes)
            if min_sz > 0 and (max_sz - min_sz) / max_sz < 0.02:
                # Same recording, different artist credits → keep largest
                winner = max(items_valid, key=lambda x: x[3])
                convert_entries.append((*winner[:6], f"kept (cross-artist dup, largest of {len(items_valid)})"))
                for item in items_valid:
                    if item is not winner:
                        dup_losers.append((*item[:6], f"cross-artist dup of {os.path.basename(winner[1])}"))
            else:
                # Different sizes → genuinely different recordings
                for item in items_valid:
                    convert_entries.append(item)

    # Third dedup pass: size+duration fingerprint
    # Any two remaining WAVs within 1% file size AND 2s duration → same recording.
    # Catches cases where title normalization diverges (encoding variants, etc.).
    # Prefers entry with more metadata (non-empty artist); else largest file.
    remaining2 = list(convert_entries)
    convert_entries = []
    processed3: set[int] = set()

    for i, item_a in enumerate(remaining2):
        if id(item_a[0]) in processed3:
            continue
        ea, pa, pta, sza, arta, tita = item_a[:6]
        matched = [item_a]
        for j, item_b in enumerate(remaining2):
            if j <= i or id(item_b[0]) in processed3:
                continue
            eb, pb, ptb, szb, artb, titb = item_b[:6]
            if sza <= 0 or szb <= 0:
                continue
            # Only fingerprint-match if same artist (or at least one has no artist)
            # to avoid false positives between different songs of similar length
            art_a_norm = (arta or "").strip().lower()
            art_b_norm = (artb or "").strip().lower()
            if art_a_norm and art_b_norm and art_a_norm != art_b_norm:
                continue
            size_ratio = abs(sza - szb) / max(sza, szb)
            dur_diff = abs(pta - ptb)
            if size_ratio < 0.01 and dur_diff <= 2:
                matched.append(item_b)

        if len(matched) == 1:
            convert_entries.append(item_a)
            processed3.add(id(ea))
        else:
            # Pick winner: prefer non-empty artist, then largest file
            def winner_key(m):
                return (1 if m[4] else 0, m[3])
            winner3 = max(matched, key=winner_key)
            convert_entries.append((*winner3[:6], f"kept (size/dur match, {len(matched)} copies)"))
            processed3.add(id(winner3[0]))
            for m in matched:
                processed3.add(id(m[0]))
                if m is not winner3:
                    dup_losers.append((*m[:6], f"size/dur dup of {os.path.basename(winner3[1])}"))

    return delete_entries, dup_losers, convert_entries


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def convert_wav_to_mp3(wav_path: str, quality: str) -> str | None:
    """
    Convert wav_path to .mp3 in the same directory.
    quality: "V0" (LAME VBR best) or e.g. "320k" (CBR).
    Returns mp3_path on success, None on failure.
    """
    mp3_path = os.path.splitext(wav_path)[0] + ".mp3"

    if quality.upper() == "V0":
        codec_args = ["-codec:a", "libmp3lame", "-q:a", "0"]
    else:
        # e.g. "320k"
        codec_args = ["-codec:a", "libmp3lame", "-b:a", quality]

    cmd = [
        "ffmpeg", "-y",          # overwrite if exists
        "-i", wav_path,
        *codec_args,
        "-map_metadata", "0",   # copy all metadata
        "-id3v2_version", "3",  # ID3v2.3 (Traktor compatible)
        mp3_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"  [ERROR] ffmpeg failed for {os.path.basename(wav_path)}:")
        print(f"    {result.stderr.decode()[-300:]}")
        return None
    return mp3_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Convert WAV files to MP3")
    parser.add_argument("--apply", action="store_true",
                        help="Execute: delete non-songs, dedup, convert, update NML")
    parser.add_argument("--quality", default="V0",
                        help="MP3 quality: 'V0' (LAME VBR best) or e.g. '320k' (default: V0)")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install with: brew install ffmpeg")
        return

    print("Stage 8f: WAV → MP3 Conversion")
    print("=" * 60)
    print(f"\nLoading {NML_SOURCE.name}...")
    tree = ET.parse(NML_SOURCE)
    root = tree.getroot()
    collection = root.find("COLLECTION")

    delete_entries, dup_losers, convert_entries = analyse(root)

    total_wav_mb  = sum(i[3] for i in delete_entries + dup_losers + convert_entries
                        if i[3] >= 0) / 1024**2
    delete_mb     = sum(i[3] for i in delete_entries if i[3] >= 0) / 1024**2
    dup_mb        = sum(i[3] for i in dup_losers if i[3] >= 0) / 1024**2
    convert_mb    = sum(i[3] for i in convert_entries if i[3] >= 0) / 1024**2

    print(f"\n  Total WAV files:    {len(delete_entries)+len(dup_losers)+len(convert_entries):3d}  ({total_wav_mb:.0f} MB)")
    print(f"  → Delete (non-song):{len(delete_entries):3d}  ({delete_mb:.0f} MB)")
    print(f"  → Delete (duplicate):{len(dup_losers):3d}  ({dup_mb:.0f} MB)")
    print(f"  → Convert to MP3:   {len(convert_entries):3d}  ({convert_mb:.0f} MB)")
    print(f"  Quality: LAME {args.quality}")

    print(f"\n--- NON-SONGS (will be deleted) ---")
    for entry, path, pt, sz, artist, title, reason in delete_entries:
        mins = pt//60; secs = pt%60
        print(f"  {sz/1024:6.0f} KB  {mins}:{secs:02d}  {reason}")
        print(f"           {os.path.basename(path)}")

    print(f"\n--- DUPLICATES (losing copies, will be deleted) ---")
    for entry, path, pt, sz, artist, title, reason in dup_losers:
        print(f"  {sz/1024**2:5.1f} MB  {artist or '(unknown)':20s}  {title}")
        print(f"           {os.path.basename(path)}  [{reason}]")

    print(f"\n--- CONVERT TO MP3 ---")
    for entry, path, pt, sz, artist, title, reason in sorted(convert_entries,
                                                              key=lambda x: x[4]+x[5]):
        mins = pt//60; secs = pt%60
        print(f"  {sz/1024**2:5.1f} MB  {mins}:{secs:02d}  {artist or '(unknown)':25s}  {title}")

    if not args.apply:
        print(f"\nDry-run complete.  Run with --apply to execute.")
        print(f"Estimated MP3 size after conversion: ~{convert_mb/6:.0f} MB "
              f"(~6× compression at V0)")
        return

    # -----------------------------------------------------------------------
    # Apply
    # -----------------------------------------------------------------------

    print(f"\n{'='*60}")
    print(f"APPLYING")
    print(f"{'='*60}")

    paths_to_remove_from_nml: set[str] = set()
    entry_to_new_path: dict[int, str] = {}   # id(entry) → new mp3 path

    # Step 1: Delete non-songs
    print(f"\nStep 1: Deleting {len(delete_entries)} non-song files...")
    for entry, path, pt, sz, artist, title, reason in delete_entries:
        paths_to_remove_from_nml.add(path)
        if os.path.exists(path):
            os.remove(path)
            print(f"  deleted  {os.path.basename(path)}")
        else:
            print(f"  missing  {os.path.basename(path)}")

    # Step 2: Delete duplicate losers
    print(f"\nStep 2: Deleting {len(dup_losers)} duplicate files...")
    for entry, path, pt, sz, artist, title, reason in dup_losers:
        paths_to_remove_from_nml.add(path)
        if os.path.exists(path):
            os.remove(path)
            print(f"  deleted  {os.path.basename(path)}")
        else:
            print(f"  missing  {os.path.basename(path)}")

    # Step 3: Convert survivors
    print(f"\nStep 3: Converting {len(convert_entries)} WAV files to MP3...")
    converted_ok = 0
    converted_fail = 0
    for entry, path, pt, sz, artist, title, reason in convert_entries:
        if not os.path.exists(path):
            print(f"  [SKIP]   {os.path.basename(path)} — not on disk")
            continue
        print(f"  converting  {os.path.basename(path)} ({sz/1024**2:.1f} MB)...")
        mp3_path = convert_wav_to_mp3(path, args.quality)
        if mp3_path:
            mp3_size = os.path.getsize(mp3_path) / 1024**2
            print(f"  → {os.path.basename(mp3_path)} ({mp3_size:.1f} MB)")
            entry_to_new_path[id(entry)] = mp3_path
            os.remove(path)  # delete source WAV after successful conversion
            converted_ok += 1
        else:
            converted_fail += 1

    print(f"\n  Converted: {converted_ok} | Failed: {converted_fail}")

    # Step 4: Update NML
    print(f"\nStep 4: Updating collection.nml...")
    if collection is None:
        print("  [ERROR] No COLLECTION element")
        return

    entries_removed = 0
    entries_updated = 0

    for entry in list(collection.findall("ENTRY")):
        path = entry_abs_path(entry)
        if path in paths_to_remove_from_nml:
            collection.remove(entry)
            entries_removed += 1
        elif id(entry) in entry_to_new_path:
            new_path = entry_to_new_path[id(entry)]
            update_entry_location(entry, new_path)
            # Update file extension in INFO if present
            info = entry.find("INFO")
            if info is not None:
                info.set("FILETYPE", "MP3")
            entries_updated += 1

    collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))

    tree.write(str(NML_SOURCE), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(NML_SOURCE)

    print(f"  NML entries removed: {entries_removed}")
    print(f"  NML entries updated: {entries_updated} (WAV → MP3 path)")

    # Clean up empty dirs
    for dirpath, dirnames, filenames in os.walk(str(CORRECTED), topdown=False):
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    print(f"\nStage 8f complete.")
    print(f"  {converted_ok} WAV files converted to MP3 ({args.quality})")
    print(f"  {entries_removed} NML entries removed (non-songs + duplicates)")
    print(f"  {entries_updated} NML entries updated to .mp3 paths")


if __name__ == "__main__":
    main()
