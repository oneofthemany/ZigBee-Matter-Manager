"""
A maintenance action on one device, observed: each step's answer from the
device, a verdict drawn from those answers, and the device's log lines
streamed while it runs (docs/device-ops.md).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("modules.device_ops")

EVENT = "device_op"
MAX_LINES = 400

_current: contextvars.ContextVar[Optional["DeviceOp"]] = contextvars.ContextVar("device_op", default=None)

# What an answer means, as a person can act on it. A status is the device's
# answer to our request: it says what it would not take, not that it is
# broken, so no advice prescribes re-pairing. Keys are reason names: "timeout"
# or a ZCL status name.
MEANING = {
    "timeout": "No answer: the device is probably asleep. Wake it (press its button) and run this "
               "again while it is awake.",
    "INSUFFICIENT_SPACE": "The device would not store this reporting setup. Some firmware answers this "
                          "to any setup it does not keep, not only when its table is full.",
    "INVALID_VALUE": "The device would not take the interval or change asked for: try other values "
                     "in the device's entry.",
    "INVALID_DATA_TYPE": "The device expects another data type for this attribute than the entry gives.",
    "UNSUPPORTED_ATTRIBUTE": "The device has no such attribute: check the attribute id in the entry.",
    "UNREPORTABLE_ATTRIBUTE": "The device has this attribute but does not report it: it can be read "
                              "(polled) instead.",
    "UNSUP_CLUSTER_COMMAND": "The device does not take this request on this cluster.",
    "NOT_SUPPORTED": "The device does not take this request.",
}
WATCH = ("Trigger the reading (walk past, open it, press it) and watch the log: if it arrives on its own, "
         "nothing needs fixing.")


def reason_of(status_or_exc: Any) -> str:
    """A step's reason from a ZCL status or an exception, by name."""
    if isinstance(status_or_exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    name = getattr(status_or_exc, "name", None)
    if name:
        return str(name)
    return str(status_or_exc) or type(status_or_exc).__name__


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    return f"{seconds / 3600:.1f} h ago"


class DeviceOp:
    def __init__(self, ieee: str, action: str):
        self.id = uuid.uuid4().hex[:8]
        self.ieee, self.action = ieee, action
        self.started = time.time()
        self.steps: List[Dict[str, Any]] = []
        self.lines: List[Dict[str, Any]] = []
        self.error: Optional[str] = None

    def step(self, what: str, ok: bool, reason: Optional[str] = None,
             cluster: Optional[int] = None, attr: Optional[int] = None) -> None:
        self.steps.append({"what": what, "ok": ok, "reason": reason, "cluster": cluster, "attr": attr})

    def _judge(self, s: Dict[str, Any], now: float) -> str:
        """ok | declined (refused, yet the device reports it anyway) | failed:
        what the device does outranks what it answered to setup."""
        if s["ok"]:
            return "ok"
        if s["cluster"] is not None and s["attr"] is not None:
            from modules.device_observer import last_report
            at = last_report(self.ieee, s["cluster"], s["attr"])
            if at is not None:
                s["reports_anyway"] = _ago(now - at)
                return "declined"
        return "failed"

    def report(self) -> Dict[str, Any]:
        now = time.time()
        for s in self.steps:
            s["outcome"] = self._judge(s, now)
        failed = [s for s in self.steps if s["outcome"] == "failed"]
        declined = [s for s in self.steps if s["outcome"] == "declined"]
        passed = len(self.steps) - len(failed) - len(declined)
        if self.error or (self.steps and not passed and not declined):
            verdict = "failed"
        elif failed:
            verdict = "partial"
        elif self.steps:
            verdict = "ok"
        else:
            verdict = "unchecked"   # ran, but nothing in it reported an answer
        advice = []
        for reason in dict.fromkeys(s["reason"] for s in failed):
            text = MEANING.get(reason, f"The device answered {reason}.")
            if reason != "timeout" and any(s["attr"] is not None for s in failed if s["reason"] == reason):
                text = f"{text} {WATCH}"
            advice.append({"reason": reason, "text": text})
        if declined:
            advice.append({"reason": "declined",
                           "text": "Some setup was declined, but the device reports those readings on its own "
                                   "(see below): nothing to fix."})
        return {"op_id": self.id, "ieee": self.ieee, "action": self.action, "verdict": verdict,
                "passed": passed, "declined": len(declined), "failed": len(failed), "steps": self.steps,
                "advice": advice, "error": self.error, "seconds": round(now - self.started, 1)}


def step(what: str, ok: bool, reason: Optional[str] = None, *,
         cluster: Optional[int] = None, attr: Optional[int] = None) -> None:
    """Record a step's answer on the action running in this context; nothing
    outside one, so instrumented code costs nothing on the normal path.
    `cluster`/`attr` name what a reporting step was for, so the verdict can
    check whether the device reports it anyway."""
    op = _current.get()
    if op is not None:
        op.step(what, ok, reason, cluster, attr)


def step_status(what: str, status: Any, **about: Any) -> None:
    """A step from a ZCL/ZDO status: 0 is success, anything else its name."""
    try:
        ok = int(status) == 0
    except (TypeError, ValueError):
        ok = False
    step(what, ok, None if ok else reason_of(status), **about)


class _LineTap(logging.Handler):
    """Tees the device's own log lines (those naming `[ieee]`) to the op.
    A record can come from any thread; the emit hops to the loop."""

    def __init__(self, op: DeviceOp, loop: asyncio.AbstractEventLoop, emit: Callable[[str, dict], None]):
        super().__init__(level=logging.INFO)
        self._op, self._loop, self._emit = op, loop, emit
        self._tag = f"[{op.ieee}]"
        self._busy = False      # an emit that logs must not feed itself

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if self._tag not in msg or len(self._op.lines) >= MAX_LINES:
                return
            line = {"ts": record.created, "level": record.levelname,
                    "message": msg.replace(self._tag, "").strip()}
            if self._busy:
                return
            self._op.lines.append(line)
            evt = {"op_id": self._op.id, "ieee": self._op.ieee, "action": self._op.action,
                   "phase": "line", "line": line}
            try:
                on_loop = asyncio.get_running_loop() is self._loop
            except RuntimeError:
                on_loop = False
            if not on_loop:
                self._loop.call_soon_threadsafe(self._emit, EVENT, evt)
                return
            # In order with the steps, so no line lands after the verdict.
            self._busy = True
            try:
                self._emit(EVENT, evt)
            finally:
                self._busy = False
        except Exception:
            pass    # never let the tap break logging


async def run(ieee: str, action: str, work: Callable[[], Awaitable[Any]],
              emit: Callable[[str, dict], None]) -> Dict[str, Any]:
    """Run `work` as `action` on `ieee`: stream its lines, then return (and
    emit) the report. `emit` is the service's sync emitter."""
    op = DeviceOp(ieee, action)
    loop = asyncio.get_running_loop()
    tap = _LineTap(op, loop, emit)
    root = logging.getLogger()
    root.addHandler(tap)
    token = _current.set(op)
    emit(EVENT, {"op_id": op.id, "ieee": ieee, "action": action, "phase": "start"})
    result: Any = None
    try:
        result = await work()
        # A refusal is already a step; only an error stops the action itself.
        if isinstance(result, dict) and result.get("success") is False and result.get("error"):
            op.error = str(result["error"])
    except Exception as e:
        op.error = f"{type(e).__name__}: {e}"
        logger.warning(f"[{ieee}] {action} failed: {op.error}")
    finally:
        _current.reset(token)
        root.removeHandler(tap)
    report = op.report()
    emit(EVENT, {**report, "phase": "done"})
    return {"success": op.error is None, **report, "result": result}
