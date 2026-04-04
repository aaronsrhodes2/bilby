#!/usr/bin/env python3
"""Quick status check for Stage 3 progress."""

import json
from pathlib import Path
from collections import Counter

STATE_DIR = Path(__file__).parent / "state"

dedup = json.loads((STATE_DIR / "dedup.json").read_text())
total = len(dedup["groups"])

cache_path = STATE_DIR / "fingerprint_cache.json"
cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
done = len(cache)

sources = Counter(r.get("source", "unknown") for r in cache.values())
needs_review = sum(1 for r in cache.values() if r.get("needs_review"))

pct = done / total * 100 if total else 0
remaining = total - done

print(f"\nStage 3 Progress")
print(f"{'─'*40}")
print(f"  Total winners:     {total:>7,}")
print(f"  Processed:         {done:>7,}  ({pct:.1f}%)")
print(f"  Remaining:         {remaining:>7,}")
print()
print(f"Source breakdown (so far):")
for src, count in sorted(sources.items(), key=lambda x: -x[1]):
    print(f"  {src:<30} {count:>6,}")
print()
print(f"  Needs review:      {needs_review:>7,}")

metadata_path = STATE_DIR / "metadata.json"
if metadata_path.exists():
    print(f"\n  metadata.json EXISTS — Stage 3 complete!")
else:
    print(f"\n  metadata.json not found — Stage 3 still in progress or not started")

# Estimate remaining time based on AcoustID rate limit (3 req/sec for fingerprinted,
# tag-fallback is instant). Rough: ~1 sec/file average including fpcalc.
# Just give a file count, not a time guess.
print()
