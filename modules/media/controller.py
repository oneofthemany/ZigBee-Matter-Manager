"""
MediaController — provider-agnostic orchestration.

Holds the registry of players across all providers, routes control calls to the
right provider by ``player_id`` prefix, and exposes a flat snapshot for the UI.
Sources (radio etc.) are kept here too so routes have one entry point.

Deliberately thin: no queue, no stream server (Phase 1). It just fans control
out to providers and aggregates their state.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from modules.media.models import MediaItem, PlayerState
from modules.media.players.base import PlayerProvider
from modules.media.sources.base import SourceProvider

logger = logging.getLogger("modules.media.controller")


class MediaController:
    def __init__(self):
        self._players: Dict[str, PlayerProvider] = {}   # provider key -> provider
        self._sources: Dict[str, SourceProvider] = {}   # source key   -> source
        self._cache: Dict[str, PlayerState] = {}        # player_id -> last state

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------
    def add_player_provider(self, provider: PlayerProvider) -> None:
        self._players[provider.provider] = provider

    def add_source(self, source: SourceProvider) -> None:
        self._sources[source.source] = source

    def get_source(self, key: str) -> Optional[SourceProvider]:
        return self._sources.get(key)

    async def start(self) -> None:
        await asyncio.gather(
            *(p.start() for p in self._players.values()),
            *(s.start() for s in self._sources.values()),
            return_exceptions=True,
        )

    async def stop(self) -> None:
        await asyncio.gather(
            *(p.stop() for p in self._players.values()),
            *(s.stop() for s in self._sources.values()),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _provider_for(self, player_id: str) -> Optional[PlayerProvider]:
        prefix = player_id.split(":", 1)[0]
        return self._players.get(prefix)

    async def refresh(self) -> List[PlayerState]:
        """Poll all providers and update the cache. Returns the flat snapshot."""
        results = await asyncio.gather(
            *(p.list_players() for p in self._players.values()),
            return_exceptions=True,
        )
        snapshot: List[PlayerState] = []
        for res in results:
            if isinstance(res, Exception):
                logger.debug(f"Provider list_players failed: {res}")
                continue
            snapshot.extend(res)
        self._cache = {s.player_id: s for s in snapshot}
        return snapshot

    def snapshot(self) -> List[PlayerState]:
        return list(self._cache.values())

    # ------------------------------------------------------------------
    # Control (routed by player_id prefix)
    # ------------------------------------------------------------------
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        await self._dispatch(player_id, "play_url", item)

    async def control(self, player_id: str, action: str) -> None:
        action_map = {
            "pause": "pause", "resume": "resume", "stop": "stop_playback",
            "next": "next_track", "prev": "prev_track",
        }
        method = action_map.get(action)
        if not method:
            raise ValueError(f"Unknown action: {action}")
        await self._dispatch(player_id, method)

    async def set_volume(self, player_id: str, level: float) -> None:
        await self._dispatch(player_id, "set_volume", max(0.0, min(1.0, level)))

    async def set_muted(self, player_id: str, muted: bool) -> None:
        await self._dispatch(player_id, "set_muted", muted)

    async def join_group(self, master_id: str, member_ids: List[str]) -> None:
        provider = self._provider_for(master_id)
        if not provider:
            raise ValueError(f"No provider for {master_id}")
        # Native groups only — all members must share the master's ecosystem.
        prefix = master_id.split(":", 1)[0]
        bad = [m for m in member_ids if not m.startswith(f"{prefix}:")]
        if bad:
            raise ValueError(
                f"Cross-ecosystem grouping unsupported (Phase 1). "
                f"Members not on '{prefix}': {bad}"
            )
        await provider.join_group(master_id, member_ids)

    async def ungroup(self, master_id: str) -> None:
        provider = self._provider_for(master_id)
        if not provider:
            raise ValueError(f"No provider for {master_id}")
        await provider.ungroup(master_id)

    async def _dispatch(self, player_id: str, method: str, *args):
        provider = self._provider_for(player_id)
        if not provider:
            raise ValueError(f"No provider for player {player_id}")
        await getattr(provider, method)(player_id, *args)
