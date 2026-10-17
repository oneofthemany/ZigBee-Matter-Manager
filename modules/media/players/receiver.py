"""
OpenZone receiver provider — clients that join zones on their own.

A browser tab, a tablet or a small box that opens the receiver page and
registers itself over the zone engine's socket, rather than being launched by
Cast. The engine owns the clients and their sockets; this is the controller's
view of them. They play only as zone members: the timeline, the correction
and the transport are the zone's (docs/open-zone.md §4.4b).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider

logger = logging.getLogger("modules.media.receiver")

PREFIX = "receiver:"


def _cid(player_id: str) -> str:
    return player_id[len(PREFIX):] if player_id.startswith(PREFIX) else ""


class ReceiverPlayerProvider(PlayerProvider):
    provider = "receiver"
    label = "OpenZone receiver"
    #: served over the zone's socket, measured off the page's DAC reports
    zone_transport = True

    def __init__(self, cast_sync):
        self._sync = cast_sync

    def _state(self, rec: dict) -> PlayerState:
        fed = set()
        try:
            fed = set(self._sync.fed_players())
        except Exception:
            pass
        playing = rec["player_id"] in fed
        return PlayerState(
            player_id=rec["player_id"], provider=self.provider,
            name=rec["name"], available=rec["connected"],
            state=PlaybackState.PLAYING if playing else PlaybackState.IDLE,
            volume=rec["volume"], muted=rec["muted"])

    async def list_players(self) -> List[PlayerState]:
        try:
            # A client waiting to be paired is not a player yet (§4.4c).
            return [self._state(r) for r in self._sync.receivers()
                    if r.get("paired")]
        except Exception as e:
            logger.debug(f"Receiver list failed: {e}")
            return []

    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        for p in await self.list_players():
            if p.player_id == player_id:
                return p
        return None

    # A receiver has no transport of its own: it plays a zone's timeline.
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        raise RuntimeError("An OpenZone receiver plays as part of a zone — "
                           "add it to one under Media → OpenZone")

    async def pause(self, player_id: str) -> None:
        raise RuntimeError("Pause the zone this receiver is in")

    async def resume(self, player_id: str) -> None:
        raise RuntimeError("Play the zone this receiver is in")

    async def stop_playback(self, player_id: str) -> None:
        raise RuntimeError("Stop the zone this receiver is in")

    async def set_volume(self, player_id: str, level: float) -> None:
        await self._sync.set_receiver_volume(_cid(player_id), level=level)

    async def set_muted(self, player_id: str, muted: bool) -> None:
        await self._sync.set_receiver_volume(_cid(player_id), muted=muted)
