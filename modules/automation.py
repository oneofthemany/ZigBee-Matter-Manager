"""
Automation engine — evaluates device state changes and fires recursive action
sequences on transitions.

Step types: command, delay, wait_for, condition, if_then_else, parallel.
Conditions support AND/OR/NOT across triggers and prerequisites, duration
("for N seconds") checks, and edge-triggered zone crossings.

Persistence: ./data/automations.json. Hook: core.py _debounced_device_update.
See docs/automations.md.
"""

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from collections import deque
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.automation")

MAX_RULES_PER_DEVICE = 10
MAX_CONDITIONS_PER_RULE = 5
MAX_PREREQUISITES_PER_RULE = 8
MAX_STEPS_PER_SEQUENCE = 15
MAX_NESTING_DEPTH = 4
DATA_FILE = "./data/automations.json"
DEFAULT_COOLDOWN = 5
WAIT_FOR_POLL_INTERVAL = 2

# How many rules may fire in one causal chain. A worker (modules/workers.py) is
# both a target a rule can command and a source another rule triggers on, so
# setting one re-enters evaluation; two rules that set each other's workers
# would recurse without end. Cooldowns cannot catch that — each hop is a
# different rule firing once, which is exactly what a cooldown permits.
MAX_CHAIN_DEPTH = 4
# A ContextVar rather than an attribute because sequences run as tasks: a task
# inherits a copy of the context it was created in, so the depth travels down
# the chain without travelling sideways into unrelated rules firing at once.
_chain_depth: ContextVar[int] = ContextVar("zmm_automation_chain_depth",
                                           default=0)

# Virtual source for clock-driven rules ("play radio at 07:00"), which fire from
# the time-boundary scheduler rather than any device update.
TIME_SOURCE = "__time__"
# Condition types that are time/astronomy based (no device attribute to watch).
TEMPORAL_TYPES = ("time_window", "sun", "time")

# A presence user's location lives in one attribute: "home", "away", "unknown",
# or a place id. Zone conditions are edge-triggered, so they need the value moved
# *from* — see AutomationEngine._last_values and docs/automations.md.
ZONE_ATTR = "place"
# The absence of a location rather than one you can stand in, so never entered or
# left: leaving "the shops" for "away" is a leave for the shops, not an enter.
ZONE_NOWHERE = frozenset({"away", "unknown", "", None})
ZONE_EVENTS = ("enter", "leave")
# Matches any real location: "at a place, whichever one".
ZONE_ANY = "any"
# A zone may group several places ("work" = two offices). Capped at the number
# of places a household can define, since grouping them all is what ZONE_ANY is.
MAX_PLACES_PER_ZONE = 16

# Attributes that report something happening rather than something being true:
# a press, a scene recall. They stay in device state after the moment has gone,
# so a condition on one matches only on the update that carries it — otherwise
# a button pressed this morning would still read "pressed" when a second
# device's update, or a clock boundary, re-evaluates the rule this evening.
EVENT_ATTRS = frozenset({"action", "click", "button_action", "event", "scene",
                         "command"})

# A condition may be a group — {"type": "group", "condition_logic": "and"|"or",
# "conditions": [...]} — which its siblings see as one condition: that is what
# "(A and B) or C" needs. One level deep: a group holds plain conditions.
MAX_CONDITIONS_PER_GROUP = 5

# How long after a sustain's deadline its re-check runs. The deadline itself is
# exact; the slack only has to cover a timer firing a hair early.
SUSTAIN_RECHECK_SLACK = 0.25

# What a rule does when it fires while its last sequence is still running.
# restart — cancel it and start again (the default, and what every rule did
#           before run modes existed); single — let it finish, ignore the new
#           one; queued — run the new one after it; parallel — run both.
RUN_MODES = ("restart", "single", "queued", "parallel")
DEFAULT_RUN_MODE = "restart"
# Live runs one rule may hold under queued/parallel, so a chatty trigger cannot
# pile up sequences without end.
MAX_RULE_RUNS = 10


def iter_leaf_conditions(conditions):
    """Every plain condition in a condition list, looking inside groups.

    Anything asking which devices, attributes or clock times a rule reads must
    ask the leaves: a group reads nothing itself.
    """
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "group":
            yield from iter_leaf_conditions(c.get("conditions"))
        else:
            yield c

OPERATORS = {
    "eq":  lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt":  lambda a, b: float(a) > float(b),
    "lt":  lambda a, b: float(a) < float(b),
    "gte": lambda a, b: float(a) >= float(b),
    "lte": lambda a, b: float(a) <= float(b),
    "in":  lambda a, b: True,  # handled specially in _evaluate_condition
    "nin": lambda a, b: True,  # handled specially in _evaluate_condition
}

VALID_COMMANDS = {
    "on", "off", "toggle", "brightness", "color_temp",
    # A colour as [hue 0-360, saturation 0-100]. The device layer has always
    # dispatched it; without it here a rule could never carry one, so a light
    # could not be used to say anything beyond on or off.
    "hs_color",
    "open", "close", "stop", "position", "temperature",
    # A lock's full set. `unlatch` retracts the latch as well as the bolt —
    # on a front door it is the difference between unlocking and actually
    # letting somebody in — and the lock drivers have always dispatched it.
    # Absent here it was dropped twice over: add_rule rejected the step, and
    # the swarm resolver filtered it out before an offer could be built.
    "lock", "unlock", "unlatch", "lock_n_go",
    # Worker commands — see modules/workers.py. A worker is an ordinary
    # command-step target, so without these the step would be rejected at
    # validation and a rule could never set one.
    "set", "increment", "decrement", "reset", "start", "extend", "cancel",
    "mark",
}

FLAT_STEP_TYPES = {"command", "delay", "wait_for", "condition", "media", "request",
                   "offer"}

# An offer is a message that can act: it asks somebody, and runs a stored
# sequence only if they say yes. Pending offers are held in memory and are
# deliberately not persisted — an offer to turn the air conditioning on is a
# question about right now, and one that survived a restart to fire hours later
# would be worse than one that quietly lapsed.
MAX_PENDING_OFFERS = 50
DEFAULT_OFFER_EXPIRY = 3600
MAX_OFFER_EXPIRY = 86400
BRANCHING_STEP_TYPES = {"if_then_else", "parallel"}
ALL_STEP_TYPES = FLAT_STEP_TYPES | BRANCHING_STEP_TYPES


