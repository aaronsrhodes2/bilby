#!/usr/bin/env python3
"""
Music Organization Pipeline — Orchestrator

Runs all 5 stages in order, skipping stages whose output already exists.
Each stage can also be run independently.

Usage:
    # Run all stages:
    export ACOUSTID_API_KEY=your_key_here
    python3 run.py

    # Run from a specific stage:
    python3 run.py --from 3

    # Run only one stage:
    python3 run.py --only 1

    # Force re-run a stage (delete its output first):
    python3 run.py --reset 3
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

STATE_DIR = Path(__file__).parent / "state"

STAGES = {
    1: {
        "name": "Scan & Hash",
        "script": "stage1_scan",
        "output": STATE_DIR / "scan.json",
    },
    2: {
        "name": "Deduplication",
        "script": "stage2_dedup",
        "output": STATE_DIR / "dedup.json",
    },
    3: {
        "name": "Fingerprinting & Metadata",
        "script": "stage3_fingerprint",
        "output": STATE_DIR / "metadata.json",
    },
    4: {
        "name": "Copy & Tag",
        "script": "stage4_copy",
        "output": STATE_DIR / "path_map.json",
    },
    5: {
        "name": "Traktor NML Update",
        "script": "stage5_traktor",
        "output": Path(__file__).parent / "corrected_traktor",
    },
}


def stage_done(n: int) -> bool:
    output = STAGES[n]["output"]
    if output.is_dir():
        return output.exists() and any(output.iterdir())
    return output.exists()


def reset_stage(n: int):
    output = STAGES[n]["output"]
    if output.exists():
        if output.is_dir():
            import shutil
            shutil.rmtree(output)
        else:
            output.unlink()
        print(f"  Reset Stage {n}: deleted {output}")


def run_stage(n: int):
    import importlib
    script = STAGES[n]["script"]
    name = STAGES[n]["name"]
    print(f"\n{'='*60}")
    print(f"STAGE {n}: {name}")
    print(f"{'='*60}")
    start = time.time()
    mod = importlib.import_module(script)
    mod.main()
    elapsed = time.time() - start
    print(f"\nStage {n} complete in {elapsed:.1f}s")


def check_acoustid_key():
    if not os.environ.get("ACOUSTID_API_KEY"):
        print("\n[WARNING] ACOUSTID_API_KEY is not set.")
        print("  Stage 3 will use tag-based fallback only (less accurate).")
        print("  To set it: export ACOUSTID_API_KEY=your_key_here")
        print("  Get a free key at: https://acoustid.org/login\n")


def main():
    parser = argparse.ArgumentParser(description="Music organization pipeline")
    parser.add_argument("--from", dest="from_stage", type=int, default=1,
                        help="Start from this stage (1-5)")
    parser.add_argument("--only", type=int, help="Run only this stage")
    parser.add_argument("--reset", type=int, help="Reset (delete output of) this stage then run it")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.reset:
        reset_stage(args.reset)
        stages_to_run = [args.reset]
    elif args.only:
        stages_to_run = [args.only]
    else:
        stages_to_run = list(range(args.from_stage, 6))

    check_acoustid_key()

    print(f"\nMusic Organization Pipeline")
    print(f"Stages to run: {stages_to_run}")
    print()

    for n in stages_to_run:
        if n not in STAGES:
            print(f"Unknown stage: {n}")
            sys.exit(1)

        if stage_done(n):
            print(f"Stage {n} ({STAGES[n]['name']}): ALREADY DONE — skipping")
            print(f"  (Delete {STAGES[n]['output']} to re-run)")
            continue

        run_stage(n)

    print("\n" + "="*60)
    print("Pipeline complete!")
    print("="*60)
    print(f"  corrected_music/   — deduplicated, tagged music files")
    print(f"  corrected_traktor/ — updated Traktor NML files")
    print(f"  review.json        — tracks needing manual attention")
    print()
    print("To import into Traktor:")
    print("  File → Import Another Collection")
    print("  → Select corrected_traktor/collection.nml")


if __name__ == "__main__":
    main()
