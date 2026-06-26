"""
MediaController — provider-agnostic orchestration.

Holds the registry of players across all providers, routes control calls to the
right provider by ``player_id`` prefix, and exposes a flat snapshot for the UI.
Sources (radio/tidal) are kept here too so routes have one entry point.

Phase 2: owns a per-player queue (`QueueController`) and auto-advances finished
tracks via `tick()` (called from the service poll loop). Still no stream server —
sequential playback; gapless/crossfade is Phase 3.

The queue/play methods here are deliberately clean and side-effect-contained so
the post-Phase-2 automation engine can call them directly.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider
from modules.media.sources.base import SourceProvider
from modules.media.queue import QueueController

logger = logging.getLogger("modules.media.controller")

_ACTIVE = (PlaybackState.PLAYING, PlaybackState.BUFFERING)


class MediaController:
    def __init__(self):
        self._players: Dict[str, PlayerProvider] = {}   # provider key -> provider
        self._sources: Dict[str, SourceProvider] = {}   # source key   -> source
        self._cache: Dict[str, PlayerState] = {}        # player_id -> last state
        self._queue = QueueController()
        self._prev_states: Dict[str, PlayerState] = {}  # for end-transition detection
        self._advancing: set[str] = set()               # players mid auto-advance
        # media_type -> async (source_id) -> fresh url. Lets sources (e.g. Tidal)
        # re-resolve time-limited stream URLs just before playback.
        self._resolvers: Dict[str, "callable"] = {}
        # async (seed_source_id) -> [MediaItem]. Appends similar tracks to keep
        # a "radio" queue infinite. Set by the source via register_extender.
        self._extender = None
        self._extending: set[str] = set()

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------
    def add_player_provider(self, provider: PlayerProvider) -> None:
        self._players[provider.provider] = provider

    def add_source(self, source: SourceProvider) -> None:
        self._sources[source.source] = source

    def get_source(self, key: str) -> Optional[SourceProvider]:
        return self._sources.get(key)

    def register_resolver(self, media_type: str, resolver) -> None:
        """Register an async `(source_id) -> fresh_url` for a media_type whose
        stream URLs expire (e.g. Tidal). Called just before each play."""
        self._resolvers[media_type] = resolver

    def register_extender(self, extender) -> None:
        """Register an async `(seed_source_id) -> [MediaItem]` used to keep a
        `auto_extend` ('radio') queue infinite by appending similar tracks."""
        self._extender = extender

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
        for s in snapshot:
            self._attach_queue(s)
        self._cache = {s.player_id: s for s in snapshot}
        return snapshot

    def _attach_queue(self, s: PlayerState) -> None:
        """Attach queue summary + enrich now-playing from the known queue item.
        Devices often expose no metadata for a raw URL, so the queue item (which
        we *do* know) is the better source of title/artist/artwork."""
        q = self._queue.get(s.player_id)
        if not q or not q.items:
            return
        s.queue = q.to_dict()
        cur = q.current()
        if cur and s.state in _ACTIVE + (PlaybackState.PAUSED,):
            it = cur.item
            if it.title:
                s.title = it.title
            if it.artist:
                s.artist = it.artist
            if it.artwork_url:
                s.artwork_url = it.artwork_url
            if it.media_type:
                s.media_type = it.media_type

    def snapshot(self) -> List[PlayerState]:
        return list(self._cache.values())

    # ------------------------------------------------------------------
    # Control (routed by player_id prefix)
    # ------------------------------------------------------------------
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        item = await self._resolve(item)
        await self._dispatch(player_id, "play_url", item)

    async def _resolve(self, item: MediaItem) -> MediaItem:
        """Refresh a time-limited stream URL via the source's resolver, if any."""
        resolver = self._resolvers.get(item.media_type)
        if resolver and item.source_id:
            try:
                fresh = await resolver(item.source_id)
                if fresh:
                    item.url = fresh
            except Exception as e:
                logger.warning(f"URL resolve failed for {item.media_type}:{item.source_id}: {e}")
        return item

    async def control(self, player_id: str, action: str) -> None:
        # next/prev navigate the queue when there is one (radio/Tidal URLs have
        # no device-native next/prev); otherwise fall back to the provider.
        if action == "next":
            return await self.queue_next(player_id)
        if action == "prev":
            return await self.queue_prev(player_id)
        action_map = {"pause": "pause", "resume": "resume", "stop": "stop_playback"}
        method = action_map.get(action)
        if not method:
            raise ValueError(f"Unknown action: {action}")
        if action == "stop":
            self._advancing.discard(player_id)
        await self._dispatch(player_id, method)

    # ------------------------------------------------------------------
    # Queue (automation-ready)
    # ------------------------------------------------------------------
    async def play_items(self, player_id: str, items: List[MediaItem],
                         start: int = 0, auto_extend: bool = False) -> None:
        """Replace the player's queue with `items` and start playing `start`.
        `auto_extend` marks the queue as infinite radio (appends similar tracks)."""
        if not items:
            return
        q = self._queue.get(player_id, create=True)
        cur = q.load(items, start)
        q.auto_extend = auto_extend
        self._advancing.discard(player_id)
        if cur:
            await self.play_url(player_id, cur.item)

    async def queue_add(self, player_id: str, items: List[MediaItem]) -> None:
        q = self._queue.get(player_id, create=True)
        was_empty = not q.items
        q.add(items)
        if was_empty and q.current():
            await self.play_url(player_id, q.current().item)

    async def queue_next(self, player_id: str) -> None:
        q = self._queue.get(player_id)
        if q and q.items:
            nxt = q.advance()
            self._advancing.discard(player_id)
            if nxt:
                await self.play_url(player_id, nxt.item)
                return
        await self._dispatch(player_id, "next_track")

    async def queue_prev(self, player_id: str) -> None:
        q = self._queue.get(player_id)
        if q and q.items:
            prev = q.previous()
            self._advancing.discard(player_id)
            if prev:
                await self.play_url(player_id, prev.item)
                return
        await self._dispatch(player_id, "prev_track")

    def set_repeat(self, player_id: str, mode: str) -> None:
        self._queue.get(player_id, create=True).set_repeat(mode)

    def set_shuffle(self, player_id: str, on: bool) -> None:
        self._queue.get(player_id, create=True).set_shuffle(on)

    def get_queue(self, player_id: str) -> Optional[dict]:
        return self._queue.summary(player_id)

    def clear_queue(self, player_id: str) -> None:
        self._queue.clear(player_id)
        self._advancing.discard(player_id)

    # ------------------------------------------------------------------
    # Auto-advance (called from the service poll loop after refresh)
    # ------------------------------------------------------------------
    async def tick(self) -> None:
        for player_id, state in list(self._cache.items()):
            q = self._queue.get(player_id)
            if not q or not q.items or not state.available:
                continue
            # A new track is running → clear the advance latch.
            if state.state in _ACTIVE:
                self._advancing.discard(player_id)
            # Infinite radio: top up the queue before it runs dry.
            if q.auto_extend and q.remaining() <= 3 and player_id not in self._extending:
                await self._extend_queue(player_id, q)
            ended = state.ended or self._transition_ended(player_id, state)
            if ended and player_id not in self._advancing:
                nxt = q.advance()
                if nxt:
                    self._advancing.add(player_id)   # latch until next track plays
                    try:
                        await self.play_url(player_id, nxt.item)
                        logger.info(f"Queue auto-advanced {player_id} → {nxt.item.title}")
                    except Exception as e:
                        logger.warning(f"Auto-advance failed for {player_id}: {e}")
                        self._advancing.discard(player_id)
        self._prev_states = dict(self._cache)

    async def _extend_queue(self, player_id: str, q) -> None:
        """Append more similar tracks to an infinite-radio queue (deduped)."""
        if not self._extender:
            return
        cur = q.current()
        seed = cur.item.source_id if cur else None
        if not seed:
            return
        self._extending.add(player_id)
        try:
            more = await self._extender(seed)
            have = q.source_ids()
            fresh = [it for it in (more or []) if it.source_id and it.source_id not in have]
            if fresh:
                q.add(fresh)
                logger.info(f"Radio extended {player_id} +{len(fresh)} tracks")
        except Exception as e:
            logger.warning(f"Radio extend failed for {player_id}: {e}")
        finally:
            self._extending.discard(player_id)

    def _transition_ended(self, player_id: str, state: PlayerState) -> bool:
        """Fallback end-detection: was playing a finite track, now idle near its end."""
        prev = self._prev_states.get(player_id)
        if not prev:
            return False
        if prev.state in _ACTIVE and state.state == PlaybackState.IDLE and prev.duration_ms > 0:
            return prev.position_ms >= prev.duration_ms - 5000
        return False

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
