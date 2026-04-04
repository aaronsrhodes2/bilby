#!/usr/bin/env python3
"""
Delete video files and large audio files (>50 MB) from ~/Music.

Dry-run by default. Pass --delete to actually remove files.
Logs all deletions to state/deletion_log.json.

Usage:
    python3 delete_candidates.py            # dry-run, show what would be deleted
    python3 delete_candidates.py --delete   # actually delete
"""

import argparse
import json
import os
from pathlib import Path
from datetime import datetime

STATE_DIR = Path(__file__).parent / "state"
CANDIDATES_JSON = STATE_DIR / "deletion_candidates.json"
LOG_JSON = STATE_DIR / "deletion_log.json"


def fmt_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1_048_576:.1f} MB"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="Actually delete files (default is dry-run)")
    args = parser.parse_args()

    if not CANDIDATES_JSON.exists():
        print("deletion_candidates.json not found — run deletion_candidates.py first")
        return

    r = json.loads(CANDIDATES_JSON.read_text())
    all_files = r["video"]["files"] + r["large_audio"]["files"]
    all_files.sort(key=lambda x: -x["size_bytes"])

    total_bytes = sum(f["size_bytes"] for f in all_files)

    mode = "DELETE" if args.delete else "DRY-RUN"
    print(f"\n{'═'*65}")
    print(f"  {mode} — {len(all_files)} files, {fmt_mb(total_bytes)} total")
    print(f"{'═'*65}\n")

    deleted = []
    skipped = []
    errors = []

    for f in all_files:
        path = f["path"]
        mb = f["size_bytes"] / 1_048_576
        short = path.split("/Music/", 1)[-1]
        tag = "VIDEO" if f["ext"] in {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx", ".3gp"} else "AUDIO"

        if not os.path.exists(path):
            print(f"  SKIP (gone)  {fmt_mb(f['size_bytes']):>10}  [{tag}]  {short}")
            skipped.append(path)
            continue

        if args.delete:
            try:
                os.remove(path)
                print(f"  DELETED      {fmt_mb(f['size_bytes']):>10}  [{tag}]  {short}")
                deleted.append({"path": path, "size_bytes": f["size_bytes"], "ext": f["ext"]})
            except OSError as e:
                print(f"  ERROR        {fmt_mb(f['size_bytes']):>10}  [{tag}]  {short}  ({e})")
                errors.append({"path": path, "error": str(e)})
        else:
            print(f"  WOULD DELETE {fmt_mb(f['size_bytes']):>10}  [{tag}]  {short}")

    print(f"\n{'─'*65}")
    if args.delete:
        deleted_bytes = sum(d["size_bytes"] for d in deleted)
        print(f"  Deleted: {len(deleted)} files, {fmt_mb(deleted_bytes)} freed")
        if skipped:
            print(f"  Skipped (already gone): {len(skipped)}")
        if errors:
            print(f"  Errors: {len(errors)}")

        log = {
            "timestamp": datetime.now().isoformat(),
            "deleted_count": len(deleted),
            "deleted_bytes": deleted_bytes,
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "deleted": deleted,
            "skipped": skipped,
            "errors": errors,
        }
        LOG_JSON.write_text(json.dumps(log, ensure_ascii=False, indent=2))
        print(f"  Log → {LOG_JSON}")
    else:
        print(f"  Total: {len(all_files)} files, {fmt_mb(total_bytes)} would be freed")
        print(f"\n  Run with --delete to execute.")


if __name__ == "__main__":
    main()
