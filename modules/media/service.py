"""
MediaService — lifecycle wrapper

Builds providers from config, owns the MediaController, and runs a poll loop
that refreshes player state and pushes a ``media_state`` event over the
WebSocket so the UI updates live.

Config (config.yaml):
  media:
    enabled: true
    cast:    { enabled: true, app_id: "CC1AD845" }
    wiim:    { enabled: true, devices: ["192.168.1.50"] }
    radio_browser: { enabled: true }
    poll_interval_seconds: 10
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from modules.media.controller import MediaController
from modules.media.models import MediaItem, PlayerState
from modules.media.players.wiim import WiiMPlayerProvider
from modules.media.sources.radio_browser import RadioBrowserSource

logger = logging.getLogger("modules.media")


class MediaService:
    def __init__(self, config: dict):
        config = config or {}
        self.enabled = config.get("enabled", False)
        self.poll_interval = int(config.get("poll_interval_seconds", 10))

        self.controller = MediaController()
        self._task: Optional[asyncio.Task] = None

        # ---- Sources ----
        rb_cfg = config.get("radio_browser", {}) or {}
        self.radio = RadioBrowserSource(enabled=rb_cfg.get("enabled", True))
        self.controller.add_source(self.radio)

        # Tidal — isolated/optional. Always registered as a source (its methods
        # no-op until logged in); a fresh stream URL is resolved at play time.
        tidal_cfg = config.get("tidal", {}) or {}
        from modules.media.sources.tidal import TidalSource
        self.tidal = TidalSource(
            enabled=tidal_cfg.get("enabled", False),
            quality=tidal_cfg.get("quality", "high"),
            manifest_base_url=tidal_cfg.get("manifest_base_url", ""),
        )
        self.controller.add_source(self.tidal)
        self.controller.register_resolver("tidal", self.tidal.resolve_url)
        # Infinite radio: top up from the seed track's "track radio".
        self.controller.register_extender(self.tidal.track_radio)

        # ---- Player providers ----
        wiim_cfg = config.get("wiim", {}) or {}
        if wiim_cfg.get("enabled", True):
            self.controller.add_player_provider(
                WiiMPlayerProvider(
                    device_ips=wiim_cfg.get("devices", []) or [],
                    enabled=True,
                )
            )

        cast_cfg = config.get("cast", {}) or {}
        if cast_cfg.get("enabled", True):
            # Imported lazily so the app still boots if pychromecast isn't installed.
            try:
                from modules.media.players.cast import CastPlayerProvider
                self.controller.add_player_provider(
                    CastPlayerProvider(app_id=cast_cfg.get("app_id") or "CC1AD845")
                )
            except ImportError as e:
                logger.warning(f"Cast support unavailable (pychromecast not installed?): {e}")

    # ------------------------------------------------------------------
    # Public helpers used by routes
    # ------------------------------------------------------------------
    async def play_radio_station(self, player_id: str, station_uuid: str) -> MediaItem:
        station = await self.radio.get_station(station_uuid)
        if not station:
            raise ValueError(f"Radio station {station_uuid} not found")
        item = station.to_media_item()
        # Single-item queue so radio shows consistently in the now-playing/queue
        # UI. Radio is LIVE (duration 0) so it never auto-advances.
        await self.controller.play_items(player_id, [item])
        return item

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if not self.enabled:
            logger.info("Media service disabled")
            return
        self._task = asyncio.create_task(self._run())
        logger.info(f"Media service started (poll={self.poll_interval}s)")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        try:
            await self.controller.start()
        except Exception as e:
            logger.error(f"Media controller start failed: {e}")
        await self._poll_loop()

    async def _poll_loop(self):
        while True:
            try:
                snapshot = await self.controller.refresh()
                await self.controller.tick()        # auto-advance finished tracks
                await self._broadcast(snapshot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Media poll failed: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _broadcast(self, snapshot: List[PlayerState]):
        try:
            from routes.websocket_routes import broadcast_event
            await broadcast_event("media_state", {
                "players": [s.to_dict() for s in snapshot],
            })
        except Exception as e:
            logger.debug(f"Media broadcast failed: {e}")
