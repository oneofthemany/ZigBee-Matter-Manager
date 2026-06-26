"""
Media subsystem data models.

Plain dataclasses with ``to_dict()`` so routes can serialise them straight to
JSON for the UI / WebSocket push. Kept deliberately small and provider-agnostic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, List, Dict, Any


class PlaybackState(str, Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    BUFFERING = "buffering"
    UNKNOWN = "unknown"


@dataclass
class MediaItem:
    """Something playable — a radio station, a Tidal track, or any stream URL."""
    url: str
    title: str = ""
    artist: str = ""
    artwork_url: str = ""
    # "radio" | "url" | "tidal" etc. Helps the UI render hints + queue behaviour.
    media_type: str = "url"
    content_type: str = "audio/mpeg"  # MIME hint for players that want one
    # Provider-specific source id (e.g. Tidal track id) — lets us re-resolve a
    # fresh stream URL when a queued item's signed URL has expired.
    source_id: str = ""
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RadioStation:
    """A Radio-Browser directory entry, normalised."""
    uuid: str
    name: str
    url: str               # resolved stream URL (url_resolved)
    favicon: str = ""
    homepage: str = ""
    country: str = ""
    tags: str = ""
    codec: str = ""
    bitrate: int = 0

    def to_media_item(self) -> MediaItem:
        return MediaItem(
            url=self.url,
            title=self.name,
            artist=self.country or "Radio",
            artwork_url=self.favicon,
            media_type="radio",
            content_type="audio/mpeg" if (self.codec or "").upper() != "AAC" else "audio/aac",
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlayerState:
    """
    Snapshot of a single player. ``provider`` distinguishes ecosystems
    ("cast" | "wiim"); ``player_id`` is globally unique (provider-prefixed).
    """
    player_id: str
    provider: str
    name: str
    available: bool = True
    state: PlaybackState = PlaybackState.UNKNOWN
    volume: float = 0.0           # 0.0–1.0
    muted: bool = False
    # Whether this device is itself a group (Cast group, or a WiiM master).
    is_group: bool = False
    group_members: List[str] = field(default_factory=list)  # player_ids
    # Now-playing
    title: str = ""
    artist: str = ""
    artwork_url: str = ""
    media_type: str = ""
    # Source id of the now-playing item (e.g. Tidal track id) — lets the UI
    # fetch lyrics for what's actually playing. Set by the controller from the queue.
    now_playing_id: str = ""
    position_ms: int = 0
    duration_ms: int = 0
    # Provider hint that the current track finished naturally (vs user-stopped).
    # Cast sets this from idle_reason==FINISHED; WiiM from curpos≈totlen.
    ended: bool = False
    # Queue summary, attached by the controller (providers don't know queues).
    queue: Optional[Dict[str, Any]] = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class QueueItem:
    """One entry in a player's queue (a MediaItem plus a stable entry id)."""
    id: str
    item: MediaItem

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "item": self.item.to_dict()}
