"""
Per-player queue engine.

Phase 2 is sequential (no stream server): the controller plays an item, and when
a provider reports the track *ended*, the controller asks the queue for the next
item and plays it. Gapless/crossfade is Phase 3 (needs the ffmpeg flow server).

`PlayerQueue` is pure state + navigation logic (no I/O). `QueueController` holds
one queue per player and exposes a UI/automation-friendly summary.
"""
from __future__ import annotations

import logging
import random
import uuid
from typing import Dict, List, Optional

from modules.media.models import MediaItem, QueueItem

logger = logging.getLogger("modules.media.queue")

REPEAT_MODES = ("off", "one", "all")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class PlayerQueue:
    """An ordered list of items with a cursor, repeat and shuffle."""

    def __init__(self, player_id: str):
        self.player_id = player_id
        self.items: List[QueueItem] = []
        self.index: int = -1                 # current item index; -1 = empty
        self.repeat: str = "off"             # off | one | all
        self.shuffle: bool = False
        self.auto_extend: bool = False       # infinite radio: append more near the end
        self._history: List[int] = []        # for `previous()`
        self._cycle_played: set[int] = set()  # shuffle: indices played this cycle

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def load(self, items: List[MediaItem], start: int = 0) -> Optional[QueueItem]:
        self.items = [QueueItem(id=_new_id(), item=i) for i in items]
        self.index = start if (0 <= start < len(self.items)) else (0 if self.items else -1)
        self._history.clear()
        self._cycle_played = {self.index} if self.index >= 0 else set()
        return self.current()

    def add(self, items: List[MediaItem]) -> None:
        self.items.extend(QueueItem(id=_new_id(), item=i) for i in items)
        if self.index == -1 and self.items:
            self.index = 0
            self._cycle_played = {0}

    def clear(self) -> None:
        self.items = []
        self.index = -1
        self.auto_extend = False
        self._history.clear()
        self._cycle_played = set()

    def remaining(self) -> int:
        """Items left after the current one."""
        return max(0, len(self.items) - self.index - 1)

    def source_ids(self) -> set:
        return {qi.item.source_id for qi in self.items if qi.item.source_id}

    def set_repeat(self, mode: str) -> None:
        if mode in REPEAT_MODES:
            self.repeat = mode

    def set_shuffle(self, on: bool) -> None:
        self.shuffle = bool(on)
        # Reset the shuffle cycle so the current item isn't immediately repeated.
        self._cycle_played = {self.index} if self.index >= 0 else set()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def current(self) -> Optional[QueueItem]:
        if 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    def advance(self) -> Optional[QueueItem]:
        """Move to and return the next item to play, or None if exhausted."""
        n = len(self.items)
        if n == 0:
            return None
        if self.repeat == "one":
            return self.current()

        if self.index >= 0:
            self._history.append(self.index)
            self._cycle_played.add(self.index)

        nxt = self._next_index()
        if nxt is None:
            return None
        self.index = nxt
        self._cycle_played.add(nxt)
        return self.current()

    def previous(self) -> Optional[QueueItem]:
        n = len(self.items)
        if n == 0:
            return None
        if self._history:
            self.index = self._history.pop()
        else:
            prev = self.index - 1
            self.index = prev if prev >= 0 else (n - 1 if self.repeat == "all" else 0)
        return self.current()

    def _next_index(self) -> Optional[int]:
        n = len(self.items)
        if self.shuffle:
            remaining = [i for i in range(n) if i not in self._cycle_played]
            if not remaining:
                if self.repeat == "all":
                    self._cycle_played = {self.index}  # start a fresh cycle
                    remaining = [i for i in range(n) if i != self.index] or [self.index]
                else:
                    return None
            return random.choice(remaining)
        # Linear
        nxt = self.index + 1
        if nxt >= n:
            return 0 if self.repeat == "all" else None
        return nxt

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        cur = self.current()
        return {
            "items": [qi.to_dict() for qi in self.items],
            "index": self.index,
            "repeat": self.repeat,
            "shuffle": self.shuffle,
            "auto_extend": self.auto_extend,
            "current_id": cur.id if cur else None,
            "length": len(self.items),
        }


class QueueController:
    """Owns one PlayerQueue per player_id."""

    def __init__(self):
        self._queues: Dict[str, PlayerQueue] = {}

    def get(self, player_id: str, create: bool = False) -> Optional[PlayerQueue]:
        q = self._queues.get(player_id)
        if q is None and create:
            q = PlayerQueue(player_id)
            self._queues[player_id] = q
        return q

    def summary(self, player_id: str) -> Optional[dict]:
        q = self._queues.get(player_id)
        if not q or not q.items:
            return None
        return q.to_dict()

    def clear(self, player_id: str) -> None:
        q = self._queues.get(player_id)
        if q:
            q.clear()
