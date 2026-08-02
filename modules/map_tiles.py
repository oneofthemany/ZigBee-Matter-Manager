"""
Caching map-tile proxy.

Proxying means the tile server sees the hub's address once per tile rather than
every viewer's address on every pan, and a cached tile leaves the house
entirely. Demand-driven and concurrency-capped to respect OSM's tile policy —
do not point a prefetcher at it. See docs/security.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("modules.map_tiles")

# Zoom bounds. 19 is the deepest most OSM styles render; beyond it the server
# returns errors and the requests are wasted.
MIN_ZOOM = 0
MAX_ZOOM = 19

# Politeness. A pan across a map fires a dozen or more tile requests at once;
# without a cap those all hit upstream simultaneously the first time.
MAX_CONCURRENT_FETCHES = 4

# Tiles are immutable enough in practice. Re-fetching monthly keeps the map
# from ossifying without generating meaningful traffic.
CACHE_TTL_S = 30 * 24 * 3600

FETCH_TIMEOUT_S = 15

DEFAULT_UPSTREAM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Identifies this software to the tile server, as their policy requires.
# A generic or absent UA is the fastest way to get a self-hosted tool blocked.
USER_AGENT = "ZigBee-Matter-Manager/1.0 (self-hosted home automation; presence map)"


class TileCache:
    """Disk-backed tile cache with an upstream fetch on miss."""

    def __init__(
            self,
            cache_dir: Path = Path("./data/tile_cache"),
            upstream: str = DEFAULT_UPSTREAM,
            ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.upstream = upstream
        self.ttl_s = ttl_s
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        # Collapses duplicate in-flight requests for the same tile: several
        # browsers opening the same map would otherwise each fetch it.
        self._inflight: dict[Tuple[int, int, int], asyncio.Future] = {}
        self.hits = 0
        self.misses = 0

    # validation

    @staticmethod
    def valid(z: int, x: int, y: int) -> bool:
        """
        Reject anything outside the tile pyramid.

        This is a path-safety check as much as a correctness one: z/x/y are
        interpolated into both a filesystem path and an upstream URL, so a
        negative or oversized index must never get that far. Integer typing
        alone stops traversal; the range check stops us hammering upstream
        with requests that cannot succeed.
        """
        if not (MIN_ZOOM <= z <= MAX_ZOOM):
            return False
        limit = 1 << z              # 2^z tiles per axis at zoom z
        return 0 <= x < limit and 0 <= y < limit

    def path_for(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    # read path

    def _fresh_bytes(self, p: Path) -> Optional[bytes]:
        try:
            st = p.stat()
        except FileNotFoundError:
            return None
        if self.ttl_s and (time.time() - st.st_mtime) > self.ttl_s:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    async def get(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Cached tile, fetching upstream on miss. None if unavailable."""
        if not self.valid(z, x, y):
            return None

        p = self.path_for(z, x, y)
        cached = self._fresh_bytes(p)
        if cached is not None:
            self.hits += 1
            return cached

        key = (z, x, y)
        inflight = self._inflight.get(key)
        if inflight is not None:
            return await inflight

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            data = await self._fetch(z, x, y)
            if data:
                self.misses += 1
                self._store(p, data)
            if not fut.done():
                fut.set_result(data)
            return data
        except Exception as e:                      # noqa: BLE001
            logger.warning("[tiles] fetch %s/%s/%s failed: %s", z, x, y, e)
            if not fut.done():
                fut.set_result(None)
            return None
        finally:
            self._inflight.pop(key, None)

    def _store(self, p: Path, data: bytes) -> None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash or a concurrent reader never sees a
            # half-written PNG, which would be cached as permanently corrupt.
            tmp = p.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(p)
        except OSError as e:
            logger.warning("[tiles] could not cache %s: %s", p, e)

    async def _fetch(self, z: int, x: int, y: int) -> Optional[bytes]:
        url = self.upstream.format(z=z, x=x, y=y)
        async with self._sem:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._fetch_blocking, url,
            )

    @staticmethod
    def _fetch_blocking(url: str) -> Optional[bytes]:
        # urllib rather than a new dependency: one GET, no redirects worth
        # following, no session state.
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
                if r.status != 200:
                    return None
                ctype = (r.headers.get("Content-Type") or "").lower()
                # Guard against caching an error page as though it were a tile.
                if "image" not in ctype:
                    logger.warning("[tiles] non-image response (%s) from %s", ctype, url)
                    return None
                return r.read()
        except urllib.error.URLError as e:
            logger.warning("[tiles] upstream error for %s: %s", url, e)
            return None

    # maintenance

    def stats(self) -> dict:
        files = 0
        total = 0
        if self.cache_dir.exists():
            for f in self.cache_dir.rglob("*.png"):
                files += 1
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return {
            "tiles": files,
            "bytes": total,
            "hits": self.hits,
            "misses": self.misses,
            "cache_dir": str(self.cache_dir),
            "upstream": self.upstream,
        }

    def clear(self) -> int:
        removed = 0
        if self.cache_dir.exists():
            for f in self.cache_dir.rglob("*.png"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


_cache: Optional[TileCache] = None


def get_tile_cache() -> TileCache:
    global _cache
    if _cache is None:
        _cache = TileCache()
    return _cache


def set_tile_cache(c: TileCache) -> None:
    global _cache
    _cache = c
