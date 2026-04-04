"""
MusicBrainz HTTP client with caching.

Rate limit: 1 request/second per MusicBrainz API terms.
Read-only access — no authentication required for lookups.
Optionally uses OAuth Bearer token if MB_OAUTH_TOKEN env var is set.
Caches recording lookups to disk to avoid repeat calls.

Usage:
    async with MusicBrainzClient() as mb:
        meta = await mb.get_recording(mb_recording_id)
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "MusicOrganizePipeline/1.0 (aaron.s.rhodes@gmail.com)"
# MusicBrainz requires 1 req/sec for unauthenticated access
RATE_LIMIT = 1.0     # minimum seconds between requests
TIMEOUT = 15
MAX_RETRIES = 3
DEFAULT_BACKOFF = 60  # seconds to pause ALL requests after a 503/429

_STATE_DIR = Path(__file__).parent.parent / "state"


class MusicBrainzClient:
    def __init__(self, cache_path: Path | None = None):
        self._session: aiohttp.ClientSession | None = None
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._cache_path = cache_path or (_STATE_DIR / "mb_cache.json")
        self._cache: dict = {}
        # Global backoff: when set, ALL requests wait until this monotonic time.
        self._backoff_until: float = 0.0

    def _load_cache(self):
        if self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text())
            except Exception:
                self._cache = {}

    def _save_cache(self):
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2))

    async def __aenter__(self):
        self._load_cache()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        # Use OAuth Bearer token if available (optional — not required for read-only)
        oauth_token = os.environ.get("MB_OAUTH_TOKEN")
        if oauth_token:
            headers["Authorization"] = f"Bearer {oauth_token}"
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        )
        return self

    async def __aexit__(self, *_):
        self._save_cache()
        if self._session:
            await self._session.close()

    def _set_global_backoff(self, seconds: float):
        """Park all future requests for `seconds`. Must be called under self._lock."""
        self._backoff_until = max(self._backoff_until, time.monotonic() + seconds)

    async def _wait_rate_limit(self):
        async with self._lock:
            # Honour any active global backoff first (set when we receive a 429/503).
            now = time.monotonic()
            if self._backoff_until > now:
                wait_time = self._backoff_until - now
                logger.info("MusicBrainz global backoff: sleeping %.1fs", wait_time)
                await asyncio.sleep(wait_time)

            # Standard 1 req/sec minimum spacing.
            now = time.monotonic()
            wait = RATE_LIMIT - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def get_recording(self, recording_id: str) -> dict | None:
        """
        Fetch metadata for a MusicBrainz recording ID.

        Returns a dict with keys: title, artist, album, track_number, year
        or None if not found.
        """
        if recording_id in self._cache:
            return self._cache[recording_id]

        await self._wait_rate_limit()

        url = f"{MB_BASE}/recording/{recording_id}"
        params = {"inc": "artists+releases", "fmt": "json"}

        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status in (429, 503):
                        # Server politely asked us to slow down — honour the request.
                        retry_after = float(resp.headers.get("Retry-After", DEFAULT_BACKOFF))
                        backoff = max(retry_after, DEFAULT_BACKOFF * (attempt + 1))
                        logger.warning(
                            "MusicBrainz %d (attempt %d) — global backoff %.0fs",
                            resp.status, attempt + 1, backoff,
                        )
                        async with self._lock:
                            self._set_global_backoff(backoff)
                        await asyncio.sleep(backoff)
                        continue
                    if resp.status == 404:
                        self._cache[recording_id] = None
                        return None
                    if resp.status != 200:
                        logger.warning("MB returned %d for %s", resp.status, recording_id)
                        await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    try:
                        result = _parse_recording(data)
                    except Exception as e:
                        logger.warning("MB parse error for %s: %s", recording_id, e)
                        return None
                    self._cache[recording_id] = result
                    return result
            except asyncio.TimeoutError:
                logger.warning("MB timeout for %s (attempt %d)", recording_id, attempt + 1)
                await asyncio.sleep(2 ** attempt)
            except aiohttp.ClientError as e:
                logger.warning("MB client error: %s", e)
                await asyncio.sleep(2 ** attempt)

        return None


def _parse_recording(data: dict) -> dict:
    """Extract clean metadata from a MusicBrainz recording response."""
    title = data.get("title", "")

    # Artist: take first credited artist
    artist_credits = data.get("artist-credit", [])
    names = []
    for credit in artist_credits:
        if isinstance(credit, dict) and "artist" in credit:
            name = credit.get("name") or credit["artist"].get("name", "")
            join = credit.get("joinphrase", "")
            names.append(name + join)
    artist = "".join(names).strip(" ,&")

    # Album and track info from releases
    album = ""
    track_number = None
    year = None
    releases = data.get("releases", [])
    if releases:
        # Prefer official releases over promos/bootlegs
        official = [r for r in releases if (r.get("status") or "").lower() == "official"]
        release = official[0] if official else releases[0]
        album = release.get("title", "")
        date = release.get("date", "")
        if date:
            year = int(date[:4]) if date[:4].isdigit() else None
        # Track number from media
        for medium in release.get("media", []):
            for track in medium.get("tracks", []):
                trk_rec = track.get("recording", {})
                if trk_rec.get("id") == data.get("id"):
                    track_number = track.get("number")
                    break

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "track_number": track_number,
        "year": year,
    }
