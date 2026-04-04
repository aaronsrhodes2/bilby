#!/usr/bin/env python3
import json
from pathlib import Path
from collections import Counter

r = json.loads(Path("state/deletion_candidates.json").read_text())

print(f'VIDEO: {r["video"]["count"]} files, {r["video"]["total_bytes"]/1_048_576:.1f} MB')
print(f'LARGE AUDIO: {r["large_audio"]["count"]} files, {r["large_audio"]["total_bytes"]/1_048_576:.1f} MB')
print()

vexts = Counter(f["ext"] for f in r["video"]["files"])
print("Video extensions:")
for ext, n in sorted(vexts.items(), key=lambda x: -x[1]):
    print(f"  {ext}: {n}")
print()

print("Large audio size buckets:")
buckets = {"50-100MB": 0, "100-200MB": 0, "200-500MB": 0, "500MB+": 0}
for f in r["large_audio"]["files"]:
    mb = f["size_bytes"] / 1_048_576
    if mb < 100:
        buckets["50-100MB"] += 1
    elif mb < 200:
        buckets["100-200MB"] += 1
    elif mb < 500:
        buckets["200-500MB"] += 1
    else:
        buckets["500MB+"] += 1
for k, v in buckets.items():
    print(f"  {k}: {v}")
print()

print("Top 15 largest audio files:")
for f in r["large_audio"]["files"][:15]:
    mb = f["size_bytes"] / 1_048_576
    short = f["path"].split("/Music/", 1)[-1]
    print(f"  {mb:7.1f} MB  {f['ext']:<6}  {short}")
