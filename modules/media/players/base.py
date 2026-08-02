"""
PlayerProvider abstract base — one provider per speaker ecosystem.

Providers are thin and mostly stateless: discovery returns the current set and
the controller holds the registry. All methods are async, and blocking SDKs wrap
their calls in an executor inside the provider, so callers never see threads.
player_id is provider-prefixed ("cast:uuid", "wiim:192.168.1.50") so control
calls route unambiguously.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from modules.media.models import PlayerState, MediaItem


class PlayerProvider(ABC):
    #: short, stable provider key — must match the player_id prefix
    provider: str = "base"

    async def start(self) -> None:
        """Begin discovery / open connections. Override if needed."""

    async def stop(self) -> None:
        """Tear down discovery / close connections. Override if needed."""

    # Discovery / state
    @abstractmethod
    async def list_players(self) -> List[PlayerState]:
        """Return the current set of known players for this provider."""

    @abstractmethod
    async def get_state(self, player_id: str) -> Optional[PlayerState]:
        """Refresh and return a single player's state, or None if unknown."""

    # Playback
    @abstractmethod
    async def play_url(self, player_id: str, item: MediaItem) -> None:
        """Play a direct stream URL (radio / arbitrary URL) on the player."""

    @abstractmethod
    async def pause(self, player_id: str) -> None: ...

    @abstractmethod
    async def resume(self, player_id: str) -> None: ...

    @abstractmethod
    async def stop_playback(self, player_id: str) -> None: ...

    async def next_track(self, player_id: str) -> None:
        """Optional — radio has no next; override where meaningful."""

    async def prev_track(self, player_id: str) -> None:
        """Optional — radio has no prev; override where meaningful."""

    @abstractmethod
    async def set_volume(self, player_id: str, level: float) -> None:
        """Set volume 0.0–1.0."""

    async def set_muted(self, player_id: str, muted: bool) -> None:
        """Optional mute toggle."""

    # Equaliser (optional, per-ecosystem)
    async def eq_info(self, player_id: str) -> Optional[dict]:
        """
        Describe this player's EQ capability, or None when it has none
        (Cast has no DSP API; the browser player does its EQ client-side).
        Shape: {"mode": "presets", "presets": [...], "enabled": bool,
        "preset": str} — "preset" may be "" when the device can't report it.
        """
        return None

    async def set_eq(self, player_id: str, enabled: Optional[bool] = None,
                     preset: Optional[str] = None) -> None:
        """Apply an EQ change. Default raises like the grouping stubs do."""
        raise NotImplementedError(f"{self.provider} does not support set_eq")

    # Native grouping (per-ecosystem; cross-ecosystem is out of scope)
    async def join_group(self, master_id: str, member_ids: List[str]) -> None:
        """
        Form a native group with ``master_id`` as leader. Default raises so
        providers without grouping (or where groups are managed externally,
        like Cast groups in Google Home) opt out explicitly.
        """
        raise NotImplementedError(f"{self.provider} does not support join_group")

    async def ungroup(self, master_id: str) -> None:
        raise NotImplementedError(f"{self.provider} does not support ungroup")
