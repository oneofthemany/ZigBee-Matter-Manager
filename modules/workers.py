"""
Workers — household state a person or a rule sets, and every rule can read.

An automation can only react to something a device did. A worker is the other
half: a value nobody's hardware reports — holiday mode, the mode the house is
in, a countdown, a tally, when something last happened — set deliberately and
then testable from anywhere.

Each worker is an ordinary device-like under `worker::<id>`, merged into the
automation engine through `add_device_getter`. That is the whole integration:
a rule triggers on a worker, tests one as a prerequisite and commands one with
no new condition type and no new step type. See docs/workers.md.

Six types, chosen because each expresses something the others cannot:

    boolean   on/off                     a flag: holiday mode
    mode      one of N labels            mutually exclusive states, which a
                                         pile of booleans cannot guarantee
    timer     on/off that clears itself   "for the next two hours"
    counter   an integer                 "how many times today"
    marker    minutes since an event     memory, which an edge-triggered
                                         engine otherwise has none of
    number    a float with bounds        a setpoint rules share

Nothing here touches a database. State lives in memory, is published to the
engine as a plain dict, and is persisted to a small JSON file only when it
actually changes — never on the countdown tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("modules.workers")

DATA_FILE = "./data/workers.json"

# IEEE-style prefix, matching the convention presence users established, so
# virtual ids can never collide with a real 16-hex-char address.
WORKER_IEEE_PREFIX = "worker::"

MAX_WORKERS = 64
MAX_MODE_OPTIONS = 12
MAX_NAME_LEN = 48

# Countdown resolution. Timers are household-scale ("an hour of quiet"), so a
# tick finer than this buys nothing and wakes the loop for no reason.
TICK_SECONDS = 30

# A marker that has never been marked reads as "a year ago" rather than as a
# missing attribute. It is the truthful answer to "how long since?" when the
# thing has never happened, and it keeps every numeric comparison well-defined:
# "more than 6 hours since" is true on a fresh install, "less than 6 hours" is
# false. A missing attribute would make both of those raise or silently pass.
MARKER_NEVER_MINUTES = 525_600

MAX_TIMER_SECONDS = 7 * 24 * 3600
MAX_COUNTER = 1_000_000

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

TYPE_BOOLEAN = "boolean"
TYPE_MODE = "mode"
TYPE_TIMER = "timer"
TYPE_COUNTER = "counter"
TYPE_MARKER = "marker"
TYPE_NUMBER = "number"

ON = "on"
OFF = "off"


# Type catalogue
#
# One entry per type, describing it well enough that the API can hand the UI a
# form and the UI needs no hard-coded knowledge of what a worker is. `commands`
# is the same shape device/commands.py returns, so the rule builder's target
# action picker renders a worker exactly as it renders a lamp.

WORKER_TYPES: Dict[str, Dict[str, Any]] = {
    TYPE_BOOLEAN: {
        "label": "Boolean",
        "icon": "toggle-on",
        "summary": "An on/off flag. Holiday mode, guest staying, bins out.",
        "primary": "value",
        "fields": [
            {"key": "initial", "label": "Starts as", "type": "select",
             "options": [ON, OFF], "default": OFF},
            {"key": "restore", "label": "Survives a restart", "type": "bool",
             "default": True},
        ],
        "commands": [
            {"command": "on", "label": "Turn on"},
            {"command": "off", "label": "Turn off"},
            {"command": "toggle", "label": "Toggle"},
        ],
    },
    TYPE_MODE: {
        "label": "Mode",
        "icon": "list-check",
        "summary": "One of several named states. House mode: home, away, "
                   "night, holiday. Only ever one at a time.",
        "primary": "value",
        "fields": [
            {"key": "options", "label": "Options", "type": "list",
             "default": ["home", "away", "night"]},
            {"key": "initial", "label": "Starts as", "type": "option_ref",
             "default": None},
            {"key": "restore", "label": "Survives a restart", "type": "bool",
             "default": True},
        ],
        "commands": [
            {"command": "set", "label": "Set to", "type": "select"},
        ],
    },
    TYPE_TIMER: {
        "label": "Timer",
        "icon": "hourglass-half",
        "summary": "A flag that clears itself. Do not disturb for two hours, "
                   "heating snoozed for thirty minutes.",
        "primary": "value",
        "fields": [
            {"key": "default_seconds", "label": "Default duration (seconds)",
             "type": "number", "min": 1, "max": MAX_TIMER_SECONDS,
             "default": 3600},
            {"key": "restore", "label": "Resumes after a restart",
             "type": "bool", "default": True},
        ],
        "commands": [
            {"command": "start", "label": "Start", "type": "number",
             "unit": "s", "min": 1, "max": MAX_TIMER_SECONDS},
            {"command": "extend", "label": "Extend by", "type": "number",
             "unit": "s", "min": 1, "max": MAX_TIMER_SECONDS},
            {"command": "cancel", "label": "Cancel"},
        ],
    },
    TYPE_COUNTER: {
        "label": "Counter",
        "icon": "hashtag",
        "summary": "A tally. Times the door opened today, coffees made, "
                   "alerts sent this evening.",
        "primary": "value",
        "fields": [
            {"key": "step", "label": "Step", "type": "number", "default": 1},
            {"key": "min", "label": "Minimum", "type": "number", "default": 0},
            {"key": "max", "label": "Maximum", "type": "number",
             "default": MAX_COUNTER},
            {"key": "reset_at", "label": "Resets daily at (HH:MM, blank for "
                                        "never)", "type": "time",
             "default": None},
            {"key": "restore", "label": "Survives a restart", "type": "bool",
             "default": True},
        ],
        "commands": [
            {"command": "increment", "label": "Add", "type": "number"},
            {"command": "decrement", "label": "Subtract", "type": "number"},
            {"command": "set", "label": "Set to", "type": "number"},
            {"command": "reset", "label": "Reset"},
        ],
    },
    TYPE_MARKER: {
        "label": "Marker",
        "icon": "clock-rotate-left",
        "summary": "When something last happened, in minutes since. Gives "
                   "rules a memory: plants last watered, boiler last ran.",
        "primary": "age_minutes",
        "fields": [
            {"key": "restore", "label": "Survives a restart", "type": "bool",
             "default": True},
        ],
        "commands": [
            {"command": "mark", "label": "Mark now"},
            {"command": "reset", "label": "Clear"},
        ],
    },
    TYPE_NUMBER: {
        "label": "Number",
        "icon": "sliders",
        "summary": "A shared setpoint. Comfort temperature, alarm delay, "
                   "brightness for the evening.",
        "primary": "value",
        "fields": [
            {"key": "min", "label": "Minimum", "type": "number", "default": 0},
            {"key": "max", "label": "Maximum", "type": "number",
             "default": 100},
            {"key": "step", "label": "Step", "type": "number", "default": 1},
            {"key": "unit", "label": "Unit", "type": "text", "default": ""},
            {"key": "initial", "label": "Starts at", "type": "number",
             "default": 0},
            {"key": "restore", "label": "Survives a restart", "type": "bool",
             "default": True},
        ],
        "commands": [
            {"command": "set", "label": "Set to", "type": "number"},
            {"command": "increment", "label": "Add", "type": "number"},
            {"command": "decrement", "label": "Subtract", "type": "number"},
        ],
    },
}

# Every command any worker accepts. The automation engine validates a command
# step against its own whitelist, which this feeds.
WORKER_COMMANDS = sorted({
    c["command"] for spec in WORKER_TYPES.values() for c in spec["commands"]
})


def worker_ieee(worker_id: str) -> str:
    """Stable virtual-IEEE for a worker id."""
    return f"{WORKER_IEEE_PREFIX}{worker_id.lower()}"


def is_worker(ieee: str) -> bool:
    return isinstance(ieee, str) and ieee.startswith(WORKER_IEEE_PREFIX)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return s[:48] or "worker"


def _num(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return fallback
        out = float(value)
        return fallback if out != out else out        # drop NaN
    except (TypeError, ValueError):
        return fallback


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "1", "yes")
    return bool(value)


class _Capabilities:
    """Duck-typed capabilities object, as every other provider presents.

    The single "worker" capability is what puts a worker in the automation
    engine's actuator list without also making it look like a lamp.
    """

    def __init__(self, caps: List[str]) -> None:
        self._caps = list(caps)

    def has_capability(self, cap: str) -> bool:
        return cap in self._caps

    def get_capabilities(self) -> List[str]:
        return list(self._caps)


class WorkerDevice:
    """One worker, shaped like every other device the engine holds.

    Semantics live here rather than in the manager because a command's effect
    is the only thing that genuinely differs between the six types; everything
    else — persistence, publication, the tick — is shared.
    """

    def __init__(self, cfg: Dict[str, Any],
                 publisher: Optional[Callable[["WorkerDevice", Dict[str, Any]],
                                              Awaitable[None]]] = None) -> None:
        self.cfg = cfg
        self._publish = publisher
        self.ieee = worker_ieee(cfg["id"])
        self.friendly_name = cfg.get("name") or cfg["id"]
        self.manufacturer = "ZMM"
        self.model = f"Worker · {WORKER_TYPES[cfg['type']]['label']}"
        self.last_seen: float = 0.0
        self.capabilities = _Capabilities(["worker"])

        self.state: Dict[str, Any] = {"available": True}
        # Timer deadline and marker instant are the only two pieces of worker
        # state that are a wall-clock time rather than a value, so they are
        # held as one and derived into attributes on every tick.
        self.expires_at: Optional[float] = None
        self.marked_at: Optional[float] = None
        # Day the counter last rolled over, so a daily reset happens once even
        # though the tick crosses the reset time many times.
        self.last_reset_day: Optional[str] = None

        self._init_state()

    # Identity

    @property
    def id(self) -> str:
        return self.cfg["id"]

    @property
    def type(self) -> str:
        return self.cfg["type"]

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def is_available(self) -> bool:
        return True

    def get_control_commands(self) -> List[Dict[str, Any]]:
        """Commands the rule builder and the UI may offer for this worker."""
        spec = WORKER_TYPES[self.type]
        out = []
        for cmd in spec["commands"]:
            c = dict(cmd)
            if self.type == TYPE_MODE and c["command"] == "set":
                c["options"] = list(self.cfg.get("options") or [])
            elif self.type == TYPE_NUMBER:
                c.setdefault("min", self.cfg.get("min"))
                c.setdefault("max", self.cfg.get("max"))
                c.setdefault("step", self.cfg.get("step"))
                if self.cfg.get("unit"):
                    c["unit"] = self.cfg["unit"]
            elif self.type == TYPE_COUNTER and c.get("type") == "number":
                c.setdefault("min", self.cfg.get("min"))
                c.setdefault("max", self.cfg.get("max"))
            elif self.type == TYPE_TIMER and c.get("type") == "number":
                c.setdefault("default", self.cfg.get("default_seconds"))
            out.append(c)
        return out

    def value_options(self, attribute: str) -> Optional[List[str]]:
        """
        Enumerated values for an attribute, so the rule builder offers a
        dropdown instead of a free-text box. The automation engine looks for
        this method on any device; workers are the first to provide it.
        """
        if attribute != "value":
            return None
        if self.type in (TYPE_BOOLEAN, TYPE_TIMER):
            return [ON, OFF]
        if self.type == TYPE_MODE:
            return list(self.cfg.get("options") or [])
        return None

    # State

    def _init_state(self) -> None:
        """Seed state from config, before any persisted value is restored."""
        t = self.type
        if t == TYPE_BOOLEAN:
            self.state["value"] = ON if _truthy(self.cfg.get("initial")) else OFF
        elif t == TYPE_MODE:
            opts = self.cfg.get("options") or []
            initial = self.cfg.get("initial")
            self.state["value"] = initial if initial in opts else (
                opts[0] if opts else "")
        elif t == TYPE_TIMER:
            self.state["value"] = OFF
            self.state["remaining_s"] = 0
        elif t == TYPE_COUNTER:
            self.state["value"] = int(_num(self.cfg.get("min"), 0) or 0)
        elif t == TYPE_MARKER:
            self.state["age_minutes"] = MARKER_NEVER_MINUTES
            self.state["marked"] = OFF
        elif t == TYPE_NUMBER:
            self.state["value"] = self._clamp_number(
                _num(self.cfg.get("initial"), 0.0))

    def apply(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Merge new attribute values, returning only what actually changed."""
        changed = {k: v for k, v in values.items() if self.state.get(k) != v}
        if changed:
            self.state.update(changed)
            self.state["last_update"] = time.time()
            self.last_seen = time.time()
        return changed

    def _clamp_number(self, value: Optional[float]) -> float:
        lo = _num(self.cfg.get("min"))
        hi = _num(self.cfg.get("max"))
        out = _num(value, 0.0) or 0.0
        if lo is not None:
            out = max(lo, out)
        if hi is not None:
            out = min(hi, out)
        return round(out, 4)

    def _clamp_counter(self, value: float) -> int:
        lo = int(_num(self.cfg.get("min"), 0) or 0)
        hi = int(_num(self.cfg.get("max"), MAX_COUNTER) or MAX_COUNTER)
        return int(max(lo, min(hi, value)))

    # Commands

    async def send_command(self, command: str, value: Any = None,
                           endpoint_id: Any = None) -> Dict[str, Any]:
        """
        Apply a command and publish whatever it changed.

        The automation engine calls this for a `command` step exactly as it
        calls a lamp, so a rule setting a worker needs no special case. The
        publish is awaited inline rather than scheduled, which is what lets the
        engine's chain-depth guard see a rule that sets a worker that fires a
        rule: it all happens in one task context.
        """
        if not self.enabled:
            return {"success": False, "error": f"Worker '{self.id}' is disabled"}

        try:
            changed = self._apply_command(command, value)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if changed and self._publish:
            await self._publish(self, changed)
        return {"success": True, "changed": changed,
                "state": self.describe()["state"]}

    def _apply_command(self, command: str, value: Any) -> Dict[str, Any]:
        handler = {
            TYPE_BOOLEAN: self._cmd_boolean,
            TYPE_MODE: self._cmd_mode,
            TYPE_TIMER: self._cmd_timer,
            TYPE_COUNTER: self._cmd_counter,
            TYPE_MARKER: self._cmd_marker,
            TYPE_NUMBER: self._cmd_number,
        }[self.type]
        return handler(command, value)

    def _cmd_boolean(self, command: str, value: Any) -> Dict[str, Any]:
        if command == "on":
            return self.apply({"value": ON})
        if command == "off":
            return self.apply({"value": OFF})
        if command == "toggle":
            return self.apply({"value": OFF if self.state.get("value") == ON else ON})
        if command == "set":
            return self.apply({"value": ON if _truthy(value) else OFF})
        raise ValueError(f"'{command}' is not a boolean worker command")

    def _cmd_mode(self, command: str, value: Any) -> Dict[str, Any]:
        opts = self.cfg.get("options") or []
        if command != "set":
            raise ValueError(f"'{command}' is not a mode worker command")
        target = str(value or "").strip()
        # Case-insensitive so a rule saved with "Away" still matches "away".
        match = next((o for o in opts if o.lower() == target.lower()), None)
        if match is None:
            raise ValueError(
                f"'{target}' is not one of {', '.join(opts) or 'this mode'}")
        return self.apply({"value": match})

    def _cmd_timer(self, command: str, value: Any) -> Dict[str, Any]:
        now = time.time()
        if command in ("start", "extend"):
            seconds = _num(value, None)
            if seconds is None:
                seconds = _num(self.cfg.get("default_seconds"), 3600) or 3600
            if seconds <= 0 or seconds > MAX_TIMER_SECONDS:
                raise ValueError(
                    f"duration must be 1-{MAX_TIMER_SECONDS} seconds")
            base = self.expires_at if (
                command == "extend" and self.expires_at and self.expires_at > now
            ) else now
            self.expires_at = min(base + seconds, now + MAX_TIMER_SECONDS)
            return self.apply({"value": ON,
                               "remaining_s": int(self.expires_at - now)})
        if command == "cancel":
            self.expires_at = None
            return self.apply({"value": OFF, "remaining_s": 0})
        raise ValueError(f"'{command}' is not a timer worker command")

    def _cmd_counter(self, command: str, value: Any) -> Dict[str, Any]:
        step = _num(self.cfg.get("step"), 1) or 1
        current = _num(self.state.get("value"), 0) or 0
        if command == "increment":
            return self.apply({"value": self._clamp_counter(
                current + (_num(value, step) or step))})
        if command == "decrement":
            return self.apply({"value": self._clamp_counter(
                current - (_num(value, step) or step))})
        if command == "set":
            amount = _num(value, None)
            if amount is None:
                raise ValueError("set needs a number")
            return self.apply({"value": self._clamp_counter(amount)})
        if command == "reset":
            return self.apply({"value": self._clamp_counter(
                _num(self.cfg.get("min"), 0) or 0)})
        raise ValueError(f"'{command}' is not a counter worker command")

    def _cmd_marker(self, command: str, value: Any) -> Dict[str, Any]:
        if command == "mark":
            self.marked_at = time.time()
            return self.apply({"age_minutes": 0, "marked": ON,
                               "last_at": self.marked_at})
        if command == "reset":
            self.marked_at = None
            self.state.pop("last_at", None)
            return self.apply({"age_minutes": MARKER_NEVER_MINUTES,
                               "marked": OFF})
        raise ValueError(f"'{command}' is not a marker worker command")

    def _cmd_number(self, command: str, value: Any) -> Dict[str, Any]:
        step = _num(self.cfg.get("step"), 1) or 1
        current = _num(self.state.get("value"), 0) or 0
        if command == "set":
            amount = _num(value, None)
            if amount is None:
                raise ValueError("set needs a number")
            return self.apply({"value": self._clamp_number(amount)})
        if command == "increment":
            return self.apply({"value": self._clamp_number(
                current + (_num(value, step) or step))})
        if command == "decrement":
            return self.apply({"value": self._clamp_number(
                current - (_num(value, step) or step))})
        raise ValueError(f"'{command}' is not a number worker command")

    # Tick

    def tick(self, now: float) -> Dict[str, Any]:
        """
        Derived attributes that move with the clock, recomputed on the shared
        tick. Returns what changed, so a worker that is simply sitting there
        publishes nothing.
        """
        if self.type == TYPE_TIMER:
            if not self.expires_at:
                return {}
            remaining = self.expires_at - now
            if remaining <= 0:
                self.expires_at = None
                return self.apply({"value": OFF, "remaining_s": 0})
            # Whole seconds only: republishing 1799.4 → 1799.1 would wake every
            # rule watching this worker twice a minute for no change of meaning.
            return self.apply({"remaining_s": int(remaining)})

        if self.type == TYPE_MARKER:
            if not self.marked_at:
                return {}
            # Whole minutes, for the same reason: "more than six hours since"
            # cannot care about seconds, and minute granularity is what keeps
            # this from re-triggering the engine on every tick.
            return self.apply({"age_minutes": int((now - self.marked_at) // 60)})

        if self.type == TYPE_COUNTER:
            return self._tick_daily_reset(now)

        return {}

    def _tick_daily_reset(self, now: float) -> Dict[str, Any]:
        reset_at = self.cfg.get("reset_at")
        if not reset_at or not _HHMM_RE.match(str(reset_at)):
            return {}
        dt = datetime.fromtimestamp(now)
        today = dt.strftime("%Y-%m-%d")
        if self.last_reset_day == today:
            return {}
        # First tick of a new day is not the trigger — the configured time is.
        # Before it, the previous day's tally is still the live one.
        if dt.strftime("%H:%M") < str(reset_at):
            return {}
        self.last_reset_day = today
        return self._cmd_counter("reset", None)

    # Serialisation

    def to_device_list_entry(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee,
            "friendly_name": self.friendly_name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "type": "worker",
            "protocol": "virtual",
            "available": True,
            "state": dict(self.state),
            "last_seen": self.last_seen,
            "capabilities": self.capabilities.get_capabilities(),
        }

    def display(self) -> str:
        """One-line current value, for the workers page and rule sentences."""
        t, s = self.type, self.state
        if t == TYPE_TIMER:
            if s.get("value") != ON:
                return "idle"
            secs = int(s.get("remaining_s") or 0)
            if secs >= 3600:
                return f"{secs // 3600}h {secs % 3600 // 60}m left"
            return f"{secs // 60}m left" if secs >= 60 else f"{secs}s left"
        if t == TYPE_MARKER:
            if s.get("marked") != ON:
                return "never"
            mins = int(s.get("age_minutes") or 0)
            if mins < 60:
                return f"{mins}m ago"
            if mins < 1440:
                return f"{mins // 60}h ago"
            return f"{mins // 1440}d ago"
        if t == TYPE_NUMBER:
            unit = self.cfg.get("unit") or ""
            value = s.get("value")
            shown = int(value) if isinstance(value, float) and value.is_integer() else value
            return f"{shown}{unit}"
        return str(s.get("value", ""))

    def describe(self) -> Dict[str, Any]:
        """Everything the API returns for one worker."""
        return {
            **{k: v for k, v in self.cfg.items()},
            "ieee": self.ieee,
            "state": dict(self.state),
            "display": self.display(),
            "primary": WORKER_TYPES[self.type]["primary"],
            "commands": self.get_control_commands(),
            "last_seen": self.last_seen,
        }

    def persist_state(self) -> Dict[str, Any]:
        """The part of live state worth surviving a restart."""
        out: Dict[str, Any] = {}
        if self.type == TYPE_TIMER:
            out["expires_at"] = self.expires_at
        elif self.type == TYPE_MARKER:
            out["marked_at"] = self.marked_at
        else:
            out["value"] = self.state.get("value")
        if self.type == TYPE_COUNTER:
            out["last_reset_day"] = self.last_reset_day
        return out

    def restore_state(self, saved: Dict[str, Any]) -> None:
        """
        Reinstate saved state, honouring the worker's `restore` setting.

        A timer is restored against its deadline rather than its duration: one
        that expired while the process was down is simply over, and one that
        has not is still counting. Restoring the duration instead would silently
        extend every timer by however long the restart took.
        """
        if not self.cfg.get("restore", True):
            return
        now = time.time()
        if self.type == TYPE_TIMER:
            expires = _num(saved.get("expires_at"))
            if expires and expires > now:
                self.expires_at = expires
                self.apply({"value": ON, "remaining_s": int(expires - now)})
            return
        if self.type == TYPE_MARKER:
            marked = _num(saved.get("marked_at"))
            if marked:
                self.marked_at = marked
                self.apply({"age_minutes": int((now - marked) // 60),
                            "marked": ON, "last_at": marked})
            return
        if "value" not in saved:
            return
        value = saved["value"]
        if self.type == TYPE_COUNTER:
            self.last_reset_day = saved.get("last_reset_day")
            self.apply({"value": self._clamp_counter(_num(value, 0) or 0)})
        elif self.type == TYPE_NUMBER:
            self.apply({"value": self._clamp_number(_num(value, 0))})
        elif self.type == TYPE_MODE:
            if value in (self.cfg.get("options") or []):
                self.apply({"value": value})
        elif self.type == TYPE_BOOLEAN:
            self.apply({"value": ON if _truthy(value) else OFF})


# Manager

class WorkerManager:
    """
    Owns every worker: validation, persistence, the shared tick, and telling
    the automation engine when a value moved.

    `evaluator` is the engine's `evaluate` coroutine. Publishing awaits it so
    that a rule which sets a worker and a rule which watches that worker run in
    one chain the engine can measure the depth of — see the chain guard in
    modules/automation.py.
    """

    def __init__(self,
                 evaluator: Optional[Callable[[str, Dict[str, Any]],
                                              Awaitable[None]]] = None,
                 event_emitter: Optional[Callable[[str, Dict[str, Any]],
                                                  Awaitable[None]]] = None,
                 data_file: str = DATA_FILE) -> None:
        self._evaluator = evaluator
        self._event_emitter = event_emitter
        self._data_file = data_file
        self.workers: Dict[str, WorkerDevice] = {}
        self._task: Optional[asyncio.Task] = None
        self._dirty = False
        self._load()

    # Lifecycle

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("Workers started (%d configured)", len(self.workers))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._dirty:
            self._save()

    def set_evaluator(self, evaluator: Callable) -> None:
        self._evaluator = evaluator

    def automation_devices(self) -> Dict[str, WorkerDevice]:
        """The registry the automation engine merges in."""
        return {w.ieee: w for w in self.workers.values() if w.enabled}

    def get(self, worker_id: str) -> Optional[WorkerDevice]:
        return self.workers.get((worker_id or "").lower())

    def by_ieee(self, ieee: str) -> Optional[WorkerDevice]:
        if not is_worker(ieee):
            return None
        return self.get(ieee[len(WORKER_IEEE_PREFIX):])

    def list(self) -> List[Dict[str, Any]]:
        return sorted((w.describe() for w in self.workers.values()),
                      key=lambda w: (w.get("name") or "").lower())

    # Publication

    async def _publish(self, worker: WorkerDevice,
                       changed: Dict[str, Any]) -> None:
        """Persist, tell the engine, tell the browser."""
        if not changed:
            return
        self._dirty = True
        self._save()
        if self._evaluator:
            try:
                await self._evaluator(worker.ieee, changed)
            except Exception as e:                              # noqa: BLE001
                logger.warning("Evaluating %s failed: %s", worker.ieee, e)
        if self._event_emitter:
            try:
                await self._event_emitter("worker_updated", {
                    "id": worker.id, "ieee": worker.ieee,
                    "state": dict(worker.state),
                    "display": worker.display(),
                    "changed": changed,
                })
            except Exception as e:                              # noqa: BLE001
                logger.debug("Worker event emit failed: %s", e)

    async def command(self, worker_id: str, command: str,
                      value: Any = None) -> Dict[str, Any]:
        """Manual actuation, from the workers page or the API."""
        worker = self.get(worker_id)
        if not worker:
            return {"success": False, "error": f"No worker '{worker_id}'"}
        result = await worker.send_command(command, value)
        if result.get("success"):
            logger.info("[workers] %s %s=%s", worker.id, command, value)
        return result

    # Tick

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:                              # noqa: BLE001
                logger.error("Worker tick failed: %s", e)

    async def tick(self) -> Dict[str, Dict[str, Any]]:
        """Advance every clock-driven worker and publish what moved."""
        now = time.time()
        moved: Dict[str, Dict[str, Any]] = {}
        for worker in list(self.workers.values()):
            if not worker.enabled:
                continue
            changed = worker.tick(now)
            if changed:
                moved[worker.id] = changed
                await self._publish(worker, changed)
        return moved

    # CRUD

    def _validate(self, data: Dict[str, Any],
                  existing_id: Optional[str] = None) -> Optional[str]:
        wtype = data.get("type")
        if wtype not in WORKER_TYPES:
            return f"Unknown worker type '{wtype}'"

        name = (data.get("name") or "").strip()
        if not name:
            return "A worker needs a name"
        if len(name) > MAX_NAME_LEN:
            return f"Name must be {MAX_NAME_LEN} characters or fewer"

        wid = (data.get("id") or _slug(name)).lower()
        if not _ID_RE.match(wid):
            return ("Id must be lower-case letters, digits, underscore or "
                    "hyphen")
        if wid != existing_id and wid in self.workers:
            return f"A worker called '{wid}' already exists"
        if existing_id is None and len(self.workers) >= MAX_WORKERS:
            return f"Maximum {MAX_WORKERS} workers"

        if wtype == TYPE_MODE:
            opts = [str(o).strip() for o in (data.get("options") or [])
                    if str(o).strip()]
            if len(opts) < 2:
                return "A mode worker needs at least two options"
            if len(opts) > MAX_MODE_OPTIONS:
                return f"A mode worker allows at most {MAX_MODE_OPTIONS} options"
            if len({o.lower() for o in opts}) != len(opts):
                return "Mode options must be distinct"

        if wtype in (TYPE_NUMBER, TYPE_COUNTER):
            lo, hi = _num(data.get("min")), _num(data.get("max"))
            if lo is not None and hi is not None and lo >= hi:
                return "Minimum must be below maximum"

        if wtype == TYPE_TIMER:
            default = _num(data.get("default_seconds"), 3600) or 3600
            if not (0 < default <= MAX_TIMER_SECONDS):
                return f"Default duration must be 1-{MAX_TIMER_SECONDS} seconds"

        if wtype == TYPE_COUNTER:
            reset_at = data.get("reset_at")
            if reset_at and not _HHMM_RE.match(str(reset_at)):
                return "Daily reset must be HH:MM"

        return None

    def _normalise(self, data: Dict[str, Any],
                   existing_id: Optional[str] = None) -> Dict[str, Any]:
        """Config with every type-specific field defaulted and coerced."""
        wtype = data["type"]
        cfg: Dict[str, Any] = {
            "id": existing_id or (data.get("id") or _slug(data["name"])).lower(),
            "name": data["name"].strip(),
            "type": wtype,
            "icon": data.get("icon") or WORKER_TYPES[wtype]["icon"],
            "description": (data.get("description") or "").strip()[:200],
            "enabled": bool(data.get("enabled", True)),
            "restore": bool(data.get("restore", True)),
        }
        if wtype == TYPE_BOOLEAN:
            cfg["initial"] = ON if _truthy(data.get("initial")) else OFF
        elif wtype == TYPE_MODE:
            cfg["options"] = [str(o).strip() for o in data.get("options") or []
                              if str(o).strip()]
            initial = data.get("initial")
            cfg["initial"] = initial if initial in cfg["options"] else cfg["options"][0]
        elif wtype == TYPE_TIMER:
            cfg["default_seconds"] = int(_num(data.get("default_seconds"), 3600) or 3600)
        elif wtype == TYPE_COUNTER:
            cfg["step"] = _num(data.get("step"), 1) or 1
            cfg["min"] = int(_num(data.get("min"), 0) or 0)
            cfg["max"] = int(_num(data.get("max"), MAX_COUNTER) or MAX_COUNTER)
            cfg["reset_at"] = data.get("reset_at") or None
        elif wtype == TYPE_NUMBER:
            cfg["min"] = _num(data.get("min"), 0.0)
            cfg["max"] = _num(data.get("max"), 100.0)
            cfg["step"] = _num(data.get("step"), 1.0) or 1.0
            cfg["unit"] = (data.get("unit") or "").strip()[:12]
            cfg["initial"] = _num(data.get("initial"), cfg["min"])
        return cfg

    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        err = self._validate(data)
        if err:
            return {"success": False, "error": err}
        cfg = self._normalise(data)
        worker = WorkerDevice(cfg, publisher=self._publish)
        self.workers[cfg["id"]] = worker
        self._save()
        logger.info("[workers] created %s (%s)", cfg["id"], cfg["type"])
        return {"success": True, "worker": worker.describe()}

    def update(self, worker_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Replace a worker's configuration, keeping its live value where the new
        configuration can still hold it. Changing a mode's options is the case
        that matters: a house sitting in "holiday" when "holiday" is removed
        has to land somewhere, and the first option is the only defensible
        choice.
        """
        worker = self.get(worker_id)
        if not worker:
            return {"success": False, "error": f"No worker '{worker_id}'"}
        merged = {**worker.cfg, **data, "type": worker.type, "id": worker.id}
        err = self._validate(merged, existing_id=worker.id)
        if err:
            return {"success": False, "error": err}

        cfg = self._normalise(merged, existing_id=worker.id)
        carried = worker.persist_state()
        replacement = WorkerDevice(cfg, publisher=self._publish)
        replacement.restore_state(carried)
        self.workers[worker.id] = replacement
        self._save()
        logger.info("[workers] updated %s", worker.id)
        return {"success": True, "worker": replacement.describe()}

    def delete(self, worker_id: str) -> Dict[str, Any]:
        worker = self.get(worker_id)
        if not worker:
            return {"success": False, "error": f"No worker '{worker_id}'"}
        del self.workers[worker.id]
        self._save()
        logger.info("[workers] deleted %s", worker.id)
        # Rules still pointing at it are the caller's to report; the engine
        # disables a rule whose source has gone the first time it evaluates.
        return {"success": True, "id": worker.id, "ieee": worker.ieee}

    def usage(self, rules: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Which rules trigger on, or command, each worker.

        Deleting a worker that three rules depend on should not be a silent
        act, so the page can show the count before asking.
        """
        found: Dict[str, Dict[str, Any]] = {
            w.id: {"triggers": [], "targets": []} for w in self.workers.values()
        }

        def walk(steps, rule):
            for step in steps or []:
                target = step.get("target_ieee")
                worker = self.by_ieee(target) if target else None
                if worker and rule["id"] not in found[worker.id]["targets"]:
                    found[worker.id]["targets"].append(rule["id"])
                for key in ("then_steps", "else_steps", "accept_steps"):
                    walk(step.get(key), rule)
                for branch in step.get("branches") or []:
                    walk(branch, rule)

        for rule in rules or []:
            worker = self.by_ieee(rule.get("source_ieee", ""))
            if worker:
                found[worker.id]["triggers"].append(rule["id"])
            # A trigger condition may read a worker other than the source.
            for cond in rule.get("conditions") or []:
                cw = self.by_ieee(cond.get("ieee", "") or "")
                if cw and rule["id"] not in found[cw.id]["triggers"]:
                    found[cw.id]["triggers"].append(rule["id"])
            for prereq in rule.get("prerequisites") or []:
                pw = self.by_ieee(prereq.get("ieee", "") or "")
                if pw and rule["id"] not in found[pw.id]["triggers"]:
                    found[pw.id]["triggers"].append(rule["id"])
            walk(rule.get("then_sequence"), rule)
            walk(rule.get("else_sequence"), rule)
        return found

    # Persistence

    def _load(self) -> None:
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file, "r") as f:
                data = json.load(f)
        except Exception as e:                                  # noqa: BLE001
            logger.error("Failed to load workers: %s", e)
            return

        saved_state = data.get("state") or {}
        for cfg in data.get("workers") or []:
            if cfg.get("type") not in WORKER_TYPES or not cfg.get("id"):
                logger.warning("Skipping unreadable worker: %s", cfg)
                continue
            try:
                worker = WorkerDevice(self._normalise(cfg, cfg["id"]),
                                      publisher=self._publish)
                worker.restore_state(saved_state.get(cfg["id"]) or {})
                self.workers[worker.id] = worker
            except Exception as e:                              # noqa: BLE001
                logger.error("Failed to restore worker %s: %s", cfg.get("id"), e)
        logger.info("Loaded %d worker(s)", len(self.workers))

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
        try:
            payload = {
                "workers": [w.cfg for w in self.workers.values()],
                "state": {w.id: w.persist_state() for w in self.workers.values()},
            }
            with open(self._data_file, "w") as f:
                json.dump(payload, f, indent=2)
            self._dirty = False
        except Exception as e:                                  # noqa: BLE001
            logger.error("Failed to save workers: %s", e)


# Singleton, wired in main.py and read by routes/worker_routes.py.

_manager: Optional[WorkerManager] = None


def set_worker_manager(manager: Optional[WorkerManager]) -> None:
    global _manager
    _manager = manager


def get_worker_manager() -> Optional[WorkerManager]:
    return _manager
