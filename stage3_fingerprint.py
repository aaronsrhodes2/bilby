#!/usr/bin/env python3
"""
Stage 3 — Acoustic Fingerprinting & Metadata Lookup

For each winner file from Stage 2:
  1. Run fpcalc to get chromaprint fingerprint
  2. Query AcoustID API for MusicBrainz recording match
  3. Fetch artist/title/album/year from MusicBrainz
  4. Fall back to existing ID3 tags + tag_cleaner for low/no-confidence matches

Persists fingerprint cache for resumability.
AcoustID API key must be set in env var ACOUSTID_API_KEY.

Reads:  state/dedup.json
Writes: state/metadata.json, state/fingerprint_cache.json, review.json
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from mutagen import File as MutagenFile
from tqdm.asyncio import tqdm as async_tqdm

from lib.acoustid_client import AcoustIDClient
from lib.mb_client import MusicBrainzClient
from lib.tag_cleaner import clean_existing_tags, is_placeholder, clean_stem

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# Load .env if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

STATE_DIR = Path(__file__).parent / "state"
DEDUP_JSON = STATE_DIR / "dedup.json"
OUTPUT = STATE_DIR / "metadata.json"
FINGERPRINT_CACHE = STATE_DIR / "fingerprint_cache.json"
REVIEW_JSON = Path(__file__).parent / "review.json"

FPCALC = "/opt/homebrew/bin/fpcalc"
CONFIDENCE_THRESHOLD = 0.5   # below this, fall back to tag cleaning
CPU_COUNT = os.cpu_count() or 4

# AcoustID API key
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")


def load_cache() -> dict:
    if FINGERPRINT_CACHE.exists():
        try:
            return json.loads(FINGERPRINT_CACHE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    FINGERPRINT_CACHE.write_text(json.dumps(cache, ensure_ascii=False))


def read_existing_tags(path: str) -> dict:
    """Read existing ID3/mutagen tags from a file."""
    try:
        f = MutagenFile(path, easy=True)
        if f is None:
            return {}
        tags = {}
        for key in ("title", "artist", "album", "tracknumber", "date"):
            val = f.get(key)
            if val:
                tags[key.replace("tracknumber", "track_number").replace("date", "year")] = str(val[0]) if isinstance(val, list) else str(val)
        return tags
    except Exception:
        return {}


async def run_fpcalc(path: str, semaphore: asyncio.Semaphore) -> tuple[str, int] | None:
    """Run fpcalc and return (fingerprint, duration) or None on failure.

    fpcalc default output format (no flags):
        DURATION=330
        FINGERPRINT=AQADtEkSJVM0...
    """
    async with semaphore:
        try:
            proc = await asyncio.create_subprocess_exec(
                FPCALC, path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                return None
            duration = None
            fingerprint = None
            for line in stdout.decode().strip().splitlines():
                if line.startswith("DURATION="):
                    try:
                        duration = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.startswith("FINGERPRINT="):
                    fingerprint = line.split("=", 1)[1].strip()
            if duration is None or not fingerprint:
                return None
            return fingerprint, duration
        except (asyncio.TimeoutError, OSError):
            return None


def fallback_from_tags(path: str) -> dict:
    """Build metadata from existing tags + tag_cleaner."""
    existing = read_existing_tags(path)
    stem = os.path.splitext(os.path.basename(path))[0]

    title = existing.get("title", "")
    artist = existing.get("artist", "")

    # If tags look clean, use them
    if title and artist:
        cleaned = clean_existing_tags({"title": title, "artist": artist})
        title = cleaned.get("title", title)
        artist = cleaned.get("artist", artist)
    else:
        # Fall back to filename cleaning
        cleaned_artist, cleaned_title = clean_stem(stem)
        if not title and cleaned_title:
            title = cleaned_title
        if not artist and cleaned_artist:
            artist = cleaned_artist

    return {
        "title": title or stem,
        "artist": artist or "",
        "album": existing.get("album", ""),
        "track_number": existing.get("track_number"),
        "year": existing.get("year"),
    }


async def process_file(
    path: str,
    sha: str,
    fpcalc_sem: asyncio.Semaphore,
    acoustid: AcoustIDClient,
    mb: MusicBrainzClient,
    cache: dict,
) -> dict:
    """Process one winner file and return its metadata record."""

    # Check fingerprint cache first
    cached = cache.get(sha)
    if cached:
        return cached

    # Run fpcalc
    fp_result = await run_fpcalc(path, fpcalc_sem)

    if fp_result is None:
        meta = fallback_from_tags(path)
        result = {
            "sha256": sha,
            "path": path,
            "source": "tag_fallback",
            "acoustid_confidence": 0.0,
            "needs_review": is_placeholder(meta.get("artist"), meta.get("title")),
            **meta,
        }
        cache[sha] = result
        return result

    fingerprint, duration = fp_result

    # AcoustID lookup
    acoustid_result = await acoustid.lookup(fingerprint, duration) if ACOUSTID_API_KEY else None

    if acoustid_result and acoustid_result.get("score", 0) >= CONFIDENCE_THRESHOLD:
        score = acoustid_result["score"]
        recordings = acoustid_result.get("recordings", [])

        mb_meta = None
        if recordings:
            # Try to get full metadata from MusicBrainz for the best recording
            best_rec = recordings[0]
            mb_id = best_rec.get("id")
            if mb_id:
                mb_meta = await mb.get_recording(mb_id)

        if mb_meta and mb_meta.get("title"):
            result = {
                "sha256": sha,
                "path": path,
                "source": "acoustid+musicbrainz",
                "acoustid_confidence": score,
                "musicbrainz_id": recordings[0].get("id") if recordings else None,
                "needs_review": False,
                "title": mb_meta["title"],
                "artist": mb_meta["artist"],
                "album": mb_meta.get("album", ""),
                "track_number": mb_meta.get("track_number"),
                "year": mb_meta.get("year"),
            }
        else:
            # AcoustID matched but no MB metadata — use title/artist from AcoustID response
            rec = recordings[0] if recordings else {}
            title = rec.get("title", "")
            artists = rec.get("artists", [])
            artist = artists[0].get("name", "") if artists else ""
            fb = fallback_from_tags(path)
            result = {
                "sha256": sha,
                "path": path,
                "source": "acoustid",
                "acoustid_confidence": score,
                "needs_review": not (title and artist),
                "title": title or fb["title"],
                "artist": artist or fb["artist"],
                "album": fb.get("album", ""),
                "track_number": fb.get("track_number"),
                "year": fb.get("year"),
            }
    else:
        # Low confidence or no match — fall back to tag cleaning
        meta = fallback_from_tags(path)
        result = {
            "sha256": sha,
            "path": path,
            "source": "tag_fallback",
            "acoustid_confidence": acoustid_result.get("score", 0.0) if acoustid_result else 0.0,
            "needs_review": is_placeholder(meta.get("artist"), meta.get("title")),
            **meta,
        }

    cache[sha] = result
    return result


async def main_async():
    if not ACOUSTID_API_KEY:
        print("WARNING: ACOUSTID_API_KEY not set. Will use tag fallback only.")
        print("  Set it with: export ACOUSTID_API_KEY=your_key_here\n")

    if not DEDUP_JSON.exists():
        print("dedup.json not found — run stage2_dedup.py first")
        sys.exit(1)

    if OUTPUT.exists():
        print(f"metadata.json already exists. Delete {OUTPUT} to re-run Stage 3.")
        return

    print("Stage 3: Loading dedup results...")
    data = json.loads(DEDUP_JSON.read_text())
    groups = data["groups"]

    # Collect winner paths
    winners = [(sha, info["winner"]) for sha, info in groups.items()]
    print(f"  {len(winners):,} unique winner files to process")

    cache = load_cache()
    already_cached = sum(1 for sha, _ in winners if sha in cache)
    print(f"  {already_cached:,} already in fingerprint cache — will skip fpcalc")

    fpcalc_sem = asyncio.Semaphore(CPU_COUNT)
    results = {}
    review_items = []

    async with AcoustIDClient(ACOUSTID_API_KEY) as acoustid, MusicBrainzClient() as mb:
        tasks = [
            process_file(path, sha, fpcalc_sem, acoustid, mb, cache)
            for sha, path in winners
        ]

        # Process with progress bar, saving cache periodically
        completed = 0
        pbar = async_tqdm(total=len(tasks), desc="Fingerprinting", unit="file")
        for coro in asyncio.as_completed(tasks):
            result = await coro
            sha = result["sha256"]
            results[sha] = result
            if result.get("needs_review"):
                review_items.append({"sha256": sha, "path": result["path"], "reason": "placeholder_or_unknown"})
            completed += 1
            pbar.update(1)
            # Save cache every 500 files
            if completed % 500 == 0:
                save_cache(cache)
        pbar.close()

    save_cache(cache)

    output = {
        "total_tracks": len(results),
        "acoustid_matched": sum(1 for r in results.values() if "acoustid" in r.get("source", "")),
        "tag_fallback": sum(1 for r in results.values() if r.get("source") == "tag_fallback"),
        "needs_review": len(review_items),
        "tracks": results,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    # Merge with any existing review.json
    existing_review = []
    if REVIEW_JSON.exists():
        try:
            existing_review = json.loads(REVIEW_JSON.read_text())
        except Exception:
            pass
    REVIEW_JSON.write_text(json.dumps(existing_review + review_items, ensure_ascii=False, indent=2))

    print(f"\n  Results: {output['acoustid_matched']:,} AcoustID matches, "
          f"{output['tag_fallback']:,} tag fallbacks, "
          f"{output['needs_review']:,} need review")
    print(f"  Written to {OUTPUT}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
