"""
Google Cast player provider (pychromecast).

pychromecast is a reverse-engineered CastV2 client

Discovery uses a persistent ``CastBrowser`` that tracks ``CastInfo`` cheaply.
Devices are connected *lazily* (on first control) and the connection is kept for
live now-playing polling. Cast **groups** created in the Google Home app appear
here as ordinary cast targets (``cast_type == 'group'``) and Google handles their
sync — so "broadcast to a speaker group" is just casting to the group device.

"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import pychromecast  # ImportError here is caught by MediaService (optional dep)
from pychromecast.discovery import CastBrowser, SimpleCastListener
import zeroconf as zeroconf_mod

from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider

logger = logging.getLogger("modules.media.cast")

# pychromecast media player_state -> our PlaybackState
_STATE_MAP = {
    "PLAYING": PlaybackState.PLAYING,
    "BUFFERING": PlaybackState.BUFFERING,
    "PAUSED": PlaybackState.PAUSED,
    "IDLE": PlaybackState.IDLE,
    "UNKNOWN": PlaybackState.UNKNOWN,
}


def _pid(uuid) -> str:
    return f"cast:{uuid}"


def _is_group(info) -> bool:
    """True for a Google-Home speaker group. Normalised against str/enum/case."""
    return str(getattr(info, "cast_type", "")).lower() == "group"


class CastPlayerProvider(PlayerProvider):
    provider = "cast"

    def __init__(self, app_id: str = "CC1AD845"):
        self.app_id = app_id
        self._zc: Optional[zeroconf_mod.Zeroconf] = None
        self._browser: Optional[CastBrowser] = None
        self._infos: Dict[str, object] = {}                 # uuid_str -> CastInfo
        self._casts: Dict[str, pychromecast.Chromecast] = {}  # uuid_str -> connected
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await asyncio.to_thread(self._start_browser)

    def _start_browser(self):
        self._zc = zeroconf_mod.Zeroconf()

        def _added(uuid, _service):
            info = self._browser.devices.get(uuid) if self._browser else None
            if info is not None:
                self._infos[str(uuid)] = info
                # Log cast_type so group-detection issues are diagnosable:
                # Google-Home speaker groups should report cast_type 'group'.
                logger.info(
                    f"Cast discovered: {getattr(info, 'friendly_name', uuid)} "
                    f"(cast_type={getattr(info, 'cast_type', '?')}, "
                    f"is_group={_is_group(info)})"
                )

        def _removed(uuid, _service, _cast_info):
            self._infos.pop(str(uuid), None)

        def _updated(uuid, _service):
            info = self._browser.devices.get(uuid) if self._browser else None
            if info is not None:
                self._infos[str(uuid)] = info

        listener = SimpleCastListener(
            add_callback=_added, remove_callback=_removed, update_callback=_updated
        )
        self._browser = CastBrowser(listener, self._zc)
        self._browser.start_discovery()
        logger.info("Cast discovery started")

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_browser)

    def _stop_browser(self):
        for cast in list(self._casts.values()):
            try:
                cast.disconnect(blocking=False)
            except Exception:
                pass
        self._casts.clear()
        if self._browser:
            try:
                self._browser.stop_discovery()
            except Exception:
                pass
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------
    async def _get_cast(self, uuid_str: str) -> Optional[pychromecast.Chromecast]:
        if uuid_str in self._casts:
            return self._casts[uuid_str]
        async with self._connect_lock:
            if uuid_str in self._casts:           # double-check after awaiting lock
                return self._casts[uuid_str]
            info = self._infos.get(uuid_str)
            if not info:
                return None
            cast = await asyncio.to_thread(self._connect, info)
            if cast:
                self._casts[uuid_str] = cast
            return cast

    def _connect(self, info) -> Optional[pychromecast.Chromecast]:
        try:
            cast = pychromecast.get_chromecast_from_cast_info(info, self._zc)
            cast.wait(timeout=10)
            return cast
        except Exception as e:
            logger.warning(f"Cast connect failed for {getattr(info, 'friendly_name', '?')}: {e}")
            return None

    # ------------------------------------------------------------------
    # Discovery / state
    # ------------------------------------------------------------------
    async def list_players(self) -> List[PlayerState]:
        out: List[PlayerState] = []
        for uuid_str, info in list(self._infos.items()):
            # Live state only for already-connected devices (cheap discovery).
            if uuid_str in self._casts:
                st = await self.get_state(_pid(uuid_str))
                if st:
                    out.append(st)
                    continue
            out.append(self._state_from_info(uuid_str, info))
        return out

    def _state_from_info(self, uuid_str: str, info) -> PlayerState:
        return PlayerState(
            player_id=_pid(uuid_str),
            provider=self.provider,
            name=getattr(info, "friendly_name", uuid_str),
            available=True,
            state=PlaybackState.UNKNOWN,
            is_group=_is_group(info),
        )

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        uuid_str = player_id.split(":", 1)[1]
        info = self._infos.get(uuid_str)
        if not info:
            return None
        cast = self._casts.get(uuid_str)
        if not cast:
            # Not connected yet — return the cheap discovery view.
            return self._state_from_info(uuid_str, info)
        return await asyncio.to_thread(self._read_state, uuid_str, info, cast)

    def _read_state(self, uuid_str, info, cast) -> PlayerState:
        mc_status = cast.media_controller.status
        cast_status = cast.status
        images = getattr(mc_status, "images", None) or []
        artwork = images[0].url if images and getattr(images[0], "url", None) else ""
        return PlayerState(
            player_id=_pid(uuid_str),
            provider=self.provider,
            name=getattr(info, "friendly_name", uuid_str),
            available=True,
            state=_STATE_MAP.get(getattr(mc_status, "player_state", "UNKNOWN"), PlaybackState.UNKNOWN),
            volume=float(getattr(cast_status, "volume_level", 0.0) or 0.0),
            muted=bool(getattr(cast_status, "volume_muted", False)),
            is_group=_is_group(info),
            title=getattr(mc_status, "title", "") or "",
            artist=getattr(mc_status, "artist", "") or "",
            artwork_url=artwork,
            media_type="radio",
            position_ms=int((getattr(mc_status, "current_time", 0) or 0) * 1000),
            duration_ms=int((getattr(mc_status, "duration", 0) or 0) * 1000),
        )

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        cast = await self._get_cast(player_id.split(":", 1)[1])
        if not cast:
            raise RuntimeError(f"Cast device {player_id} unreachable")
        await asyncio.to_thread(self._play, cast, item)

    def _play(self, cast, item: MediaItem):
        # Cold-start hardening: on a fresh connection the first play_media can
        # race the default-receiver launch and get dropped (the "buffers, then
        # only plays on the 2nd try" symptom). We ensure the socket client is
        # connected, then issue play_media and confirm it actually goes active —
        # retrying once if it doesn't, so the user never has to click twice.
        stream_type = "LIVE" if item.media_type == "radio" else "BUFFERED"
        last_err = None
        for attempt in (1, 2):
            try:
                cast.wait(timeout=10)            # ensure the socket client is up
                mc = cast.media_controller
                mc.play_media(
                    item.url,
                    content_type=item.content_type or "audio/mpeg",
                    title=item.title or None,
                    stream_type=stream_type,
                )
                mc.block_until_active(timeout=15)
                # Confirm playback actually started before declaring success.
                deadline = time.time() + 6
                while time.time() < deadline:
                    if mc.status.player_state in ("PLAYING", "BUFFERING"):
                        return
                    time.sleep(0.5)
                last_err = RuntimeError(
                    f"media did not start (state={mc.status.player_state})"
                )
            except Exception as e:
                last_err = e
            logger.info(
                f"Cast '{getattr(cast, 'name', '?')}' play attempt {attempt} "
                f"did not start; {'retrying' if attempt == 1 else 'giving up'}"
            )
        if last_err:
            raise last_err

    async def pause(self, player_id: str) -> None:
        await self._mc_call(player_id, "pause")

    async def resume(self, player_id: str) -> None:
        await self._mc_call(player_id, "play")

    async def stop_playback(self, player_id: str) -> None:
        await self._mc_call(player_id, "stop")

    async def next_track(self, player_id: str) -> None:
        await self._mc_call(player_id, "queue_next")

    async def prev_track(self, player_id: str) -> None:
        await self._mc_call(player_id, "queue_prev")

    async def _mc_call(self, player_id: str, method: str):
        cast = await self._get_cast(player_id.split(":", 1)[1])
        if not cast:
            raise RuntimeError(f"Cast device {player_id} unreachable")
        await asyncio.to_thread(getattr(cast.media_controller, method))

    async def set_volume(self, player_id: str, level: float) -> None:
        cast = await self._get_cast(player_id.split(":", 1)[1])
        if not cast:
            raise RuntimeError(f"Cast device {player_id} unreachable")
        await asyncio.to_thread(cast.set_volume, max(0.0, min(1.0, level)))

    async def set_muted(self, player_id: str, muted: bool) -> None:
        cast = await self._get_cast(player_id.split(":", 1)[1])
        if not cast:
            raise RuntimeError(f"Cast device {player_id} unreachable")
        await asyncio.to_thread(cast.set_volume_muted, muted)

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------
    # Cast groups are created/managed in the Google Home app and appear here as
    # ordinary 'group' cast targets. We deliberately don't expose join/ungroup —
    # the base class raises NotImplementedError, which the UI surfaces as
    # "manage Cast groups in Google Home".
