"""
OpenZone on its own event loop (open-zone.md §A.1).

The engine used to share the app's loop, so any stall in another subsystem
stopped every speaker's stream. These run the real engine behind the real
``ZoneLoop`` and stall the app's loop on purpose.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
import types
import urllib.request
from types import SimpleNamespace

from harness import Checker

from modules.media.cast_sync import OpenZone, _acall
from modules.media.zone_loop import ZoneLoop, CALL_TIMEOUT_S


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _zone(tmp: str, port: int, cast=None) -> OpenZone:
    return OpenZone(cast or SimpleNamespace(_infos={}, _casts={}), {
        "http_port": port,
        "trims_file": f"{tmp}/trims.json",
        "model_trims_file": f"{tmp}/model_trims.json",
        "groups_file": f"{tmp}/groups.json",
        "model_file": f"{tmp}/model.json",
        "policy_file": f"{tmp}/policy.json",
        "last_file": f"{tmp}/last.json",
        "trim_graph_file": f"{tmp}/graph.json",
    })


def _get(port: int, timeout: float) -> tuple:
    """(status, seconds taken) for the engine's health URL, or (None, err)."""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=timeout) as r:
            return r.status, time.monotonic() - t0
    except Exception as e:
        return None, e


async def _until_up(port: int) -> bool:
    for _ in range(100):
        status, _ = await asyncio.to_thread(_get, port, 0.5)
        if status == 200:
            return True
        await asyncio.sleep(0.05)
    return False


def _stall(c: Checker, tmp: str) -> None:
    c.section("a stalled app loop does not stall the engine")

    async def go():
        port = _free_port()
        zl = ZoneLoop(_zone(tmp, port))
        zl.start()
        try:
            c.check("the engine's listener comes up", await _until_up(port))
            got = {}

            def fetch():
                time.sleep(0.2)          # land inside the stall
                got["r"] = _get(port, 1.0)

            t = threading.Thread(target=fetch)
            t.start()
            time.sleep(1.5)              # the app's loop is now blocked
            t.join()
            status, took = got["r"]
            c.check("the stream listener answers during a 1.5 s app-loop stall",
                    status == 200, took)
            c.check("…promptly, not after the stall ends",
                    status == 200 and took < 0.5, took)
        finally:
            zl.stop()
    asyncio.run(go())


def _forwarding(c: Checker, tmp: str) -> None:
    c.section("calls from the app run on the engine's thread")

    async def go():
        port = _free_port()
        z = _zone(tmp, port)
        zl = ZoneLoop(z)

        def where(self):
            return threading.current_thread().name

        async def where_async(self):
            await asyncio.sleep(0)
            return threading.current_thread().name

        z._where = types.MethodType(where, z)
        z._where_async = types.MethodType(where_async, z)
        c.check("before start, a call goes straight to the engine",
                zl._where() == threading.current_thread().name)
        zl.start()
        try:
            c.check("a plain method runs on the engine thread",
                    zl._where() == "openzone", zl._where())
            c.check("a coroutine method runs on the engine thread",
                    await zl._where_async() == "openzone")
            st = zl.status()
            c.check("a real engine method answers across the boundary",
                    isinstance(st, dict) and st.get("running") is False, st)
            res = await zl.stop_session()
            c.check("an async engine method answers across the boundary",
                    res.get("success") is True, res)
        finally:
            zl.stop()
    asyncio.run(go())


def _back_to_app(c: Checker, tmp: str) -> None:
    c.section("the engine reaches app-loop objects on the app's loop")

    async def go():
        seen = {}
        lock = asyncio.Lock()            # bound to the app's loop, as Cast's is

        async def get_cast(uuid_str):
            async with lock:
                seen["thread"] = threading.current_thread().name
                seen["loop"] = asyncio.get_running_loop()
                return f"cast {uuid_str}"

        def learner(host, mode):
            seen["learner"] = threading.current_thread().name
            return True

        app_loop = asyncio.get_running_loop()
        port = _free_port()
        z = _zone(tmp, port, SimpleNamespace(_infos={}, _casts={},
                                             _get_cast=get_cast))
        zl = ZoneLoop(z)
        zl.start()
        try:
            async def from_engine():
                cast = await z._on_main(z.cast._get_cast("a"))
                learned = await z._on_main(_acall(learner, "h", 1))
                return cast, learned, threading.current_thread().name

            async with lock:             # contended, so the lock binds a loop
                fut = asyncio.run_coroutine_threadsafe(from_engine(), zl._zl_loop)
                await asyncio.sleep(0.1)
            cast, learned, caller = await asyncio.wrap_future(fut)
            c.check("the call starts on the engine thread", caller == "openzone")
            c.check("the Cast connect runs on the app's loop",
                    seen.get("loop") is app_loop, seen)
            c.check("…and its answer comes back to the engine", cast == "cast a")
            c.check("a plain app callable runs on the app's thread too",
                    seen.get("learner") == threading.current_thread().name
                    and learned is True, seen)
        finally:
            zl.stop()
    asyncio.run(go())


def _wedged(c: Checker, tmp: str) -> None:
    c.section("a wedged engine fails a call instead of hanging the app")

    async def go():
        port = _free_port()
        zl = ZoneLoop(_zone(tmp, port))
        zl.start()
        try:
            zl._zl_loop.call_soon_threadsafe(time.sleep, CALL_TIMEOUT_S + 1.0)
            t0 = time.monotonic()
            err = None
            try:
                zl.status()
            except TimeoutError as e:
                err = e
            took = time.monotonic() - t0
            c.check("the call raises TimeoutError", err is not None, err)
            c.check("…after the bounded wait", took < CALL_TIMEOUT_S + 0.5, took)
        finally:
            zl.stop()
    asyncio.run(go())


def _shutdown(c: Checker, tmp: str) -> None:
    c.section("shutdown ends the thread and the listener")

    async def go():
        port = _free_port()
        zl = ZoneLoop(_zone(tmp, port))
        zl.start()
        await _until_up(port)
        thread = zl._zl_thread
        zl.stop()
        c.check("the engine thread has exited", not thread.is_alive())
        status, _ = _get(port, 0.5)
        c.check("the listener no longer answers", status is None)
        c.check("after stop, calls go straight to the engine again",
                isinstance(zl.status(), dict))
    asyncio.run(go())


def run() -> Checker:
    c = Checker("zone_loop")
    with tempfile.TemporaryDirectory() as tmp:
        _stall(c, tmp)
        _forwarding(c, tmp)
        _back_to_app(c, tmp)
        _wedged(c, tmp)
        _shutdown(c, tmp)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
