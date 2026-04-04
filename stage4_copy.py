#!/usr/bin/env python3
"""
Stage 4 — Apply Metadata & Copy Files

For each winner file:
  1. Build corrected tags from metadata.json
  2. Copy file to corrected_music/Artist/Album (Year)/NN - Title.ext
  3. Write corrected tags to the copy (never touching source)
  4. Record every source path (winner + all losers) → new path in state/path_map.json

Reads:  state/dedup.json, state/metadata.json
Writes: corrected_music/, state/path_map.json
"""

import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TPE1, TIT2, TALB, TRCK, TDRC, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from tqdm import tqdm

STATE_DIR = Path(__file__).parent / "state"
# Use metadata-dedup result if available (from stage2b --apply), else base dedup
DEDUP_JSON = STATE_DIR / "dedup_final.json" if (STATE_DIR / "dedup_final.json").exists() \
             else STATE_DIR / "dedup.json"
METADATA_JSON = STATE_DIR / "metadata.json"
OUTPUT_JSON = STATE_DIR / "path_map.json"
REVIEW_JSON = Path(__file__).parent / "review.json"

DEST_ROOT = Path(__file__).parent / "corrected_music"
SKIPPED_LARGE_JSON = STATE_DIR / "skipped_large.json"

UNSAFE_CHARS = r'\/:*?"<>|'

# Files larger than this are mixes/full-albums/WAV masters — skip them.
SKIP_ABOVE_BYTES = 50 * 1_048_576  # 50 MB


def sanitize(name: str, max_len: int = 100) -> str:
    """Make a string safe to use as a filename/directory component."""
    name = unicodedata.normalize("NFKC", name.strip())
    for ch in UNSAFE_CHARS:
        name = name.replace(ch, "_")
    # Collapse multiple spaces/underscores
    name = re.sub(r"[ _]{2,}", " ", name)
    name = name.strip(" ._")
    return name[:max_len] if name else "Unknown"


def make_dest_path(
    dest_root: Path,
    artist: str,
    album: str,
    year: int | str | None,
    track_number: str | int | None,
    title: str,
    ext: str,
    used_paths: set[str],
) -> Path:
    """Build Apple Music–style dest path, handling collisions."""
    artist_s = sanitize(artist or "Unknown Artist")
    title_s = sanitize(title or "Unknown Title")

    # Album folder: "Album Name (Year)" or just "Album Name"
    if album:
        album_s = sanitize(album)
        if year:
            album_folder = f"{album_s} ({year})"
        else:
            album_folder = album_s
    else:
        album_folder = "Unknown Album"

    # Track prefix: "02 - " or just empty
    if track_number is not None:
        try:
            # Handle "2/12" format
            tn = str(track_number).split("/")[0]
            track_prefix = f"{int(tn):02d} - "
        except (ValueError, TypeError):
            track_prefix = ""
    else:
        track_prefix = ""

    stem = f"{track_prefix}{title_s}"
    folder = dest_root / artist_s / album_folder
    base = folder / (stem + ext)

    # Collision handling
    candidate = str(base)
    if candidate not in used_paths:
        return base

    suffix = 2
    while True:
        candidate = str(folder / f"{stem}_{suffix}{ext}")
        if candidate not in used_paths:
            return Path(candidate)
        suffix += 1


def write_tags_mp3(path: Path, meta: dict):
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TPE1(encoding=3, text=meta.get("artist", "")))
        tags.add(TIT2(encoding=3, text=meta.get("title", "")))
        if meta.get("album"):
            tags.add(TALB(encoding=3, text=meta["album"]))
        if meta.get("track_number") is not None:
            tags.add(TRCK(encoding=3, text=str(meta["track_number"])))
        if meta.get("year"):
            tags.add(TDRC(encoding=3, text=str(meta["year"])))
        tags.save(path)
    except Exception as e:
        print(f"\n[WARN] Could not write MP3 tags to {path}: {e}")


def write_tags_m4a(path: Path, meta: dict):
    try:
        tags = MP4(path)
        tags["©nam"] = [meta.get("title", "")]
        tags["©ART"] = [meta.get("artist", "")]
        if meta.get("album"):
            tags["©alb"] = [meta["album"]]
        if meta.get("track_number") is not None:
            try:
                tn = int(str(meta["track_number"]).split("/")[0])
                tags["trkn"] = [(tn, 0)]
            except (ValueError, TypeError):
                pass
        if meta.get("year"):
            tags["©day"] = [str(meta["year"])]
        tags.save()
    except Exception as e:
        print(f"\n[WARN] Could not write M4A tags to {path}: {e}")


def write_tags_flac(path: Path, meta: dict):
    try:
        tags = FLAC(path)
        tags["title"] = [meta.get("title", "")]
        tags["artist"] = [meta.get("artist", "")]
        if meta.get("album"):
            tags["album"] = [meta["album"]]
        if meta.get("track_number") is not None:
            tags["tracknumber"] = [str(meta["track_number"])]
        if meta.get("year"):
            tags["date"] = [str(meta["year"])]
        tags.save()
    except Exception as e:
        print(f"\n[WARN] Could not write FLAC tags to {path}: {e}")


