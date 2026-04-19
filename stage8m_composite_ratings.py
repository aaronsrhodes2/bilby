#!/usr/bin/env python3
"""
Stage 8m — Composite Ratings: Last.fm + Spotify → Traktor Stars

Combines two popularity signals into a single 1-5 star rating:
  • Last.fm listener count  (already cached from Stage 8j)
  • Spotify popularity      (0-100 score, fetched here via Client Credentials)

Both signals are percentile-normalized within your own collection so
underground goth/industrial tracks aren't penalized against mainstream pop.

Composite score = 0.5 × lastfm_pct + 0.5 × spotify_pct
If only one source exists for a track, that source gets full weight (1.0×).
Tracks with no data remain unrated (RANKING = 0, stars unchanged).

Phases:
  --fetch-spotify   Search Spotify for each track and cache popularity scores
                    (no OAuth needed — uses Client Credentials flow)
  --report          Preview composite distribution, dry run
  --apply           Write RANKING values into both NML files

Credentials:
  spotify_creds.txt must contain:
      client_id=YOUR_CLIENT_ID
      client_secret=YOUR_CLIENT_SECRET

Usage:
  python3 stage8m_composite_ratings.py --fetch-spotify
  python3 stage8m_composite_ratings.py --report
  python3 stage8m_composite_ratings.py --apply
  python3 stage8m_composite_ratings.py --fetch-spotify --apply
"""

import argparse
import base64
import json
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE              = Path(__file__).parent
STATE_DIR         = BASE / "state"
LASTFM_CACHE      = STATE_DIR / "lastfm_listeners_cache.json"
SPOTIFY_CACHE     = STATE_DIR / "spotify_popularity_cache.json"
COMPOSITE_LOG     = STATE_DIR / "composite_ratings.log"
TRAKTOR_NML       = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
OUR_NML           = BASE / "corrected_traktor" / "collection.nml"
SPOTIFY_CREDS     = BASE / "spotify_creds.txt"

# Traktor RANKING int values for 1–5 stars
RANKING = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}

# ── Spotify Client Credentials ────────────────────────────────────────────────

_spotify_token: str | None = None
_token_expires: float = 0.0


def _load_spotify_creds() -> tuple[str, str]:
    client_id = client_secret = ""
    if SPOTIFY_CREDS.exists():
        for line in SPOTIFY_CREDS.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "client_id":     client_id     = v
                if k == "client_secret": client_secret = v
    if not client_id or not client_secret:
        print(f"ERROR: {SPOTIFY_CREDS} missing client_id or client_secret")
        sys.exit(1)
    return client_id, client_secret


def _get_token() -> str:
    global _spotify_token, _token_expires
    if _spotify_token and time.time() < _token_expires - 30:
        return _spotify_token
    client_id, client_secret = _load_spotify_creds()
    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {creds_b64}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"ERROR getting Spotify token: {e}")
        sys.exit(1)
    _spotify_token   = data["access_token"]
    _token_expires   = time.time() + data.get("expires_in", 3600)
    return _spotify_token


