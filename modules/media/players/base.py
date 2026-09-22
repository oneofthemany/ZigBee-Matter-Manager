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
    #: how this ecosystem is named to a listener
    label: str = ""
    #: the provider walks its own queue (OpenZone's shared timeline). The
    #: controller then hands it whole queues, follows its index instead of
    #: auto-advancing, and navigates with skip_to. See docs/open-zone.md §4.1b.
    self_advancing: bool = False
    #: volume is fanned out to members by the provider, so the controller
    #: must not fan out over group_members as well.
    fans_out_volume: bool = False
    #: OpenZone can drive this provider's devices: it can point one at a URL it
    #: serves *and* read a playback position back off it. Both halves are
    #: required — a device that plays but cannot be measured is an open loop,
    #: and a zone is a closed one (docs/open-zone.md §6.1).
    zone_transport: bool = False
    #: Implements join_group/ungroup: this ecosystem's own firmware syncs the
    #: members, so a group here needs no timing help from us.
    groups_natively: bool = False
    #: the whole queue is labelled once rather than per item — a zone streams
    #: it as one endless session, so its endpoint displays carry one title for
    #: the lot (docs/open-zone.md §10.7). Callers that can name the *set* a queue
    #: came from check this before paying to look it up.
    labels_queue_once: bool = False

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

    def device_key(self, player_id: str) -> str:
        """A stable identity for the physical box, or "".

        One speaker can be reachable through two ecosystems at once — a WiiM
        Ultra answers Cast discovery *and* LinkPlay — and then arrives as two
        players with two ids, two model keys and two sets of learned timing
        that know nothing about each other. This is what says they are one
        device. The address is the identity both sides can agree on, since
        neither vendor's id means anything to the other.
        """
        return ""

    def model_key(self, player_id: str) -> str:
        """Stable identity for "devices that behave like this one", or "".

        Output-pipeline latency is a property of the hardware, not the unit, so
        what is set for one device of a model applies to the rest and survives
        its address changing. The key must not flicker: a value keyed on an
        identity that changes with the discovery that minted it silently
        becomes another device's. Providers that cannot name their hardware
        stably return "" and opt out.
        """
        return ""

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

    # Self-advancing providers only (self_advancing = True)
    async def play_queue(self, player_id: str, items: List["MediaItem"],
                         start: int = 0, loop: bool = False,
                         collection: Optional[dict] = None) -> None:
        """Play an ordered queue from ``start``; ``loop`` is repeat-all.
        ``collection`` names the set the items came from ({"title", "artist",
        "artwork_url"}), for providers that label the queue once."""
        raise NotImplementedError(f"{self.provider} does not support play_queue")

    async def skip_to(self, player_id: str, index: int) -> None:
        """Move the provider's own queue cursor to ``index``."""
        raise NotImplementedError(f"{self.provider} does not support skip_to")

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

    # Device panel (optional, per-ecosystem)
    #: advertised to the UI, which offers the panel button only where true
    has_device_panel = False

    async def device_panel(self, player_id: str) -> Optional[dict]:
        """The device's own settings and state beyond playback — inputs,
        presets, outputs, sleep timer, identity — or None when the ecosystem
        exposes none. Sections are independent and may be absent."""
        return None

    async def device_action(self, player_id: str, action: str, value=None) -> None:
        raise NotImplementedError(f"{self.provider} has no device controls")

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
