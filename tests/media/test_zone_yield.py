"""
OpenZone: a device someone else is using is yielded, not re-cast over
(open-zone.md §7.1).

The case this exists for: a WiiM switched to HDMI-ARC looks to Cast like a
receiver that stopped playing, and the interruption rung's re-LOAD switches it
straight back to the network input — pulling the TV's audio off the speaker
every ten seconds. The real `_reload_stream`, `_launch_stream`,
`_read_media_time`, `_rejoin_stream`, sweeps and `status` are exercised against
a fake Cast provider and a stub input resolver; `LinkPlayDirectory` is
exercised against a stubbed httpapi.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync
from modules.media.cast_sync import OpenZone, _Stream, DEFAULT_APP_ID
from modules.media import linkplay
from modules.media.linkplay import LinkPlayDirectory, mode_owner


class _FakeSource:
    delay_s = 4.0

    def earliest_sample(self) -> int:
        return -10 ** 9

    def latest_sample(self) -> int:
        return 10 ** 9

    # What status() reads beyond the devices, which is all this test wants.
    def item_position_s(self):
        return None

    def stats(self) -> dict:
        return {}


def _fake_cast(app_id=DEFAULT_APP_ID, content_id="http://h/sync/stream/a.wav",
               player_state="PLAYING", display_name=""):
    ms = SimpleNamespace(content_id=content_id, player_state=player_state,
                         title="", last_updated=None,
                         adjusted_current_time=12.0)
    mc = SimpleNamespace(status=ms, update_status=lambda: None)
    return SimpleNamespace(
        status=SimpleNamespace(app_id=app_id, display_name=display_name),
        media_controller=mc,
        cast_info=SimpleNamespace(host="127.0.0.1"),
    )


class _FakeCastProvider:
    def __init__(self, cast):
        self.cast = cast
        self._casts = {"a": cast}

    async def _get_cast(self, _uuid):
        return self.cast

    def device_key(self, _pid):
        return "192.168.1.102"


def _zone(tmp: str, cast) -> OpenZone:
    z = OpenZone(_FakeCastProvider(cast), {
        "trims_file": f"{tmp}/trims.json",
        "model_trims_file": f"{tmp}/model_trims.json",
        "groups_file": f"{tmp}/groups.json",
        "model_file": f"{tmp}/model.json",
    })
    z.running = True
    z._source = _FakeSource()
    z._epoch = time.monotonic() - 100.0
    return z


def _stream(z: OpenZone) -> _Stream:
    st = _Stream("a", "cast:a", "Lounge WiiM")
    st.pos = 0.0
    st.natural_lag = 1.0
    st.connected = True
    z._streams["a"] = st
    z._pending["a"] = {"player_id": "cast:a", "name": st.name}
    return st


def _loads(z: OpenZone) -> list:
    loaded = []
    z._play_stream = lambda cast, url: loaded.append(url)
    return loaded


def _input(z: OpenZone, holder: dict) -> None:
    async def resolve(host):
        return holder.get(host)
    z.set_input_resolver(resolve)


def _modes(c: Checker) -> None:
    c.section("mode classification")
    c.check("HDMI-ARC (49) is foreign", mode_owner(49) == "HDMI-ARC")
    c.check("network idle (0) is free", mode_owner(0) is None)
    c.check("network playlist (10) is free", mode_owner(10) is None)
    c.check("Spotify Connect (31) is foreign",
            mode_owner(31) == "Spotify Connect")
    c.check("an unlisted input in the band is still an input",
            mode_owner(55) == "input 55")
    c.check("no reading is not a verdict", mode_owner(None) is None)


def _directory(c: Checker) -> None:
    c.section("LinkPlay directory")

    async def go():
        d = LinkPlayDirectory()
        answers = {
            ("10.0.0.2", "getStatusEx"): {"project": "WiiM_Ultra",
                                          "DeviceName": "Lounge", "MAC": "m"},
            ("10.0.0.3", "getStatusEx"): None,      # a Nest: no httpapi
            ("10.0.0.2", "getPlayerStatus"): {"mode": "49"},
        }
        calls = []

        async def get(ip, command, timeout):
            calls.append((ip, command))
            return answers.get((ip, command))
        d._get_json = get
        found = []
        d.on_found(lambda ip, ident: found.append((ip, ident["name"])))
        new = await d.discover(["10.0.0.2", "10.0.0.3"])
        c.check("a LinkPlay host is found", new == ["10.0.0.2"], new)
        c.check("listeners hear it once", found == [("10.0.0.2", "Lounge")],
                found)
        await d.discover(["10.0.0.2", "10.0.0.3"])
        c.check("neither host is re-probed inside the retry window",
                calls.count(("10.0.0.3", "getStatusEx")) == 1
                and calls.count(("10.0.0.2", "getStatusEx")) == 1, calls)
        c.check("its input is read as the owner",
                await d.foreign_owner("10.0.0.2") == "HDMI-ARC")
        answers[("10.0.0.2", "getPlayerStatus")] = None
        c.check("an unreachable httpapi answers from the recent reading",
                await d.foreign_owner("10.0.0.2") == "HDMI-ARC")
        d._last_mode["10.0.0.2"] = (time.monotonic()
                                    - linkplay.MODE_MEMORY_S - 1, 49)
        c.check("…but not from a stale one",
                await d.foreign_owner("10.0.0.2") is None)
        c.check("a host that is not LinkPlay is never foreign",
                await d.foreign_owner("10.0.0.3") is None)
    asyncio.run(go())


def _cast_owner(c: Checker, tmp: str) -> None:
    c.section("Cast ownership")
    z = _zone(tmp, _fake_cast())
    c.check("our stream on the default receiver is ours",
            z._cast_owner(_fake_cast()) is None)
    c.check("another app is foreign",
            z._cast_owner(_fake_cast(app_id="233637DE",
                                     display_name="YouTube")) == "YouTube")
    c.check("the home screen is free",
            z._cast_owner(_fake_cast(app_id=cast_sync.BACKDROP_APP_ID)) is None)
    c.check("no app is free", z._cast_owner(_fake_cast(app_id=None)) is None)
    c.check("other media playing on the default receiver is foreign",
            z._cast_owner(_fake_cast(content_id="http://x/tts.mp3"))
            is not None)
    c.check("other media finished on the default receiver is free",
            z._cast_owner(_fake_cast(content_id="http://x/tts.mp3",
                                     player_state="IDLE")) is None)


def _reload_yields(c: Checker, tmp: str) -> None:
    c.section("a WiiM on HDMI-ARC is not re-cast")

    async def go():
        z = _zone(tmp, _fake_cast(app_id=None, player_state="IDLE"))
        loaded = _loads(z)
        holder = {"192.168.1.102": "HDMI-ARC"}
        _input(z, holder)
        st = _stream(z)
        st.interrupted_since = time.monotonic() - 5.0
        z._sweep_interrupted()
        await asyncio.sleep(0.05)
        c.check("the interruption rung issues no LOAD", loaded == [], loaded)
        c.check("the device is yielded", st.yield_kind == "input"
                and st.yield_reason == "HDMI-ARC"
                and st.parked_since is not None)
        c.check("no reload is charged to it",
                st.reloads == 0 and st.reloads_since_align == 0)
        c.check("a yield is not counted as an absence", st.parks == 0)
        dev = z.status()["devices"][0]
        c.check("status reports yielded, not parked",
                dev["yielded"] == "HDMI-ARC" and not dev["parked"], dev)
        st.interrupted_since = time.monotonic() - 30.0
        st.last_interrupt_reload = None
        z._sweep_interrupted()
        await asyncio.sleep(0.05)
        c.check("the sweep leaves a yielded device alone", loaded == [])
        await z._realign_group("test")
        c.check("a group re-align does not LOAD it", loaded == [], loaded)

        # Still on HDMI: the probe keeps it out.
        await z._rejoin_stream(st)
        c.check("still held: stays yielded, no LOAD",
                loaded == [] and st.parked_since is not None)
        # Back on the network input: settles before rejoining.
        holder.clear()
        await z._rejoin_stream(st)
        c.check("free, but not for the settle window: still out",
                loaded == [] and st.free_since is not None)
        st.free_since -= cast_sync.STREAM_YIELD_FREE_INPUT_S + 1
        await z._rejoin_stream(st)
        c.check("free for the settle window: rejoins with one LOAD",
                len(loaded) == 1 and st.parked_since is None
                and not st.yield_kind, loaded)
    asyncio.run(go())


def _flap(c: Checker, tmp: str) -> None:
    c.section("an input that flaps resets the settle window")

    async def go():
        z = _zone(tmp, _fake_cast(app_id=None, player_state="IDLE"))
        loaded = _loads(z)
        holder = {}
        _input(z, holder)
        st = _stream(z)
        z._yield_stream(st, "input", "HDMI-ARC")
        await z._rejoin_stream(st)
        st.free_since -= cast_sync.STREAM_YIELD_FREE_INPUT_S - 1
        holder["192.168.1.102"] = "Optical-In"
        await z._rejoin_stream(st)
        c.check("a new owner restarts the clock and is named",
                st.free_since is None and st.yield_reason == "Optical-In")
        holder.clear()
        await z._rejoin_stream(st)
        c.check("no rejoin on the first free reading after a flap",
                loaded == [] and st.parked_since is not None)
    asyncio.run(go())


def _cast_detected(c: Checker, tmp: str) -> None:
    c.section("another Cast app is detected while the zone plays")

    async def go():
        cast = _fake_cast(app_id="233637DE", display_name="YouTube",
                          player_state="IDLE")
        z = _zone(tmp, cast)
        loaded = _loads(z)
        st = _stream(z)
        got = await z._read_media_time(st)
        c.check("no reading is taken from it", got is None)
        c.check("it is yielded to the app",
                st.yield_kind == "cast" and st.yield_reason == "YouTube")
        c.check("and not marked interrupted", st.interrupted_since is None)
        cast.status.app_id = None
        await z._rejoin_stream(st)
        st.free_since -= cast_sync.STREAM_YIELD_FREE_CAST_S + 1
        await z._rejoin_stream(st)
        c.check("rejoins once the app has gone", len(loaded) == 1, loaded)
    asyncio.run(go())


def _session_start(c: Checker, tmp: str) -> None:
    c.section("session start")

    async def go():
        z = _zone(tmp, _fake_cast(app_id="233637DE", display_name="YouTube"))
        loaded = _loads(z)
        holder = {}
        _input(z, holder)
        st = _stream(z)
        await z._launch_stream("cast:a", "a", gate="input")
        c.check("starting a zone takes over another Cast app",
                len(loaded) == 1 and st.parked_since is None, loaded)
        holder["192.168.1.102"] = "HDMI-ARC"
        await z._launch_stream("cast:a", "a", gate="input")
        c.check("…but not a box on another input",
                len(loaded) == 1 and st.yield_reason == "HDMI-ARC", loaded)
    asyncio.run(go())


def run() -> Checker:
    c = Checker("zone_yield")
    import tempfile
    _modes(c)
    _directory(c)
    with tempfile.TemporaryDirectory() as tmp:
        _cast_owner(c, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _reload_yields(c, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _flap(c, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _cast_detected(c, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _session_start(c, tmp)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
