#!/usr/bin/env python3
"""
Stage 1 — Scan & Hash

Walk ~/Music recursively, compute SHA-256 of every audio file,
and write state/scan.json.

Skips: ~/Music/Traktor (DJ software files, not source music)
       ~/Music/Spotify  (encrypted, unreadable)
       ~/Music/Sounds   (sound effects, not music)
"""

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

MUSIC_ROOT = Path.home() / "Music"
STATE_DIR = Path(__file__).parent / "state"
OUTPUT = STATE_DIR / "scan.json"
CHECKPOINT = STATE_DIR / "scan.json.partial"

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aiff", ".aif", ".wma", ".opus"}

# Skip these top-level subdirectories of ~/Music
SKIP_DIRS = {
    str(MUSIC_ROOT / "Traktor"),
    str(MUSIC_ROOT / "Spotify"),
    str(MUSIC_ROOT / "Sounds"),
}

CHUNK_SIZE = 1024 * 1024  # 1 MB


def is_audio(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS


def should_skip(dirpath: str) -> bool:
    return dirpath in SKIP_DIRS or any(dirpath.startswith(s + os.sep) for s in SKIP_DIRS)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def collect_files() -> list[str]:
    """Quick pass to collect all audio file paths."""
    files = []
    for dirpath, dirnames, filenames in os.walk(MUSIC_ROOT):
        if should_skip(dirpath):
            dirnames.clear()
            continue
        # Skip hidden directories in-place
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for fname in filenames:
            if is_audio(fname):
                files.append(os.path.join(dirpath, fname))
    return files


def hash_file(path: str) -> dict | None:
    try:
        stat = os.stat(path)
        sha = sha256_file(path)
        return {
            "path": path,
            "sha256": sha,
            "size_bytes": stat.st_size,
            "ext": os.path.splitext(path)[1].lower(),
            "mtime": stat.st_mtime,
        }
    except PermissionError:
        print(f"\n[SKIP] Permission denied: {path}", file=sys.stderr)
        return None
    except OSError as e:
        print(f"\n[SKIP] Error reading {path}: {e}", file=sys.stderr)
        return None


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if OUTPUT.exists():
        print(f"scan.json already exists. Delete {OUTPUT} to re-run Stage 1.")
        return

    print("Stage 1: Collecting audio files...")
    files = collect_files()
    print(f"  Found {len(files):,} audio files. Hashing...")

    start = time.time()
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(hash_file, f): f for f in files}
        with tqdm(total=len(files), unit="file", desc="Hashing") as pbar:
            for future in as_completed(futures):
                pbar.update(1)
                result = future.result()
                if result:
                    results.append(result)
                    # Checkpoint every 5,000 files
                    if len(results) % 5000 == 0:
                        CHECKPOINT.write_text(json.dumps({
                            "partial": True,
                            "files": results,
                        }, ensure_ascii=False))
                else:
                    errors.append(futures[future])

    elapsed = time.time() - start
    print(f"  Hashed {len(results):,} files in {elapsed:.1f}s ({len(errors)} errors)")

    output = {
        "scan_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_files": len(results),
        "error_count": len(errors),
        "files": results,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    if CHECKPOINT.exists():
        CHECKPOINT.unlink()

    print(f"  Written to {OUTPUT}")


if __name__ == "__main__":
    main()
