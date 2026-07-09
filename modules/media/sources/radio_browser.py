"""
Radio-Browser source — the free community internet-radio directory.

API: https://api.radio-browser.info  (https://www.radio-browser.info)

Etiquette the directory asks for and we honour:
  * Resolve a concrete server from the round-robin host ``all.api.radio-browser.info``
    (DNS returns multiple mirrors) and reuse it; don't hammer one mirror.
  * Send a descriptive, unique User-Agent.
  * Use ``url_resolved`` (the directory follows playlists/redirects for us) so
    the player gets a directly-playable stream.
"""
from __future__ import annotations

import logging
import random
import socket
from typing import List, Optional

import httpx

from modules.media.models import MediaItem, RadioStation
from modules.media.sources.base import SourceProvider

logger = logging.getLogger("modules.media.radio_browser")

USER_AGENT = "ZigBeeMatterManager/1.0 (+media-subsystem)"
_BOOTSTRAP_HOST = "all.api.radio-browser.info"


class RadioBrowserSource(SourceProvider):
    source = "radio_browser"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._base_url: Optional[str] = None

    async def start(self) -> None:
        if not self.enabled:
            return
        self._base_url = self._resolve_server()
        if self._base_url:
            logger.info(f"Radio-Browser using mirror {self._base_url}")
        else:
            logger.warning("Radio-Browser: could not resolve a mirror; will retry on demand")

    def _resolve_server(self) -> Optional[str]:
        """
        Pick a random Radio-Browser mirror by resolving the bootstrap host.
        Synchronous DNS, but only called once at startup (and lazily on retry).
        """
        try:
            infos = socket.getaddrinfo(_BOOTSTRAP_HOST, 443, proto=socket.IPPROTO_TCP)
            hosts = set()
            for info in infos:
                ip = info[4][0]
                try:
                    name = socket.gethostbyaddr(ip)[0]
                    hosts.add(name)
                except (socket.herror, OSError):
                    hosts.add(ip)
            if hosts:
                return f"https://{random.choice(sorted(hosts))}"
        except (socket.gaierror, OSError) as e:
            logger.warning(f"Radio-Browser DNS resolution failed: {e}")
        return None

    async def _ensure_base(self) -> Optional[str]:
        if not self._base_url:
            self._base_url = self._resolve_server()
        return self._base_url

    async def _get_json(self, path: str, params: Optional[dict] = None):
        """GET a Radio-Browser endpoint. If the cached mirror has gone stale,
        re-resolve and retry once within this call (rather than failing now
        and only succeeding on the caller's next attempt). Returns None if
        both attempts fail."""
        headers = {"User-Agent": USER_AGENT}
        for attempt in (1, 2):
            base = await self._ensure_base()
            if not base:
                return None
            try:
                async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                    resp = await client.get(f"{base}{path}", params=params)
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"Radio-Browser request failed on {base} "
                               f"(attempt {attempt}/2): {e}")
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
                favicon=r.get("favicon", ""),
                homepage=r.get("homepage", ""),
                country=r.get("country", ""),
                tags=r.get("tags", ""),
                codec=r.get("codec", ""),
                bitrate=int(r.get("bitrate", 0) or 0),
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
            favicon=r.get("favicon", ""),
            homepage=r.get("homepage", ""),
            country=r.get("country", ""),
            tags=r.get("tags", ""),
            codec=r.get("codec", ""),
            bitrate=int(r.get("bitrate", 0) or 0),
        )
