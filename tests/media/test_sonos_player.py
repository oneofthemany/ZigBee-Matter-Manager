"""
The Sonos player provider.

SoCo is replaced by an in-memory fake for the whole module: the real library
talks SOAP to speakers on import-free calls, and a dev box has neither soco nor
Sonos hardware. The fake models only what the provider reads — uid, name,
visibility, group coordinator/members, transport and track info, volume, mute —
so what is tested is the provider's own routing: transport calls landing on the
group coordinator, radio being forced into Sonos' radio URI form, group state
reported from the coordinator's side, and speakers failing soft.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types

from harness import Checker


class FakeSoCoException(Exception):
    pass


class FakeGroup:
    def __init__(self, coordinator, members):
        self.coordinator = coordinator
        self.members = set(members)


class FakeZone:
    def __init__(self, uid, name, ip, visible=True):
        self.uid, self.player_name, self.ip_address = uid, name, ip
        self.is_visible = visible
        self.group = None
        self.volume, self.mute = 30, False
        self.transport = {"current_transport_state": "STOPPED"}
        self.track = {"title": "", "artist": "", "position": "0:00:00",
                      "duration": "0:00:00", "album_art": ""}
        self.calls = []
        self.fail = False

    def get_current_transport_info(self):
        if self.fail:
            raise FakeSoCoException("unreachable")
        return self.transport

    def get_current_track_info(self):
        return self.track

    def play_uri(self, uri, title="", force_radio=False):
        self.calls.append(("play_uri", uri, title, force_radio))

    def pause(self):
        self.calls.append(("pause",))

    def play(self):
        self.calls.append(("play",))

    def stop(self):
        self.calls.append(("stop",))

    def next(self):
        raise FakeSoCoException("UPnP Error 711")

    def previous(self):
        self.calls.append(("previous",))

    def join(self, master):
        self.calls.append(("join", master.uid))

    def unjoin(self):
        self.calls.append(("unjoin",))


def _group(coordinator, *members):
    g = FakeGroup(coordinator, (coordinator, *members))
    for z in g.members:
        z.group = g
    return g


def _load_provider(discovered=(), by_ip=None):
    """Import modules.media.players.sonos against a fake soco."""
    fake = types.ModuleType("soco")
    fake.discover = lambda timeout=5: set(discovered) or None
    fake.SoCo = lambda ip: (by_ip or {})[ip]
    exc = types.ModuleType("soco.exceptions")
    exc.SoCoException = FakeSoCoException
    fake.exceptions = exc
    saved = {k: sys.modules.get(k) for k in ("soco", "soco.exceptions")}
    sys.modules["soco"], sys.modules["soco.exceptions"] = fake, exc
    sys.modules.pop("modules.media.players.sonos", None)
    try:
        return importlib.import_module("modules.media.players.sonos")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _run(coro):
    return asyncio.run(coro)


def _helpers(c: Checker) -> None:
    c.section("time parsing")
    S = _load_provider()
    c.check("H:MM:SS to ms", S._hms_to_ms("1:02:03") == 3_723_000, S._hms_to_ms("1:02:03"))
    c.check("radio's NOT_IMPLEMENTED is 0", S._hms_to_ms("NOT_IMPLEMENTED") == 0)
    c.check("empty is 0", S._hms_to_ms("") == 0)


def _discovery(c: Checker) -> None:
    c.section("discovery")
    lounge = FakeZone("RINCON_A", "Lounge", "10.0.0.1")
    sub = FakeZone("RINCON_SUB", "Sub", "10.0.0.9", visible=False)
    kitchen = FakeZone("RINCON_B", "Kitchen", "10.0.0.2")
    S = _load_provider(discovered=[lounge], by_ip={"10.0.0.9": sub, "10.0.0.2": kitchen})

    p = S.SonosPlayerProvider(device_ips=["10.0.0.9", "10.0.0.2"])
    _run(p.start())
    c.check("discovered and manual speakers both registered",
            set(p._zones) == {"RINCON_A", "RINCON_B"}, sorted(p._zones))
    c.check("an invisible (bonded) manual IP is not a target", "RINCON_SUB" not in p._zones)

    p._discovery = False
    p._manual_ips = []
    _run(p._refresh_zones())
    c.check("a speaker missing from a later sweep is kept", set(p._zones) == {"RINCON_A", "RINCON_B"})

    async def _poll_while_sweeping():
        slow = S.SonosPlayerProvider(discovery=False)
        release = asyncio.Event()

        async def _slow_sweep():
            await release.wait()
        slow._refresh_zones = _slow_sweep
        t0 = asyncio.get_running_loop().time()
        await slow.list_players()
        waited = asyncio.get_running_loop().time() - t0
        pending = slow._discover_task is not None and not slow._discover_task.done()
        release.set()
        await slow._discover_task
        return waited, pending
    waited, pending = _run(_poll_while_sweeping())
    c.check("a stale sweep runs in the background, not inside the poll",
            waited < 0.5 and pending, (waited, pending))

    off = S.SonosPlayerProvider(device_ips=[], discovery=False)
    _run(off.start())
    c.check("discovery off and no IPs finds nothing", off._zones == {})


def _state(c: Checker) -> None:
    c.section("state")
    S = _load_provider()
    coord = FakeZone("RINCON_A", "Lounge", "10.0.0.1")
    member = FakeZone("RINCON_B", "Kitchen", "10.0.0.2")
    _group(coord, member)
    coord.transport = {"current_transport_state": "PLAYING"}
    coord.track = {"title": "Song", "artist": "Band", "position": "0:02:58",
                   "duration": "0:03:00", "album_art": "http://10.0.0.1:1400/getaa?x"}
    member.volume, member.mute = 55, True

    p = S.SonosPlayerProvider(discovery=False)
    p._zones = {"RINCON_A": coord, "RINCON_B": member}
    p._last_discovery = float("inf")
    states = {s.player_id: s for s in _run(p.list_players())}
    a, b = states["sonos:RINCON_A"], states["sonos:RINCON_B"]

    c.check("coordinator is the group", a.is_group and a.group_members == ["sonos:RINCON_B"],
            (a.is_group, a.group_members))
    c.check("member is not reported as a group", not b.is_group and b.group_members == [])
    c.check("member reports the group's now-playing", b.title == "Song" and b.state.value == "playing",
            (b.title, b.state))
    c.check("volume and mute are the member's own", b.volume == 0.55 and b.muted, (b.volume, b.muted))
    c.check("transport states map", S._STATE_MAP["PAUSED_PLAYBACK"].value == "paused"
            and S._STATE_MAP["TRANSITIONING"].value == "buffering")
    c.check("position near the end flags ended", a.ended and a.duration_ms == 180_000)
    c.check("speaker-hosted http art is dropped (mixed content)", a.artwork_url == "")

    coord.fail = True
    down = _run(p.get_state("sonos:RINCON_A"))
    c.check("an unreachable speaker is unavailable, not an error",
            down is not None and not down.available, down)
    c.check("an unknown id is None", _run(p.get_state("sonos:RINCON_X")) is None)


def _control(c: Checker) -> None:
    c.section("control")
    from modules.media.models import MediaItem
    S = _load_provider()
    coord = FakeZone("RINCON_A", "Lounge", "10.0.0.1")
    member = FakeZone("RINCON_B", "Kitchen", "10.0.0.2")
    _group(coord, member)
    p = S.SonosPlayerProvider(discovery=False)
    p._zones = {"RINCON_A": coord, "RINCON_B": member}

    _run(p.play_url("sonos:RINCON_B", MediaItem(url="https://s/radio", title="R1", media_type="radio")))
    c.check("play on a member goes to the coordinator", member.calls == [] and coord.calls
            and coord.calls[-1][0] == "play_uri", (member.calls, coord.calls))
    c.check("radio is forced into Sonos' radio URI form", coord.calls[-1][3] is True)

    _run(p.play_url("sonos:RINCON_A", MediaItem(url="https://t/track.m4a", title="T", media_type="tidal")))
    c.check("a finite track is not forced to radio", coord.calls[-1] == ("play_uri", "https://t/track.m4a", "T", False),
            coord.calls[-1])

    _run(p.pause("sonos:RINCON_B"))
    c.check("pause on a member goes to the coordinator", coord.calls[-1] == ("pause",))

    try:
        _run(p.next_track("sonos:RINCON_A"))
        c.check("next with no Sonos queue is not an error", True)
    except Exception as e:
        c.check("next with no Sonos queue is not an error", False, e)

    _run(p.set_volume("sonos:RINCON_B", 0.426))
    c.check("volume is set on the addressed speaker", member.volume == 43 and coord.volume == 30,
            (member.volume, coord.volume))

    try:
        _run(p.pause("sonos:RINCON_X"))
        c.check("an unknown player raises", False)
    except ValueError:
        c.check("an unknown player raises", True)


def _grouping(c: Checker) -> None:
    c.section("grouping")
    S = _load_provider()
    a = FakeZone("RINCON_A", "Lounge", "10.0.0.1")
    b = FakeZone("RINCON_B", "Kitchen", "10.0.0.2")
    d = FakeZone("RINCON_D", "Den", "10.0.0.3")
    for z in (a, b, d):
        _group(z)
    p = S.SonosPlayerProvider(discovery=False)
    p._zones = {"RINCON_A": a, "RINCON_B": b, "RINCON_D": d}

    _run(p.join_group("sonos:RINCON_A", ["sonos:RINCON_A", "sonos:RINCON_B", "sonos:RINCON_D"]))
    c.check("members join the master; the master does not join itself",
            a.calls == [] and b.calls == [("join", "RINCON_A")] and d.calls == [("join", "RINCON_A")],
            (a.calls, b.calls, d.calls))

    _group(a, b, d)
    for z in (a, b, d):
        z.calls.clear()
    _run(p.ungroup("sonos:RINCON_B"))
    c.check("ungroup from any member unjoins everyone but the coordinator",
            a.calls == [] and b.calls == [("unjoin",)] and d.calls == [("unjoin",)],
            (a.calls, b.calls, d.calls))


def run() -> Checker:
    c = Checker("sonos_player")
    _helpers(c)
    _discovery(c)
    _state(c)
    _control(c)
    _grouping(c)
    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
