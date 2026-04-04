"""
Rate-limited async AcoustID client.

AcoustID API limit: 3 requests/second.
Supports batch lookup (up to 3 fingerprints per call).

Usage:
    async with AcoustIDClient(api_key) as client:
        result = await client.lookup(fingerprint, duration)
"""

import asyncio
import time
import os
import logging

import aiohttp

logger = logging.getLogger(__name__)

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
RATE_LIMIT = 3      # requests per second (AcoustID allows up to 3)
BATCH_SIZE = 1      # AcoustID batch param is complex; keep 1 per request for reliability
TIMEOUT = 10        # seconds per request
MAX_RETRIES = 3
DEFAULT_BACKOFF = 30   # seconds to pause ALL requests after a 429


class AcoustIDClient:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._last_requests: list[float] = []
        self._lock = asyncio.Lock()
        # Global backoff: when set, ALL requests wait until this monotonic time.
        # This ensures that a 429 from any one coroutine quiets every other coroutine too.
        self._backoff_until: float = 0.0

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        )
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    async def _wait_for_rate_limit(self):
        """Enforce 3 requests/second using a sliding window, with global backoff support."""
        async with self._lock:
            # Honour any active global backoff first (set when we receive a 429).
            now = time.monotonic()
            if self._backoff_until > now:
                wait_time = self._backoff_until - now
                logger.info("AcoustID global backoff: sleeping %.1fs", wait_time)
                await asyncio.sleep(wait_time)

            # Sliding-window rate limiter: no more than RATE_LIMIT calls per second.
            now = time.monotonic()
            self._last_requests = [t for t in self._last_requests if now - t < 1.0]
            if len(self._last_requests) >= RATE_LIMIT:
                wait_time = 1.0 - (now - self._last_requests[0])
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self._last_requests = self._last_requests[1:]
            self._last_requests.append(time.monotonic())

    def _set_global_backoff(self, seconds: float):
        """Park all future requests for `seconds`. Called under self._lock."""
        self._backoff_until = max(self._backoff_until, time.monotonic() + seconds)

    async def lookup(self, fingerprint: str, duration: int) -> dict | None:
        """
        Look up a fingerprint via AcoustID.

        Returns the best match dict:
        {
            'id': acoustid_id,
            'score': float,
            'recordings': [{'id': mb_id, 'title': ..., 'artists': [...], ...}]
        }
        or None if no match found.
        """
        await self._wait_for_rate_limit()

        params = {
            "client": self._api_key,
            "fingerprint": fingerprint,
            "duration": str(int(duration)),
            "meta": "recordings releases releasegroups tracks",
        }

        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.get(ACOUSTID_URL, params=params) as resp:
                    if resp.status in (429, 503):
                        # 429 = rate limited, 503 = server overloaded — both mean back off.
                        retry_after = float(resp.headers.get("Retry-After", DEFAULT_BACKOFF))
                        backoff = max(retry_after, DEFAULT_BACKOFF * (attempt + 1))
                        logger.warning(
                            "AcoustID %d (attempt %d) — global backoff %.0fs",
                            resp.status, attempt + 1, backoff,
                        )
                        async with self._lock:
                            self._set_global_backoff(backoff)
                        await asyncio.sleep(backoff)
                        continue
                    if resp.status != 200:
                        logger.warning("AcoustID returned %d for fingerprint (attempt %d)", resp.status, attempt + 1)
                        await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    if data.get("status") != "ok":
                        logger.warning("AcoustID status not ok: %s", data.get("status"))
                        return None
                    results = data.get("results", [])
                    if not results:
                        return None
                    # Return the highest-score result
                    return max(results, key=lambda r: r.get("score", 0))
            except asyncio.TimeoutError:
                logger.warning("AcoustID timeout (attempt %d)", attempt + 1)
                await asyncio.sleep(2 ** attempt)
            except aiohttp.ClientError as e:
                logger.warning("AcoustID client error: %s (attempt %d)", e, attempt + 1)
                await asyncio.sleep(2 ** attempt)

        return None