class AutomationEngine:

    def __init__(self, device_registry_getter: Callable[[], Dict],
                 friendly_names_getter: Callable[[], Dict],
                 event_emitter: Optional[Callable] = None,
                 group_manager_getter: Optional[Callable] = None,
                 matter_device_getter: Optional[Callable] = None):
        self._get_devices = device_registry_getter
        self._get_names = friendly_names_getter
        self._event_emitter = event_emitter
        self._get_group_manager = group_manager_getter
        self._get_matter_devices = matter_device_getter or (lambda: {})
        # Providers that come up after the engine (e.g. Nuki locks) register a
        # getter returning {ieee: device-like} with .state, .friendly_name and
        # async send_command().
        self._extra_device_getters: List[Callable[[], Dict]] = []
        # Injected post-construction via set_media_service_getter, since the media
        # service is built after the engine.
        self._get_media_service: Optional[Callable] = None

        self.rules: List[Dict[str, Any]] = []
        self._source_index: Dict[str, List[str]] = {}
        self._cooldowns: Dict[str, float] = {}
        self._sustain_tracker: Dict[str, float] = {}
        # rule_id -> the task that re-evaluates the rule when its soonest
        # pending sustain runs out. See _schedule_sustain_recheck.
        self._sustain_timers: Dict[str, asyncio.Task] = {}
        self._rule_states: Dict[str, Optional[str]] = {}
        # Per source device, its state as of the previous evaluation: by the time
        # evaluate() runs device.state already holds the new value, so zone
        # conditions need the old one remembered here.
        self._last_values: Dict[str, Dict[str, Any]] = {}
        # Accepted offers' sequences, keyed "offer:<token>".
        self._running_sequences: Dict[str, asyncio.Task] = {}
        # rule_id -> its live runs, oldest first: the running one plus any
        # queued behind it (or several at once under parallel).
        self._rule_runs: Dict[str, List[asyncio.Task]] = {}
        self._time_scheduler_task: Optional[asyncio.Task] = None

        # token -> pending offer. See MAX_PENDING_OFFERS.
        self._offers: Dict[str, Dict[str, Any]] = {}

        self._trace_log: List[Dict[str, Any]] = []
        self._max_trace_entries = 200
        # Chatty rules churn the 200-entry shared buffer in minutes, which left a
        # rule-filtered trace holding only its newest entry or two.
        self._trace_by_rule: Dict[str, deque] = {}
        self._max_trace_per_rule = 100

        self._stats = {
            "evaluations": 0, "matches": 0, "transitions": 0,
            "executions": 0, "execution_successes": 0,
            "execution_failures": 0, "errors": 0,
            # Chains cut at MAX_CHAIN_DEPTH. Present from the start so the
            # stats shape does not change the first time a loop is stopped.
            "chain_stops": 0,
        }

        self._load_rules()
        logger.info(f"Automation engine initialised with {len(self.rules)} rule(s)")

    def set_media_service_getter(self, getter: Callable) -> None:
        """Wire the media service in after construction (see __init__)."""
        self._get_media_service = getter

    def add_device_getter(self, getter: Callable) -> None:
        """Merge another device registry into the engine's view (see __init__).
        Idempotent-unsafe — callers register once."""
        self._extra_device_getters.append(getter)

    def _get_all_devices(self) -> Dict:
        """Merged view of Zigbee + Matter (+ extra provider) devices."""
        merged = dict(self._get_devices())
        merged.update(self._get_matter_devices())
        for getter in self._extra_device_getters:
            try:
                merged.update(getter())
            except Exception as e:
                logger.debug(f"Extra device getter failed: {e}")
        return merged

    def _get_all_names(self) -> Dict:
        """Merged friendly names: Zigbee names + provider friendly_name attrs."""
        names = dict(self._get_names())
        extra: Dict = dict(self._get_matter_devices())
        for getter in self._extra_device_getters:
            try:
                extra.update(getter())
            except Exception:
                pass
        for ieee, dev in extra.items():
            if ieee not in names:
                names[ieee] = getattr(dev, 'friendly_name', ieee)
        return names


    # TIME SCHEDULER

    async def start(self):
        """Start background time-boundary scheduler and set initial rule states."""
        self._time_scheduler_task = asyncio.create_task(self._time_boundary_loop())
        logger.info("Automation time scheduler started")

    async def stop(self):
        """Stop background tasks."""
        for task in list(self._sustain_timers.values()):
            task.cancel()
        self._sustain_timers.clear()
        if self._time_scheduler_task:
            self._time_scheduler_task.cancel()
            try:
                await self._time_scheduler_task
            except asyncio.CancelledError:
                pass

    async def _time_boundary_loop(self):
        """
        Runs every 30s. At each time-window boundary (time_from / time_to)
        re-evaluates all rules that contain time_window conditions so they
        fire at the correct clock time rather than waiting for the next
        incidental device update.
        """
        import datetime
        last_minute_checked = None

        # Evaluate on startup so initial state is set correctly
        await asyncio.sleep(2)  # Brief delay to let devices load
        # Also buys the zone baseline: without it the first place change after a
        # restart has no "from" value, so a mid-afternoon restart misses that
        # day's "leaves work".
        self._seed_last_values()
        await self._evaluate_timed_rules()

        while True:
            try:
                await asyncio.sleep(30)
                now_dt = datetime.datetime.now()
                now_hhmm = now_dt.strftime("%H:%M")

                if now_hhmm == last_minute_checked:
                    continue
                last_minute_checked = now_hhmm

                # Collect all boundary times across all enabled rules
                boundaries: set = set()
                for rule in self.rules:
                    if not rule.get("enabled", True):
                        continue
                    boundaries.update(self._rule_temporal_boundaries(rule))

                if now_hhmm in boundaries:
                    logger.info(f"[AUTO] Time boundary hit {now_hhmm} — evaluating timed rules")
                    await self._evaluate_timed_rules(boundary_hhmm=now_hhmm)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AUTO] Time boundary loop error: {e}")

    def _seed_last_values(self):
        """Snapshot the current state of every rule source as the zone baseline.

        Presence state is restored from disk at startup, so this recovers where
        each person was before the restart rather than starting blind.
        """
        try:
            devices = self._get_all_devices()
        except Exception as e:                          # noqa: BLE001
            logger.warning(f"[AUTO] zone baseline skipped: {e}")
            return
        for src in self._source_index:
            dev = devices.get(src)
            state = getattr(dev, "state", None) if dev else None
            if state:
                self._last_values[src] = dict(state)

    @staticmethod
    def _watched_attributes(conditions) -> set:
        """Source attributes a rule's conditions read.

        Temporal conditions watch the clock rather than the device, and zone
        conditions watch the one attribute a person's location lives in.
        """
        watched = set()
        for c in iter_leaf_conditions(conditions):
            ctype = c.get("type", "attribute")
            if ctype in TEMPORAL_TYPES:
                continue
            if ctype == "zone":
                watched.add(ZONE_ATTR)
            elif c.get("attribute"):
                watched.add(c["attribute"])
        return watched

    @staticmethod
    def _has_zone(conditions) -> bool:
        return any(c.get("type") == "zone" for c in iter_leaf_conditions(conditions))

    @staticmethod
    def _condition_logic(rule) -> str:
        """'and' (default) or 'or' — how a rule joins its trigger conditions.
        Rules saved before OR support carry no key, so they stay AND."""
        return "or" if str(rule.get("condition_logic", "and")).lower() == "or" else "and"

    @staticmethod
    def _cond_source(rule, cond) -> Optional[str]:
        """The device a trigger condition reads, or None for a clock condition.

        A condition may name its own device in `ieee`; without one it reads the
        rule's source, which is how every rule saved before multi-source reads.
        """
        if cond.get("type", "attribute") in TEMPORAL_TYPES:
            return None
        return cond.get("ieee") or rule.get("source_ieee")

    @classmethod
    def rule_sources(cls, rule) -> List[str]:
        """Every device whose updates can move this rule — its source first,
        then each other device a trigger condition names."""
        out: List[str] = []
        candidates = [rule.get("source_ieee")] + [
            cls._cond_source(rule, c)
            for c in iter_leaf_conditions(rule.get("conditions"))]
        for src in candidates:
            if src and src not in out:
                out.append(src)
        return out

    def _rule_temporal_boundaries(self, rule) -> set:
        """HH:MM strings at which this rule's temporal conditions can change state."""
        b: set = set()
        for c in [*iter_leaf_conditions(rule.get("conditions")),
                  *rule.get("prerequisites", [])]:
            ct = c.get("type")
            if ct == "time_window":
                b.add(c.get("time_from"))
                b.add(c.get("time_to"))
            elif ct == "time":
                # Alarm: evaluate at the fire minute and the minute after
                # (so the rule resets unmatched and can fire again).
                at = c.get("at")
                if at:
                    b.add(at)
                    b.add(self._plus_one_minute(at))
            elif ct == "sun":
                b.update(self._sun_boundary_hhmm(c))
        b.discard(None)
        return b

    async def _evaluate_timed_rules(self, boundary_hhmm: str = None):
        """
        Evaluate all enabled rules that have at least one time_window condition.
        Uses empty changed_data since time_window conditions don't need attribute data.
        Runs the same state-machine transition logic as evaluate().

        boundary_hhmm: when set (called from the boundary loop), only rules whose
        own temporal boundaries include that minute are evaluated — otherwise every
        timed rule gets re-evaluated (and traces NO_MATCH) at every other rule's
        boundary. None (startup) evaluates all timed rules.
        """
        now = time.time()
        devices = self._get_all_devices()
        names = self._get_all_names()

        for rule in self.rules:
            if not rule.get("enabled", True):
                continue

            has_tw_cond = any(
                c.get("type") in TEMPORAL_TYPES
                for c in iter_leaf_conditions(rule.get("conditions"))
            )
            has_tw_prereq = any(
                p.get("type") in TEMPORAL_TYPES for p in rule.get("prerequisites", [])
            )
            if not (has_tw_cond or has_tw_prereq):
                continue

            if boundary_hhmm and boundary_hhmm not in self._rule_temporal_boundaries(rule):
                continue

            # A clock tick carries no place change, so a zone condition can only
            # read FAIL here — and firing this rule's ELSE off that would invent
            # a departure nobody made. Zone rules run from device updates only.
            if self._has_zone(rule.get("conditions", [])):
                continue

            # No device updated: every device condition reads its device as it
            # stands, with nothing marked as changed — a clock tick is not a
            # button press, whichever device the condition names.
            self._evaluate_rule(rule, devices, names, now,
                                self._condition_view(rule, devices))

    # PERSISTENCE

    def _load_rules(self):
        if not os.path.exists(DATA_FILE):
            self.rules = []
            self._rebuild_index()
            return
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            self.rules = data.get("rules", [])
            migrated = self._migrate_rules()
            if migrated:
                self._save_rules()
            self._rebuild_index()
            logger.info(f"Loaded {len(self.rules)} automation rule(s)")
        except Exception as e:
            logger.error(f"Failed to load automations: {e}")
            self.rules = []
            self._rebuild_index()

    def _migrate_rules(self) -> int:
        count = 0
        for rule in self.rules:
            if "name" not in rule:
                rule["name"] = ""
            if "threshold" in rule and "conditions" not in rule:
                rule["conditions"] = [rule.pop("threshold")]
                count += 1
            if "action" in rule and "then_sequence" not in rule:
                action = rule.pop("action")
                target = rule.pop("target_ieee", "")
                steps = []
                delay = action.get("delay", 0) or 0
                if delay > 0:
                    steps.append({"type": "delay", "seconds": delay})
                steps.append({
                    "type": "command",
                    "target_ieee": target,
                    "command": action.get("command", "on"),
                    "value": action.get("value"),
                    "endpoint_id": action.get("endpoint_id"),
                })
                rule["then_sequence"] = steps
                count += 1
            for key in ("then_sequence", "else_sequence", "prerequisites"):
                if key not in rule:
                    rule[key] = []
        return count

    def _save_rules(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        try:
            with open(DATA_FILE, "w") as f:
                json.dump({"rules": self.rules}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save automations: {e}")

    def _rebuild_index(self):
        self._source_index.clear()
        for rule in self.rules:
            # Indexed under every trigger device, so an update on any of them
            # re-evaluates the rule — that is what makes AND/OR span devices.
            for src in self.rule_sources(rule):
                self._source_index.setdefault(src, []).append(rule["id"])

    def _disable_broken_rule(self, rule_id: str, reason: str):
        """
        Disable a rule whose configuration is permanently broken (e.g. it
        targets a group that no longer exists) and raise a user-visible
        alert so it can be fixed instead of failing silently forever.
        """
        rule = next((r for r in self.rules if r.get("id") == rule_id), None)
        if not rule or not rule.get("enabled", True):
            return
        rule["enabled"] = False
        rule["disabled_reason"] = reason
        self._save_rules()

        name = rule.get("name") or rule_id
        self._trace(rule_id, "engine", "DISABLED",
                    f"Automation '{name}' disabled: {reason}", level="WARNING")
        try:
            from modules.app_alerts import raise_alert
            raise_alert(
                severity="warning",
                source="automation",
                title=f"Automation '{name}' disabled",
                message=f"It was disabled because {reason}. "
                        "Fix its target in the Automations page and re-enable it.",
                dedupe_key=f"automation:disabled:{rule_id}",
                data={"rule_id": rule_id},
            )
        except Exception as e:
            logger.debug(f"Could not raise alert for disabled rule: {e}")

    # TRACING

    def _trace(self, rule_id, phase, result, message, level="INFO", **extra):
        entry = {
            "timestamp": time.time(), "rule_id": rule_id,
            "phase": phase, "result": result, "message": message,
            "level": level, **extra,
        }
        self._trace_log.append(entry)
        if len(self._trace_log) > self._max_trace_entries:
            self._trace_log = self._trace_log[-self._max_trace_entries:]
        if rule_id not in self._trace_by_rule:
            self._trace_by_rule[rule_id] = deque(maxlen=self._max_trace_per_rule)
        self._trace_by_rule[rule_id].append(entry)

        log_msg = f"[AUTO {rule_id}] {message}"
        if level == "ERROR": logger.error(log_msg)
        elif level == "WARNING": logger.warning(log_msg)
        elif level == "INFO": logger.info(log_msg)
        else: logger.debug(log_msg)

        if self._event_emitter:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_emitter("automation_trace", entry))
            except RuntimeError:
                pass

    def get_trace_log(self, rule_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if rule_id is not None:
            return list(self._trace_by_rule.get(rule_id, ()))
        return list(self._trace_log)

    # VALIDATION (recursive)

    def _validate_conditions(self, conds: List[Dict], depth: int = 0) -> Optional[str]:
        import re
        if not isinstance(conds, list) or not conds:
            return ("conditions must be a non-empty list" if depth == 0
                    else "a group needs at least one condition")
        if depth == 0 and len(conds) > MAX_CONDITIONS_PER_RULE:
            return f"Max {MAX_CONDITIONS_PER_RULE} conditions"
        if depth > 0 and len(conds) > MAX_CONDITIONS_PER_GROUP:
            return f"Max {MAX_CONDITIONS_PER_GROUP} conditions in a group"
        for i, c in enumerate(conds):
            if not isinstance(c, dict):
                return f"Condition {i+1} must be an object"
            ctype = c.get("type", "attribute")
            if ctype == "group":
                # One level: "(A and B) or C" covers what a household writes,
                # and a tree of trees is harder to read than two rules.
                if depth > 0:
                    return f"Condition {i+1}: a group can't contain another group"
                logic = str(c.get("condition_logic", "and") or "and").lower()
                if logic not in ("and", "or"):
                    return (f"Condition {i+1} (group): condition_logic must be "
                            f"'and' or 'or'")
                c["condition_logic"] = logic
                c.pop("ieee", None)            # a group reads no device itself
                err = self._validate_conditions(c.get("conditions"), depth + 1)
                if err:
                    return f"Group {i+1}: {err}"
                continue
            # The device this condition reads, when it is not the rule's source.
            # A clock condition reads no device, so an ieee on one is dropped.
            if "ieee" in c:
                src = str(c.get("ieee") or "").strip()
                if ctype in TEMPORAL_TYPES or not src:
                    c.pop("ieee", None)
                elif src.startswith("group:"):
                    return (f"Condition {i+1}: a group can't trigger a rule — it "
                            f"never reports a change of its own. Check it with a "
                            f"prerequisite instead")
                elif src == TIME_SOURCE:
                    return f"Condition {i+1}: '{TIME_SOURCE}' is not a device"
                else:
                    c["ieee"] = src
            if ctype == "time_window":
                for f in ("time_from", "time_to"):
                    if f not in c:
                        return f"Condition {i+1} (time_window) missing '{f}'"
                    if not re.match(r"^\d{2}:\d{2}$", str(c[f])):
                        return f"Condition {i+1} '{f}' must be HH:MM"
            elif ctype == "time":
                if not re.match(r"^\d{2}:\d{2}$", str(c.get("at", ""))):
                    return f"Condition {i+1} (alarm) 'at' must be HH:MM"
            elif ctype == "sun":
                err = self._validate_sun(c, f"Condition {i+1}")
                if err:
                    return err
            elif ctype == "zone":
                if c.get("event") not in ZONE_EVENTS:
                    return (f"Condition {i+1} (zone) 'event' must be "
                            f"'enter' or 'leave'")
                raw = c.get("place")
                places = raw if isinstance(raw, (list, tuple)) else [raw]
                places = [str(p or "").strip() for p in places]
                places = [p for p in places if p]
                if not places:
                    return f"Condition {i+1} (zone) needs a place"
                if len(places) > MAX_PLACES_PER_ZONE:
                    return (f"Condition {i+1} (zone): max "
                            f"{MAX_PLACES_PER_ZONE} places in one zone")
                for p in places:
                    if p in ZONE_NOWHERE:
                        return (f"Condition {i+1} (zone): '{p}' is the absence of a "
                                f"place, so it can't be entered or left — use a "
                                f"named place, 'home', or '{ZONE_ANY}'")
                if ZONE_ANY in places and len(places) > 1:
                    return (f"Condition {i+1} (zone): '{ZONE_ANY}' already covers "
                            f"every place, so it can't be combined with one")
                # One place stays a plain string — a list is only meaningful
                # when it groups several into a single zone.
                c["place"] = places[0] if len(places) == 1 else places
            else:
                for f in ("attribute", "operator", "value"):
                    if f not in c:
                        return f"Condition {i+1} missing '{f}'"
                if c["operator"] not in OPERATORS:
                    return f"Condition {i+1} invalid operator"
                s = c.get("sustain")
                if s:
                    try:
                        s = int(s)
                        c["sustain"] = s if s > 0 else None
                    except (ValueError, TypeError):
                        c["sustain"] = None
                if not c.get("sustain"):
                    c.pop("sustain", None)
        return None

    def _validate_condition_sources(self, conds: List[Dict],
                                    source_ieee: str) -> Optional[str]:
        """Check the device each trigger condition reads.

        A condition naming its own device must name one that exists; a zone
        condition reads `place`, which only presence users have; and a device
        condition on a clock rule has no source to fall back on, so it must
        name a device.
        """
        devices = self._get_all_devices()
        for i, c in enumerate(iter_leaf_conditions(conds)):
            ctype = c.get("type", "attribute")
            if ctype in TEMPORAL_TYPES:
                continue
            src = c.get("ieee") or source_ieee
            if src == TIME_SOURCE:
                return (f"Condition {i+1} reads a device, but a time rule has "
                        f"no source device — pick the device it reads")
            dev = devices.get(src)
            if c.get("ieee") and dev is None:
                return f"Condition {i+1}: device not found: {src}"
            if ctype == "zone":
                state = getattr(dev, "state", None) if dev else None
                if not state or ZONE_ATTR not in state:
                    return ("Enters/leaves conditions need a presence user as the "
                            "device — only people have a place.")
        return None

    def _source_cap_error(self, sources: List[str],
                          exclude_rule_id: Optional[str] = None) -> Optional[str]:
        """MAX_RULES_PER_DEVICE, counted on every device a rule triggers on —
        each is evaluated on every update of that device, source or not."""
        for src in sources:
            ids = [r for r in self._source_index.get(src, []) if r != exclude_rule_id]
            if len(ids) >= MAX_RULES_PER_DEVICE:
                name = self._get_all_names().get(src, src)
                return f"Max {MAX_RULES_PER_DEVICE} rules per trigger device ({name})"
        return None

    # The swarm's suggestion builder validates through the older name.
    _validate_zone_source = _validate_condition_sources

    def _validate_prerequisites(self, prereqs: List[Dict]) -> Optional[str]:
        import re
        if len(prereqs) > MAX_PREREQUISITES_PER_RULE:
            return f"Max {MAX_PREREQUISITES_PER_RULE} prerequisites"
        for i, p in enumerate(prereqs):
            ptype = p.get("type", "device")
            if ptype == "time_window":
                for f in ("time_from", "time_to"):
                    if f not in p:
                        return f"Prerequisite {i+1} (time_window) missing '{f}'"
                    if not re.match(r"^\d{2}:\d{2}$", str(p[f])):
                        return f"Prerequisite {i+1} '{f}' must be HH:MM"
            elif ptype == "sun":
                err = self._validate_sun(p, f"Prerequisite {i+1}")
                if err:
                    return err
            else:
                for f in ("ieee", "attribute", "operator", "value"):
                    if f not in p:
                        return f"Prerequisite {i+1} missing '{f}'"
                if p["operator"] not in OPERATORS:
                    return f"Prerequisite {i+1} invalid operator"
        return None

    @staticmethod
    def _validate_sun(c: Dict, label: str) -> Optional[str]:
        import re
        for f in ("from", "to"):
            v = c.get(f)
            if v not in ("sunrise", "sunset") and not re.match(r"^\d{2}:\d{2}$", str(v or "")):
                return f"{label} sun '{f}' must be 'sunrise', 'sunset', or HH:MM"
        for f in ("offset_from", "offset_to"):
            if f in c and not isinstance(c[f], (int, float)):
                return f"{label} sun '{f}' must be a number of minutes"
        return None

    def _validate_sequence(self, steps: List[Dict], label: str, depth: int = 0) -> Optional[str]:
        if depth > MAX_NESTING_DEPTH:
            return f"{label}: max nesting depth {MAX_NESTING_DEPTH} exceeded"
        if len(steps) > MAX_STEPS_PER_SEQUENCE:
            return f"{label}: max {MAX_STEPS_PER_SEQUENCE} steps"

        for i, step in enumerate(steps):
            st = step.get("type")
            if st not in ALL_STEP_TYPES:
                return f"{label}[{i+1}]: invalid type '{st}'"

            if st == "command":
                if not step.get("target_ieee"):
                    return f"{label}[{i+1}]: command needs target_ieee"
                if step.get("command") not in VALID_COMMANDS:
                    return f"{label}[{i+1}]: invalid command"
            elif st == "delay":
                if not isinstance(step.get("seconds", 0), (int, float)) or step.get("seconds", 0) < 0:
                    return f"{label}[{i+1}]: delay needs positive seconds"
            elif st == "media":
                if not step.get("player_id"):
                    return f"{label}[{i+1}]: media needs player_id"
                ma = step.get("media_action")
                if ma not in ("play_radio", "play_tidal", "control", "volume",
                              "announce", "volume_fade", "volume_adjust",
                              "play_zone"):
                    return f"{label}[{i+1}]: invalid media_action"
                is_zone = str(step.get("player_id", "")).startswith("zone:")
                if ma == "play_zone" and not is_zone:
                    return f"{label}[{i+1}]: play_zone needs an OpenZone zone"
                if ma == "volume_adjust" and not isinstance(step.get("delta"), (int, float)):
                    return f"{label}[{i+1}]: volume_adjust needs a numeric delta"
                if ma == "play_radio" and not step.get("station_uuid"):
                    return f"{label}[{i+1}]: play_radio needs station_uuid"
                if ma == "play_tidal" and not (step.get("tidal_kind") and step.get("tidal_id")):
                    return f"{label}[{i+1}]: play_tidal needs tidal_kind and tidal_id"
                if ma == "control" and step.get("control_action") not in (
                        "pause", "resume", "stop", "next", "prev"):
                    return f"{label}[{i+1}]: control needs a valid control_action"
                if ma == "announce" and not step.get("text"):
                    return f"{label}[{i+1}]: announce needs text"
            elif st == "request":
                # Historical name for the message step (saved rules carry it).
                if not step.get("to_user"):
                    return f"{label}[{i+1}]: message needs to_user"
                if not (step.get("message") or "").strip():
                    return f"{label}[{i+1}]: message needs text"
            elif st == "offer":
                if not step.get("to_user"):
                    return f"{label}[{i+1}]: offer needs to_user"
                if not (step.get("message") or "").strip():
                    return f"{label}[{i+1}]: offer needs text"
                accept = step.get("accept_steps") or []
                if not accept:
                    return (f"{label}[{i+1}]: offer needs accept_steps — an offer "
                            f"with nothing to run is a message")
                exp = step.get("expires_in", DEFAULT_OFFER_EXPIRY)
                if not isinstance(exp, (int, float)) or not (0 < exp <= MAX_OFFER_EXPIRY):
                    return (f"{label}[{i+1}]: offer expires_in must be 1-"
                            f"{MAX_OFFER_EXPIRY} seconds")
                # An offer inside an offer's accept branch could nest without
                # limit, so the accept sequence is validated at the next depth.
                err = self._validate_sequence(accept, f"{label}[{i+1}].accept",
                                              depth + 1)
                if err:
                    return err
            elif st in ("wait_for", "condition"):
                for f in ("ieee", "attribute", "operator", "value"):
                    if f not in step:
                        return f"{label}[{i+1}]: {st} needs '{f}'"
            elif st == "if_then_else":
                inline = step.get("inline_conditions", [])
                if not inline:
                    return f"{label}[{i+1}]: if_then_else needs inline_conditions"
                for j, ic in enumerate(inline):
                    for f in ("ieee", "attribute", "operator", "value"):
                        if f not in ic:
                            return f"{label}[{i+1}] condition {j+1} missing '{f}'"
                err = self._validate_sequence(step.get("then_steps", []), f"{label}[{i+1}].then", depth + 1)
                if err: return err
                err = self._validate_sequence(step.get("else_steps", []), f"{label}[{i+1}].else", depth + 1)
                if err: return err
            elif st == "parallel":
                branches = step.get("branches", [])
                if len(branches) < 2:
                    return f"{label}[{i+1}]: parallel needs >= 2 branches"
                for bi, branch in enumerate(branches):
                    err = self._validate_sequence(branch, f"{label}[{i+1}].branch{bi+1}", depth + 1)
                    if err: return err
        return None

    # RULE CRUD

    def add_rule(self, data: Dict[str, Any]) -> Dict[str, Any]:
        conditions = data.get("conditions")
        if conditions:
            err = self._validate_conditions(conditions)
            if err: return {"success": False, "error": err}
        elif all(k in data for k in ("attribute", "operator", "value")):
            conditions = [{"attribute": data["attribute"],
                           "operator": data["operator"], "value": data["value"]}]
        else:
            return {"success": False, "error": "Provide conditions list"}

        cond_logic = str(data.get("condition_logic", "and") or "and").lower()
        if cond_logic not in ("and", "or"):
            return {"success": False, "error": "condition_logic must be 'and' or 'or'"}

        run_mode = str(data.get("run_mode") or DEFAULT_RUN_MODE).lower()
        if run_mode not in RUN_MODES:
            return {"success": False,
                    "error": f"run_mode must be one of {', '.join(RUN_MODES)}"}

        prereqs = data.get("prerequisites", [])
        if prereqs:
            err = self._validate_prerequisites(prereqs)
            if err: return {"success": False, "error": err}

        then_seq = data.get("then_sequence", [])
        else_seq = data.get("else_sequence", [])
        if not then_seq and not else_seq:
            return {"success": False, "error": "At least one action step required"}
        err = self._validate_sequence(then_seq, "THEN")
        if err: return {"success": False, "error": err}
        err = self._validate_sequence(else_seq, "ELSE")
        if err: return {"success": False, "error": err}

        source = data.get("source_ieee")
        if not source:
            return {"success": False, "error": "source_ieee required"}
        if source == TIME_SOURCE:
            # Clock-triggered rule: it has no physical source device, so it must
            # carry a temporal condition or a condition on a device whose
            # updates can move it.
            if not any(c.get("type") in TEMPORAL_TYPES or c.get("ieee")
                       for c in iter_leaf_conditions(conditions)):
                return {"success": False,
                        "error": "Time/alarm rule needs a time, alarm, or sun "
                                 "condition, or a condition on a device"}
        elif source not in self._get_all_devices():
            return {"success": False, "error": f"Source not found: {source}"}

        err = self._validate_condition_sources(conditions, source)
        if err: return {"success": False, "error": err}
        err = self._source_cap_error(self.rule_sources(
            {"source_ieee": source, "conditions": conditions}))
        if err: return {"success": False, "error": err}

        rule = {
            "id": f"auto_{uuid.uuid4().hex[:8]}",
            "name": data.get("name", ""),
            "enabled": data.get("enabled", True),
            "source_ieee": source,
            "conditions": conditions,
            "condition_logic": cond_logic,
            "run_mode": run_mode,
            "prerequisites": prereqs,
            "then_sequence": then_seq,
            "else_sequence": else_seq,
            "cooldown": data.get("cooldown", DEFAULT_COOLDOWN),
            "created": time.time(),
        }
        self.rules.append(rule)
        self._rebuild_index()
        self._save_rules()
        logger.info(f"Rule added: {rule['id']} '{rule['name']}'")
        return {"success": True, "rule": rule}

    def update_rule(self, rule_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Not found: {rule_id}"}

        if "name" in updates:
            rule["name"] = str(updates["name"])[:100]
        if "conditions" in updates:
            err = self._validate_conditions(updates["conditions"])
            if err: return {"success": False, "error": err}
            source = rule.get("source_ieee", "")
            err = self._validate_condition_sources(updates["conditions"], source)
            if err: return {"success": False, "error": err}
            err = self._source_cap_error(
                self.rule_sources({"source_ieee": source,
                                   "conditions": updates["conditions"]}),
                exclude_rule_id=rule_id)
            if err: return {"success": False, "error": err}
            rule["conditions"] = updates["conditions"]
            # Sustain clocks are keyed by position, which a new shape invalidates.
            self._clear_sustains(rule_id)
        if "condition_logic" in updates:
            cl = str(updates["condition_logic"] or "and").lower()
            if cl not in ("and", "or"):
                return {"success": False, "error": "condition_logic must be 'and' or 'or'"}
            rule["condition_logic"] = cl
        if "run_mode" in updates:
            mode = str(updates["run_mode"] or DEFAULT_RUN_MODE).lower()
            if mode not in RUN_MODES:
                return {"success": False,
                        "error": f"run_mode must be one of {', '.join(RUN_MODES)}"}
            rule["run_mode"] = mode
        if "prerequisites" in updates:
            p = updates["prerequisites"] or []
            if p:
                err = self._validate_prerequisites(p)
                if err: return {"success": False, "error": err}
            rule["prerequisites"] = p
        if "then_sequence" in updates:
            err = self._validate_sequence(updates["then_sequence"], "THEN")
            if err: return {"success": False, "error": err}
            rule["then_sequence"] = updates["then_sequence"]
        if "else_sequence" in updates:
            err = self._validate_sequence(updates["else_sequence"], "ELSE")
            if err: return {"success": False, "error": err}
            rule["else_sequence"] = updates["else_sequence"]
        if "enabled" in updates:
            rule["enabled"] = bool(updates["enabled"])
            if not rule["enabled"]:
                self._cancel_sequence(rule_id)
                self._rule_states.pop(rule_id, None)
                self._clear_sustains(rule_id)
        if "cooldown" in updates:
            rule["cooldown"] = max(0, int(updates["cooldown"]))

        rule["updated"] = time.time()
        self._rebuild_index()
        self._save_rules()
        return {"success": True, "rule": rule}

    def delete_rule(self, rule_id: str) -> Dict[str, Any]:
        rule = self._find_rule(rule_id)
        if not rule:
            return {"success": False, "error": f"Not found: {rule_id}"}
        self._cancel_sequence(rule_id)
        self.rules.remove(rule)
        self._cooldowns.pop(rule_id, None)
        self._rule_states.pop(rule_id, None)
        self._clear_sustains(rule_id)
        self._rebuild_index()
        self._save_rules()
        return {"success": True}

    def get_rules(self, source_ieee: Optional[str] = None) -> List[Dict[str, Any]]:
        names = self._get_all_names()
        # A rule belongs to every device it triggers on, not only its source.
        rules = self.rules if not source_ieee else [
            r for r in self.rules if source_ieee in self.rule_sources(r)
        ]
        enriched = []
        for rule in rules:
            r = json.loads(json.dumps(rule))  # deep copy
            r["source_name"] = names.get(rule["source_ieee"], rule["source_ieee"])
            r["sources"] = self.rule_sources(rule)
            self._enrich_names(list(iter_leaf_conditions(r.get("conditions"))),
                               names, "ieee", "device_name")
            r["_state"] = self._rule_states.get(rule["id"], "unknown")
            live = self._live_runs(rule["id"])
            r["_running"] = bool(live)
            r["_runs"] = len(live)          # >1 only under queued / parallel
            self._enrich_names(r.get("prerequisites", []), names, "ieee", "device_name")
            self._enrich_steps(r.get("then_sequence", []), names)
            self._enrich_steps(r.get("else_sequence", []), names)
            enriched.append(r)
        return enriched

    def _enrich_names(self, items, names, ieee_key, name_key):
        for item in items:
            if item.get(ieee_key):
                item[name_key] = names.get(item[ieee_key], item[ieee_key])

    def _enrich_steps(self, steps, names):
        for step in steps:
            if step.get("target_ieee"):
                step["target_name"] = names.get(step["target_ieee"], step["target_ieee"])
            if step.get("ieee"):
                step["device_name"] = names.get(step["ieee"], step["ieee"])
            if step.get("inline_conditions"):
                for ic in step["inline_conditions"]:
                    if ic.get("ieee"):
                        ic["device_name"] = names.get(ic["ieee"], ic["ieee"])
            for sub in ("then_steps", "else_steps"):
                if step.get(sub):
                    self._enrich_steps(step[sub], names)
            if step.get("branches"):
                for branch in step["branches"]:
                    self._enrich_steps(branch, names)

    def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        return self._find_rule(rule_id)

    def _find_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        for r in self.rules:
            if r["id"] == rule_id:
                return r
        return None

    # STATE MACHINE EVALUATION

    async def evaluate(self, source_ieee: str, changed_data: Dict[str, Any]):
        rule_ids = self._source_index.get(source_ieee)
        if not rule_ids:
            return

        depth = _chain_depth.get()
        if depth >= MAX_CHAIN_DEPTH:
            self._stats["chain_stops"] += 1
            self._trace("-", "entry", "CHAIN_LIMIT",
                        f"Chain depth {depth} reached on {source_ieee} — "
                        f"not evaluating further", level="WARNING",
                        source_ieee=source_ieee)
            return

        self._stats["evaluations"] += 1
        now = time.time()
        devices = self._get_all_devices()
        names = self._get_all_names()
        source_name = names.get(source_ieee, source_ieee)

        source_device = devices.get(source_ieee)
        if not source_device:
            return

        full_state = source_device.state or {}
        # What this device looked like before the update now being evaluated.
        # First sight of a source (added after startup): everything it did not
        # just change is still its old value, and the changed keys have no
        # "before" — a zone condition treats that as "was nowhere".
        prev_values = self._last_values.get(source_ieee)
        if prev_values is None:
            prev_values = {k: v for k, v in full_state.items() if k not in changed_data}

        self._trace("-", "entry", "EVALUATING",
                    f"State change on {source_name}: {list(changed_data.keys())} — {len(rule_ids)} rule(s)",
                    level="DEBUG", source_ieee=source_ieee)

        for rule_id in rule_ids:
            rule = self._find_rule(rule_id)
            if not rule or not rule.get("enabled", True):
                continue

            conditions = rule.get("conditions", [])
            if not conditions:
                continue

            # Relevance — judged on the conditions that read this device, inside
            # groups too. A rule indexed here only as its source, whose device
            # conditions all read other devices, has nothing this update can move.
            leaves = list(iter_leaf_conditions(conditions))
            own = [c for c in leaves if self._cond_source(rule, c) == source_ieee]
            if own:
                watched = self._watched_attributes(own)
                if watched and not watched.intersection(changed_data.keys()):
                    continue
            elif any(self._cond_source(rule, c) for c in leaves):
                continue

            view = self._condition_view(rule, devices, source_ieee, changed_data,
                                        full_state, prev_values)
            self._evaluate_rule(rule, devices, names, now, view)

        # Baseline for the next update. full_state is already the new state, so
        # this is the "before" that the next evaluation compares against.
        self._last_values[source_ieee] = {**full_state, **changed_data}

    def _evaluate_rule(self, rule, devices, names, now, view) -> None:
        """Run one rule through the state machine — conditions, prerequisites,
        the transition, and the sequence that transition fires.

        Device updates, clock boundaries and sustain re-checks all come through
        here; they differ only in what `view` reads (see _condition_view).
        """
        rule_id = rule["id"]
        rule_name = rule.get("name") or rule_id
        conditions = rule.get("conditions", [])

        # CONDITIONS
        logic = self._condition_logic(rule)
        has_zone = self._has_zone(conditions)
        all_matched, cond_results, has_sustain = self._eval_conditions_block(
            conditions, rule_id, {}, {}, now, logic, view=view, names=names)

        if has_sustain:
            wait = self._schedule_sustain_recheck(rule_id, cond_results)
            self._trace(rule_id, "evaluate", "SUSTAIN_WAIT",
                        f"Sustain pending: {rule_name} — re-checking in {wait:.1f}s",
                        conditions=cond_results, condition_logic=logic)
            return
        # Decided either way, so a re-check left from an earlier pass would only
        # re-read a settled answer.
        self._cancel_sustain_recheck(rule_id)

        # PREREQUISITES
        prereq_results = []
        prereqs_met = True
        if all_matched:
            prereqs = rule.get("prerequisites", [])
            prereqs_met, prereq_results = self._eval_prerequisites(prereqs, devices, names)

        # DETERMINE STATE
        conditions_met = all_matched and prereqs_met
        new_state = "matched" if conditions_met else "unmatched"
        prev_state = self._rule_states.get(rule_id)

        if not all_matched:
            self._trace(rule_id, "evaluate", "NO_MATCH",
                        f"Conditions ({logic.upper()}) not met: {rule_name}",
                        level="DEBUG", conditions=cond_results,
                        condition_logic=logic)
        elif not prereqs_met:
            self._trace(rule_id, "prerequisite", "PREREQ_FAIL",
                        f"Prerequisites not met: {rule_name}",
                        conditions=cond_results, prerequisites=prereq_results,
                        condition_logic=logic)

        # TRANSITION
        self._rule_states[rule_id] = new_state

        # A zone rule triggers on a crossing, not on a state. "No crossing
        # right now" is not the opposite crossing, so an unmatched pass must
        # not run the ELSE path — leaving is its own rule with its own THEN.
        if has_zone and new_state == "unmatched":
            return

        if prev_state == new_state:
            if new_state == "matched":
                self._trace(rule_id, "transition", "STILL_MATCHED",
                            f"No transition: {rule_name}", level="DEBUG")
            return

        if prev_state is None and new_state == "unmatched":
            self._trace(rule_id, "transition", "INIT_UNMATCHED",
                        f"Initial: unmatched — {rule_name}", level="DEBUG")
            return

        # Cooldown
        cooldown = rule.get("cooldown", DEFAULT_COOLDOWN)
        elapsed = now - self._cooldowns.get(rule_id, 0)
        if elapsed < cooldown:
            self._trace(rule_id, "cooldown", "BLOCKED",
                        f"Cooldown {elapsed:.1f}s < {cooldown}s")
            return

        self._cooldowns[rule_id] = now
        self._stats["transitions"] += 1
        # Having fired, the rule needs each sustain's full time again.
        for k in [k for k in self._sustain_tracker if k.startswith(f"{rule_id}_")]:
            del self._sustain_tracker[k]

        # Fire sequence
        path = "THEN" if new_state == "matched" else "ELSE"
        seq = rule.get("then_sequence" if path == "THEN" else "else_sequence", [])
        if not seq:
            self._trace(rule_id, "transition", "NO_SEQUENCE",
                        f"Transition → {new_state}, no {path} sequence: {rule_name}")
            return

        self._trace(rule_id, "transition", f"{path}_FIRING",
                    f"⚡ {prev_state or 'init'}→{new_state}: {path} ({len(seq)} steps) — {rule_name}",
                    conditions=cond_results, prerequisites=prereq_results,
                    condition_logic=logic)

        self._start_sequence(rule, seq, path)

        # EVENT ATTRIBUTE RESET
        # Momentary triggers (a button press, a boundary crossing) have to
        # re-arm: they are never "still true", so without this the second
        # press — or the second arrival — would look like no transition.
        if has_zone or any(c.get("attribute") in EVENT_ATTRS
                           for c in iter_leaf_conditions(conditions)):
            self._rule_states[rule_id] = "unmatched"

    # SUSTAIN RE-CHECKS

    def _schedule_sustain_recheck(self, rule_id, cond_results) -> float:
        """Re-evaluate the rule when its soonest pending sustain runs out.

        A sustain used to be re-read only on its device's next update, and a
        sensor that has settled may send none: a door left open reports once,
        so "open for 10 minutes" never fired. One timer per rule — the latest
        evaluation knows the soonest deadline. Returns the delay.
        """
        waits = [r.get("sustain_remaining", 0) for r in self._leaf_results(cond_results)
                 if r.get("result") == "SUSTAIN_WAIT"]
        delay = max(0.0, min(waits) if waits else 0.0) + SUSTAIN_RECHECK_SLACK
        self._cancel_sustain_recheck(rule_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return delay                    # no loop to wait on (a sync caller)
        self._sustain_timers[rule_id] = loop.create_task(
            self._sustain_recheck(rule_id, delay))
        return delay

    def _cancel_sustain_recheck(self, rule_id) -> None:
        task = self._sustain_timers.pop(rule_id, None)
        if task and not task.done():
            task.cancel()

    def _clear_sustains(self, rule_id) -> None:
        """Forget a rule's sustain clocks and any pending re-check."""
        prefix = f"{rule_id}_"
        for k in [k for k in self._sustain_tracker if k.startswith(prefix)]:
            del self._sustain_tracker[k]
        self._cancel_sustain_recheck(rule_id)

    async def _sustain_recheck(self, rule_id, delay) -> None:
        await asyncio.sleep(delay)
        # Off the books before evaluating, since the evaluation may schedule
        # the next re-check and must not cancel this one to do it.
        if self._sustain_timers.get(rule_id) is asyncio.current_task():
            del self._sustain_timers[rule_id]
        rule = self._find_rule(rule_id)
        if not rule or not rule.get("enabled", True):
            return
        try:
            devices = self._get_all_devices()
            # No device updated: everything is read as it stands.
            self._evaluate_rule(rule, devices, self._get_all_names(), time.time(),
                                self._condition_view(rule, devices))
        except Exception as e:                          # noqa: BLE001
            self._stats["errors"] += 1
            self._trace(rule_id, "evaluate", "EXCEPTION",
                        f"Sustain re-check failed: {e}", level="ERROR",
                        traceback=traceback.format_exc())

    @classmethod
    def _leaf_results(cls, results):
        """Condition results, looking inside group results."""
        for r in results or []:
            if r.get("type") == "group":
                yield from cls._leaf_results(r.get("conditions"))
            else:
                yield r

    # CONDITION / PREREQUISITE EVALUATION

    def _condition_view(self, rule, devices, updating=None, changed_data=None,
                        full_state=None, prev_values=None):
        """Return view(cond) -> (changed_data, full_state, prev_values) for a rule.

        A condition on the device that just updated reads the update. A condition
        on any other device reads that device as it stands, with nothing marked
        as changed: it did not change in this update, so neither a zone crossing
        nor a momentary press on it can match on another device's update.
        `updating=None` is a clock tick — no device changed at all.
        """
        default = rule.get("source_ieee", "")
        snapshots: Dict[str, tuple] = {}

        def view(cond):
            src = cond.get("ieee") or default
            if updating is not None and src == updating:
                return changed_data or {}, full_state or {}, prev_values or {}
            if src not in snapshots:
                dev = devices.get(src)
                state = (getattr(dev, "state", None) or {}) if dev else {}
                snapshots[src] = (state, self._last_values.get(src, state))
            state, prev = snapshots[src]
            return {}, state, prev
        return view

    def _eval_conditions_block(self, conditions, rule_id, changed_data, full_state,
                               now, logic="and", prev_values=None, view=None,
                               names=None, key_prefix=""):
        """Evaluate trigger conditions. Returns (matched, results, has_sustain).

        logic 'and' (default): every condition must pass.
        logic 'or':            any one condition passing is enough.

        Every condition is evaluated even once the answer is known, because
        evaluating one is what starts its sustain clock: "dark AND door open
        for 10 minutes" has to time the door from when it opened, not from when
        it got dark. The trace shows every condition as a result.

        has_sustain means "nothing is settled yet; only a sustain clock is
        holding the answer" — under AND, every unmet condition is mid-sustain;
        under OR, none has passed and one is mid-sustain. The caller holds the
        rule and schedules a re-check instead of deciding.

        A {"type": "group"} item is a block of its own with its own
        condition_logic, which its siblings see as a single condition.

        view, when given (see _condition_view), supplies each condition's own
        device reading, which is what lets AND/OR span several devices. Without
        it every condition reads the one device passed in.
        """
        or_mode = str(logic).lower() == "or"
        results = []
        any_passed, all_passed = False, True
        any_pending, hard_fail = False, False

        for i, cond in enumerate(conditions):
            key = f"{key_prefix}{i}"
            if cond.get("type") == "group":
                g_logic = self._condition_logic(cond)
                matched, inner, sustain_pending = self._eval_conditions_block(
                    cond.get("conditions") or [], rule_id, changed_data, full_state,
                    now, g_logic, prev_values, view, names, key_prefix=f"{key}.")
                result = {"index": i + 1, "type": "group",
                          "condition_logic": g_logic, "conditions": inner,
                          "result": "PASS" if matched
                          else "SUSTAIN_WAIT" if sustain_pending else "FAIL"}
            else:
                cd, fs, pv = (view(cond) if view
                              else (changed_data, full_state, prev_values or {}))
                # Top-level keys stay "<rule>_<i>", as they always were.
                matched, result, sustain_pending = self._eval_one_condition(
                    cond, i, rule_id, cd, fs, now, pv, skey=f"{rule_id}_{key}")
                if cond.get("ieee"):
                    # Name the device in the trace — otherwise a FAIL on another
                    # device's attribute reads as though it were the source's.
                    result["ieee"] = cond["ieee"]
                    result["device_name"] = (names or {}).get(cond["ieee"], cond["ieee"])
            results.append(result)

            if matched:
                any_passed = True
            else:
                all_passed = False
                if sustain_pending:
                    any_pending = True
                else:
                    hard_fail = True

        if or_mode:
            return any_passed, results, (not any_passed and any_pending)
        return all_passed, results, (not all_passed and not hard_fail)

    def _eval_one_condition(self, cond, i, rule_id, changed_data, full_state, now,
                            prev_values=None, skey=None):
        """Evaluate a single trigger condition.

        Returns (matched, result_dict, sustain_pending). sustain_pending is True when
        the condition's value matched but its "for N seconds" window hasn't elapsed —
        matched is False in that case, the caller decides what to do with it.
        """
        ctype = cond.get("type", "attribute")

        if ctype == "zone":
            return self._eval_zone(cond, i, changed_data, prev_values or {})

        if ctype == "sun":
            import datetime
            matched, info = self._eval_sun(cond, datetime.datetime.now())
            return matched, {"index": i + 1, "type": "sun", **info,
                             "result": "PASS" if matched else "FAIL"}, False

        if ctype == "time_window":
            import datetime
            negate = cond.get("negate", False)
            now_dt = datetime.datetime.now()
            now_time = now_dt.time()
            weekday = now_dt.weekday()
            t_from = datetime.time(*map(int, cond["time_from"].split(":")))
            t_to   = datetime.time(*map(int, cond["time_to"].split(":")))
            days   = cond.get("days", list(range(7)))
            # An absent "days" key defaults to all 7 (handled by .get above);
            # an explicitly empty list means "no days" → never matches.
            day_ok = weekday in days
            if t_from <= t_to:
                time_ok = t_from <= now_time <= t_to
            else:
                time_ok = now_time >= t_from or now_time <= t_to
            matched = day_ok and time_ok
            if negate:
                matched = not matched
            return matched, {
                "index": i + 1, "type": "time_window",
                "time_from": cond["time_from"], "time_to": cond["time_to"],
                "days": days, "negate": negate,
                "now_time": now_dt.strftime("%H:%M"), "now_weekday": weekday,
                "result": "PASS" if matched else "FAIL",
            }, False

        if ctype == "time":
            # Point-in-time alarm: matched only during the exact HH:MM minute
            # on the selected weekdays. Fires the THEN sequence once at that
            # minute (the scheduler evaluates the boundary).
            import datetime
            negate = cond.get("negate", False)
            now_dt = datetime.datetime.now()
            at = str(cond.get("at", ""))
            days = cond.get("days", list(range(7)))
            matched = (now_dt.weekday() in days) and (now_dt.strftime("%H:%M") == at)
            if negate:
                matched = not matched
            return matched, {
                "index": i + 1, "type": "time", "at": at, "days": days,
                "negate": negate, "now_time": now_dt.strftime("%H:%M"),
                "now_weekday": now_dt.weekday(),
                "result": "PASS" if matched else "FAIL",
            }, False

        attr = cond["attribute"]
        op = cond["operator"]
        threshold = cond["value"]
        sustain = cond.get("sustain", 0) or 0
        skey = skey or f"{rule_id}_{i}"

        # A momentary attribute is true only on the update that carries it (see
        # EVENT_ATTRS): its last value lingering in state is not a new press.
        if attr in EVENT_ATTRS and attr not in changed_data:
            self._sustain_tracker.pop(skey, None)
            return False, {"index": i + 1, "attribute": attr, "result": "FAIL",
                           "reason": f"'{attr}' is momentary and did not fire "
                                     f"in this update"}, False

        if attr in changed_data:
            val = changed_data[attr]; src = "changed_data"
        elif attr in full_state:
            val = full_state[attr]; src = "full_state"
        else:
            self._sustain_tracker.pop(skey, None)
            return False, {"index": i + 1, "attribute": attr, "result": "FAIL",
                           "reason": f"'{attr}' not in state"}, False

        try:
            matched = self._evaluate_condition(val, op, threshold)
        except Exception as e:
            self._sustain_tracker.pop(skey, None)
            return False, {"index": i + 1, "attribute": attr,
                           "result": "ERROR", "reason": str(e)}, False

        if matched and sustain > 0:
            if skey not in self._sustain_tracker:
                self._sustain_tracker[skey] = now
            el = now - self._sustain_tracker[skey]
            if el < sustain:
                return False, {"index": i + 1, "attribute": attr, "operator": op,
                               "threshold_raw": repr(threshold), "actual_raw": repr(val),
                               "actual_type": type(val).__name__, "value_source": src,
                               "result": "SUSTAIN_WAIT", "sustain_required": sustain,
                               "sustain_elapsed": round(el, 1),
                               "sustain_remaining": round(sustain - el, 3),
                               "reason": f"Sustained {el:.1f}s / {sustain}s"}, True

        self._sustain_tracker.pop(skey, None)

        return matched, {"index": i + 1, "attribute": attr, "operator": op,
                         "threshold_raw": repr(threshold),
                         "actual_raw": repr(val),
                         "actual_type": type(val).__name__,
                         "value_source": src,
                         "result": "PASS" if matched else "FAIL"}, False


    @staticmethod
    def _is_somewhere(value) -> bool:
        """Is this place value a real location rather than the absence of one?"""
        return value not in ZONE_NOWHERE

    @staticmethod
    def _in_zone(value, target) -> bool:
        """Is a person whose place is `value` inside the zone `target`?

        `target` may be one place id or a list of them. A list is one zone made
        of several places — "work" spanning two offices — so moving between its
        members is movement *within* the zone, not a departure and an arrival.
        """
        if value in ZONE_NOWHERE:
            return False
        if target == ZONE_ANY:
            return True
        if isinstance(target, (list, tuple, set)):
            return str(value) in {str(t) for t in target}
        return str(value) == str(target)

    def _eval_zone(self, cond, i, changed_data, prev_values):
        """Evaluate one enter/leave condition. Returns (matched, result, False).

        Edge-triggered: the crossing is the trigger, so this passes only on the
        update that carries the place change. An evaluation with no place change
        is not a crossing in the other direction — it is no crossing at all.
        """
        event = str(cond.get("event", "enter")).lower()
        target = cond.get("place", ZONE_ANY)
        result = {"index": i + 1, "type": "zone", "event": event, "place": target}

        if ZONE_ATTR not in changed_data:
            result.update({"result": "FAIL", "reason": "no place change in this update"})
            return False, result, False

        new = changed_data[ZONE_ATTR]
        old = prev_values.get(ZONE_ATTR)

        if target == ZONE_ANY:
            # "Any place" is not one big zone you stay inside while hopping
            # between places: every arrival is an arrival and every departure a
            # departure, so a place-to-place move is both.
            matched = self._is_somewhere(new) if event == "enter" \
                else self._is_somewhere(old)
        else:
            was_in = self._in_zone(old, target)
            now_in = self._in_zone(new, target)
            matched = (now_in and not was_in) if event == "enter" \
                else (was_in and not now_in)

        result.update({
            "from_place": old, "to_place": new,
            "result": "PASS" if matched else "FAIL",
        })
        if not matched:
            result["reason"] = f"{old!r} → {new!r} is not a {event} of {target!r}"
        return matched, result, False

    def _eval_prerequisites(self, prereqs, devices, names):
        """Evaluate prerequisites. Temporal entries (time_window/sun) are OR'd;
        device entries are AND'd."""
        import datetime
        results = []
        all_met = True

        # Partition
        _TEMPORAL = ("time_window", "sun")
        tw_prereqs  = [(j, p) for j, p in enumerate(prereqs) if p.get("type") in _TEMPORAL]
        dev_prereqs = [(j, p) for j, p in enumerate(prereqs) if p.get("type", "device") not in _TEMPORAL]

        # temporal: OR logic
        if tw_prereqs:
            tw_any_passed = False
            for j, p in tw_prereqs:
                if p.get("type") == "sun":
                    matched, info = self._eval_sun(p, datetime.datetime.now())
                    results.append({"index": j + 1, "type": "sun", **info,
                                    "result": "PASS" if matched else "FAIL"})
                    if matched:
                        tw_any_passed = True
                    continue
                negate = p.get("negate", False)
                now_dt = datetime.datetime.now()
                now_time = now_dt.time()
                weekday = now_dt.weekday()
                t_from = datetime.time(*map(int, p["time_from"].split(":")))
                t_to   = datetime.time(*map(int, p["time_to"].split(":")))
                days   = p.get("days", list(range(7)))
                # An absent "days" key defaults to all 7 (handled by .get above);
                # an explicitly empty list means "no days" → never matches.
                day_ok = weekday in days
                if t_from <= t_to:
                    time_ok = t_from <= now_time <= t_to
                else:  # overnight wrap
                    time_ok = now_time >= t_from or now_time <= t_to
                matched = day_ok and time_ok
                if negate:
                    matched = not matched
                results.append({
                    "index": j + 1,
                    "type": "time_window",
                    "time_from": p["time_from"],
                    "time_to": p["time_to"],
                    "days": days,
                    "negate": negate,
                    "now_time": now_dt.strftime("%H:%M"),
                    "now_weekday": weekday,
                    "result": "PASS" if matched else "FAIL",
                })
                if matched:
                    tw_any_passed = True

            if not tw_any_passed:
                all_met = False
                return all_met, results

        for j, p in dev_prereqs:
            negate = p.get("negate", False)
            ieee = p["ieee"]
            attr = p["attribute"]
            op   = p["operator"]
            val  = p["value"]

            dname, state = self._resolve_state(ieee)
            if state is None:
                results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                                "attribute": attr, "result": "FAIL",
                                "reason": "Device/group not found"})
                all_met = False; break

            actual = state.get(attr)
            if actual is None:
                results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                                "attribute": attr, "result": "FAIL",
                                "reason": f"'{attr}' not in state",
                                "available_keys": list(state.keys())})
                all_met = False; break

            try:
                matched = self._evaluate_condition(actual, op, val)
                if negate:
                    matched = not matched
            except Exception as e:
                results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                                "attribute": attr, "result": "ERROR", "reason": str(e)})
                all_met = False; break

            results.append({"index": j+1, "ieee": ieee, "device_name": dname,
                            "attribute": attr, "operator": op, "negate": negate,
                            "threshold_raw": repr(val),
                            "threshold_normalised": repr(self._normalise_value(val)),
                            "actual_raw": repr(actual),
                            "actual_normalised": repr(self._normalise_value(actual)),
                            "actual_type": type(actual).__name__,
                            "result": "PASS" if matched else "FAIL"})
            if not matched:
                all_met = False; break

        return all_met, results

    # SUN (dynamic sunrise/sunset) — re-resolved every evaluation, so rules
    # track the seasons rather than freezing to one day's clock times.

    def _eval_sun(self, cond, now_dt):
        """Return (matched: bool, info: dict). Window between two boundaries that
        may be 'sunrise', 'sunset', or a fixed 'HH:MM', each with an optional
        minute offset. Overnight wrap supported, identical to time_window."""
        import datetime
        from modules.sun_times import sun_times
        st = sun_times(now_dt.date())
        info = {"from": cond.get("from"), "to": cond.get("to"),
                "now_time": now_dt.strftime("%H:%M")}
        if not st.get("available"):
            info["reason"] = "location not configured (set weather lat/lon)"
            return False, info

        t_from = self._resolve_sun_boundary(cond.get("from", "sunset"), st,
                                            cond.get("offset_from", 0))
        t_to = self._resolve_sun_boundary(cond.get("to", "sunrise"), st,
                                          cond.get("offset_to", 0))
        if t_from is None or t_to is None:
            info["reason"] = f"polar {st.get('polar')}" if st.get("polar") else "no sun event"
            return False, info

        info["resolved"] = f"{t_from.strftime('%H:%M')}–{t_to.strftime('%H:%M')}"
        days = cond.get("days", list(range(7)))
        day_ok = now_dt.weekday() in days
        now_time = now_dt.time()
        if t_from <= t_to:
            time_ok = t_from <= now_time <= t_to
        else:  # overnight wrap
            time_ok = now_time >= t_from or now_time <= t_to
        matched = day_ok and time_ok
        if cond.get("negate"):
            matched = not matched
        return matched, info

    @staticmethod
    def _resolve_sun_boundary(spec, st, offset_min):
        import datetime
        if spec in ("sunrise", "sunset"):
            base = st.get(spec)
            if base is None:
                return None
            ref = datetime.datetime.combine(datetime.date.today(), base) \
                + datetime.timedelta(minutes=offset_min or 0)
            return ref.time()
        try:
            hh, mm = map(int, str(spec).split(":"))
            return datetime.time(hh, mm)
        except Exception:
            return None

    @staticmethod
    def _plus_one_minute(hhmm: str) -> str:
        """'07:00' -> '07:01' (wraps at midnight). Used for alarm reset boundary."""
        try:
            h, m = map(int, hhmm.split(":"))
            total = (h * 60 + m + 1) % (24 * 60)
            return f"{total // 60:02d}:{total % 60:02d}"
        except Exception:
            return hhmm

    def _sun_boundary_hhmm(self, cond) -> set:
        """Today's resolved HH:MM boundaries for a sun condition, for the
        scheduler's boundary set."""
        from modules.sun_times import sun_times
        st = sun_times()
        out = set()
        for spec, off in ((cond.get("from", "sunset"), cond.get("offset_from", 0)),
                          (cond.get("to", "sunrise"), cond.get("offset_to", 0))):
            t = self._resolve_sun_boundary(spec, st, off)
            if t is not None:
                out.add(t.strftime("%H:%M"))
        return out

    def _eval_inline_conditions(self, inline_conditions, logic="and"):
        """Evaluate inline conditions for if_then_else steps.
        Returns (met: bool, results: list).
        logic: 'and' or 'or'
        """
        devices = self._get_all_devices()
        names = self._get_all_names()
        results = []
        any_pass = False
        all_pass = True

        for ic in inline_conditions:
            ieee = ic["ieee"]
            attr = ic["attribute"]
            op = ic["operator"]
            threshold = ic["value"]
            negate = ic.get("negate", False)
            duration = ic.get("duration", 0) or 0  # "for" N seconds — check sustained

            dname, state = self._resolve_state(ieee)

            if state is None:
                results.append({"device_name": dname, "attribute": attr,
                                "result": "FAIL", "reason": "Device/group not found"})
                all_pass = False
                continue

            actual = state.get(attr)
            if actual is None:
                results.append({"device_name": dname, "attribute": attr,
                                "result": "FAIL", "reason": f"'{attr}' not in state"})
                all_pass = False
                continue

            try:
                matched = self._evaluate_condition(actual, op, threshold)
                if negate:
                    matched = not matched
            except Exception as e:
                results.append({"device_name": dname, "attribute": attr,
                                "result": "ERROR", "reason": str(e)})
                all_pass = False
                continue

            # Duration check is handled by wait_for in practice
            # For inline conditions we just report current match
            results.append({"device_name": dname, "attribute": attr,
                            "operator": op, "negate": negate,
                            "threshold": repr(threshold), "actual": repr(actual),
                            "result": "PASS" if matched else "FAIL"})

            if matched:
                any_pass = True
            else:
                all_pass = False

        if logic == "or":
            return any_pass, results
        return all_pass, results

    # SEQUENCE EXECUTOR (recursive)

    def _run_mode(self, rule) -> str:
        """How a rule treats firing while it is still running. Rules saved
        before run modes carry no key, so they keep restarting."""
        mode = str(rule.get("run_mode") or DEFAULT_RUN_MODE).lower()
        return mode if mode in RUN_MODES else DEFAULT_RUN_MODE

    def _live_runs(self, rule_id: str) -> List[asyncio.Task]:
        return [t for t in self._rule_runs.get(rule_id, []) if not t.done()]

    def _cancel_sequence(self, rule_id: str):
        """Cancel every run of a rule: the running one and any queued behind it."""
        live = self._live_runs(rule_id)
        self._rule_runs.pop(rule_id, None)
        for task in live:
            task.cancel()
        if live:
            self._trace(rule_id, "sequence", "CANCELLED",
                        "Previous sequence cancelled" if len(live) == 1
                        else f"{len(live)} running/queued sequences cancelled")

    def _start_sequence(self, rule, seq, path) -> bool:
        """Start a fired sequence the way the rule's run mode says (RUN_MODES).

        Returns False when the mode drops it. Runs are tracked per task and
        removed by their own done callback, so a run finishing — or being
        cancelled — can never untrack the run that replaced it.
        """
        rule_id = rule["id"]
        rule_name = rule.get("name") or rule_id
        mode = self._run_mode(rule)
        live = self._live_runs(rule_id)

        if live and mode == "restart":
            self._cancel_sequence(rule_id)
            live = []
        elif live and mode == "single":
            self._trace(rule_id, "sequence", "RUN_SKIPPED",
                        f"{path} not run: still running, and the run mode is "
                        f"single — {rule_name}")
            return False
        elif len(live) >= MAX_RULE_RUNS:
            self._trace(rule_id, "sequence", "QUEUE_FULL",
                        f"{path} not run: {len(live)} {mode} runs already live "
                        f"— {rule_name}", level="WARNING")
            return False

        if live and mode == "queued":
            self._trace(rule_id, "sequence", "QUEUED",
                        f"{path} queued behind {len(live)} run(s) — {rule_name}")
            coro = self._run_after(live[-1], rule_id, rule_name, seq, path)
        else:
            coro = self._run_sequence(rule_id, rule_name, seq, path)
        task = asyncio.create_task(coro)
        self._rule_runs[rule_id] = live + [task]
        task.add_done_callback(lambda t, rid=rule_id: self._forget_run(rid, t))
        return True

    async def _run_after(self, previous, rule_id, rule_name, seq, path):
        """A queued run: wait for the run ahead — which waits for the one ahead
        of it — then run. A predecessor that was cancelled frees the slot too."""
        await asyncio.wait({previous})
        self._trace(rule_id, "sequence", "DEQUEUED",
                    f"{path} starting, its turn in the queue — {rule_name}")
        await self._run_sequence(rule_id, rule_name, seq, path)

    def _forget_run(self, rule_id: str, task: asyncio.Task) -> None:
        runs = self._rule_runs.get(rule_id)
        if runs and task in runs:
            runs.remove(task)
            if not runs:
                del self._rule_runs[rule_id]

    async def _run_sequence(self, rule_id: str, rule_name: str,
                            steps: List[Dict], path: str, depth: int = 0):
        """Execute steps in order. Recursive for if_then_else/parallel."""
        # Only the outermost call opens a link in the causal chain: the nested
        # calls for if_then_else and parallel are the same rule still firing,
        # not a new one it caused.
        token = _chain_depth.set(_chain_depth.get() + 1) if depth == 0 else None
        prefix = "  " * depth
        try:
            for i, step in enumerate(steps):
                num = i + 1
                total = len(steps)
                st = step["type"]

                if st == "command":
                    await self._step_command(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                elif st == "media":
                    await self._step_media(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                elif st == "request":
                    await self._step_request(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                elif st == "offer":
                    await self._step_offer(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                elif st == "delay":
                    secs = step.get("seconds", 0) or 0
                    if secs > 0:
                        self._trace(rule_id, "step", "DELAY",
                                    f"{prefix}[{path} {num}/{total}] ⏱ {secs}s")
                        await asyncio.sleep(secs)
                elif st == "wait_for":
                    met = await self._step_wait_for(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                    if not met:
                        self._trace(rule_id, "step", "WAIT_TIMEOUT",
                                    f"{prefix}[{path} {num}/{total}] ⏰ Timeout — stopping", level="WARNING")
                        break
                elif st == "condition":
                    met = self._step_gate(rule_id, step, f"{prefix}[{path} {num}/{total}]")
                    if not met:
                        self._trace(rule_id, "step", "GATE_STOP",
                                    f"{prefix}[{path} {num}/{total}] Gate failed — stopping")
                        break
                elif st == "if_then_else":
                    await self._step_if_then_else(rule_id, rule_name, step,
                                                  f"{prefix}[{path} {num}/{total}]", depth)
                elif st == "parallel":
                    await self._step_parallel(rule_id, rule_name, step,
                                              f"{prefix}[{path} {num}/{total}]", depth)

            if depth == 0:
                self._trace(rule_id, "sequence", "COMPLETE",
                            f"✅ {path} sequence complete — {rule_name}")

        except asyncio.CancelledError:
            if depth == 0:
                self._trace(rule_id, "sequence", "CANCELLED",
                            f"{path} cancelled — {rule_name}")
        except Exception as e:
            self._stats["errors"] += 1
            self._trace(rule_id, "sequence", "EXCEPTION",
                        f"💥 {path} failed: {e}", level="ERROR",
                        traceback=traceback.format_exc())
        finally:
            if token is not None:
                _chain_depth.reset(token)
            # Rule runs are untracked by _start_sequence's done callback, which
            # removes exactly this task. Popping by rule id here used to drop the
            # newer run that had just replaced a cancelled one, so the next
            # restart could not cancel it and two sequences ran at once.

    async def _step_command(self, rule_id, step, tag):
        target_ieee = step["target_ieee"]
        command = step["command"]
        value = self._resolve_value(step.get("value"))
        endpoint_id = step.get("endpoint_id")
        if isinstance(step.get("value"), dict) and value is None:
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "VALUE_ERROR",
                        f"{tag} could not resolve {step['value']}", level="ERROR")
            return
        devices = self._get_all_devices()
        names = self._get_all_names()

        # GROUP TARGET ROUTING
        if target_ieee.startswith("group:"):
            await self._step_group_command(rule_id, step, tag)
            return

        tname = names.get(target_ieee, target_ieee)
        target = devices.get(target_ieee)
        if not target or not hasattr(target, 'send_command'):
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "TARGET_ERROR",
                        f"{tag} {tname} not found or no send_command", level="ERROR")
            return

        self._trace(rule_id, "step", "SENDING",
                    f"{tag} → {tname} {command}={value} EP={endpoint_id}")
        try:
            result = await target.send_command(command, value, endpoint_id=endpoint_id)
            success = True
            if isinstance(result, dict):
                success = result.get("success", True)
            elif result is not None:
                success = bool(result)

            self._stats["executions"] += 1
            if success:
                self._stats["execution_successes"] += 1
                self._trace(rule_id, "step", "SUCCESS",
                            f"{tag} ✅ {tname} {command}={value}")
            else:
                self._stats["execution_failures"] += 1
                self._trace(rule_id, "step", "CMD_FAIL",
                            f"{tag} ❌ {tname} {command} failed", level="ERROR")

            if self._event_emitter:
                await self._event_emitter("automation_triggered", {
                    "rule_id": rule_id, "target_ieee": target_ieee,
                    "command": command, "value": value, "success": success,
                    "timestamp": time.time()})
        except Exception as e:
            self._stats["errors"] += 1
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "EXCEPTION",
                        f"{tag} 💥 {tname} {command}: {e}", level="ERROR",
                        traceback=traceback.format_exc())


    async def _step_request(self, rule_id, step, tag):
        """
        Send someone a message.

        The step type is still called "request" because saved rules carry it,
        but it now delivers through the messages store: the text lands in the
        recipient's conversation thread and goes out as a web push that wakes
        their phone. The old accept/decline-with-expiry flow was retired in
        its favour — a message the recipient can simply reply to closes the
        loop better than an escalation nobody asked for.
        """
        from modules.messages_store import get_message_store

        store = get_message_store()
        if not store:
            self._trace(rule_id, "step", "MESSAGE_SKIP",
                        f"{tag} Message store unavailable", level="WARNING")
            return

        to_user = step.get("to_user")
        message = (step.get("message") or "").strip()
        # Attribute the ask to a person where the rule names one, otherwise to
        # the system. "ZMM asks you to get milk" is odd but honest; inventing a
        # sender would be worse, since knowing who is asking is the point.
        from_user = step.get("from_user") or "zmm"

        result = await store.send(
            from_user=from_user,
            to_user=to_user,
            body=message,
            source="automation",
        )
        if result.get("success"):
            self._trace(rule_id, "step", "MESSAGE",
                        f"{tag} \u2709 messaged {to_user}: {message[:60]}")
        else:
            self._trace(rule_id, "step", "MESSAGE_FAIL",
                        f"{tag} Message failed: {result.get('error')}", level="WARNING")

    async def _step_offer(self, rule_id, step, tag):
        """Ask somebody, and remember what to run if they say yes.

        The message goes out through the same store as a plain message, tagged
        so a client can render it with an Accept control rather than as text.
        The action itself is held here, not in the message, so what runs is
        whatever the rule said — not whatever came back over the wire.
        """
        from modules.messages_store import get_message_store

        store = get_message_store()
        if not store:
            self._trace(rule_id, "step", "OFFER_SKIP",
                        f"{tag} Message store unavailable", level="WARNING")
            return

        to_user = step.get("to_user")
        message = (step.get("message") or "").strip()
        expires_in = step.get("expires_in", DEFAULT_OFFER_EXPIRY)

        self._expire_offers()
        # An offer nobody answers must not accumulate. The oldest goes first:
        # a stale question is the one least worth keeping.
        while len(self._offers) >= MAX_PENDING_OFFERS:
            oldest = min(self._offers, key=lambda t: self._offers[t]["created"])
            self._offers.pop(oldest, None)

        # One live offer per rule and recipient. A rule re-firing should replace
        # its own question, not queue a second copy of it.
        for token, existing in list(self._offers.items()):
            if existing["rule_id"] == rule_id and existing["to_user"] == to_user:
                self._offers.pop(token, None)

        token = uuid.uuid4().hex[:12]
        rule = self._find_rule(rule_id) or {}
        self._offers[token] = {
            "token": token,
            "rule_id": rule_id,
            "rule_name": rule.get("name") or rule_id,
            "to_user": to_user,
            "message": message,
            "accept_steps": step.get("accept_steps") or [],
            "created": time.time(),
            "expires_at": time.time() + float(expires_in),
            "state": "pending",
        }

        result = await store.send(
            from_user=step.get("from_user") or "zmm",
            to_user=to_user,
            body=message,
            source="automation_offer",
        )
        if result.get("success"):
            self._trace(rule_id, "step", "OFFER",
                        f"{tag} \u2753 offered {to_user}: {message[:60]}",
                        offer_token=token)
        else:
            # Nobody was told, so nothing can be accepted.
            self._offers.pop(token, None)
            self._trace(rule_id, "step", "OFFER_FAIL",
                        f"{tag} Offer failed: {result.get('error')}", level="WARNING")

    def _expire_offers(self) -> int:
        """Drop offers nobody answered in time. Lazy — no sweeper task."""
        now = time.time()
        stale = [t for t, o in self._offers.items()
                 if o["state"] == "pending" and o["expires_at"] <= now]
        for token in stale:
            offer = self._offers.pop(token)
            self._trace(offer["rule_id"], "offer", "EXPIRED",
                        f"Offer to {offer['to_user']} expired unanswered",
                        level="DEBUG")
        return len(stale)

    def get_offers(self, to_user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Offers still awaiting an answer, newest first."""
        self._expire_offers()
        out = [
            {k: v for k, v in o.items() if k != "accept_steps"}
            for o in self._offers.values()
            if o["state"] == "pending" and (not to_user or o["to_user"] == to_user)
        ]
        return sorted(out, key=lambda o: o["created"], reverse=True)

    async def accept_offer(self, token: str,
                           as_user: Optional[str] = None) -> Dict[str, Any]:
        """Run what the rule said to run if this offer were accepted.

        Removed before the sequence starts, so a double tap cannot run the
        action twice.
        """
        self._expire_offers()
        offer = self._offers.get(token)
        if not offer:
            return {"success": False, "error": "That offer has expired or was already answered"}
        if as_user and offer["to_user"] != as_user:
            return {"success": False, "error": "That offer was not addressed to you"}

        self._offers.pop(token, None)
        self._trace(offer["rule_id"], "offer", "ACCEPTED",
                    f"{offer['to_user']} accepted: {offer['message'][:60]}")

        task = asyncio.create_task(self._run_sequence(
            offer["rule_id"], offer["rule_name"], offer["accept_steps"], "ACCEPT"))
        # Tracked like any other sequence so a shutdown does not orphan it, and
        # untracked by its own task once finished rather than by rule id.
        key = f"offer:{token}"
        self._running_sequences[key] = task
        task.add_done_callback(
            lambda t, k=key: self._running_sequences.pop(k, None)
            if self._running_sequences.get(k) is t else None)
        return {"success": True, "rule_id": offer["rule_id"],
                "steps": len(offer["accept_steps"])}

    def decline_offer(self, token: str,
                      as_user: Optional[str] = None) -> Dict[str, Any]:
        """Answer no. Nothing runs; the offer simply goes away."""
        self._expire_offers()
        offer = self._offers.get(token)
        if not offer:
            return {"success": False, "error": "That offer has expired or was already answered"}
        if as_user and offer["to_user"] != as_user:
            return {"success": False, "error": "That offer was not addressed to you"}
        self._offers.pop(token, None)
        self._trace(offer["rule_id"], "offer", "DECLINED",
                    f"{offer['to_user']} declined: {offer['message'][:60]}")
        return {"success": True}

    async def _step_media(self, rule_id, step, tag):
        """Play radio/Tidal or control a media player (Cast/WiiM)."""
        svc = self._get_media_service() if self._get_media_service else None
        if not svc or not getattr(svc, "enabled", False):
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "MEDIA_UNAVAILABLE",
                        f"{tag} Media service not enabled", level="WARNING")
            return

        player_id = step.get("player_id")
        action = step.get("media_action")
        label = step.get("label") or action
        self._trace(rule_id, "step", "MEDIA", f"{tag} ♪ {label} → {player_id}")
        try:
            ok, detail = True, ""
            gid = svc.zone_id(player_id)
            if gid:
                ok, detail = await self._media_zone(svc, gid, action, step)
            elif action == "play_radio":
                # Favourited stations play from their pinned snapshot (no
                # directory lookup), so the rule still fires when the
                # radio-browser directory is unreachable; falls back to a
                # live lookup for non-favourited stations.
                await svc.play_radio_favourite(player_id, step["station_uuid"])
            elif action == "play_tidal":
                res = await svc.play_tidal(
                    player_id, step.get("tidal_kind"), step.get("tidal_id"),
                    step.get("tidal_mode", "play"))
                ok = res.get("success", False)
                detail = res.get("error", "") or f"{res.get('count', 0)} track(s)"
            elif action == "control":
                await svc.controller.control(player_id, step.get("control_action", "stop"))
            elif action == "volume":
                await svc.controller.set_volume(player_id, float(step.get("volume", 0.3)))
            elif action == "volume_adjust":
                delta = float(step.get("delta", 0.1))
                new = await svc.controller.adjust_volume(player_id, delta)
                detail = f"{'+' if delta >= 0 else ''}{int(delta * 100)}% → {int(new * 100)}%"
            elif action == "announce":
                res = await svc.announce(player_id, step.get("text", ""),
                                         volume=step.get("volume"))
                ok = res.get("success", False)
                detail = res.get("error", "")
            elif action == "volume_fade":
                # Fire-and-forget background ramp (wake-up / sleep-timer fade).
                svc.controller.fade_volume(
                    player_id, float(step.get("volume", 0.3)),
                    int(step.get("fade_seconds", 300)),
                    bool(step.get("stop_at_end", False)))
                detail = f"→ {int(float(step.get('volume', 0.3))*100)}% over {step.get('fade_seconds', 300)}s"
            else:
                ok, detail = False, f"unknown media_action '{action}'"

            self._stats["executions"] += 1
            if ok:
                self._stats["execution_successes"] += 1
                self._trace(rule_id, "step", "SUCCESS", f"{tag} ✅ {label} {detail}".rstrip())
            else:
                self._stats["execution_failures"] += 1
                self._trace(rule_id, "step", "MEDIA_FAIL",
                            f"{tag} ❌ {label}: {detail}", level="ERROR")
        except Exception as e:
            self._stats["errors"] += 1
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "EXCEPTION",
                        f"{tag} 💥 media {label}: {e}", level="ERROR",
                        traceback=traceback.format_exc())

    async def _media_zone(self, svc, gid, action, step):
        """A media step whose target is an OpenZone zone (``zone:<gid>``).

        Playback is one server-built timeline shared by every member, so
        anything that starts audio starts a session; volume stays a property of
        each speaker and fans out. Returns ``(ok, detail)``.
        """
        zone = getattr(svc, "cast_sync", None)
        if zone is None:
            return False, "OpenZone is disabled"
        if action == "play_zone":
            res = await svc.start_zone(gid, use_saved=True)
            return (res.get("success", False),
                    res.get("error", "") or "playing its saved source")
        if action == "play_radio":
            res = await svc.start_zone(
                gid, media={"station_uuid": step["station_uuid"]},
                use_saved=True)
            return res.get("success", False), res.get("error", "")
        if action == "play_tidal":
            # A zone walks a queue the engine re-resolves as it goes; it has no
            # auto-extending radio, so tidal_mode is not offered for a zone.
            res = await svc.start_zone(gid, media={
                "source_id": step.get("tidal_id", ""),
                "media_type": "tidal",
                "kind": step.get("tidal_kind", "track"),
                "title": step.get("label", "") or "Tidal",
            }, use_saved=True)
            return res.get("success", False), res.get("error", "")
        if action == "announce":
            # Spoken through the zone rather than device-by-device: one
            # timeline means one voice, not a room full of echoes. The source
            # is finite, so the session ends itself when it has been heard.
            if step.get("volume") is not None:
                await self._zone_volume(svc, gid, float(step["volume"]))
            text = (step.get("text") or "").strip()
            res = await svc.start_zone(gid, media={
                "url": svc.tts_url(text), "title": text[:60],
                "artist": "Announcement"})
            return res.get("success", False), res.get("error", "")
        if action == "control":
            act = step.get("control_action", "stop")
            if act == "stop" and zone.active_group != gid:
                return True, "already stopped"
            # Zones are ordinary players now, so the controller routes
            # stop/pause/resume/next/prev to the zone provider.
            await svc.controller.control(f"{svc.ZONE_PREFIX}{gid}", act)
            return True, ""
        if action == "volume":
            n = await self._zone_volume(svc, gid, float(step.get("volume", 0.3)))
            return n > 0, f"{n} speaker(s)" if n else "no members reachable"
        if action == "volume_adjust":
            delta = float(step.get("delta", 0.1))
            members = await svc.zone_members(gid)
            for pid in members:
                await svc.controller.adjust_volume(pid, delta)
            return bool(members), (f"{'+' if delta >= 0 else ''}"
                                   f"{int(delta * 100)}% on {len(members)} speaker(s)")
        if action == "volume_fade":
            members = await svc.zone_members(gid)
            for pid in members:
                svc.controller.fade_volume(
                    pid, float(step.get("volume", 0.3)),
                    int(step.get("fade_seconds", 300)),
                    bool(step.get("stop_at_end", False)))
            return bool(members), (f"→ {int(float(step.get('volume', 0.3)) * 100)}% "
                                   f"over {step.get('fade_seconds', 300)}s "
                                   f"on {len(members)} speaker(s)")
        return False, f"unknown media_action '{action}'"

    @staticmethod
    async def _zone_volume(svc, gid, volume):
        members = await svc.zone_members(gid)
        for pid in members:
            await svc.controller.set_volume(pid, volume)
        return len(members)

    async def _step_group_command(self, rule_id, step, tag):
        """Execute a command step targeting a group."""
        target_id_str = step["target_ieee"]
        command = step["command"]
        value = step.get("value")

        try:
            group_id = int(target_id_str.split(":", 1)[1])
        except (ValueError, IndexError):
            self._trace(rule_id, "step", "TARGET_ERROR",
                        f"{tag} Invalid group target: {target_id_str}", level="ERROR")
            return

        gm = self._get_group_manager() if self._get_group_manager else None
        if not gm or group_id not in gm.groups:
            self._stats["execution_failures"] += 1
            self._trace(rule_id, "step", "TARGET_ERROR",
                        f"{tag} Group {group_id} not found", level="ERROR")
            # A missing group is a config error, not a transient failure —
            # the rule will fail identically on every trigger. Disable it
            # and alert the user so they can retarget it. (Skip when the
            # registry itself isn't up yet — that IS transient.)
            if gm:
                self._disable_broken_rule(
                    rule_id, f"its target group {group_id} no longer exists"
                )
            return

        group_name = gm.groups[group_id]["name"]

        # Build command dict for control_group()
        cmd = {}
        if command in ("on", "off", "toggle"):
            cmd["state"] = command.upper()
        elif command == "brightness":
            cmd["brightness"] = int(value) if value is not None else 254
        elif command == "color_temp":
            cmd["color_temp"] = int(value) if value is not None else 370
        elif command in ("open", "close", "stop"):
            cmd["cover_state"] = command.upper()
        elif command == "position":
            cmd["position"] = int(value) if value is not None else 50
        elif command in ("lock", "unlock"):
            cmd["state"] = command.upper()
        else:
            cmd[command] = value

        self._trace(rule_id, "step", "SENDING",
                    f"{tag} → Group '{group_name}' {command}={value}")
        try:
            result = await gm.control_group(group_id, cmd)
            success = result.get("success", False)
            self._stats["executions"] += 1
            if success:
                self._stats["execution_successes"] += 1
                self._trace(rule_id, "step", "SUCCESS",
                            f"{tag} ✅ Group '{group_name}' {command}={value}")
            else:
                self._stats["execution_failures"] += 1
                self._trace(rule_id, "step", "CMD_FAIL",
                            f"{tag} ❌ Group '{group_name}' {command} failed: "
                            f"{result.get('error', '')}", level="ERROR")
        except Exception as e:
            self._stats["execution_failures"] += 1
            self._stats["errors"] += 1
            self._trace(rule_id, "step", "EXCEPTION",
                        f"{tag} 💥 Group '{group_name}' failed: {e}", level="ERROR")


    async def _step_wait_for(self, rule_id, step, tag) -> bool:
        ieee = step["ieee"]
        attr = step["attribute"]
        op = step["operator"]
        threshold = step["value"]
        timeout = step.get("timeout", 300) or 300

        dname, _ = self._resolve_state(ieee)
        self._trace(rule_id, "step", "WAITING",
                    f"{tag} ⏳ {dname} {attr} {op} {threshold} (timeout {timeout}s)")

        start = time.time()
        while time.time() - start < timeout:
            _, state = self._resolve_state(ieee)
            if state:
                val = state.get(attr)
                if val is not None:
                    try:
                        negate = step.get("negate", False)
                        matched = self._evaluate_condition(val, op, threshold)
                        if negate: matched = not matched
                        if matched:
                            el = time.time() - start
                            self._trace(rule_id, "step", "WAIT_MET",
                                        f"{tag} ✅ {dname} {attr}={repr(val)} met after {el:.1f}s")
                            return True
                    except Exception:
                        pass
            await asyncio.sleep(WAIT_FOR_POLL_INTERVAL)
        return False

    def _step_gate(self, rule_id, step, tag) -> bool:
        ieee = step["ieee"]
        attr = step["attribute"]
        op = step["operator"]
        threshold = step["value"]
        negate = step.get("negate", False)

        dname, state = self._resolve_state(ieee)
        if not state:
            return False
        val = state.get(attr)
        if val is None:
            return False
        try:
            result = self._evaluate_condition(val, op, threshold)
            if negate: result = not result
        except Exception:
            return False

        self._trace(rule_id, "step",
                    "GATE_PASS" if result else "GATE_FAIL",
                    f"{tag} {'🔒' if not result else '✅'} {dname} {attr} {op} {threshold}"
                    f"{' NOT' if negate else ''} → {repr(val)} → {'PASS' if result else 'FAIL'}",
                    level="DEBUG")
        return result

    async def _step_if_then_else(self, rule_id, rule_name, step, tag, depth):
        inline = step.get("inline_conditions", [])
        logic = step.get("condition_logic", "and")

        met, ic_results = self._eval_inline_conditions(inline, logic)

        branch_label = "if.then" if met else "if.else"
        self._trace(rule_id, "step", f"IF_{'TRUE' if met else 'FALSE'}",
                    f"{tag} IF ({logic.upper()}) → {'TRUE' if met else 'FALSE'}: "
                    f"running {branch_label}",
                    inline_conditions=ic_results)

        if met:
            sub_steps = step.get("then_steps", [])
        else:
            sub_steps = step.get("else_steps", [])

        if sub_steps:
            await self._run_sequence(rule_id, rule_name, sub_steps,
                                     f"{branch_label}", depth + 1)

    async def _step_parallel(self, rule_id, rule_name, step, tag, depth):
        branches = step.get("branches", [])
        self._trace(rule_id, "step", "PARALLEL",
                    f"{tag} ⚡ Running {len(branches)} branches in parallel")

        tasks = []
        for bi, branch in enumerate(branches):
            t = asyncio.create_task(
                self._run_sequence(rule_id, rule_name, branch,
                                   f"parallel.{bi+1}", depth + 1)
            )
            tasks.append(t)

        await asyncio.gather(*tasks, return_exceptions=True)
        self._trace(rule_id, "step", "PARALLEL_DONE",
                    f"{tag} All parallel branches complete")

    # CONDITION HELPERS

    def _resolve_value(self, value):
        """
        Resolve a value that points at another device's attribute.

        A step or a threshold normally carries a literal. A dict of the form
        {"ref": "<ieee>", "attribute": "value"} — or the {"worker": "<id>"}
        shorthand — reads it live instead, which is what lets one shared
        number drive many rules: change the worker, not the fifteen rules.

        An unresolvable reference returns None rather than a stale or invented
        number, so the comparison fails and the command is skipped instead of
        acting on a guess.
        """
        if not isinstance(value, dict):
            return value
        ieee = value.get("ref")
        if not ieee and value.get("worker"):
            ieee = f"worker::{str(value['worker']).lower()}"
        if not ieee:
            return None
        attribute = value.get("attribute") or "value"
        _, state = self._resolve_state(ieee)
        if not state:
            logger.debug(f"Value reference {ieee} not found")
            return None
        return state.get(attribute)

    def _evaluate_condition(self, actual_value, operator, threshold_value) -> bool:
        # Every comparison the engine makes — conditions, prerequisites, gates,
        # wait_for, inline branches — funnels through here, so resolving the
        # threshold at this one point makes references work everywhere at once.
        if isinstance(threshold_value, dict):
            threshold_value = self._resolve_value(threshold_value)
            if threshold_value is None:
                return False

        op_func = OPERATORS.get(operator)
        if not op_func:
            return False

        # Handle "in" / "nin" operators — threshold is a list
        if operator in ("in", "nin"):
            actual = self._normalise_value(actual_value)
            if isinstance(threshold_value, list):
                values = [self._normalise_value(v) for v in threshold_value]
            elif isinstance(threshold_value, str) and "," in threshold_value:
                values = [self._normalise_value(v.strip()) for v in threshold_value.split(",")]
            else:
                values = [self._normalise_value(threshold_value)]
            # Case-insensitive string matching
            matched = False
            for v in values:
                if isinstance(actual, str) and isinstance(v, str):
                    if actual.lower() == v.lower():
                        matched = True; break
                elif actual == v:
                    matched = True; break
            return matched if operator == "in" else not matched

        actual = self._normalise_value(actual_value)
        threshold = self._normalise_value(threshold_value)

        if isinstance(actual, str) and isinstance(threshold, str) and operator in ("eq", "neq"):
            if operator == "eq": return actual.lower() == threshold.lower()
            return actual.lower() != threshold.lower()

        if isinstance(actual, bool) and isinstance(threshold, str):
            threshold = threshold.lower() in ("on", "true")
        elif isinstance(threshold, bool) and isinstance(actual, str):
            actual = actual.lower() in ("on", "true")

        try:
            return op_func(actual, threshold)
        except (TypeError, ValueError):
            return op_func(str(actual).lower(), str(threshold).lower())

    @staticmethod
    def _normalise_value(value):
        if isinstance(value, str):
            stripped = value.strip().strip("'\"")
            lower = stripped.lower()
            if lower == "true": return True
            if lower == "false": return False
            try:
                if "." in stripped: return float(stripped)
                return int(stripped)
            except ValueError:
                return stripped
        return value

    # GROUP STATE HELPERS

    def _get_group_state(self, group_id: int) -> dict:
        """Aggregate state from group members.
        ON/OFF: any ON → ON. Numerics: average. Others: first value."""
        gm = self._get_group_manager() if self._get_group_manager else None
        if not gm or group_id not in gm.groups:
            return {}
        devices = self._get_devices()
        members = [devices.get(ieee) for ieee in gm.groups[group_id].get("members", [])
                   if devices.get(ieee)]
        if not members:
            return {}

        all_states = [m.state or {} for m in members]
        all_keys = set()
        for s in all_states:
            all_keys.update(s.keys())

        skip = {"last_seen", "available", "manufacturer", "model",
                "power_source", "lqi", "linkquality"}
        merged = {}
        for key in all_keys:
            if key in skip or key.endswith("_raw") or key.startswith("attr_"):
                continue
            values = [s[key] for s in all_states if key in s and s[key] is not None]
            if not values:
                continue
            first = values[0]
            if isinstance(first, str) and first.upper() in ("ON", "OFF"):
                merged[key] = "ON" if any(
                    v.upper() == "ON" for v in values if isinstance(v, str)
                ) else "OFF"
            elif isinstance(first, bool):
                merged[key] = any(values)
            elif isinstance(first, (int, float)):
                merged[key] = round(sum(values) / len(values), 1)
            else:
                merged[key] = first
        return merged

    def _resolve_state(self, ieee_or_group: str):
        """Resolve (friendly_name, state_dict) for device OR group:<id>.
        Returns (name, None) if not found."""
        if ieee_or_group.startswith("group:"):
            try:
                gid = int(ieee_or_group.split(":", 1)[1])
            except (ValueError, IndexError):
                return ieee_or_group, None
            gm = self._get_group_manager() if self._get_group_manager else None
            if not gm or gid not in gm.groups:
                return ieee_or_group, None
            return f"\U0001F517 {gm.groups[gid]['name']}", self._get_group_state(gid)

        devices = self._get_all_devices()
        names = self._get_all_names()
        dev = devices.get(ieee_or_group)
        if not dev:
            return names.get(ieee_or_group, ieee_or_group), None
        return names.get(ieee_or_group, ieee_or_group), dev.state or {}

    @staticmethod
    def _presence_value_options(attribute: str) -> Optional[List[str]]:
        """
        Enumerated values for a presence user's attributes, so the rule
        builder offers a dropdown instead of a free-text box nobody could
        guess place ids into.

        `place` lists the apiary: "home"/"away"/"unknown" plus every
        configured place id, read live so a place added a minute ago is
        offerable immediately.
        """
        if attribute == "presence":
            return ["home", "away", "unknown"]
        if attribute == "place":
            opts = ["home", "away", "unknown"]
            try:
                from modules.places import get_place_manager
                pm = get_place_manager()
                if pm:
                    opts += sorted(p["id"] for p in pm.list() if p.get("id"))
            except Exception:                     # noqa: BLE001
                # No place manager just means a shorter dropdown.
                pass
            return opts
        return None

    @staticmethod
    def _declared_value_options(dev, attribute: str) -> Optional[List[str]]:
        """
        Options a device declares for one of its own attributes, or None.

        Anything in the merged registry may offer `value_options(attribute)`.
        Workers use it so a mode's choices reach the rule builder as a dropdown
        instead of a free-text box the user has to spell an option into.
        """
        hook = getattr(dev, "value_options", None)
        if not callable(hook):
            return None
        try:
            opts = hook(attribute)
        except Exception:                         # noqa: BLE001
            # A provider with a broken hook loses its dropdown, nothing more.
            return None
        return [str(o) for o in opts] if opts else None

    def get_source_attributes(self, ieee: str) -> List[Dict[str, Any]]:
        # Merged view — matter/nuki/etc. devices trigger automations too
        devices = self._get_all_devices()
        if ieee not in devices: return []
        state = devices[ieee].state
        skip = {"last_seen","available","manufacturer","model","power_source","lqi","linkquality"}
        is_presence = ieee.startswith("user::")
        attrs = []
        for k, v in state.items():
            if k in skip or k.endswith("_raw") or k.startswith("attr_"): continue
            if isinstance(v, (list, dict)): continue
            a = {"attribute":k,"current_value":v,"type":self._type(v)}
            enum_opts = (self._presence_value_options(k) if is_presence
                         else self._declared_value_options(devices[ieee], k))
            if enum_opts:
                a["operators"]=["eq","neq","in","nin"]; a["value_options"]=enum_opts
            elif isinstance(v, bool):
                a["operators"]=["eq","neq"]; a["value_options"]=["true","false"]
            elif isinstance(v, str) and v.upper() in ("ON","OFF"):
                a["operators"]=["eq","neq","in","nin"]; a["value_options"]=["ON","OFF"]
            elif isinstance(v,(int,float)):
                a["operators"]=["eq","neq","gt","lt","gte","lte"]
            else:
                a["operators"]=["eq","neq","in","nin"]
            attrs.append(a)
        return sorted(attrs, key=lambda x:x["attribute"])

    def get_device_state(self, ieee: str) -> Dict[str, Any]:
        # GROUP TARGET
        if ieee.startswith("group:"):
            try:
                gid = int(ieee.split(":", 1)[1])
            except (ValueError, IndexError):
                return {}
            gm = self._get_group_manager() if self._get_group_manager else None
            if not gm or gid not in gm.groups:
                return {}
            group = gm.groups[gid]
            gstate = self._get_group_state(gid)
            attrs = []
            for k, v in gstate.items():
                a = {"attribute": k, "current_value": v, "type": self._type(v),
                     "operators": ["eq", "neq", "in", "nin"] if isinstance(v, str) else
                     ["eq", "neq"] if isinstance(v, bool) else
                     ["eq", "neq", "gt", "lt", "gte", "lte"]}
                if isinstance(v, bool):
                    a["value_options"] = ["true", "false"]
                elif isinstance(v, str) and v.upper() in ("ON", "OFF"):
                    a["value_options"] = ["ON", "OFF"]
                attrs.append(a)
            return {"ieee": ieee,
                    "friendly_name": f"\U0001F517 {group['name']}",
                    "state": gstate, "attributes": attrs}

        # NORMAL DEVICE
        devices = self._get_all_devices()
        names = self._get_all_names()
        if ieee not in devices: return {}
        state = devices[ieee].state or {}
        is_presence = ieee.startswith("user::")
        attrs = []
        for k, v in state.items():
            if k.endswith("_raw") or k.startswith("attr_"): continue
            if isinstance(v, (list, dict)): continue
            a = {"attribute": k, "current_value": v, "type": self._type(v),
                 "operators": ["eq", "neq", "in", "nin"] if isinstance(v, str) else
                 ["eq", "neq"] if isinstance(v, bool) else
                 ["eq", "neq", "gt", "lt", "gte", "lte"]}
            enum_opts = (self._presence_value_options(k) if is_presence
                         else self._declared_value_options(devices[ieee], k))
            if enum_opts:
                a["operators"] = ["eq", "neq", "in", "nin"]
                a["value_options"] = enum_opts
            elif isinstance(v, bool): a["value_options"] = ["true", "false"]
            elif isinstance(v, str) and v.upper() in ("ON", "OFF"): a["value_options"] = ["ON", "OFF"]
            attrs.append(a)
        return {"ieee": ieee, "friendly_name": names.get(ieee, ieee),
                "state": state, "attributes": attrs}

    def get_target_actions(self, ieee):
        d = self._get_all_devices().get(ieee)
        return d.get_control_commands() if d and hasattr(d,"get_control_commands") else []

    def get_actuator_devices(self):
        devices = self._get_all_devices(); names = self._get_all_names()
        out = []
        for ieee, dev in devices.items():
            caps = getattr(dev, "capabilities", None)
            if caps:
                # Zigbee device — capabilities object with has_capability()
                hc = getattr(caps, "has_capability", lambda x: False)
                # "worker" is here so a rule can set one; it is deliberately
                # not a real actuator capability, so nothing else treats a
                # worker as hardware.
                if not any(hc(c) for c in ["on_off", "light", "switch", "cover",
                                           "window_covering", "thermostat", "fan_control",
                                           "lock", "worker"]):
                    continue
            elif hasattr(dev, "_get_capabilities"):
                # Matter device — capabilities as a list
                cap_list = dev._get_capabilities()
                if not any(c in cap_list for c in ["on_off", "light", "switch", "cover",
                                                   "window_covering", "thermostat", "fan_control",
                                                   "lock"]):
                    continue
            else:
                continue
            out.append({"ieee": ieee, "friendly_name": names.get(ieee, ieee),
                        "model": getattr(dev, "model", "Unknown"),
                        "commands": dev.get_control_commands() if hasattr(dev, "get_control_commands") else []})

        # Append eligible homogeneous groups
        gm = self._get_group_manager() if self._get_group_manager else None
        if gm:
            for group_id, group in gm.groups.items():
                if not self._is_group_homogeneous(gm, group):
                    continue
                gtype = group.get("type", "switch")
                caps_list = group.get("capabilities", [])
                out.append({
                    "ieee": f"group:{group_id}",
                    "friendly_name": f"\U0001F517 {group['name']}",
                    "model": f"{gtype.capitalize()} Group ({len(group['members'])} devices)",
                    "commands": self._get_group_commands(gtype, caps_list),
                    "_is_group": True,
                })

        return sorted(out, key=lambda d: d.get("friendly_name", ""))


    @staticmethod
    def _get_group_commands(group_type: str, capabilities: list) -> list:
        """Generate command list for a group based on type and capabilities."""
        cmds = []
        if group_type in ("light", "switch"):
            cmds.extend([
                {"command": "on",     "label": "On",     "endpoint_id": None},
                {"command": "off",    "label": "Off",    "endpoint_id": None},
                {"command": "toggle", "label": "Toggle", "endpoint_id": None},
            ])
        if "brightness" in capabilities:
            cmds.append({"command": "brightness", "label": "Brightness",
                         "type": "slider", "min": 0, "max": 254, "endpoint_id": None})
        if "color_temp" in capabilities:
            cmds.append({"command": "color_temp", "label": "Color Temp",
                         "type": "slider", "min": 153, "max": 500, "endpoint_id": None})
        if group_type == "cover":
            cmds.extend([
                {"command": "open",     "label": "Open",     "endpoint_id": None},
                {"command": "close",    "label": "Close",    "endpoint_id": None},
                {"command": "stop",     "label": "Stop",     "endpoint_id": None},
                {"command": "position", "label": "Position",
                 "type": "slider", "min": 0, "max": 100, "endpoint_id": None},
            ])
        if group_type == "lock":
            cmds.extend([
                {"command": "lock",   "label": "Lock",   "endpoint_id": None},
                {"command": "unlock", "label": "Unlock", "endpoint_id": None},
            ])
        return cmds

    def _is_group_homogeneous(self, gm, group: dict) -> bool:
        """Check all members resolve to the same device type."""
        members = group.get("members", [])
        if len(members) < 2:
            return False
        types = set()
        for ieee in members:
            device = self._get_devices().get(ieee)
            if not device:
                continue
            dtype = gm.get_device_type(device)
            if dtype:
                types.add(dtype)
        return len(types) == 1

    def get_group_target_actions(self, group_id: int) -> list:
        """Get available commands for a group target."""
        gm = self._get_group_manager() if self._get_group_manager else None
        if not gm or group_id not in gm.groups:
            return []
        group = gm.groups[group_id]
        return self._get_group_commands(group.get("type", "switch"),
                                        group.get("capabilities", []))


    def get_all_devices_summary(self):
        devices = self._get_all_devices(); names = self._get_all_names()
        out = sorted([
            {"ieee": ieee, "friendly_name": names.get(ieee, ieee),
             "model": getattr(d, "model", "Unknown"),
             "state_keys": [k for k in (d.state or {}).keys()
                            if not k.endswith("_raw") and not k.startswith("attr_")
                            and not isinstance((d.state or {}).get(k), (list, dict))]}
            for ieee, d in devices.items()
        ], key=lambda x: x.get("friendly_name", ""))

        # Append homogeneous groups
        gm = self._get_group_manager() if self._get_group_manager else None
        if gm:
            for group_id, group in gm.groups.items():
                if not self._is_group_homogeneous(gm, group):
                    continue
                gstate = self._get_group_state(group_id)
                out.append({
                    "ieee": f"group:{group_id}",
                    "friendly_name": f"\U0001F517 {group['name']}",
                    "model": f"{group.get('type', 'switch').capitalize()} Group",
                    "state_keys": list(gstate.keys()),
                    "_is_group": True,
                })

        return out

    @staticmethod
    def _type(v):
        if isinstance(v,bool): return "boolean"
        if isinstance(v,int): return "integer"
        if isinstance(v,float): return "float"
        return "string"

    def get_stats(self):
        self._expire_offers()
        return {**self._stats, "total_rules":len(self.rules),
                "pending_offers":len(self._offers),
                "enabled_rules":sum(1 for r in self.rules if r.get("enabled",True)),
                "trace_entries":len(self._trace_log),
                "active_sustains":len(self._sustain_tracker),
                "running_sequences":sum(1 for t in self._running_sequences.values() if not t.done())
                                    + sum(len(self._live_runs(r)) for r in list(self._rule_runs))}