"""OpenZone on an event loop of its own (open-zone.md §A.1).

The app's loop is shared by every subsystem, and a stall anywhere on it stops
every stream generator, so the speakers drain and the group falls apart. The
engine runs on a dedicated thread instead. ``ZoneLoop`` stands in for the
engine: its methods are forwarded, coroutines run on the engine's loop, plain
methods run there too and the caller waits for the answer.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
import time

logger = logging.getLogger("modules.media.cast_sync")

CALL_TIMEOUT_S = 2.0      # a plain method call waits this long for the loop
STOP_TIMEOUT_S = 5.0      # shutdown waits this long for the session to end
LAG_TICK_S = 0.5
LAG_WARN_S = 0.25         # engine-loop lateness worth a warning
LAG_LOG_EVERY_S = 30.0


class ZoneLoop:
    """Runs an ``OpenZone`` on its own thread and forwards calls to it."""

    def __init__(self, zone):
        self._zl_zone = zone
        self._zl_loop: asyncio.AbstractEventLoop | None = None
        self._zl_thread: threading.Thread | None = None

    @property
    def engine(self):
        return self._zl_zone

    def __getattr__(self, name):
        attr = getattr(self._zl_zone, name)
        if not inspect.ismethod(attr):
            return attr
        if inspect.iscoroutinefunction(attr):
            async def forward(*a, **k):
                return await self._zl_submit(attr(*a, **k))
        else:
            def forward(*a, **k):
                return self._zl_call(attr, *a, **k)
        forward.__name__ = name
        return forward

    def _zl_on_engine(self) -> bool:
        loop = self._zl_loop
        return loop is None or self._zl_thread is threading.current_thread()

    async def _zl_submit(self, coro):
        if self._zl_on_engine():
            return await coro
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, self._zl_loop))

    def _zl_call(self, fn, *a, **k):
        if self._zl_on_engine():
            return fn(*a, **k)
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run():
            try:
                fut.set_result(fn(*a, **k))
            except BaseException as e:
                fut.set_exception(e)

        self._zl_loop.call_soon_threadsafe(run)
        try:
            return fut.result(timeout=CALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"OpenZone engine did not answer "
                               f"{fn.__name__} within {CALL_TIMEOUT_S:.0f}s")

    def start(self) -> None:
        """Start the engine thread and the engine's HTTP listener on it.
        Called from the app's loop, which the engine keeps as ``_main_loop``."""
        if self._zl_thread is not None:
            return
        try:
            self._zl_zone._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._zl_zone._main_loop = None
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()

        # Assigned before the thread runs, so no call slips through directly.
        self._zl_loop = loop
        self._zl_thread = threading.Thread(target=run, name="openzone",
                                           daemon=True)
        self._zl_thread.start()
        ready.wait(5.0)
        loop.call_soon_threadsafe(self._zl_zone.start)
        loop.call_soon_threadsafe(lambda: loop.create_task(self._zl_lag_watch()))

    async def _zl_lag_watch(self) -> None:
        """Warn when the engine's own loop runs late: with the app's stalls
        gone, this is the one left that reaches the speakers."""
        logged = 0.0
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(LAG_TICK_S)
            late = time.monotonic() - t0 - LAG_TICK_S
            if late > LAG_WARN_S and time.monotonic() - logged > LAG_LOG_EVERY_S:
                logged = time.monotonic()
                logger.warning(f"OpenZone engine loop ran {late * 1000:.0f} ms "
                               f"late — something blocked it")

    def stop(self) -> None:
        """End the session, close the listener and stop the thread. Blocks the
        caller for at most a few seconds; it runs at app shutdown."""
        loop, thread = self._zl_loop, self._zl_thread
        zone = self._zl_zone
        if loop is None:
            zone.stop()
            return

        async def shutdown():
            if zone.running:
                try:
                    await asyncio.wait_for(zone.stop_session(), STOP_TIMEOUT_S)
                except Exception as e:
                    logger.warning(f"OpenZone session did not stop cleanly: {e}")
            http = zone._http_task
            zone.stop()
            if http is not None:
                try:
                    await asyncio.wait_for(http, 3.0)
                except Exception:
                    pass
            me = asyncio.current_task()
            rest = [t for t in asyncio.all_tasks() if t is not me]
            for t in rest:
                t.cancel()
            await asyncio.gather(*rest, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(
                STOP_TIMEOUT_S + 5.0)
        except Exception as e:
            logger.warning(f"OpenZone shutdown incomplete: {e}")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2.0)
        self._zl_loop = self._zl_thread = None
