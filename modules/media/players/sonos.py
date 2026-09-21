"""
Sonos player provider, over the local UPnP API via SoCo.

Everything stays on the LAN: speakers are found by SSDP discovery plus an
optional manual IP list (for networks that drop multicast), and controlled with
SOAP calls. SoCo is synchronous, so every call runs in a worker thread.

Sonos groups are native and speaker-managed. Transport commands (play, pause,
stop, next/prev) are only accepted by a group's coordinator, so calls addressed
to a member are routed to its coordinator; volume and mute stay per speaker.
player_id is the speaker UID ("sonos:RINCON_…"), which survives DHCP changes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import soco  # ImportError here is caught by MediaService (optional dep)
from soco.exceptions import SoCoException

from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider

logger = logging.getLogger("modules.media.sonos")

# AVTransport CurrentTransportState -> our PlaybackState
_STATE_MAP = {
    "PLAYING": PlaybackState.PLAYING,
    "TRANSITIONING": PlaybackState.BUFFERING,
    "PAUSED_PLAYBACK": PlaybackState.PAUSED,
    "STOPPED": PlaybackState.IDLE,
    "NO_MEDIA_PRESENT": PlaybackState.IDLE,
}

# Endless streams: Sonos refuses plain http(s) radio URIs unless they are
# re-prefixed as x-rincon-mp3radio (SoCo's force_radio).
_LIVE_TYPES = {"radio", "live"}


def _pid(uid: str) -> str:
    return f"sonos:{uid}"


def _hms_to_ms(value: str) -> int:
    """"H:MM:SS" -> ms. Radio reports "NOT_IMPLEMENTED" or ""; those are 0."""
    try:
        parts = [int(p) for p in str(value).split(":")]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total * 1000


class SonosPlayerProvider(PlayerProvider):
    provider = "sonos"
    label = "Sonos"
    groups_natively = True

    def __init__(self, device_ips: Optional[List[str]] = None,
                 discovery: bool = True,
                 rediscover_seconds: int = 300):
        self._manual_ips: List[str] = [ip for ip in (device_ips or []) if ip]
        self._discovery = discovery
        self._rediscover_seconds = rediscover_seconds
        self._zones: Dict[str, "soco.SoCo"] = {}      # uid -> SoCo
        # None, not 0.0: monotonic() is time since boot, so on a freshly
        # booted host 0.0 would read as a recent sweep.
        self._last_discovery: Optional[float] = None
        self._discover_lock = asyncio.Lock()
        self._discover_task: Optional[asyncio.Task] = None

    # Discovery
    async def start(self) -> None:
        await self._refresh_zones()

    async def stop(self) -> None:
        if self._discover_task is not None:
            self._discover_task.cancel()
            self._discover_task = None

    def _maybe_rediscover(self) -> None:
        """Start a background sweep when the last one is stale. Never awaited
        by the poll: an unreachable manual IP holds a sweep for its full
        connect timeout, which would stall every player's state refresh."""
        if (self._last_discovery is not None
                and time.monotonic() - self._last_discovery < self._rediscover_seconds):
            return
        if self._discover_task is not None and not self._discover_task.done():
            return
        self._discover_task = asyncio.create_task(self._refresh_zones())

    async def _refresh_zones(self) -> None:
        async with self._discover_lock:
            found = await asyncio.to_thread(self._discover_blocking)
            self._last_discovery = time.monotonic()
            # Keep speakers that dropped out of this sweep: SSDP misses replies
            # routinely, and an unreachable speaker is reported as unavailable
            # by get_state rather than vanishing from the UI.
            new = set(found) - set(self._zones)
            self._zones.update(found)
            if new:
                logger.info("Sonos discovered: " + ", ".join(
                    self._label(found[u]) for u in sorted(new)))

    def _discover_blocking(self) -> Dict[str, "soco.SoCo"]:
        zones: Dict[str, "soco.SoCo"] = {}
        if self._discovery:
            try:
                for z in soco.discover(timeout=3) or ():
                    zones[z.uid] = z
            except Exception as e:
                logger.warning(f"Sonos discovery failed: {e}")
        for ip in self._manual_ips:
            try:
                z = soco.SoCo(ip)
                # Bonded satellites and subs are invisible: they are part of
                # another player, not something to target.
                if z.is_visible:
                    zones[z.uid] = z
            except Exception as e:
                logger.warning(f"Sonos {ip} unreachable: {e}")
        return zones

    @staticmethod
    def _label(zone) -> str:
        try:
            return f"{zone.player_name} ({zone.ip_address})"
        except Exception:
            return str(getattr(zone, "ip_address", "?"))

    def _zone(self, player_id: str):
        zone = self._zones.get(player_id.split(":", 1)[-1])
        if zone is None:
            raise ValueError(f"Unknown Sonos player {player_id}")
        return zone

    @staticmethod
    def _coordinator(zone):
        group = zone.group
        return group.coordinator if group is not None and group.coordinator else zone

    # State
    async def list_players(self) -> List[PlayerState]:
        self._maybe_rediscover()
        uids = list(self._zones)
        states = await asyncio.gather(
            *(self.get_state(_pid(u)) for u in uids), return_exceptions=True)
        return [s for s in states if isinstance(s, PlayerState)]

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        zone = self._zones.get(player_id.split(":", 1)[-1])
        if zone is None:
            return None
        return await asyncio.to_thread(self._state_blocking, player_id, zone)

    def _state_blocking(self, player_id: str, zone) -> PlayerState:
        try:
            name = zone.player_name
        except Exception:
            name = str(getattr(zone, "ip_address", player_id))
        try:
            group = zone.group
            coord = group.coordinator if group is not None and group.coordinator else zone
            members = [m for m in (group.members if group is not None else ()) if m.is_visible]
            is_coord = coord.uid == zone.uid
            is_group = is_coord and len(members) > 1

            # Now-playing lives on the coordinator; a member reports the group's.
            transport = coord.get_current_transport_info()
            track = coord.get_current_track_info()
            volume = zone.volume / 100.0
            muted = bool(zone.mute)
        except Exception as e:   # SoCoException, requests errors, bad XML
            logger.debug(f"Sonos {name} state failed: {e}")
            return PlayerState(player_id=player_id, provider=self.provider, name=name,
                               available=False, state=PlaybackState.UNKNOWN)

        position = _hms_to_ms(track.get("position", ""))
        duration = _hms_to_ms(track.get("duration", ""))
        art = track.get("album_art") or ""
        return PlayerState(
            player_id=player_id,
            provider=self.provider,
            name=name,
            available=True,
            state=_STATE_MAP.get(transport.get("current_transport_state", ""),
                                 PlaybackState.UNKNOWN),
            volume=volume,
            muted=muted,
            is_group=is_group,
            group_members=[_pid(m.uid) for m in members if m.uid != zone.uid] if is_group else [],
            title=track.get("title") or "",
            artist=track.get("artist") or "",
            # Speaker-hosted art is plain http on port 1400; the UI is served
            # over HTTPS, so only pass art that will load without mixed content.
            artwork_url=art if art.startswith("https://") else "",
            # No idle_reason on UPnP; position reaching the end is the signal,
            # with the controller's playing->idle transition check as backstop.
            ended=duration > 0 and position >= duration - 3000,
            position_ms=position,
            duration_ms=duration,
        )

    # Playback
    async def _call(self, player_id: str, fn) -> None:
        zone = self._zone(player_id)
        await asyncio.to_thread(fn, zone)

    async def play_url(self, player_id: str, item: MediaItem) -> None:
        live = item.media_type in _LIVE_TYPES
        title = item.title or ("Radio" if live else "ZMM")

        def _play(zone):
            self._coordinator(zone).play_uri(item.url, title=title, force_radio=live)
        await self._call(player_id, _play)

    async def pause(self, player_id: str) -> None:
        await self._call(player_id, lambda z: self._coordinator(z).pause())

    async def resume(self, player_id: str) -> None:
        await self._call(player_id, lambda z: self._coordinator(z).play())

    async def stop_playback(self, player_id: str) -> None:
        await self._call(player_id, lambda z: self._coordinator(z).stop())

    async def next_track(self, player_id: str) -> None:
        await self._transport_optional(player_id, "next")

    async def prev_track(self, player_id: str) -> None:
        await self._transport_optional(player_id, "previous")

    async def _transport_optional(self, player_id: str, method: str) -> None:
        # A single URI (radio, a stream we pushed) has no Sonos queue to move
        # through; the speaker answers with a UPnP error, which is not a fault.
        def _run(zone):
            try:
                getattr(self._coordinator(zone), method)()
            except SoCoException as e:
                logger.debug(f"Sonos {method} not available: {e}")
        await self._call(player_id, _run)

    async def set_volume(self, player_id: str, level: float) -> None:
        vol = max(0, min(100, round(level * 100)))

        def _set(zone):
            zone.volume = vol
        await self._call(player_id, _set)

    async def set_muted(self, player_id: str, muted: bool) -> None:
        def _set(zone):
            zone.mute = bool(muted)
        await self._call(player_id, _set)

    # Native grouping
    async def join_group(self, master_id: str, member_ids: List[str]) -> None:
        master = self._zone(master_id)
        members = [self._zone(m) for m in member_ids if m != master_id]

        def _join():
            for m in members:
                m.join(master)
        await asyncio.to_thread(_join)

    async def ungroup(self, master_id: str) -> None:
        master = self._zone(master_id)

        def _ungroup():
            coord = self._coordinator(master)
            group = coord.group
            for m in list(group.members if group is not None else ()):
                if m.uid != coord.uid:
                    m.unjoin()
        await asyncio.to_thread(_ungroup)
