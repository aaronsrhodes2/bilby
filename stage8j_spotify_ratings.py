#!/usr/bin/env python3
"""
Stage 8j — Spotify Popularity → Traktor Star Ratings

Fetches Spotify popularity scores (0–100) for unrated tracks and maps them
to Traktor's 1–5 star RANKING field using percentile normalization within
your own collection (so goth/industrial tracks aren't penalized for being
less mainstream than pop).

Phases:
  --fetch    Fetch popularity scores from Spotify, cache to state/
  --report   Show score distribution and proposed star mapping (dry-run)
  --apply    Write RANKING values into both NML files

Credentials:
  Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET as environment variables,
  or create a file called spotify_creds.txt in this directory with:
      client_id=YOUR_ID
      client_secret=YOUR_SECRET

Usage:
  python3 stage8j_spotify_ratings.py --fetch              # run in background
  python3 stage8j_spotify_ratings.py --report             # preview mapping
  python3 stage8j_spotify_ratings.py --apply              # write to NML
  python3 stage8j_spotify_ratings.py --fetch --apply      # fetch then apply
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE          = Path(__file__).parent
STATE_DIR     = BASE / "state"
CACHE_FILE    = STATE_DIR / "spotify_popularity_cache.json"
LOG_FILE      = STATE_DIR / "spotify_fetch.log"
TRAKTOR_NML   = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
OUR_NML       = BASE / "corrected_traktor" / "collection.nml"
CREDS_FILE    = BASE / "spotify_creds.txt"

# Traktor RANKING values for 0–5 stars
RANKING = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}

# ── Credentials ───────────────────────────────────────────────────────────────

def load_credentials() -> tuple[str, str]:
    client_id     = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id and CREDS_FILE.exists():
        for line in CREDS_FILE.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "client_id":     client_id     = v
                if k == "client_secret": client_secret = v
    if not client_id or not client_secret:
        print("ERROR: Spotify credentials not found.")
        print(f"  Set SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET env vars, or create:")
        print(f"  {CREDS_FILE}")
        print(f"  with lines:  client_id=XXX  /  client_secret=XXX")
        sys.exit(1)
    return client_id, client_secret


# ── Spotify API ───────────────────────────────────────────────────────────────

class SpotifyClient:
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self._token        = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        import base64
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        req = Request(
            self.TOKEN_URL,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        self._token        = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]
        return self._token

    def search_track(self, artist: str, title: str) -> dict | None:
        """
        Search for a track by artist + title.
        Returns the best-matching track dict (with 'popularity'), or None.
        """
        # Clean artist/title for better matching
        q = f"artist:{_clean_query(artist)} track:{_clean_query(title)}"
        params = urlencode({"q": q, "type": "track", "limit": 5})
        url = f"{self.SEARCH_URL}?{params}"
        token = self._get_token()
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                time.sleep(retry_after + 1)
                return self.search_track(artist, title)
            return None
        except URLError:
            return None

        tracks = data.get("tracks", {}).get("items", [])
        if not tracks:
            return None

        # Pick best match: prefer tracks where artist name is close
        artist_norm = _normalize(artist)
        for track in tracks:
            track_artists = " ".join(a["name"] for a in track.get("artists", []))
            if _normalize(track_artists).startswith(artist_norm[:6]):
                return track
        # Fall back to top result
        return tracks[0]


def _clean_query(s: str) -> str:
    """Strip parentheticals and punctuation that confuse Spotify search."""
    s = re.sub(r"\([^)]*\)", "", s)   # remove (feat. ...), (remix), etc.
    s = re.sub(r"\[[^\]]*\]", "", s)  # remove [...]
    s = re.sub(r"[^\w\s'-]", " ", s)
    return s.strip()[:80]


def _normalize(s: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


# ── NML helpers ───────────────────────────────────────────────────────────────

def load_unrated_tracks(nml_path: Path) -> list[dict]:
    """Return list of {path, artist, title} for unrated (RANKING=0) entries."""
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    results = []
    for e in coll.findall("ENTRY"):
        info = e.find("INFO")
        if info is None:
            continue
        if int(info.get("RANKING", 0)) != 0:
            continue
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


# ── Popularity → stars mapping ────────────────────────────────────────────────

def popularity_to_stars(scores: list[int]) -> dict[int, int]:
    """
    Map raw Spotify popularity scores (0–100) to 1–5 stars using
    percentile bucketing within the provided score list.
    Returns {raw_score: stars}.
    """
    if not scores:
        return {}
    sorted_scores = sorted(set(scores))
    n = len(sorted_scores)
    result = {}
    for score in sorted_scores:
        rank = sorted_scores.index(score) / n  # 0.0–1.0 percentile
        if   rank < 0.20: stars = 1
        elif rank < 0.40: stars = 2
        elif rank < 0.60: stars = 3
        elif rank < 0.80: stars = 4
        else:             stars = 5
        result[score] = stars
    return result


# ── Fetch phase ───────────────────────────────────────────────────────────────

def run_fetch(args) -> None:
    client_id, client_secret = load_credentials()
    spotify = SpotifyClient(client_id, client_secret)

    STATE_DIR.mkdir(exist_ok=True)
    cache: dict = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text())

    tracks = load_unrated_tracks(TRAKTOR_NML)
    todo = [t for t in tracks if t["path"] not in cache]

    print(f"Spotify popularity fetch")
    print(f"  Unrated tracks:       {len(tracks)}")
    print(f"  Already cached:       {len(tracks) - len(todo)}")
    print(f"  To fetch:             {len(todo)}")
    if not todo:
        print("  Nothing to do — all unrated tracks already cached.")
        return

    eta_min = len(todo) / 5 / 60  # ~5 req/sec
    print(f"  Estimated time:       {eta_min:.0f} min")
    print()

    found = not_found = errors = 0
    start = time.time()

    for i, track in enumerate(todo, 1):
        artist = track["artist"]
        title  = track["title"]

        if not artist and not title:
            cache[track["path"]] = None
            not_found += 1
            continue

        try:
            result = spotify.search_track(artist, title)
        except Exception as ex:
            cache[track["path"]] = None
            errors += 1
            if errors <= 5:
                print(f"  [ERROR] {artist} — {title}: {ex}")
            time.sleep(1)
            continue

        if result:
            cache[track["path"]] = {
                "popularity":    result["popularity"],
                "spotify_title": result["name"],
                "spotify_artist": ", ".join(a["name"] for a in result["artists"]),
                "spotify_id":    result["id"],
            }
            found += 1
        else:
            cache[track["path"]] = None
            not_found += 1

        # Save cache every 100 tracks
        if i % 100 == 0:
            CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
            elapsed = time.time() - start
            rate = i / elapsed
            remaining = (len(todo) - i) / rate / 60
            msg = (f"  {i}/{len(todo)} — found {found}, "
                   f"not found {not_found}, errors {errors} "
                   f"— ~{remaining:.0f} min remaining")
            print(msg)
            LOG_FILE.write_text(msg + "\n")

        # Rate limit: ~5 req/sec (Spotify allows ~180/30s)
        time.sleep(0.2)

    # Final save
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
    elapsed = time.time() - start
    print(f"\nFetch complete in {elapsed/60:.1f} min")
    print(f"  Found:     {found}")
    print(f"  Not found: {not_found}")
    print(f"  Errors:    {errors}")
    print(f"  Cache:     {CACHE_FILE}")


# ── Report phase ──────────────────────────────────────────────────────────────

def run_report() -> dict[str, int] | None:
    if not CACHE_FILE.exists():
        print("No cache yet — run --fetch first.")
        return None

    cache = json.loads(CACHE_FILE.read_text())
    scores = [v["popularity"] for v in cache.values() if v and "popularity" in v]
    score_map = popularity_to_stars(scores)

    star_counts = Counter(score_map[s] for s in scores)
    total_cached = len([v for v in cache.values() if v])
    total_none   = len([v for v in cache.values() if v is None])

    print(f"Spotify cache summary")
    print(f"  Tracks with score:    {total_cached}")
    print(f"  Tracks not found:     {total_none}")
    print(f"  Unique scores:        {len(set(scores))}")
    print(f"  Score range:          {min(scores)}–{max(scores)}" if scores else "")
    print()
    print("Proposed star distribution (percentile bucketing):")
    for stars in range(1, 6):
        count = star_counts.get(stars, 0)
        bar = "█" * (count // 50)
        print(f"  {stars}★  {count:6d}  {bar}")
    print()
    print("Percentile thresholds:")
    sorted_s = sorted(scores)
    n = len(sorted_s)
    for pct, label in [(0.20, "20th"), (0.40, "40th"), (0.60, "60th"), (0.80, "80th")]:
        idx = int(pct * n)
        print(f"  {label} percentile → popularity {sorted_s[min(idx, n-1)]}")

    return score_map


# ── Apply phase ───────────────────────────────────────────────────────────────

def run_apply(score_map: dict[str, int]) -> None:
    cache = json.loads(CACHE_FILE.read_text())

    # Build path → RANKING map
    path_ranking: dict[str, int] = {}
    for path, data in cache.items():
        if not data or "popularity" not in data:
            continue
        pop   = data["popularity"]
        stars = score_map.get(pop, 0)
        if stars:
            path_ranking[path] = RANKING[stars]

    print(f"Applying ratings to NML files...")
    print(f"  Tracks to rate: {len(path_ranking)}")

    updated_total = 0
    for nml_path, label in [(TRAKTOR_NML, "Traktor NML"), (OUR_NML, "Our NML")]:
        if not nml_path.exists():
            print(f"  [{label}] NOT FOUND — skipping")
            continue
        ET.register_namespace("", "")
        tree = ET.parse(nml_path)
        coll = tree.getroot().find("COLLECTION")
        updated = 0
        for e in coll.findall("ENTRY"):
            info = e.find("INFO")
            if info is None:
                continue
            if int(info.get("RANKING", 0)) != 0:
                continue  # preserve existing user ratings
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
        backup = nml_path.parent / f"{nml_path.stem}_pre_ratings_{stamp}.nml"
        shutil.copy2(nml_path, backup)
        tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
        fix_xml_declaration(nml_path)
        print(f"  [{label}] {updated} ratings written → backup: {backup.name}")
        updated_total += updated

    print(f"\nDone. {updated_total // 2} tracks rated in Traktor NML.")
    print("Reload collection in Traktor to see star ratings.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Spotify popularity and set Traktor star ratings"
    )
    parser.add_argument("--fetch",  action="store_true", help="Fetch from Spotify API")
    parser.add_argument("--report", action="store_true", help="Preview star mapping")
    parser.add_argument("--apply",  action="store_true", help="Write ratings to NML")
    args = parser.parse_args()

    if not any([args.fetch, args.report, args.apply]):
        parser.print_help()
        return

    score_map = None

    if args.fetch:
        run_fetch(args)

    if args.report or args.apply:
        score_map = run_report()
        if score_map is None:
            return

    if args.apply:
        print()
        run_apply(score_map)


if __name__ == "__main__":
    main()
