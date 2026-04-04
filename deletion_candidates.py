#!/usr/bin/env python3
"""
Find deletion candidates in ~/Music:
  1. Video files (any video extension)
  2. Oversized audio files (>50 MB)

Respects the same exclusion dirs as Stage 1.
Outputs a JSON report to state/deletion_candidates.json and prints a summary.
"""

import json
import os
from pathlib import Path

MUSIC_ROOT = Path.home() / "Music"
EXCLUDE_DIRS = {
    str(MUSIC_ROOT / "Traktor"),
    str(MUSIC_ROOT / "Spotify"),
    str(MUSIC_ROOT / "Sounds"),
}

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aiff", ".aif", ".wma", ".opus"}

LARGE_AUDIO_THRESHOLD_MB = 50

STATE_DIR = Path(__file__).parent / "state"


def is_excluded(path: str) -> bool:
    return any(path.startswith(ex) for ex in EXCLUDE_DIRS)


def fmt_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1_048_576:.1f} MB"


def main():
    video_files = []
    large_audio_files = []

    print(f"Scanning {MUSIC_ROOT} ...")
    for dirpath, dirnames, filenames in os.walk(MUSIC_ROOT):
        # Prune excluded dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded(os.path.join(dirpath, d))
        ]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if is_excluded(full):
                continue
            ext = os.path.splitext(fname)[1].lower()
            try:
                size = os.path.getsize(full)
            except OSError:
                continue

            if ext in VIDEO_EXTS:
                video_files.append({"path": full, "ext": ext, "size_bytes": size})
            elif ext in AUDIO_EXTS and size > LARGE_AUDIO_THRESHOLD_MB * 1_048_576:
                large_audio_files.append({"path": full, "ext": ext, "size_bytes": size})

    # Sort by size descending
    video_files.sort(key=lambda x: -x["size_bytes"])
    large_audio_files.sort(key=lambda x: -x["size_bytes"])

    total_video_bytes = sum(f["size_bytes"] for f in video_files)
    total_large_audio_bytes = sum(f["size_bytes"] for f in large_audio_files)

    # Print video summary
    print(f"\n{'═'*65}")
    print(f"VIDEO FILES  ({len(video_files):,} files, {fmt_mb(total_video_bytes)} total)")
    print(f"{'─'*65}")
    for f in video_files:
        print(f"  {fmt_mb(f['size_bytes']):>10}  {f['path']}")

    # Print large audio summary
    print(f"\n{'═'*65}")
    print(f"LARGE AUDIO (>{LARGE_AUDIO_THRESHOLD_MB} MB)  ({len(large_audio_files):,} files, {fmt_mb(total_large_audio_bytes)} total)")
    print(f"{'─'*65}")
    for f in large_audio_files:
        print(f"  {fmt_mb(f['size_bytes']):>10}  {f['path']}")

    print(f"\n{'═'*65}")
    print(f"TOTAL RECOVERABLE: {fmt_mb(total_video_bytes + total_large_audio_bytes)}")
    print(f"  Video:       {fmt_mb(total_video_bytes)}  ({len(video_files):,} files)")
    print(f"  Large audio: {fmt_mb(total_large_audio_bytes)}  ({len(large_audio_files):,} files)")

    # Write JSON report
    report = {
        "large_audio_threshold_mb": LARGE_AUDIO_THRESHOLD_MB,
        "video": {
            "count": len(video_files),
            "total_bytes": total_video_bytes,
            "files": video_files,
        },
        "large_audio": {
            "count": len(large_audio_files),
            "total_bytes": total_large_audio_bytes,
            "files": large_audio_files,
        },
    }
    out = STATE_DIR / "deletion_candidates.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nFull report → {out}")


if __name__ == "__main__":
    main()
