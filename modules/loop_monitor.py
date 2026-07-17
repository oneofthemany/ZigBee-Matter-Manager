"""
Event-loop responsiveness monitor.

A blocked asyncio loop is the worst failure mode this app has: HTTP stops
dead (including /api/system/health), so the process looks alive while serving
nothing, and the manager watchdog needs several failed checks (~minutes) to
act. This monitor closes that gap from inside the process:

  - a heartbeat coroutine bumps a timestamp every second on the loop;
  - a daemon *thread* (immune to the stall) watches the heartbeat age:
      * stall ≥ warn_after  → logs the loop thread's current stack, so the
        log names exactly what is blocking (the Octopus backfill incident of
        2026-07-17 took an hour to diagnose without this);
      * stall ≥ exit_after  → writes data/last_crash.json and hard-exits
        non-zero. The launcher treats a non-zero exit after a healthy boot
        as a runtime crash and restarts main.py within seconds — far faster
        than the manager watchdog's grace + streak cycle.

Stats are surfaced in /api/system/health under "loop" so past stalls are
visible even after recovery.

Env overrides:
  ZMM_LOOP_STALL_WARN_SEC  (default 5, 0 disables the warning/stack dump)
  ZMM_LOOP_STALL_EXIT_SEC  (default 60, 0 disables the self-restart)
"""
import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("modules.loop_monitor")

CRASH_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "last_crash.json")
# Launcher restarts on any non-zero exit after a healthy boot; 70 = EX_SOFTWARE
STALL_EXIT_CODE = 70


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class LoopMonitor:
    def __init__(self,
                 warn_after: Optional[float] = None,
                 exit_after: Optional[float] = None):
        self.warn_after = _env_float("ZMM_LOOP_STALL_WARN_SEC", 5.0) \
            if warn_after is None else warn_after
        self.exit_after = _env_float("ZMM_LOOP_STALL_EXIT_SEC", 60.0) \
            if exit_after is None else exit_after
        self._beat = time.monotonic()
        self._loop_thread_id: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        # Incident stats (read by /api/system/health)
        self._stalls = 0
        self._worst_ms = 0.0
        self._last_stall: Optional[Dict[str, Any]] = None
        self._in_stall_since: Optional[float] = None

    # ------------------------------------------------------------------

    def start(self):
        """Call from inside the running event loop."""
        loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._beat = time.monotonic()
        self._task = loop.create_task(self._beat_loop())
        self._thread = threading.Thread(
            target=self._watch, name="loop-monitor", daemon=True)
        self._thread.start()
        logger.info(
            f"Loop monitor started (stack dump at {self.warn_after:.0f}s stall, "
            + (f"self-restart at {self.exit_after:.0f}s)" if self.exit_after
               else "self-restart disabled)")
        )

    def stop(self):
        """Call FIRST in shutdown — a stopping loop must not look stalled."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            self._task = None

    def get_stats(self) -> Dict[str, Any]:
        age = time.monotonic() - self._beat
        return {
            "monitoring": self._thread is not None and self._thread.is_alive(),
            "stalls": self._stalls,
            "worst_ms": round(self._worst_ms),
            "last_stall": self._last_stall,
            "heartbeat_age_ms": round(age * 1000),
        }

    # ------------------------------------------------------------------

    async def _beat_loop(self):
        while True:
            self._beat = time.monotonic()
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return

    def _loop_stack(self) -> str:
        try:
            frame = sys._current_frames().get(self._loop_thread_id)
            if frame is None:
                return "<loop thread frame unavailable>"
            return "".join(traceback.format_stack(frame))
        except Exception as e:  # pragma: no cover
            return f"<stack capture failed: {e}>"

    def _watch(self):
        dumped = False
        while not self._stopping:
            time.sleep(1)
            if self._stopping:
                return
            age = time.monotonic() - self._beat

            if age < max(2.0, self.warn_after if self.warn_after else 2.0):
                # Healthy (or recovered) — close out any open stall.
                if self._in_stall_since is not None:
                    duration = time.monotonic() - self._in_stall_since
                    self._stalls += 1
                    self._worst_ms = max(self._worst_ms, duration * 1000)
                    self._last_stall = {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "duration_ms": round(duration * 1000),
                    }
                    logger.warning(
                        f"Event loop recovered after {duration:.1f}s stall "
                        f"(stall #{self._stalls}, worst {self._worst_ms / 1000:.1f}s)")
                    self._in_stall_since = None
                    dumped = False
                continue

            if self._in_stall_since is None:
                self._in_stall_since = self._beat

            if self.warn_after and not dumped:
                dumped = True
                logger.error(
                    f"EVENT LOOP STALLED for {age:.1f}s — blocking call on the "
                    f"loop thread. Current loop stack:\n{self._loop_stack()}")

            if self.exit_after and age >= self.exit_after:
                self._die(age)

    def _die(self, age: float):
        stack = self._loop_stack()
        logger.critical(
            f"EVENT LOOP STALLED for {age:.0f}s — exiting so the launcher "
            f"restarts the app (exit {STALL_EXIT_CODE}). Loop stack:\n{stack}")
        try:
            os.makedirs(os.path.dirname(CRASH_FILE), exist_ok=True)
            with open(CRASH_FILE, "w") as f:
                json.dump({
                    "timestamp": datetime.now(timezone.utc)
                        .replace(tzinfo=None).isoformat() + "Z",
                    "exc_type": "EventLoopStall",
                    "exc_value": f"asyncio loop unresponsive for {age:.0f}s "
                                 f"(threshold {self.exit_after:.0f}s)",
                    "traceback": stack[-12000:],
                    "exit_code": STALL_EXIT_CODE,
                    "source": "loop_monitor",
                }, f, indent=2)
        except Exception:
            pass
        # Flush logs, then hard-exit: the loop is wedged, so a graceful
        # shutdown is impossible by definition.
        try:
            for h in logging.getLogger().handlers:
                h.flush()
        except Exception:
            pass
        os._exit(STALL_EXIT_CODE)
