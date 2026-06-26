"""
SourceProvider abstract base class.

A source turns a query/browse request into playable ``MediaItem``s. Phase 1 has
one concrete source (Radio-Browser). Tidal/local etc. plug in here later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from modules.media.models import MediaItem


class SourceProvider(ABC):
    source: str = "base"

    async def start(self) -> None:
        """Resolve endpoints / warm caches. Override if needed."""

    async def stop(self) -> None:
        """Release resources. Override if needed."""

    @abstractmethod
    async def search(self, query: str, limit: int = 25) -> List[MediaItem]:
        """Return playable items matching ``query``."""
