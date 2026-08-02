"""
Radio-Browser source — the free community internet-radio directory
(https://api.radio-browser.info).

Etiquette the directory asks for and this honours: resolve a concrete mirror
from the round-robin host and reuse it rather than hammering one, send a
descriptive unique User-Agent, and use `url_resolved` so the player gets a
directly-playable stream. Mirror failover: docs/speaker_sync.md.
"""
from __future__ import annotations

import asyncio
import logging
import random
import socket
from typing import List, Optional, Set

import httpx

from modules.media.models import MediaItem, RadioStation, https_url
from modules.media.sources.base import SourceProvider

logger = logging.getLogger("modules.media.radio_browser")

USER_AGENT = "ZigBeeMatterManager/1.0 (+media-subsystem)"
_BOOTSTRAP_HOST = "all.api.radio-browser.info"
_MIRROR_TRIES = 3          # distinct mirrors per request before giving up


class RadioBrowserSource(SourceProvider):
    source = "radio_browser"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._base_url: Optional[str] = None    # mirror that last answered
        self._mirrors: List[str] = []           # known mirrors, in try order

    async def start(self) -> None:
        if not self.enabled:
            return
        self._mirrors = await asyncio.to_thread(self._resolve_mirrors)
        self._base_url = self._mirrors[0] if self._mirrors else None
        if self._base_url:
            logger.info(f"Radio-Browser using mirror {self._base_url} "
                        f"({len(self._mirrors)} known)")
        else:
            logger.warning("Radio-Browser: could not resolve a mirror; will retry on demand")

    def _resolve_mirrors(self) -> List[str]:
        """Every mirror behind the round-robin host, shuffled. Blocking DNS —
        always call in a thread."""
        hosts: Set[str] = set()
        try:
            infos = socket.getaddrinfo(_BOOTSTRAP_HOST, 443, proto=socket.IPPROTO_TCP)
            for info in infos:
                ip = info[4][0]
                try:
                    hosts.add(socket.gethostbyaddr(ip)[0])
                except (socket.herror, OSError):
                    hosts.add(ip)
        except (socket.gaierror, OSError) as e:
            logger.warning(f"Radio-Browser DNS resolution failed: {e}")
        mirrors = [f"https://{h}" for h in sorted(hosts)]
        random.shuffle(mirrors)
        mirrors.append(f"https://{_BOOTSTRAP_HOST}")   # last resort: reverse DNS blocked
        return mirrors

    async def _next_mirror(self, tried: Set[str]) -> Optional[str]:
        """An untried mirror; re-resolves the list once it's exhausted."""
        if self._base_url and self._base_url not in tried:
            return self._base_url
        candidates = [m for m in self._mirrors if m not in tried]
        if not candidates:
            self._mirrors = await asyncio.to_thread(self._resolve_mirrors)
            candidates = [m for m in self._mirrors if m not in tried]
        return candidates[0] if candidates else None

    async def _get_json(self, path: str, params: Optional[dict] = None):
        """GET a Radio-Browser endpoint, walking distinct mirrors on failure.
        Returns None if every attempt fails."""
        headers = {"User-Agent": USER_AGENT}
        tried: Set[str] = set()
        for attempt in range(1, _MIRROR_TRIES + 1):
            base = await self._next_mirror(tried)
            if not base:
                return None
            tried.add(base)
            try:
                async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                    resp = await client.get(f"{base}{path}", params=params)
                    resp.raise_for_status()
                    data = resp.json()
                self._base_url = base       # this one answers — stick with it
                return data
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"Radio-Browser request failed on {base} "
                               f"(attempt {attempt}/{_MIRROR_TRIES}): {e}")
                if self._base_url == base:
                    self._base_url = None
        return None

    async def search(self, query: str, limit: int = 25) -> List[MediaItem]:
        stations = await self.search_stations(query, limit)
        return [s.to_media_item() for s in stations]

    async def search_stations(self, query: str, limit: int = 25) -> List[RadioStation]:
        if not self.enabled:
            return []
        params = {
            "name": query,
            "limit": limit,
            "hidebroken": "true",
            "order": "votes",
            "reverse": "true",
        }
        rows = await self._get_json("/json/stations/search", params)
        if rows is None:
            return []

        stations: List[RadioStation] = []
        for r in rows:
            stream = r.get("url_resolved") or r.get("url")
            if not stream:
                continue
            stations.append(RadioStation(
                uuid=r.get("stationuuid", ""),
                name=r.get("name", "").strip() or "Unknown station",
                url=stream,
                favicon=https_url(r.get("favicon", "")),
                homepage=r.get("homepage", ""),
                country=r.get("country", ""),
                tags=r.get("tags", ""),
                codec=r.get("codec", ""),
                bitrate=int(r.get("bitrate", 0) or 0),
                hls=bool(int(r.get("hls", 0) or 0)),
            ))
        return stations

    async def get_station(self, uuid: str) -> Optional[RadioStation]:
        """Fetch a single station by its stable UUID (used by /play)."""
        if not self.enabled or not uuid:
            return None
        rows = await self._get_json(f"/json/stations/byuuid/{uuid}")
        if not rows:
            return None
        r = rows[0]
        stream = r.get("url_resolved") or r.get("url")
        if not stream:
            return None
        # Best-effort click counter — the directory uses it for ranking.
        if self._base_url:
            try:
                async with httpx.AsyncClient(timeout=5.0,
                                             headers={"User-Agent": USER_AGENT}) as client:
                    await client.get(f"{self._base_url}/json/url/{uuid}")
            except httpx.HTTPError:
                pass
        return RadioStation(
            uuid=r.get("stationuuid", ""),
            name=r.get("name", "").strip() or "Unknown station",
            url=stream,
            favicon=https_url(r.get("favicon", "")),
            homepage=r.get("homepage", ""),
            country=r.get("country", ""),
            tags=r.get("tags", ""),
            codec=r.get("codec", ""),
            bitrate=int(r.get("bitrate", 0) or 0),
            hls=bool(int(r.get("hls", 0) or 0)),
        )
