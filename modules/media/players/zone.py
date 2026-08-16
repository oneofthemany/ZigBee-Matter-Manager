"""
OpenZone player provider — a zone as an ordinary player.

Adapts CastSyncPoc to the PlayerProvider interface so ``zone:<gid>`` routes
through the controller like any speaker, which is what gives a zone the queue,
lyrics, favourites and artist actions the single-player path already has.
A zone walks its own queue server-side, so this provider is self-advancing:
the controller hands it whole queues and follows its index rather than
driving it item by item. See docs/open-zone.md §4.1b.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider

logger = logging.getLogger("modules.media.zone")

PREFIX = "zone:"

# Fields a queue row carries into the engine.
_ROW_FIELDS = ("source_id", "url", "title", "artist", "artwork_url",
               "media_type", "duration_ms")


def _pid(gid: str) -> str:
    return f"{PREFIX}{gid}"


def _row(item: MediaItem) -> dict:
    d = {k: getattr(item, k, "") for k in _ROW_FIELDS}
    d["duration_ms"] = int(item.duration_ms or 0)
    return d


class ZonePlayerProvider(PlayerProvider):
    provider = "zone"
    #: the engine advances its own queue; the controller must not auto-advance
    self_advancing = True
    #: volume is fanned out here, so the controller must not fan out again
    fans_out_volume = True

    def __init__(self, cast_sync, start_zone):
        self._sync = cast_sync
        # MediaService.start_zone — one validation + saved-window path for
        # every caller (Media page, automation, resume).
        self._start_zone = start_zone
        # What a paused zone was playing, so resume has something to re-issue.
        self._paused: Dict[str, dict] = {}

    # Discovery / state
    def _groups(self) -> List[dict]:
        try:
            return self._sync.list_groups().get("groups", []) or []
        except Exception as e:
            logger.debug(f"Zone list failed: {e}")
            return []

    async def list_players(self) -> List[PlayerState]:
        return [await self._state(g) for g in self._groups()]

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        gid = zone_id(player_id)
        for g in self._groups():
            if g["id"] == gid:
                return await self._state(g)
        return None

    async def _state(self, group: dict) -> PlayerState:
        gid = group["id"]
        members = [m["player_id"] for m in group.get("members", [])]
        active = bool(group.get("active"))
        st = PlayerState(
            player_id=_pid(gid),
            provider=self.provider,
            name=group.get("name", gid),
            available=True,
            state=PlaybackState.PLAYING if active else PlaybackState.IDLE,
            is_group=True,
            group_members=members,
        )
        vol, muted = await self._member_volume(members)
        st.volume, st.muted = vol, muted
        if active:
            np = self._sync.now_playing()
            st.title = np.get("title", "")
            st.artist = np.get("artist", "")
            st.artwork_url = np.get("artwork_url", "")
            st.media_type = np.get("media_type", "")
            st.now_playing_id = np.get("source_id", "")
            st.position_ms = int(np.get("position_ms") or 0)
            st.duration_ms = int(np.get("duration_ms") or 0)
        return st

    async def _member_volume(self, members: List[str]) -> tuple:
        """Mean member volume, and muted only when every member is."""
        cast = getattr(self._sync, "cast", None)
        if cast is None or not members:
            return 0.0, False
        states = await asyncio.gather(
            *(cast.get_state(m) for m in members), return_exceptions=True)
        # UNKNOWN is the discovery view of a device we aren't connected to —
        # its volume reads 0.0, which would drag the mean down to nothing.
        live = [s for s in states if isinstance(s, PlayerState) and s.available
                and s.state is not PlaybackState.UNKNOWN]
        if not live:
            return 0.0, False
        return (round(sum(s.volume for s in live) / len(live), 4),
                all(s.muted for s in live))

    # Playback
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        await self._start(player_id, self._media(item))

    async def play_queue(self, player_id: str, items: List[MediaItem],
                         start: int = 0, loop: bool = False) -> None:
        """Hand the whole queue to the engine, which walks it at the seams."""
        rows = [_row(i) for i in items if i.source_id or i.url]
        if not rows:
            raise RuntimeError("Nothing in that queue a zone can play")
        head = items[start] if 0 <= start < len(items) else items[0]
        await self._start(player_id, {
            **self._media(head),
            "items": rows,
            "start_index": max(0, min(start, len(rows) - 1)),
            "loop": bool(loop),
        })

    @staticmethod
    def _media(item: MediaItem) -> dict:
        return {"url": item.url or "", "source_id": item.source_id or "",
                "media_type": item.media_type or "", "kind": "track",
                "title": item.title or "", "artist": item.artist or "",
                "artwork_url": item.artwork_url or ""}

    async def _start(self, player_id: str, media: dict) -> None:
        # duration_s=0 (until stopped): the zone's saved window is a *test*
        # length, and truncating an album at 5 minutes is not what play means.
        # Crossfade still comes from the zone's own config.
        gid = self._require(player_id)
        self._paused.pop(gid, None)
        res = await self._start_zone(gid, media=media, duration_s=0,
                                     use_saved=True)
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "Zone would not start")

    async def pause(self, player_id: str) -> None:
        """A shared timeline has no pause — stop it, and remember enough that
        resume can re-issue the same queue at the same item."""
        gid = self._require(player_id)
        if self._sync.active_group != gid:
            return
        media = self._sync.session_snapshot().get("media")
        await self._sync.stop_session()
        if media:
            self._paused[gid] = media

    async def resume(self, player_id: str) -> None:
        gid = self._require(player_id)
        if self._sync.active_group == gid:
            return
        media = self._paused.pop(gid, None)
        res = await self._start_zone(gid, media=media, duration_s=0,
                                     use_saved=True)
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "Zone would not restart")

    async def stop_playback(self, player_id: str) -> None:
        gid = self._require(player_id)
        self._paused.pop(gid, None)
        if self._sync.active_group == gid:
            await self._sync.stop_session()

    async def skip_to(self, player_id: str, index: int) -> None:
        gid = self._require(player_id)
        if self._sync.active_group != gid:
            raise RuntimeError("That zone is not playing")
        res = await self._sync.skip_to(int(index))
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "Zone would not skip")

    async def next_track(self, player_id: str) -> None:
        await self._step(player_id, 1)

    async def prev_track(self, player_id: str) -> None:
        await self._step(player_id, -1)

    async def _step(self, player_id: str, delta: int) -> None:
        np = self._sync.now_playing()
        await self.skip_to(player_id, int(np.get("index") or 0) + delta)

    # Volume — a property of each speaker, never of the timeline
    async def set_volume(self, player_id: str, level: float) -> None:
        await self._each(player_id, "set_volume", max(0.0, min(1.0, level)))

    async def set_muted(self, player_id: str, muted: bool) -> None:
        await self._each(player_id, "set_muted", bool(muted))

    async def _each(self, player_id: str, method: str, *args) -> None:
        gid = self._require(player_id)
        cast = getattr(self._sync, "cast", None)
        members = [m["player_id"] for g in self._groups() if g["id"] == gid
                   for m in g.get("members", [])]
        if cast is None or not members:
            return
        results = await asyncio.gather(
            *(getattr(cast, method)(m, *args) for m in members),
            return_exceptions=True)
        for member, r in zip(members, results):
            if isinstance(r, Exception):
                logger.warning(f"Zone {method} failed for {member}: {r}")

    def _require(self, player_id: str) -> str:
        gid = zone_id(player_id)
        if not gid:
            raise ValueError(f"Not a zone player id: {player_id}")
        return gid


def zone_id(player_id: str) -> str:
    """The group id inside ``zone:<gid>``, or "" for any other player id."""
    pid = player_id or ""
    return pid[len(PREFIX):] if pid.startswith(PREFIX) else ""
