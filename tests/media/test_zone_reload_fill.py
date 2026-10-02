"""
OpenZone: a reload's own fill is not an interruption (open-zone.md §7.1).

A slow receiver reports BUFFERING for about its pipeline latency after every
LOAD. Charged as an interruption, that re-arms the interruption rung on each
reload and the device is reloaded every cycle forever. The real
`_reload_stream`, `_launch_stream`, `_read_media_time` and `_sweep_interrupted`
are exercised against a fake Cast provider.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync
from modules.media.cast_sync import OpenZone, _Stream, DEFAULT_APP_ID

LATENCY_S = 6.6     # a Pixel Tablet's measured pipeline


class _FakeSource:
    delay_s = 4.0

    def earliest_sample(self) -> int:
        return -10 ** 9

    def latest_sample(self) -> int:
        return 10 ** 9


def _fake_cast():
    ms = SimpleNamespace(content_id="http://h/sync/stream/a.wav",
                         player_state="PLAYING", title="", last_updated=None,
                         adjusted_current_time=12.0)
    mc = SimpleNamespace(status=ms, update_status=lambda: None)
    return SimpleNamespace(
        status=SimpleNamespace(app_id=DEFAULT_APP_ID, display_name=""),
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
        return "192.168.1.95"


def _setup(tmp: str):
    cast = _fake_cast()
    z = OpenZone(_FakeCastProvider(cast), {
        "trims_file": f"{tmp}/trims.json",
        "model_trims_file": f"{tmp}/model_trims.json",
        "groups_file": f"{tmp}/groups.json",
        "model_file": f"{tmp}/model.json",
    })
    z.running = True
    z._source = _FakeSource()
    z._epoch = time.monotonic() - 100.0
    st = _Stream("a", "cast:a", "Pixel Tablet")
    st.pos = 0.0
    st.natural_lag = 1.0
    st.connected = True
    st.latency_s = LATENCY_S
    z._streams["a"] = st
    z._pending["a"] = {"player_id": "cast:a", "name": st.name}
    loaded = []
    z._play_stream = lambda _cast, url: loaded.append(url)
    return z, st, cast.media_controller.status, loaded


def _sweep(z: OpenZone, st: _Stream) -> None:
    st.cooldown_until = 0.0     # the reload's cooldown has run out
    z._sweep_interrupted()


def _fill_after_reload(c: Checker, tmp: str) -> None:
    c.section("a slow device filling after a reload is left alone")

    async def go():
        z, st, ms, loaded = _setup(tmp)
        await z._reload_stream(st)
        c.check("the reload LOADs once", len(loaded) == 1, loaded)
        ms.player_state = "BUFFERING"
        st.last_reload = time.monotonic() - (LATENCY_S + 3.0)
        await z._read_media_time(st)
        c.check("its fill is not marked as an interruption",
                st.interrupted_since is None, st.interrupted_since)
        if st.interrupted_since is not None:
            st.interrupted_since -= 10.0     # the fill lasts ~10 s in life
        ms.player_state = "PLAYING"
        await z._read_media_time(st)
        c.check("nothing is held over once it plays",
                st.interrupt_held == 0.0, st.interrupt_held)
        _sweep(z, st)
        await asyncio.sleep(0.5)   # the sweep reloads from a task
        c.check("and it is not reloaded again", len(loaded) == 1, loaded)
    asyncio.run(go())


def _stuck_buffering(c: Checker, tmp: str) -> None:
    c.section("a device still filling past the grace is treated as down")

    async def go():
        z, st, ms, loaded = _setup(tmp)
        await z._reload_stream(st)
        ms.player_state = "BUFFERING"
        st.last_reload = time.monotonic() - (
            LATENCY_S + cast_sync.STREAM_RELOAD_BUFFER_GRACE_S + 1.0)
        await z._read_media_time(st)
        c.check("it is marked interrupted", st.interrupted_since is not None)
        st.interrupted_since -= cast_sync.STREAM_INTERRUPT_MIN_S + 1.0
        st.last_interrupt_reload = None
        _sweep(z, st)
        await asyncio.sleep(0.5)   # the sweep reloads from a task
        c.check("and reloaded", len(loaded) == 2, loaded)
    asyncio.run(go())


def _real_interruption(c: Checker, tmp: str) -> None:
    c.section("a real interruption right after a reload is still caught")

    async def go():
        z, st, ms, loaded = _setup(tmp)
        await z._reload_stream(st)
        ms.player_state = "PAUSED"
        await z._read_media_time(st)
        c.check("PAUSED inside the window still arms the rung",
                st.interrupted_since is not None)
    asyncio.run(go())


def _no_reload_no_grace(c: Checker, tmp: str) -> None:
    c.section("a mid-session rebuffer with no reload behind it")

    async def go():
        z, st, ms, _loaded = _setup(tmp)
        ms.player_state = "BUFFERING"
        await z._read_media_time(st)
        c.check("is still an interruption", st.interrupted_since is not None)
    asyncio.run(go())


def run() -> Checker:
    c = Checker("zone_reload_fill")
    import tempfile
    for case in (_fill_after_reload, _stuck_buffering, _real_interruption,
                 _no_reload_no_grace):
        with tempfile.TemporaryDirectory() as tmp:
            case(c, tmp)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
