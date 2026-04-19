#!/usr/bin/env python3
"""
Stage 8j — Last.fm Listener Counts → Traktor Star Ratings

Fetches Last.fm listener counts for unrated tracks and maps them to
Traktor's 1–5 star RANKING field using percentile normalization within
your own collection (so underground goth/industrial tracks aren't penalized
vs mainstream pop).

Phases:
  --fetch    Fetch listener counts from Last.fm, cache to state/
  --report   Show count distribution and proposed star mapping (dry-run)
  --apply    Write RANKING values into both NML files

Credentials:
  Create lastfm_creds.txt in this directory with:
      api_key=YOUR_KEY

Usage:
  python3 stage8j_spotify_ratings.py --fetch              # ~90 min for 23k tracks
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
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE        = Path(__file__).parent
STATE_DIR   = BASE / "state"
CACHE_FILE  = STATE_DIR / "lastfm_listeners_cache.json"
LOG_FILE    = STATE_DIR / "lastfm_fetch.log"
TRAKTOR_NML = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
OUR_NML     = BASE / "corrected_traktor" / "collection.nml"
CREDS_FILE  = BASE / "lastfm_creds.txt"

# Traktor RANKING values for 0–5 stars
RANKING = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}

# ── Credentials ───────────────────────────────────────────────────────────────

def load_api_key() -> str:
    api_key = os.environ.get("LASTFM_API_KEY", "")
    if not api_key and CREDS_FILE.exists():
        for line in CREDS_FILE.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == "api_key":
                    api_key = v.strip()
    if not api_key:
        print("ERROR: Last.fm API key not found.")
        print(f"  Set LASTFM_API_KEY env var, or create {CREDS_FILE}")
        print(f"  with line:  api_key=YOUR_KEY")
        sys.exit(1)
    return api_key


# ── Last.fm API ───────────────────────────────────────────────────────────────

LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"

def get_track_listeners(api_key: str, artist: str, title: str) -> int | None:
    """
    Fetch listener count from Last.fm track.getInfo.
    Returns integer listener count, or None if track not found.
    """
    params = urlencode({
        "method":      "track.getInfo",
        "api_key":     api_key,
        "artist":      artist,
        "track":       title,
        "autocorrect": "1",
        "format":      "json",
    })
    url = f"{LASTFM_BASE}?{params}"
    req = Request(url, headers={"User-Agent": "music-organizer/1.0"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        if e.code == 429:
            time.sleep(10)
            return get_track_listeners(api_key, artist, title)  # one retry
        return None
    except URLError:
        return None

    if "error" in data:
        return None  # track not found or bad request

    try:
        return int(data["track"]["listeners"])
    except (KeyError, ValueError, TypeError):
        return None


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


# ── Listener count → stars mapping ───────────────────────────────────────────

def listeners_to_stars(counts: list[int]) -> dict[int, int]:
    """
    Map raw listener counts to 1–5 stars using percentile bucketing
    within the provided count list. Normalizes against your own collection
    so underground acts aren't penalized vs mainstream.
    Returns {raw_count: stars}.
    """
    if not counts:
        return {}
    sorted_counts = sorted(set(counts))
    n = len(sorted_counts)
    result = {}
    for count in sorted_counts:
        rank = sorted_counts.index(count) / n  # 0.0–1.0 percentile
        if   rank < 0.20: stars = 1
        elif rank < 0.40: stars = 2
        elif rank < 0.60: stars = 3
        elif rank < 0.80: stars = 4
        else:             stars = 5
        result[count] = stars
    return result


# ── Fetch phase ───────────────────────────────────────────────────────────────

def run_fetch() -> None:
    api_key = load_api_key()

    STATE_DIR.mkdir(exist_ok=True)
    cache: dict = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
        except json.JSONDecodeError:
            cache = {}

    tracks = load_unrated_tracks(TRAKTOR_NML)
    todo   = [t for t in tracks if t["path"] not in cache]

    print(f"Last.fm listener fetch")
    print(f"  Unrated tracks:  {len(tracks)}")
    print(f"  Already cached:  {len(tracks) - len(todo)}")
    print(f"  To fetch:        {len(todo)}")
    if not todo:
        print("  Nothing to do — all unrated tracks already cached.")
        return

    eta_min = len(todo) * 0.22 / 60  # ~0.2s per request + overhead
    print(f"  Estimated time:  ~{eta_min:.0f} min")
    print()

    found = not_found = errors = 0
    start = time.time()

    for i, track in enumerate(todo, 1):
        artist = track["artist"]
        title  = track["title"]

        if not artist and not title:
            cache[track["path"]] = None
            not_found += 1
        else:
            try:
                count = get_track_listeners(api_key, artist, title)
            except Exception as ex:
                count = None
                errors += 1
                if errors <= 5:
                    print(f"  [ERROR] {artist} — {title}: {ex}")
                time.sleep(1)

            if count is not None:
                cache[track["path"]] = count
                found += 1
            else:
                cache[track["path"]] = None
                not_found += 1

        if i % 200 == 0:
            CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
            elapsed   = time.time() - start
            rate      = i / elapsed
            remaining = (len(todo) - i) / rate / 60
            msg = (f"  {i}/{len(todo)} — found {found}, "
                   f"not found {not_found}, errors {errors} "
                   f"— ~{remaining:.0f} min left")
            print(msg)
            LOG_FILE.write_text(msg + "\n")

        time.sleep(0.2)  # ~5 req/sec, well inside Last.fm limits

    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
    elapsed = time.time() - start
    print(f"\nFetch complete in {elapsed/60:.1f} min")
    print(f"  With listener count: {found}")
    print(f"  Not found:           {not_found}")
    print(f"  Errors:              {errors}")
    print(f"  Cache:               {CACHE_FILE}")


# ── Report phase ──────────────────────────────────────────────────────────────

def run_report() -> dict[int, int] | None:
    if not CACHE_FILE.exists():
        print("No cache yet — run --fetch first.")
        return None

    cache = json.loads(CACHE_FILE.read_text())
    counts = [v for v in cache.values() if isinstance(v, int)]
    if not counts:
        print("Cache exists but has no listener counts — run --fetch.")
        return None

    star_map   = listeners_to_stars(counts)
    star_counts = Counter(star_map[c] for c in counts)
    total_none  = sum(1 for v in cache.values() if v is None)

    print(f"Last.fm cache summary")
    print(f"  Tracks with count:  {len(counts)}")
    print(f"  Tracks not found:   {total_none}")
    print(f"  Count range:        {min(counts):,} – {max(counts):,} listeners")
    print()
    print("Proposed star distribution (percentile bucketing within your collection):")
    for stars in range(1, 6):
        count = star_counts.get(stars, 0)
        bar   = "█" * (count // 100)
        print(f"  {stars}★  {count:6d}  {bar}")
    print()
    print("Percentile thresholds (listener counts):")
    sorted_c = sorted(counts)
    n = len(sorted_c)
    for pct, label in [(0.20, "20th"), (0.40, "40th"), (0.60, "60th"), (0.80, "80th")]:
        idx = int(pct * n)
        print(f"  {label} percentile → {sorted_c[min(idx, n-1)]:,} listeners")

    return star_map


# ── Apply phase ───────────────────────────────────────────────────────────────

def run_apply(star_map: dict[int, int]) -> None:
    cache = json.loads(CACHE_FILE.read_text())

    # Build path → RANKING value map
    path_ranking: dict[str, int] = {}
    for path, count in cache.items():
        if not isinstance(count, int):
            continue
        stars = star_map.get(count, 0)
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

    print(f"\nDone. {updated_total // 2} tracks rated.")
    print("Reload collection in Traktor to see star ratings.")


# ── Spotify OAuth (optional second source) ────────────────────────────────────

SPOTIFY_CREDS_FILE = BASE / "spotify_creds.txt"
SPOTIFY_TOKEN_FILE = BASE / "spotify_token.json"
SPOTIFY_REDIRECT   = "http://127.0.0.1:9977/callback"


def run_spotify_auth() -> None:
    """
    One-time Spotify OAuth flow. Run in a real terminal window.
    Saves token to spotify_token.json for use by --fetch-spotify.
    """
    import http.server
    import base64

    # Load Spotify creds
    client_id = client_secret = ""
    if SPOTIFY_CREDS_FILE.exists():
        for line in SPOTIFY_CREDS_FILE.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "client_id":     client_id     = v
                if k == "client_secret": client_secret = v
    if not client_id:
        print(f"ERROR: spotify_creds.txt not found or missing client_id")
        return

    auth_url = (
        "https://accounts.spotify.com/authorize?"
        + urlencode({
            "client_id":     client_id,
            "response_type": "code",
            "redirect_uri":  SPOTIFY_REDIRECT,
            "scope":         "user-read-private",
        })
    )

    code_holder: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            params = {k: v[0] for k, v in
                      parse_qs(urlparse(self.path).query).items()}
            if "code" in params:
                code_holder["code"] = params["code"]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorized! You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>No code received.</h2>")
        def log_message(self, *_): pass

    server = http.server.HTTPServer(("127.0.0.1", 9977), Handler)

    print("\nStep 1: Open this URL in your browser:\n")
    print(f"  {auth_url}\n")
    print("Step 2: Log in and click Agree.")
    print("Step 3: Browser redirects to 127.0.0.1 — this script catches it.\n")
    print("Waiting (up to 5 minutes)...")

    server.timeout = 300
    server.handle_request()

    if "code" not in code_holder:
        print("No authorization code received. Run --spotify-auth again.")
        return

    # Exchange code for tokens
    auth_header = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    body = urlencode({
        "grant_type":   "authorization_code",
        "code":         code_holder["code"],
        "redirect_uri": SPOTIFY_REDIRECT,
    }).encode()
    req = Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    resp = json.loads(urlopen(req, timeout=10).read())
    if "access_token" not in resp:
        print(f"Token error: {resp}")
        return

    token_data = {
        "access_token":  resp["access_token"],
        "refresh_token": resp.get("refresh_token", ""),
        "expires_at":    time.time() + resp["expires_in"],
    }
    SPOTIFY_TOKEN_FILE.write_text(json.dumps(token_data))

    # Quick sanity check
    test_req = Request(
        "https://api.spotify.com/v1/tracks/7dEdD8frVrU93o1cDadbOb",
        headers={"Authorization": f"Bearer {resp['access_token']}"},
    )
    test = json.loads(urlopen(test_req, timeout=10).read())
    print(f"\n✓ Auth complete!")
    print(f"  Test track popularity: {test.get('popularity')}")
    print(f"  Token saved → {SPOTIFY_TOKEN_FILE}")
    print(f"\nNow run: python3 stage8j_spotify_ratings.py --fetch")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Last.fm listener counts and set Traktor star ratings"
    )
    parser.add_argument("--fetch",        action="store_true", help="Fetch from Last.fm API")
    parser.add_argument("--report",       action="store_true", help="Preview star mapping")
    parser.add_argument("--apply",        action="store_true", help="Write ratings to NML")
    parser.add_argument("--spotify-auth", action="store_true", help="One-time Spotify OAuth (run in a real terminal)")
    args = parser.parse_args()

    if not any([args.fetch, args.report, args.apply, args.spotify_auth]):
        parser.print_help()
        return

    if args.spotify_auth:
        run_spotify_auth()
        return

    star_map = None

    if args.fetch:
        run_fetch()

    if args.report or args.apply:
        star_map = run_report()
        if star_map is None:
            return

    if args.apply:
        print()
        run_apply(star_map)


if __name__ == "__main__":
    main()
