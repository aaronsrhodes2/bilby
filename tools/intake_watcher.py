#!/usr/bin/env python3
"""
tools/intake_watcher.py — Automatic music intake pipeline for Mac Bilby.

Monitors an incoming/ drop folder and feeds audio files through add_track.

Usage:
  python3 tools/intake_watcher.py            # watch mode (daemon, Ctrl+C to stop)
  python3 tools/intake_watcher.py --once     # process all current files, then exit
  python3 tools/intake_watcher.py --dry-run  # simulate without moving files
  python3 tools/intake_watcher.py --no-upload # skip Drive upload + git
  python3 tools/intake_watcher.py --no-cues  # skip librosa autocue step
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# Insert project root so add_track can find its own deps
sys.path.insert(0, str(BASE))

# add_track.py lives in the same tools/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

import add_track as _add_track_mod
from add_track import add_track

# ── Config ────────────────────────────────────────────────────────────────────

INCOMING_DIR = BASE / "incoming"
DONE_DIR     = INCOMING_DIR / "done"
FAILED_DIR   = INCOMING_DIR / "failed"
STATE_DIR    = BASE / "state"
LOG_FILE     = STATE_DIR / "intake_watcher.log"

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".aiff", ".aar"}

POLL_INTERVAL    = 8   # seconds between scans
STABILITY_PAUSE  = 3   # seconds between size checks
POST_FILE_PAUSE  = 1   # seconds after each file to let Drive/git settle

# ── Logging ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[{_ts()}] [INTAKE] WARNING: could not write to log file: {e}", flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ensure_dirs() -> None:
    for d in (INCOMING_DIR, DONE_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def is_stable(path: Path) -> bool:
    """Return True if the file size is unchanged after STABILITY_PAUSE seconds and is readable."""
    try:
        size1 = path.stat().st_size
    except OSError:
        return False
    time.sleep(STABILITY_PAUSE)
    try:
        size2 = path.stat().st_size
        # Also verify we can open it
        with path.open("rb") as f:
            f.read(1)
    except OSError:
        return False
    return size1 == size2


def scan_incoming() -> list[Path]:
    """Return audio files in incoming/ (recursive, skipping done/ and failed/)."""
    results = []
    skip = {DONE_DIR.resolve(), FAILED_DIR.resolve()}
    for p in sorted(INCOMING_DIR.rglob("*")):
        # Skip files inside done/ or failed/
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if any(resolved.is_relative_to(s) for s in skip):
            continue
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            results.append(p)
    return results


def already_done(name: str) -> bool:
    """Check if a filename already exists in incoming/done/."""
    return (DONE_DIR / name).exists()


def safe_move(src: Path, dest_dir: Path) -> None:
    """Move src to dest_dir/src.name, logging a warning on failure."""
    dest = dest_dir / src.name
    # Avoid collisions in done/failed by appending timestamp if needed
    if dest.exists():
        stem = src.stem
        suffix = src.suffix
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{stem}__{ts}{suffix}"
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        log(f"[INTAKE] WARNING: could not move {src.name} to {dest_dir.name}/: {e}")


# ── Core processor ────────────────────────────────────────────────────────────

class IntakeWatcher:
    def __init__(self, *, once: bool, dry_run: bool, no_upload: bool, no_cues: bool):
        self.once      = once
        self.dry_run   = dry_run
        self.no_upload = no_upload
        self.no_cues   = no_cues

        self._processing: set[Path] = set()
        self._stop       = False

        self.n_processed = 0
        self.n_failed    = 0
        self.n_skipped   = 0

        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        log("[INTAKE] SIGTERM received — stopping after current file.")
        self._stop = True

    def process_file(self, path: Path) -> None:
        name = path.name

        # Duplicate check vs done/
        if already_done(name):
            log(f"[INTAKE] SKIP (already done): {name}")
            self.n_skipped += 1
            return

        if path in self._processing:
            return

        if not is_stable(path):
            log(f"[INTAKE] SKIP (still writing): {name}")
            self.n_skipped += 1
            return

        self._processing.add(path)
        log(f"[INTAKE] Processing: {name}")

        if self.dry_run:
            log(f"[INTAKE] DRY-RUN: would call add_track({path})")
            self.n_processed += 1
            self._processing.discard(path)
            return

        try:
            result = add_track(
                path,
                no_cues=self.no_cues,
                no_upload=self.no_upload,
            )
            if result.get("ok"):
                dest_label = result.get("dest", "?")
                log(f"[INTAKE] OK: {name} → {dest_label}")
                safe_move(path, DONE_DIR)
                self.n_processed += 1
            else:
                err = result.get("error", "unknown error")
                log(f"[INTAKE] FAILED: {name} — {err}")
                safe_move(path, FAILED_DIR)
                self.n_failed += 1
        except Exception:
            tb = traceback.format_exc()
            log(f"[INTAKE] EXCEPTION processing {name}:\n{tb}")
            safe_move(path, FAILED_DIR)
            self.n_failed += 1
        finally:
            self._processing.discard(path)

        time.sleep(POST_FILE_PAUSE)

    def run_once(self) -> None:
        files = scan_incoming()
        if not files:
            log("[INTAKE] No audio files found in incoming/.")
            return
        log(f"[INTAKE] Found {len(files)} file(s) to process.")
        for f in files:
            if self._stop:
                break
            self.process_file(f)

    def run_watch(self) -> None:
        log(f"[INTAKE] Watcher started — polling incoming/ every {POLL_INTERVAL}s. Ctrl+C to stop.")
        try:
            while not self._stop:
                files = scan_incoming()
                for f in files:
                    if self._stop:
                        break
                    self.process_file(f)
                if not self._stop:
                    time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log("[INTAKE] KeyboardInterrupt received.")

    def run(self) -> None:
        ensure_dirs()
        log(f"[INTAKE] Starting — dry_run={self.dry_run} no_upload={self.no_upload} no_cues={self.no_cues}")

        if self.once:
            self.run_once()
        else:
            self.run_watch()

        self._summary()

    def _summary(self) -> None:
        log(
            f"[INTAKE] Watcher stopped. "
            f"Processed: {self.n_processed}, "
            f"Failed: {self.n_failed}, "
            f"Skipped: {self.n_skipped}."
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mac Bilby intake watcher — monitors incoming/ and runs add_track."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process all files currently in incoming/ then exit (no daemon).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate processing — don't move files or call add_track for real.",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Google Drive upload and git commit.",
    )
    parser.add_argument(
        "--no-cues",
        action="store_true",
        help="Skip librosa autocue step.",
    )
    args = parser.parse_args()

    watcher = IntakeWatcher(
        once=args.once,
        dry_run=args.dry_run,
        no_upload=args.no_upload,
        no_cues=args.no_cues,
    )
    watcher.run()


if __name__ == "__main__":
    main()