def write_tags_generic(path: Path, meta: dict):
    """For WAV, OGG, OPUS — use mutagen easy interface."""
    try:
        f = MutagenFile(path, easy=True)
        if f is None:
            return
        if meta.get("title"):
            f["title"] = [meta["title"]]
        if meta.get("artist"):
            f["artist"] = [meta["artist"]]
        if meta.get("album"):
            f["album"] = [meta["album"]]
        if meta.get("track_number") is not None:
            f["tracknumber"] = [str(meta["track_number"])]
        if meta.get("year"):
            f["date"] = [str(meta["year"])]
        f.save()
    except Exception as e:
        print(f"\n[WARN] Could not write tags to {path}: {e}")


def write_tags(path: Path, ext: str, meta: dict):
    ext = ext.lower()
    if ext == ".mp3":
        write_tags_mp3(path, meta)
    elif ext in (".m4a", ".mp4"):
        write_tags_m4a(path, meta)
    elif ext == ".flac":
        write_tags_flac(path, meta)
    else:
        write_tags_generic(path, meta)


def main():
    if not DEDUP_JSON.exists():
        print("dedup.json not found — run stage2_dedup.py first")
        sys.exit(1)
    if not METADATA_JSON.exists():
        print("metadata.json not found — run stage3_fingerprint.py first")
        sys.exit(1)

    if OUTPUT_JSON.exists():
        print(f"path_map.json already exists. Delete {OUTPUT_JSON} to re-run Stage 4.")
        return

    print("Stage 4: Loading data...")
    dedup = json.loads(DEDUP_JSON.read_text())
    meta_data = json.loads(METADATA_JSON.read_text())

    groups = dedup["groups"]
    tracks = meta_data["tracks"]  # sha256 → metadata
    old_to_sha = dedup["old_to_winner_sha"]

    DEST_ROOT.mkdir(parents=True, exist_ok=True)

    path_map: dict[str, str] = {}   # old_abs_path → new_abs_path
    used_dest_paths: set[str] = set()
    collisions = []
    copy_errors = []
    skipped_large = []

    print(f"  Processing {len(groups):,} unique tracks...")
    print(f"  Skipping files > {SKIP_ABOVE_BYTES // 1_048_576} MB (mixes/masters)")

    for sha, group_info in tqdm(groups.items(), desc="Copying", unit="track"):
        winner_src = group_info["winner"]
        losers = group_info["losers"]
        ext = group_info["winner_format"]

        # Skip large files (mixes, full albums, WAV masters)
        try:
            src_size = os.path.getsize(winner_src)
        except OSError:
            src_size = 0
        if src_size > SKIP_ABOVE_BYTES:
            skipped_large.append({"sha256": sha, "path": winner_src, "size_bytes": src_size})
            continue

        meta = tracks.get(sha, {})
        artist = meta.get("artist", "") or "Unknown Artist"
        title = meta.get("title", "") or os.path.splitext(os.path.basename(winner_src))[0]
        album = meta.get("album", "")
        year = meta.get("year")
        track_number = meta.get("track_number")

        dest_path = make_dest_path(
            DEST_ROOT, artist, album, year, track_number, title, ext, used_dest_paths
        )

        # Track collision
        dest_str = str(dest_path)
        if dest_str in used_dest_paths:
            collisions.append({"sha256": sha, "path": winner_src, "dest": dest_str})
        used_dest_paths.add(dest_str)

        # Copy the file
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(winner_src, dest_path)
            # Strip macOS immutable flag (uchg) if inherited from source
            try:
                import subprocess
                subprocess.run(["chflags", "nouchg", str(dest_path)], check=False)
            except Exception:
                pass
            write_tags(dest_path, ext, meta)
        except OSError as e:
            copy_errors.append({"path": winner_src, "error": str(e)})
            continue

        # Map winner and all losers → new path
        path_map[winner_src] = dest_str
        for loser in losers:
            path_map[loser] = dest_str

    print(f"\n  Copied {len(path_map) - len(copy_errors):,} files")
    if skipped_large:
        skipped_bytes = sum(s["size_bytes"] for s in skipped_large)
        print(f"  {len(skipped_large)} files skipped (>{SKIP_ABOVE_BYTES // 1_048_576} MB) — {skipped_bytes / 1_048_576:.0f} MB not copied")
        SKIPPED_LARGE_JSON.write_text(json.dumps(skipped_large, ensure_ascii=False, indent=2))
    if copy_errors:
        print(f"  {len(copy_errors)} copy errors")
    if collisions:
        print(f"  {len(collisions)} filename collisions (disambiguated with _2, _3 suffixes)")

    # Write path map
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(path_map, ensure_ascii=False, indent=2))
    print(f"  path_map.json: {len(path_map):,} entries → {OUTPUT_JSON}")

    # Append collisions to review.json
    if collisions:
        existing = []
        if REVIEW_JSON.exists():
            try:
                existing = json.loads(REVIEW_JSON.read_text())
            except Exception:
                pass
        for c in collisions:
            existing.append({"sha256": c["sha256"], "path": c["path"], "reason": "filename_collision", "dest": c["dest"]})
        REVIEW_JSON.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
