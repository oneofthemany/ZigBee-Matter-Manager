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
        self.cast = None                       # set if Cast is enabled

        # "Karaoke mode": cast synced lyrics (+ art) to the custom receiver.
        # Default from config, then overridden by the persisted runtime toggle.
        cast_cfg0 = config.get("cast", {}) or {}
        self._prefs_file = "./data/media_prefs.json"
        self._prefs = self._load_prefs()
        self._karaoke = bool(self._prefs.get("karaoke", cast_cfg0.get("karaoke", True)))

        # Text-to-speech for announcements. Default is the keyless Google
        # Translate TTS endpoint (returns a plain MP3 Cast/WiiM play directly).
        tts_cfg = config.get("tts", {}) or {}
        self._tts_base = tts_cfg.get("base_url", "https://translate.google.com/translate_tts")
        self._tts_lang = tts_cfg.get("lang", "en")

        # ---- Sources ----
        rb_cfg = config.get("radio_browser", {}) or {}
        self.radio = RadioBrowserSource(enabled=rb_cfg.get("enabled", True))
        self.controller.add_source(self.radio)

        # Pinned radio stations so the user doesn't re-search the directory each
        # time. Stored with the resolved stream URL → plays without a lookup.
        from modules.media.favourites import RadioFavourites
        self.radio_favourites = RadioFavourites(
            rb_cfg.get("favourites_file", "./data/radio_favourites.json"))

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
                self.cast = CastPlayerProvider(
                    app_id=cast_cfg.get("app_id") or "CC1AD845",
                    # Custom receiver that shows album art + synced lyrics on
                    # screened devices (Nest Hub). Empty → feature off.
                    lyrics_app_id=cast_cfg.get("lyrics_app_id", ""),
                    lyrics_getter=self.tidal.track_lyrics,
                    karaoke=self._karaoke,
                )
                self.controller.add_player_provider(self.cast)
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
    # Karaoke mode (cast synced lyrics to the custom receiver)
    # ------------------------------------------------------------------
    def get_karaoke(self) -> bool:
        return self._karaoke

    def set_karaoke(self, on: bool) -> dict:
        self._karaoke = bool(on)
        if self.cast is not None:
            self.cast.karaoke = self._karaoke
        self._prefs["karaoke"] = self._karaoke
        self._save_prefs()
        # Whether the feature can actually do anything (needs a custom receiver).
        configured = bool(getattr(self.cast, "lyrics_app_id", "")) if self.cast else False
        return {"success": True, "karaoke": self._karaoke,
                "receiver_configured": configured}

    def _load_prefs(self) -> dict:
        import json
        try:
            with open(self._prefs_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"Could not read {self._prefs_file}: {e}")
            return {}

    def _save_prefs(self) -> None:
        import json, os, tempfile
        d = os.path.dirname(self._prefs_file) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._prefs, f, indent=2)
            os.replace(tmp, self._prefs_file)
        except Exception as e:
            logger.error(f"Could not write {self._prefs_file}: {e}")

    async def play_radio_favourite(self, player_id: str, uuid: str) -> MediaItem:
        """Play a pinned station straight from its stored snapshot — no
        directory lookup. Falls back to a live lookup if it isn't pinned."""
        fav = self.radio_favourites.get(uuid)
        if not fav:
            return await self.play_radio_station(player_id, uuid)
        from modules.media.models import RadioStation
        station = RadioStation(**{k: fav.get(k) for k in (
            "uuid", "name", "url", "favicon", "homepage",
            "country", "tags", "codec", "bitrate") if fav.get(k) is not None})
        item = station.to_media_item()
        await self.controller.play_items(player_id, [item])
        return item

    async def announce(self, player_id: str, text: str, lang: str = None,
                       volume: float = None) -> dict:
        """Speak ``text`` on a player via TTS (e.g. an automation alert).
        Optionally sets volume first. Plays directly, bypassing the queue."""
        text = (text or "").strip()
        if not text:
            return {"success": False, "error": "No text to announce"}
        if volume is not None:
            await self.controller.set_volume(player_id, float(volume))
        item = MediaItem(
            url=self._tts_url(text, lang),
            title=text[:60],
            artist="Announcement",
            media_type="tts",            # no resolver → URL used as-is
            content_type="audio/mpeg",
        )
        await self.controller.play_url(player_id, item)
        return {"success": True}

    def _tts_url(self, text: str, lang: str = None) -> str:
        from urllib.parse import quote
        # The Translate TTS endpoint caps ~200 chars/request; truncate for safety.
        return (f"{self._tts_base}?ie=UTF-8&client=tw-ob"
                f"&tl={lang or self._tts_lang}&q={quote(text[:200])}")

    async def play_tidal(self, player_id: str, kind: str, tidal_id: str,
                         mode: str = "play") -> dict:
        """Resolve a Tidal track/album/playlist/artist/mix to items and play them.
        ``mode='radio'`` makes track/artist play an infinite auto-extending queue.
        Shared by the API route and the automation engine."""
        src = self.controller.get_source("tidal")
        if not src:
            return {"success": False, "error": "Tidal unavailable"}
        radio = mode == "radio"
        if kind == "track":
            if radio:
                items = await src.track_radio(tidal_id)
            else:
                item = await src.single_item(tidal_id)
                items = [item] if item else []
        elif kind == "album":
            items = await src.album_items(tidal_id)
        elif kind == "playlist":
            items = await src.playlist_items(tidal_id)
        elif kind == "artist":
            items = await (src.artist_radio(tidal_id) if radio else src.artist_tracks(tidal_id))
        elif kind == "mix":
            items = await src.mix_items(tidal_id)
        else:
            return {"success": False, "error": "kind must be track|album|playlist|artist|mix"}
        if not items:
            return {"success": False, "error": "Nothing to play (empty, not found, or login required)"}
        await self.controller.play_items(player_id, items, auto_extend=radio)
        return {"success": True, "count": len(items), "radio": radio}

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