def _spotify_get(url: str, params: dict | None = None) -> dict | None:
    token = _get_token()
    full_url = url + ("?" + urlencode(params) if params else "")
    req = Request(full_url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", "5"))
            print(f"  [rate limit] sleeping {retry_after}s …")
            time.sleep(retry_after)
            return _spotify_get(url, params)  # one retry
        if e.code in (400, 404):
            return None
        raise
    except URLError:
        return None


def search_track_id(artist: str, title: str) -> str | None:
    """Return Spotify track ID for artist+title, or None if not found."""
    # Try exact field search first
    q = f"artist:{artist} track:{title}"
    data = _spotify_get("https://api.spotify.com/v1/search",
                        {"q": q, "type": "track", "limit": 1, "market": "US"})
    if data:
        items = data.get("tracks", {}).get("items", [])
        if items:
            return items[0]["id"]
    # Fallback: plain text search
    q2 = f"{artist} {title}"
    data2 = _spotify_get("https://api.spotify.com/v1/search",
                         {"q": q2, "type": "track", "limit": 1, "market": "US"})
    if data2:
        items2 = data2.get("tracks", {}).get("items", [])
        if items2:
            return items2[0]["id"]
    return None


def batch_popularity(track_ids: list[str]) -> dict[str, int]:
    """
    Fetch popularity for up to 50 track IDs in one call.
    Returns {track_id: popularity (0-100)}.
    """
    if not track_ids:
        return {}
    data = _spotify_get("https://api.spotify.com/v1/tracks",
                        {"ids": ",".join(track_ids[:50])})
    if not data:
        return {}
    result = {}
    for t in data.get("tracks", []):
        if t and t.get("id"):
            result[t["id"]] = t.get("popularity", 0)
    return result


# ── NML helpers ───────────────────────────────────────────────────────────────

def load_all_tracks(nml_path: Path) -> list[dict]:
    """Return all tracks as {path, artist, title}."""
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    results = []
    for e in coll.findall("ENTRY"):
        loc = e.find("LOCATION")
        if loc is None:
            continue
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        results.append({
            "path":   path,
            "artist": e.get("ARTIST", ""),
            "title":  e.get("TITLE", ""),
        })
    return results


def fix_xml_declaration(path: Path) -> None:
    content = path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(content)


# ── Percentile normalizer ─────────────────────────────────────────────────────

def percentile_scores(values: dict[str, float]) -> dict[str, float]:
    """
    Given {key: raw_value}, return {key: 0.0-1.0 percentile rank}
    normalized within this collection. Ties share the same percentile.
    """
    if not values:
        return {}
    sorted_unique = sorted(set(values.values()))
    n = len(sorted_unique)
    rank_map = {v: i / (n - 1) if n > 1 else 0.5 for i, v in enumerate(sorted_unique)}
    return {k: rank_map[v] for k, v in values.items()}


# ── Fetch phase ───────────────────────────────────────────────────────────────

def run_fetch_spotify() -> None:
    STATE_DIR.mkdir(exist_ok=True)

    cache: dict = {}
    if SPOTIFY_CACHE.exists():
        try:
            cache = json.loads(SPOTIFY_CACHE.read_text())
        except json.JSONDecodeError:
            cache = {}

    if not TRAKTOR_NML.exists():
        print(f"ERROR: NML not found at {TRAKTOR_NML}")
        sys.exit(1)

    all_tracks = load_all_tracks(TRAKTOR_NML)
    todo = [t for t in all_tracks if t["path"] not in cache and (t["artist"] or t["title"])]

    print(f"Spotify popularity fetch")
    print(f"  Total tracks:    {len(all_tracks)}")
    print(f"  Already cached:  {len(all_tracks) - len(todo)}")
    print(f"  To fetch:        {len(todo)}")
    if not todo:
        print("  Nothing to do — all tracks already cached.")
        return

    # Phase 1: search for track IDs (two calls each: field search + fallback)
    print(f"\nPhase 1: searching for track IDs …")
    id_map: dict[str, str] = {}   # path → spotify_id
    found_ids = 0

    for i, track in enumerate(todo, 1):
        sp_id = search_track_id(track["artist"], track["title"])
        if sp_id:
            id_map[track["path"]] = sp_id
            found_ids += 1
        else:
            cache[track["path"]] = None  # not found on Spotify

        if i % 100 == 0:
            SPOTIFY_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
            print(f"  {i}/{len(todo)} searched — {found_ids} IDs found so far")
            COMPOSITE_LOG.write_text(f"search {i}/{len(todo)}\n")

        time.sleep(0.07)  # ~14 req/sec, well within Spotify limits

    SPOTIFY_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    print(f"\nSearch complete: {found_ids}/{len(todo)} tracks found on Spotify")

    # Phase 2: batch popularity fetch (50 IDs per call)
    print(f"\nPhase 2: fetching popularity scores in batches of 50 …")
    paths   = list(id_map.keys())
    ids     = [id_map[p] for p in paths]
    batched = 0

    for start in range(0, len(ids), 50):
        batch_paths = paths[start:start + 50]
        batch_ids   = ids[start:start + 50]
        pops = batch_popularity(batch_ids)
        for path, sp_id in zip(batch_paths, batch_ids):
            cache[path] = pops.get(sp_id)  # None if track missing from response
        batched += len(batch_ids)
        SPOTIFY_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
        if batched % 500 == 0:
            print(f"  {batched}/{len(ids)} popularity scores fetched")
        time.sleep(0.12)  # ~8 batch calls/sec

    SPOTIFY_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    with_pop = sum(1 for v in cache.values() if isinstance(v, int))
    print(f"\nFetch complete. {with_pop} tracks have Spotify popularity scores.")
    print(f"Cache: {SPOTIFY_CACHE}")


# ── Report phase ──────────────────────────────────────────────────────────────

def _build_composite() -> dict[str, int]:
    """
    Build {path: composite_stars (1-5)} for all rated tracks.
    Returns empty dict if caches are missing.
    """
    # Load Last.fm
    lastfm_raw: dict[str, int] = {}
    if LASTFM_CACHE.exists():
        raw = json.loads(LASTFM_CACHE.read_text())
        lastfm_raw = {k: v for k, v in raw.items() if isinstance(v, int)}

    # Load Spotify
    spotify_raw: dict[str, int] = {}
    if SPOTIFY_CACHE.exists():
        raw = json.loads(SPOTIFY_CACHE.read_text())
        spotify_raw = {k: v for k, v in raw.items() if isinstance(v, int)}

    if not lastfm_raw and not spotify_raw:
        return {}

    # Percentile-normalize each source within collection
    lastfm_pct  = percentile_scores(lastfm_raw)    # {path: 0.0-1.0}
    spotify_pct = percentile_scores(spotify_raw)

    all_paths = set(lastfm_pct) | set(spotify_pct)
    composite: dict[str, float] = {}

    for path in all_paths:
        has_lf = path in lastfm_pct
        has_sp = path in spotify_pct
        if has_lf and has_sp:
            score = 0.5 * lastfm_pct[path] + 0.5 * spotify_pct[path]
        elif has_lf:
            score = lastfm_pct[path]
        else:
            score = spotify_pct[path]
        composite[path] = score

    # Map 0.0-1.0 score → 1-5 stars (quintile bucketing)
    star_map: dict[str, int] = {}
    for path, score in composite.items():
        if   score < 0.20: stars = 1
        elif score < 0.40: stars = 2
        elif score < 0.60: stars = 3
        elif score < 0.80: stars = 4
        else:              stars = 5
        star_map[path] = stars

    return star_map


def run_report() -> dict[str, int]:
    if not LASTFM_CACHE.exists() and not SPOTIFY_CACHE.exists():
        print("No caches found. Run --fetch-spotify first (Last.fm cache expected from stage8j).")
        return {}

    # Source coverage
    lastfm_raw  = {}
    spotify_raw = {}
    if LASTFM_CACHE.exists():
        raw = json.loads(LASTFM_CACHE.read_text())
        lastfm_raw = {k: v for k, v in raw.items() if isinstance(v, int)}
    if SPOTIFY_CACHE.exists():
        raw = json.loads(SPOTIFY_CACHE.read_text())
        spotify_raw = {k: v for k, v in raw.items() if isinstance(v, int)}

    both = len(set(lastfm_raw) & set(spotify_raw))
    either = len(set(lastfm_raw) | set(spotify_raw))

    print("Composite ratings report")
    print(f"  Last.fm tracks with counts:    {len(lastfm_raw):,}")
    print(f"  Spotify tracks with scores:    {len(spotify_raw):,}")
    print(f"  Covered by both sources:       {both:,}")
    print(f"  Covered by at least one:       {either:,}")
    if spotify_raw:
        sp_vals = list(spotify_raw.values())
        print(f"  Spotify score range:           {min(sp_vals)} – {max(sp_vals)}")
    print()

    star_map = _build_composite()
    if not star_map:
        print("Nothing to map — run --fetch-spotify first.")
        return {}

    dist = Counter(star_map.values())
    print("Proposed composite star distribution:")
    for s in range(1, 6):
        count = dist.get(s, 0)
        bar = "█" * (count // 100)
        print(f"  {s}★  {count:6,}  {bar}")

    return star_map


# ── Apply phase ───────────────────────────────────────────────────────────────

def run_apply(star_map: dict[str, int]) -> None:
    if not star_map:
        print("No composite data — run --report first.")
        return

    path_ranking = {p: RANKING[s] for p, s in star_map.items()}
    print(f"Applying composite ratings to NML files …")
    print(f"  Tracks to rate: {len(path_ranking):,}")

    for nml_path, label in [(TRAKTOR_NML, "Traktor NML"), (OUR_NML, "Our NML")]:
        if not nml_path.exists():
            print(f"  [{label}] NOT FOUND — skipping")
            continue
        tree = ET.parse(nml_path)
        coll = tree.getroot().find("COLLECTION")
        updated = 0
        for e in coll.findall("ENTRY"):
            info = e.find("INFO")
            if info is None:
                continue
            loc = e.find("LOCATION")
            if loc is None:
                continue
            path = traktor_to_abs(
                loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
            )
            if path in path_ranking:
                info.set("RANKING", str(path_ranking[path]))
                updated += 1

        stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = nml_path.parent / f"{nml_path.stem}_pre_composite_{stamp}.nml"
        shutil.copy2(nml_path, backup)
        tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
        fix_xml_declaration(nml_path)
        print(f"  [{label}] {updated:,} ratings written → backup: {backup.name}")

    print(f"\nDone. Reload collection in Traktor to see composite star ratings.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 8m — Composite ratings (Last.fm + Spotify)")
    parser.add_argument("--fetch-spotify", action="store_true", help="Fetch Spotify popularity scores")
    parser.add_argument("--report",        action="store_true", help="Preview composite distribution")
    parser.add_argument("--apply",         action="store_true", help="Write ratings to NML files")
    args = parser.parse_args()

    if not any([args.fetch_spotify, args.report, args.apply]):
        parser.print_help()
        sys.exit(0)

    if args.fetch_spotify:
        run_fetch_spotify()

    star_map: dict[str, int] = {}
    if args.report or args.apply:
        star_map = run_report()

    if args.apply:
        if not star_map:
            print("No data to apply.")
            sys.exit(1)
        print()
        run_apply(star_map)


if __name__ == "__main__":
    main()
