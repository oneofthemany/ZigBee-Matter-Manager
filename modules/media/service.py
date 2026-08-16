"""
MediaService — lifecycle wrapper. Builds providers from config, owns the
MediaController, and polls player state, pushing `media_state` over the
websocket. Config: docs/speaker_sync.md.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

from modules.media.controller import MediaController
from modules.media.models import MediaItem, PlayerState, RadioStation
from modules.media.players.wiim import WiiMPlayerProvider
from modules.media.players.zone import PREFIX as _ZONE_PREFIX, zone_id as _zone_id
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

        # Session adoption: persist queues + eagerly connect to casting devices so
        # control (next/prev, lyrics, artist) survives an app restart/upgrade.
        self._adopt_sessions = bool(config.get("adopt_sessions", True))
        self._sessions_file = config.get("sessions_file", "./data/media_sessions.json")
        self._sessions_blob = None             # last-written JSON, to debounce saves
        self._sessions_written_at = 0.0        # ...and when, to keep saved_at fresh
        self._restored: dict = {}              # the file as loaded, for resume
        # Restart resume: re-issue playback the app was serving when it went
        # down. Only meaningful where we are in the audio path — a speaker
        # fetching its source directly never notices a restart.
        self._resume_enabled = bool(config.get("resume_after_restart", True))
        self._resume_max_age_s = float(config.get("resume_max_age_s", 600))

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

        # Sources
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
            # Lossless to a zone is decoded on this host, so it fetches the
            # manifest over loopback — deferred, the listener is built below.
            local_base=lambda: f"http://127.0.0.1:{self.device_http.port}",
        )
        self.controller.add_source(self.tidal)
        self.controller.register_resolver("tidal", self.tidal.resolve_url)
        # Infinite radio: top up from the seed track's "track radio".
        self.controller.register_extender(self.tidal.track_radio)

        # Always constructed; wants() gates on the zmm_eq wheel + ffmpeg + a
        # device-reachable base URL. Devices reject the self-signed HTTPS, so their
        # streams come from the plain-HTTP device listener below and the base URL is
        # derived from it — media.eq.base_url only overrides for multi-homed hosts.
        eq_cfg = config.get("eq", {}) or {}
        from modules.media.device_http import DeviceAudioListener, lan_ip
        self.device_http = DeviceAudioListener(
            self, port=int(config.get("device_http_port", 8011)))
        from modules.media.eq_stream import EqStreamEngine
        self.eq_stream = EqStreamEngine(
            base_url=eq_cfg.get("base_url") or tidal_cfg.get("manifest_base_url", ""),
            settings_file=eq_cfg.get("settings_file", "./data/media_eq.json"),
            fallback_base=lambda: (
                f"http://{lan_ip()}:{self.device_http.port}" if lan_ip() else ""),
        )
        self.controller.set_eq_engine(self.eq_stream)

        # Player providers
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

        # Sync PoC — synchronised multi-speaker casting without a Google-Home
        # group (custom receiver + shared server clock). Off by default; see
        # modules/media/cast_sync.py and static/cast/README.md.
        self.cast_sync = None
        sync_cfg = cast_cfg.get("sync", {}) or {}
        if self.cast is not None and sync_cfg.get("enabled", False):
            try:
                from modules.media.cast_sync import CastSyncPoc
                self.cast_sync = CastSyncPoc(self.cast, sync_cfg)
                # Lets a sync group carry the same server-side EQ a single
                # Cast player gets, keyed "syncgroup:<gid>".
                self.cast_sync.set_eq_engine(self.eq_stream)
                # Resolved as a zone: ffmpeg decodes the timeline here, so it
                # takes Tidal's DASH/FLAC where a speaker would need AAC.
                self.cast_sync.set_url_resolver(
                    lambda mt, sid: self.controller.resolve_source_url(
                        mt, sid, provider="zone"))
                # Lets a zone play a Tidal album/playlist/mix/artist, not just
                # one track: the engine walks this list and re-resolves each
                # item's (expiring) URL as it reaches it.
                self.cast_sync.set_queue_resolver(self.sync_queue_items)
                # A zone as an ordinary player, so the Media page, the API and
                # the queue/lyrics/favourite actions can all target one.
                from modules.media.players.zone import ZonePlayerProvider
                self.controller.add_player_provider(
                    ZonePlayerProvider(self.cast_sync, self.start_zone))
            except Exception as e:
                logger.warning(f"Cast sync PoC unavailable: {e}")

    # Public helpers used by routes
    async def resolve_station(self, station_uuid: str,
                              hint: Optional[dict] = None) -> Optional[RadioStation]:
        """Turn a station UUID into a playable station: pinned snapshot, then
        the directory, then `hint` (the caller's own snapshot). The directory
        is last because it is regularly unreachable."""
        keys = ("uuid", "name", "url", "favicon", "homepage",
                "country", "tags", "codec", "bitrate", "hls")

        def _from(d: dict) -> RadioStation:
            return RadioStation(**{k: d.get(k) for k in keys if d.get(k) is not None})

        fav = self.radio_favourites.get(station_uuid)
        if fav and fav.get("url"):
            return _from(fav)
        station = await self.radio.get_station(station_uuid)
        if station:
            return station
        if hint and hint.get("url"):
            logger.info(f"Radio directory unreachable — playing {station_uuid} "
                        f"from caller snapshot")
            return _from({**hint, "uuid": station_uuid})
        return None

    async def play_radio_station(self, player_id: str, station_uuid: str,
                                 hint: Optional[dict] = None) -> MediaItem:
        station = await self.resolve_station(station_uuid, hint)
        if not station:
            raise ValueError(f"Radio station {station_uuid} not found "
                             f"(or radio directory unreachable)")
        item = station.to_media_item()
        # Single-item queue so radio shows consistently in the now-playing/queue
        # UI. Radio is LIVE (duration 0) so it never auto-advances.
        await self.controller.play_items(player_id, [item])
        return item

    # Karaoke mode (cast synced lyrics to the custom receiver)
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
        return await self.play_radio_station(player_id, uuid)

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
            url=self.tts_url(text, lang),
            title=text[:60],
            artist="Announcement",
            media_type="tts",            # no resolver → URL used as-is
            content_type="audio/mpeg",
        )
        await self.controller.play_url(player_id, item)
        return {"success": True}

    def tts_url(self, text: str, lang: str = None) -> str:
        """URL of a spoken clip. Public because a zone speaks it as ordinary
        media through the shared timeline rather than via announce()."""
        from urllib.parse import quote
        # The Translate TTS endpoint caps ~200 chars/request; truncate for safety.
        return (f"{self._tts_base}?ie=UTF-8&client=tw-ob"
                f"&tl={lang or self._tts_lang}&q={quote(text[:200])}")

    async def tidal_items(self, kind: str, tidal_id: str,
                          mode: str = "play") -> List[MediaItem]:
        """Resolve a Tidal track/album/playlist/artist/mix to MediaItems.

        Raises ValueError for a bad kind or an empty result. Shared by casting
        (play_tidal) and browser-local playback, which builds its own queue.
        """
        src = self.controller.get_source("tidal")
        if not src:
            raise ValueError("Tidal unavailable")
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
            raise ValueError("kind must be track|album|playlist|artist|mix")
        if not items:
            raise ValueError("Nothing to play (empty, not found, or login required)")
        return items

    async def sync_queue_items(self, media_type: str, kind: str,
                               container_id: str) -> List[dict]:
        """Expand a container into the plain rows the sync engine walks.

        Only the fields it needs: the id it re-resolves a fresh stream URL
        from, and what to show while that item plays. Same expansion the
        single-player path uses, so a playlist means the same thing in a zone
        as it does on one speaker."""
        if media_type != "tidal":
            return []
        items = await self.tidal_items(kind, container_id)
        # duration_ms rides along so a zone can report a position against it.
        return [{"source_id": i.source_id, "title": i.title,
                 "artist": i.artist, "artwork_url": i.artwork_url,
                 "media_type": i.media_type, "duration_ms": i.duration_ms}
                for i in items if i.source_id]

    async def play_tidal(self, player_id: str, kind: str, tidal_id: str,
                         mode: str = "play") -> dict:
        """Resolve a Tidal track/album/playlist/artist/mix to items and play them.
        ``mode='radio'`` makes track/artist play an infinite auto-extending queue.
        Shared by the API route and the automation engine."""
        radio = mode == "radio"
        if radio and self.zone_id(player_id):
            # A zone plays one server-built timeline, which the controller does
            # not top up — saying so beats quietly playing a finite 50 tracks.
            return {"success": False,
                    "error": "Radio∞ isn't available on a zone — play the "
                             "artist, album or a mix instead"}
        try:
            items = await self.tidal_items(kind, tidal_id, mode)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await self.controller.play_items(player_id, items, auto_extend=radio)
        return {"success": True, "count": len(items), "radio": radio}

    # OpenZone
    # A zone is started from three places — the Media page, an automation rule
    # and a session resume — and each of them has to turn a saved reference
    # (a station id, a Tidal container) into something playable. That belongs
    # in one place, so all three fail and succeed the same way.

    ZONE_PREFIX = _ZONE_PREFIX

    @staticmethod
    def zone_id(player_id: str) -> str:
        """The group id inside a ``zone:<gid>`` player id, or "" if it is an
        ordinary player."""
        return _zone_id(player_id)

    async def start_zone(self, group_id: str, media: Optional[dict] = None,
                         duration_s: Optional[int] = None,
                         crossfade_s: Optional[float] = None,
                         use_saved: bool = False) -> dict:
        """Start an OpenZone group.

        ``use_saved`` takes the zone's stored source and window — what the
        Media page last set — which is how a rule can say "play the kitchen"
        without restating what the kitchen plays. An explicit ``media``
        overrides it; ``media=None`` without ``use_saved`` is the test signal,
        exactly as the API has always meant it.
        """
        sync = self.cast_sync
        if sync is None:
            return {"success": False,
                    "error": "OpenZone is disabled — enable it under Settings → Audio"}
        saved = sync.group_config(group_id)
        if use_saved and media is None:
            media = saved.get("media")
            if media is None:
                # Falling through to the test signal would answer "play the
                # kitchen" with two hours of clicks.
                return {"success": False,
                        "error": "This zone has no saved source — pick one "
                                 "under Media → OpenZone"}
        if duration_s is None:
            duration_s = (saved.get("duration_s") or 0) if use_saved else 0
        if crossfade_s is None and use_saved:
            crossfade_s = saved.get("crossfade_s")

        if media:
            media = dict(media)
            ok, err = await self.resolve_zone_media(media)
            if not ok:
                return {"success": False, "error": err}
        return await sync.start_session(
            None, group_id=group_id,
            duration_s=min(max(int(duration_s or 0), 0), 3600),
            media=media or None, crossfade_s=crossfade_s)

    async def resolve_zone_media(self, media: dict) -> Tuple[bool, str]:
        """Validate a zone media block in place, resolving a station id to a
        stream URL. Returns ``(ok, error)``.

        Stations resolve here rather than being stored as URLs: directory
        stream URLs move, and a favourite saved months ago should still start.
        Tidal deliberately does not resolve here — the engine re-resolves its
        signed URLs per item, which is what lets a long session outlive them.
        """
        rows = media.get("items")
        if rows is not None:
            # An explicit queue is already resolved — each row carries the id
            # or URL the engine re-resolves per item.
            if not [r for r in rows if r.get("source_id") or r.get("url")]:
                return False, "That queue has nothing a zone can play"
            return True, ""
        if media.get("station_uuid") and not media.get("url"):
            station = None
            if self.radio is not None:
                station = await self.resolve_station(media["station_uuid"])
            if station is None:
                return False, "Radio station not found (or directory unreachable)"
            media["url"] = station.url
            media["title"] = media.get("title") or station.name
            # The directory's logo is what a screened speaker shows while the
            # station plays — same picture the single-player path sends.
            media["artwork_url"] = media.get("artwork_url") or station.favicon
        kind = (media.get("kind") or "track").strip().lower()
        if kind not in ("track", "album", "playlist", "artist", "mix"):
            return False, "kind must be track|album|playlist|artist|mix"
        media["kind"] = kind
        if kind != "track" and not media.get("source_id"):
            return False, f"a {kind} needs a source_id to expand"
        url = (media.get("url") or "").strip()
        # A source_id block carries no URL yet on purpose — the engine resolves
        # one at session start and again whenever it expires.
        if not url and not media.get("source_id"):
            return False, "Media given with no url, station_uuid or source_id"
        if url.startswith("-"):
            # The decoder takes its input as a bare argument, so a leading dash
            # would be read as an option instead of a source.
            return False, "URL may not start with '-'"
        media["url"] = url
        return True, ""

    async def zone_members(self, group_id: str) -> List[str]:
        """Member player ids of a zone, for the things that act per speaker
        (volume, fades) rather than on the shared timeline."""
        sync = self.cast_sync
        if sync is None:
            return []
        for g in sync.list_groups().get("groups", []):
            if g["id"] == group_id:
                return [m["player_id"] for m in g.get("members", [])]
        return []

    def start(self):
        if not self.enabled:
            logger.info("Media service disabled")
            return
        if self._task is not None:      # idempotent: a second poll loop would
            return                      # double every device poll and save
        self._task = asyncio.create_task(self._run())
        if self.cast_sync is not None:
            self.cast_sync.start()      # brings up the plain-HTTP sync listener
        self.device_http.start()        # plain-HTTP device-audio listener
        logger.info(f"Media service started (poll={self.poll_interval}s)")

    def stop(self):
        self._save_sessions()       # best-effort final persist before going down
        if self.cast_sync is not None:
            self.cast_sync.stop()
        self.device_http.stop()
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        try:
            await self.controller.start()
        except Exception as e:
            logger.error(f"Media controller start failed: {e}")
        # Adopt anything already casting across a restart/upgrade: restore the
        # saved queues (for next/prev + Tidal lyrics/artist linkage) and eagerly
        # connect to discovered devices so the poll reports live now-playing.
        if self._adopt_sessions:
            try:
                data = self._load_sessions()
                if data:
                    self._restored = data
                    self.controller.restore_sessions(data)
            except Exception as e:
                logger.warning(f"Session restore failed: {e}")
            asyncio.create_task(self._adopt_casts())
        await self._poll_loop()

    async def _adopt_casts(self):
        await asyncio.sleep(4)      # let mDNS discovery find the devices first
        try:
            if self.cast is not None:
                n = await self.cast.connect_all()
                logger.info(f"Adopted {n} cast device(s) for live control")
        except Exception as e:
            logger.debug(f"Cast adoption failed: {e}")
        try:
            await self._resume_playback()
        except Exception as e:
            logger.warning(f"Playback resume after restart failed: {e}")

    async def _resume_playback(self):
        """Put back what we were serving when the process went down.

        Only reached for players that were *playing*, and only those not
        playing now — a speaker fetching its source directly sailed through the
        restart and must not be interrupted. The age limit is the guard against
        a long outage: waking the house at 3am because the box was down since
        midnight is a worse failure than a stream that stayed stopped."""
        if not self._resume_enabled:
            return
        rec = (self._restored or {}).get("playback") or {}
        sync_rec = (self._restored or {}).get("sync") or {}
        if not rec and not sync_rec:
            return
        age = time.time() - float((self._restored or {}).get("saved_at") or 0)
        if age > self._resume_max_age_s:
            logger.info(
                f"Not resuming playback — last session is {age / 60:.0f} min old "
                f"(limit {self._resume_max_age_s / 60:.0f} min)")
            return
        await self.controller.refresh()          # live state before deciding
        if rec:
            n = await self.controller.resume_players(rec.keys())
            if n:
                logger.info(f"Resumed {n} player(s) after restart "
                            f"({age:.0f}s gap)")
        if sync_rec and self.cast_sync is not None:
            await self.cast_sync.resume_session(sync_rec, age)

    async def _poll_loop(self):
        while True:
            try:
                snapshot = await self.controller.refresh()
                await self.controller.tick()        # auto-advance finished tracks
                await self._broadcast(snapshot)
                self._save_sessions()               # debounced (writes only on change)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Media poll failed: {e}")
            await asyncio.sleep(self.poll_interval)

    # Session persistence (data/media_sessions.json)
    def _load_sessions(self) -> dict:
        import json
        try:
            with open(self._sessions_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"Could not read {self._sessions_file}: {e}")
            return {}

    def _save_sessions(self) -> None:
        if not self._adopt_sessions:
            return
        import json, os, tempfile
        try:
            snap = self.controller.sessions_snapshot()
            if self.cast_sync is not None:
                s = self.cast_sync.session_snapshot()
                if s:
                    snap["sync"] = s
            blob = json.dumps(snap, sort_keys=True)
            now = time.time()
            # `saved_at` dates the state and the resume age limit measures from it,
            # so it must keep ticking during steady playback or an hour-long stream
            # looks an hour stale. At most once a minute, and only while playing.
            active = bool(snap.get("playback") or snap.get("sync"))
            stale = (now - self._sessions_written_at) > 60
            if blob == self._sessions_blob and not (active and stale):
                return
            self._sessions_blob = blob
            self._sessions_written_at = now
            d = os.path.dirname(self._sessions_file) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({**snap, "saved_at": now}, f, sort_keys=True)
            os.replace(tmp, self._sessions_file)
        except Exception as e:
            logger.debug(f"Could not write {self._sessions_file}: {e}")

    async def _broadcast(self, snapshot: List[PlayerState]):
        try:
            from routes.websocket_routes import broadcast_event
            await broadcast_event("media_state", {
                "players": [s.to_dict() for s in snapshot],
            })
        except Exception as e:
            logger.debug(f"Media broadcast failed: {e}")
