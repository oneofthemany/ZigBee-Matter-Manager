"""
Notification rules: per-user alerts on device events, evaluated on the hub so
they fire with no browser open. See docs/notifications.md §Notification rules.

Edge-triggered: a rule fires on a transition between the state this engine last
saw for a device and its current state, never on a steady state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("notification_rules")

RULES_PATH = Path("./data/notification_rules.json")
# Cooldowns and last firings, kept apart from the rules because they change on every firing.
STATE_PATH = Path("./data/notification_rules_state.json")
STATE_SAVE_DELAY_S = 2.0      # a burst of firings becomes one write
SWEEP_INTERVAL_S = 30          # online/offline has no state-change event to ride on
COOLDOWN_MINUTES = (0, 1, 5, 15, 30, 60)

# The navbar bell's switches, kept per user and run as rules marked source="bell".
BELL_DEFAULTS = {"enabled": False, "deviceOffline": True, "deviceOnline": False,
                 "lowBattery": True, "thermostatReached": True, "suppressMinutes": 5}
# switch -> (trigger, title, message); titles and wording are the bell's own.
BELL_SWITCHES = {
    "deviceOffline": ("offline", "Device Offline", "{device} has gone offline"),
    "deviceOnline": ("online", "Device Online", "{device} is back online"),
    "lowBattery": ("low_battery", "Low Battery", None),
    "thermostatReached": ("temp_target_reached", "Target Temperature Reached", None),
}
BELL_SUPPRESS_MINUTES = (1, 5, 15, 30, 60)
SCOPES = ("all", "devices", "tab")
MAX_RULES_PER_USER = 200
MAX_TEXT = 200


def _first(state: Dict[str, Any], *keys: str) -> Any:
    """First key that is present and not None — JS `a ?? b` over state keys."""
    for k in keys:
        v = state.get(k)
        if v is not None:
            return v
    return None


def _motion(s: Dict[str, Any]) -> bool:
    return bool(s.get("occupancy") or s.get("motion") or s.get("presence"))


def _open(s: Dict[str, Any]) -> bool:
    # contact False means open in the ZCL convention used here
    return s.get("contact") is False or s.get("is_open") is True


def _battery(s: Dict[str, Any]) -> Any:
    return _first(s, "battery", "battery_percentage")


def _temp(s: Dict[str, Any]) -> Any:
    return _first(s, "temperature", "local_temperature", "internal_temperature")


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return str(v)


def _target_reached(p: Dict[str, Any], c: Dict[str, Any]) -> bool:
    target = _first(c, "occupied_heating_setpoint", "heating_setpoint")
    now = _first(c, "internal_temperature", "temperature", "local_temperature")
    before = _first(p, "internal_temperature", "temperature", "local_temperature")
    if not target or now is None or before is None:
        return False
    return before < target - 0.3 <= now


def _crossed(p: Dict[str, Any], c: Dict[str, Any], rule: Dict[str, Any], above: bool) -> bool:
    t, pt = _temp(c), _temp(p)
    if t is None or pt is None:
        return False
    thr = float(rule["threshold"])
    return (pt <= thr < t) if above else (pt >= thr > t)


ZONE_NAMES = "_zone_names"


def _zones_of(c: Dict[str, Any], group: str) -> str:
    """' (Drive, Porch)' for the camera zones the object is in. Signals are
    keyed by a zone's fixed id (`person_drive`); the names come alongside."""
    names = c.get(ZONE_NAMES) or {}
    ids = [k[len(group) + 1:] for k, v in c.items() if v is True and k.startswith(group + "_")]
    zones = sorted(names.get(i) or i.replace("_", " ") for i in ids)
    return f" ({', '.join(zones)})" if zones else ""


@dataclass(frozen=True)
class Trigger:
    match: Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]], bool]
    body: Callable[[str, Dict[str, Any], Dict[str, Any]], str]
    label: str
    persistent: bool = False
    needs_threshold: bool = False


# Keys and labels mirror TRIGGERS in static/js/notifications.js (the editor);
# tests/notifications checks the two lists agree.
TRIGGERS: Dict[str, Trigger] = {
    "motion_detected": Trigger(
        lambda p, c, r, ch: not _motion(p) and _motion(c),
        lambda n, c, r: f"Motion detected — {n}", "Motion detected"),
    "motion_cleared": Trigger(
        lambda p, c, r, ch: _motion(p) and not _motion(c),
        lambda n, c, r: f"Motion cleared — {n}", "Motion cleared"),
    "contact_opened": Trigger(
        lambda p, c, r, ch: not _open(p) and _open(c),
        lambda n, c, r: f"{n} opened", "Door / window opened"),
    "contact_closed": Trigger(
        lambda p, c, r, ch: _open(p) and not _open(c),
        lambda n, c, r: f"{n} closed", "Door / window closed"),
    "water_leak": Trigger(
        lambda p, c, r, ch: not p.get("water_leak") and bool(c.get("water_leak")),
        lambda n, c, r: f"🚨 Water leak — {n}", "Water leak detected", persistent=True),
    "smoke": Trigger(
        lambda p, c, r, ch: not p.get("smoke") and bool(c.get("smoke")),
        lambda n, c, r: f"🚨 Smoke detected — {n}", "Smoke detected", persistent=True),
    "vibration": Trigger(
        lambda p, c, r, ch: not p.get("vibration") and bool(c.get("vibration")),
        lambda n, c, r: f"Vibration — {n}", "Vibration / tamper"),
    # A repeat of the same action still arrives as a change event, so a second
    # identical press counts even though the stored value didn't move.
    "button_pressed": Trigger(
        lambda p, c, r, ch: bool(c.get("action")) and ("action" in ch or p.get("action") != c.get("action")),
        lambda n, c, r: f"{n}: {c.get('action')}", "Button pressed"),
    "low_battery": Trigger(
        lambda p, c, r, ch: _battery(c) is not None and _battery(c) <= 15
                            and (_battery(p) is None or _battery(p) > 15),
        lambda n, c, r: f"{n} battery at {_battery(c)}%", "Low battery (< 15%)", persistent=True),
    "offline": Trigger(
        lambda p, c, r, ch: p.get("available") is True and c.get("available") is False,
        lambda n, c, r: f"{n} is offline", "Device went offline"),
    "online": Trigger(
        lambda p, c, r, ch: p.get("available") is False and c.get("available") is True,
        lambda n, c, r: f"{n} is online", "Device came online"),
    "temp_target_reached": Trigger(
        lambda p, c, r, ch: _target_reached(p, c),
        lambda n, c, r: (f"{n} reached {_fmt(_first(c, 'internal_temperature', 'temperature', 'local_temperature'))}°C "
                         f"(target {_fmt(_first(c, 'occupied_heating_setpoint', 'heating_setpoint'))}°C)"),
        "Heating target reached"),
    "temp_above": Trigger(
        lambda p, c, r, ch: _crossed(p, c, r, above=True),
        lambda n, c, r: f"{n} now {_fmt(_temp(c))}°C (above {r['threshold']}°C)",
        "Temperature rises above threshold", needs_threshold=True),
    "temp_below": Trigger(
        lambda p, c, r, ch: _crossed(p, c, r, above=False),
        lambda n, c, r: f"{n} now {_fmt(_temp(c))}°C (below {r['threshold']}°C)",
        "Temperature drops below threshold", needs_threshold=True),
    "person_detected": Trigger(
        lambda p, c, r, ch: not p.get("person") and bool(c.get("person")),
        lambda n, c, r: f"Person seen — {n}{_zones_of(c, 'person')}", "Person seen on camera"),
    "vehicle_detected": Trigger(
        lambda p, c, r, ch: not p.get("vehicle") and bool(c.get("vehicle")),
        lambda n, c, r: f"Vehicle seen — {n}{_zones_of(c, 'vehicle')}", "Vehicle seen on camera"),
    "animal_detected": Trigger(
        lambda p, c, r, ch: not p.get("animal") and bool(c.get("animal")),
        lambda n, c, r: f"Animal seen — {n}{_zones_of(c, 'animal')}", "Animal seen on camera"),
    "valve_alarm": Trigger(
        lambda p, c, r, ch: not p.get("valve_alarm") and bool(c.get("valve_alarm")),
        lambda n, c, r: f"Valve alarm — {n}", "Valve alarm (TRV)", persistent=True),
    "window_open_trv": Trigger(
        lambda p, c, r, ch: not p.get("window_open") and bool(c.get("window_open")),
        lambda n, c, r: f"Window-open detected — {n}", "Window-open detected (TRV)"),
}


def _hhmm(v: Any) -> Optional[str]:
    if not v:
        return None
    try:
        h, m = (int(x) for x in str(v).split(":")[:2])
    except ValueError:
        raise ValueError(f"time must be HH:MM, got {v!r}")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time must be HH:MM, got {v!r}")
    return f"{h:02d}:{m:02d}"


def _text(v: Any) -> Optional[str]:
    v = (str(v).strip() if v is not None else "")[:MAX_TEXT]
    return v or None


_CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def normalise_rule(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an editor payload into the stored shape; raises ValueError."""
    trigger = data.get("trigger")
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    scope = data.get("scope") or "all"
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    devices = [str(d) for d in (data.get("devices") or [])] if scope == "devices" else []
    if scope == "devices" and not devices:
        raise ValueError("pick at least one device")
    tab = _text(data.get("tab")) if scope == "tab" else None
    if scope == "tab" and not tab:
        raise ValueError("pick a device tab")
    threshold = None
    if TRIGGERS[trigger].needs_threshold:
        try:
            threshold = float(data.get("threshold"))
        except (TypeError, ValueError):
            raise ValueError("this trigger needs a numeric threshold")
    try:
        cooldown = int(data.get("cooldownMinutes") or 0)
    except (TypeError, ValueError):
        cooldown = 0
    if cooldown not in COOLDOWN_MINUTES:
        raise ValueError(f"cooldown must be one of {COOLDOWN_MINUTES}")
    time_from, time_to = _hhmm(data.get("timeFrom")), _hhmm(data.get("timeTo"))
    if bool(time_from) != bool(time_to):
        raise ValueError("set both times of the window, or neither")
    camera = _text(data.get("camera"))
    if camera and not _CAMERA_ID_RE.match(camera):
        raise ValueError("unknown camera")
    return {
        "enabled": data.get("enabled", True) is not False,
        "trigger": trigger,
        # A camera whose snapshot goes with the notification; a rule on a
        # camera device uses that camera without being told.
        "camera": camera,
        "scope": scope,
        "devices": devices,
        "tab": tab,
        "threshold": threshold,
        "timeFrom": time_from,
        "timeTo": time_to,
        "cooldownMinutes": cooldown,
        "title": _text(data.get("title")),
        "message": _text(data.get("message")),
    }


class NotificationRuleStore:
    """Rules for every user in one JSON file; each rule carries its owner.

    Bell rules (source="bell") are derived from the user's bell settings by
    set_bell(); the rule API neither lists nor changes them."""

    def __init__(self, path: Path = RULES_PATH) -> None:
        self.path = Path(path)
        self.rules: Dict[str, Dict[str, Any]] = {}
        self.bell: Dict[str, Dict[str, Any]] = {}       # owner -> bell settings

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("[notification_rules] unreadable %s: %s", self.path, e)
            return
        self.rules = {r["id"]: r for r in raw.get("rules", []) if r.get("id") and r.get("owner")}
        self.bell = raw.get("bell") or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rules": list(self.rules.values()), "bell": self.bell},
                                  indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    def for_owner(self, owner: str) -> List[Dict[str, Any]]:
        """The owner's own rules, as listed in Settings; bell rules excluded."""
        return [r for r in self.rules.values() if r["owner"] == owner and not r.get("source")]

    def enabled(self) -> List[Dict[str, Any]]:
        return [r for r in self.rules.values() if r.get("enabled", True)]

    def create(self, owner: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if len(self.for_owner(owner)) >= MAX_RULES_PER_USER:
            raise ValueError(f"limit of {MAX_RULES_PER_USER} rules reached")
        rule = {"id": "rule-" + secrets.token_hex(6), "owner": owner, **normalise_rule(data)}
        self.rules[rule["id"]] = rule
        self.save()
        return rule

    def update(self, owner: str, rule_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """None when the rule doesn't exist or belongs to someone else."""
        current = self.rules.get(rule_id)
        if not current or current["owner"] != owner or current.get("source"):
            return None
        rule = {"id": rule_id, "owner": owner, **normalise_rule(data)}
        self.rules[rule_id] = rule
        self.save()
        return rule

    def delete(self, owner: str, rule_id: str) -> bool:
        current = self.rules.get(rule_id)
        if not current or current["owner"] != owner or current.get("source"):
            return False
        del self.rules[rule_id]
        self.save()
        return True

    def import_rules(self, owner: str, items: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
        """Adopt rules a browser kept locally; returns (imported, errors).

        Invalid rules are skipped and reported. A rule identical to one the owner
        already has is skipped silently: two tabs open at the first load after
        the move both upload the same local list."""
        imported, errors = 0, []
        have = [{k: v for k, v in r.items() if k not in ("id", "owner")} for r in self.for_owner(owner)]
        for item in items[:MAX_RULES_PER_USER]:
            try:
                rule = normalise_rule(item)
                if rule in have:
                    continue
                self.create(owner, item)
                have.append(rule)
                imported += 1
            except ValueError as e:
                errors.append(f"{item.get('title') or item.get('trigger')}: {e}")
        return imported, errors

    def bell_settings(self, owner: str) -> Dict[str, Any]:
        """The owner's bell settings; configured=False until they've saved any."""
        return {**BELL_DEFAULTS, **self.bell.get(owner, {}), "configured": owner in self.bell}

    def set_bell(self, owner: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Save bell settings and rebuild the owner's bell rules to match."""
        settings = {k: bool(data.get(k, BELL_DEFAULTS[k])) for k in BELL_DEFAULTS if k != "suppressMinutes"}
        try:
            suppress = int(data.get("suppressMinutes", BELL_DEFAULTS["suppressMinutes"]))
        except (TypeError, ValueError):
            suppress = -1
        if suppress not in BELL_SUPPRESS_MINUTES:
            raise ValueError(f"suppressMinutes must be one of {BELL_SUPPRESS_MINUTES}")
        settings["suppressMinutes"] = suppress
        self.bell[owner] = settings

        for rid in [rid for rid, r in self.rules.items() if r["owner"] == owner and r.get("source") == "bell"]:
            del self.rules[rid]
        if settings["enabled"]:
            for switch, (trigger, title, message) in BELL_SWITCHES.items():
                if settings[switch]:
                    rule = normalise_rule({"trigger": trigger, "title": title, "message": message,
                                           "cooldownMinutes": suppress})
                    rid = f"bell-{trigger}-{owner}"
                    self.rules[rid] = {"id": rid, "owner": owner, "source": "bell", **rule}
        self.save()
        return self.bell_settings(owner)


# Returns what reached the owner (pages / push counts) for a test to report; None is fine.
Deliver = Callable[[str, Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


class NotificationRuleEngine:
    """Evaluates enabled rules against device state transitions."""

    def __init__(self, store: NotificationRuleStore,
                 get_devices: Callable[[], Dict[str, Any]],
                 get_names: Callable[[], Dict[str, str]],
                 get_tabs: Callable[[], Dict[str, List[str]]],
                 deliver: Deliver,
                 clock: Callable[[], float] = time.time,
                 local_now: Callable[[], datetime] = datetime.now,
                 state_path: Optional[Path] = None) -> None:
        """state_path: where cooldowns and last firings survive a restart; None keeps them in memory."""
        self.store = store
        self._get_devices = get_devices
        self._get_names = get_names
        self._get_tabs = get_tabs
        self._deliver = deliver
        self._clock = clock
        self._local_now = local_now
        self._prev: Dict[str, Dict[str, Any]] = {}       # ieee -> last state seen, with "available"
        self._fired_at: Dict[str, float] = {}           # "rule|ieee" -> epoch s
        self._last: Dict[str, Dict[str, Any]] = {}      # rule id -> latest firing, for the rule list
        self._tasks: set = set()
        self._state_path = Path(state_path) if state_path else None
        self._save_pending = False
        self._load_state()

    # inputs

    def observe(self, ieee: str, changed: Dict[str, Any]) -> None:
        """State-change listener; must not block (runs inside automation.evaluate)."""
        device = self._get_devices().get(ieee)
        if device is None:
            return
        curr = self._snapshot(device)
        prev = self._prev.get(ieee)
        if prev is None:
            # First sight: keys that didn't just change still hold their old value.
            prev = {k: v for k, v in curr.items() if k not in changed}
        self._prev[ieee] = curr
        self._dispatch(self.evaluate(ieee, device, prev, curr, changed))

    def sweep_availability(self) -> None:
        """Catch online/offline flips, which change no state key."""
        for ieee, device in self._get_devices().items():
            prev = self._prev.get(ieee)
            curr = self._snapshot(device)
            self._prev[ieee] = curr
            if prev is not None and prev.get("available") != curr["available"]:
                self._dispatch(self.evaluate(ieee, device, prev, curr, {"available": curr["available"]}))

    async def run_sweeper(self, interval_s: float = SWEEP_INTERVAL_S) -> None:
        while True:
            try:
                self.sweep_availability()
            except Exception as e:
                logger.warning("[notification_rules] availability sweep failed: %s", e)
            await asyncio.sleep(interval_s)

    # evaluation

    def evaluate(self, ieee: str, device: Any, prev: Dict[str, Any], curr: Dict[str, Any],
                 changed: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """(owner, payload) for every rule this transition fires; records cooldowns."""
        out = []
        tabs = None
        name = self._get_names().get(ieee) or getattr(device, "friendly_name", None) or ieee[-8:]
        for rule in self.store.enabled():
            trigger = TRIGGERS.get(rule["trigger"])
            if trigger is None:
                continue
            if rule["scope"] == "devices" and ieee not in rule["devices"]:
                continue
            if rule["scope"] == "tab":
                if tabs is None:
                    tabs = self._safe_tabs()
                if ieee not in tabs.get(rule["tab"], []):
                    continue
            if not self._in_window(rule) or not self._cooled(rule, ieee):
                continue
            try:
                if not trigger.match(prev, curr, rule, changed):
                    continue
                zone_names = getattr(device, "zone_names", None)
                body = (rule["message"].replace("{device}", name) if rule.get("message")
                        else trigger.body(name, {**curr, ZONE_NAMES: zone_names} if zone_names else curr, rule))
            except Exception as e:
                logger.debug("[notification_rules] %s on %s: %s", rule["id"], ieee, e)
                continue
            self._fired_at[f"{rule['id']}|{ieee}"] = self._clock()
            self._last[rule["id"]] = {"at": self._clock(), "ieee": ieee, "device": name, "body": body}
            out.append((rule["owner"], {
                "rule_id": rule["id"], "ieee": ieee,
                "title": rule.get("title") or trigger.label, "body": body,
                # Same tag on push and in-page, so one device shows one notification.
                "tag": f"zmm-rule-{rule['id']}-{ieee}",
                "persistent": trigger.persistent,
                "camera": rule.get("camera"),
            }))
        return out

    def last_fired(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """Latest real firing (kept across restarts with a state_path); tests don't count."""
        return self._last.get(rule_id)

    async def send_test(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver the rule's notification now, through the real channels, without touching cooldowns."""
        trigger = TRIGGERS[rule["trigger"]]
        sample = "Test device"
        body = (rule["message"].replace("{device}", sample) if rule.get("message")
                else f"Test of “{trigger.label}” — this is what it will look like")
        result = await self._deliver(rule["owner"], {
            "rule_id": rule["id"], "ieee": None,
            "title": rule.get("title") or trigger.label, "body": body,
            "tag": f"zmm-rule-{rule['id']}-test", "persistent": False, "test": True,
            "camera": rule.get("camera"),
        })
        return result or {}

    # helpers

    @staticmethod
    def _snapshot(device: Any) -> Dict[str, Any]:
        state = dict(getattr(device, "state", None) or {})
        try:
            state["available"] = bool(device.is_available())
        except Exception:
            state["available"] = state.get("available", True) is not False
        return state

    def _safe_tabs(self) -> Dict[str, List[str]]:
        try:
            return self._get_tabs() or {}
        except Exception as e:
            logger.debug("[notification_rules] tabs unavailable: %s", e)
            return {}

    def _in_window(self, rule: Dict[str, Any]) -> bool:
        if not rule.get("timeFrom") or not rule.get("timeTo"):
            return True
        now = self._local_now()
        mins = now.hour * 60 + now.minute
        fh, fm = map(int, rule["timeFrom"].split(":"))
        th, tm = map(int, rule["timeTo"].split(":"))
        start, end = fh * 60 + fm, th * 60 + tm
        if start == end:
            return True
        if start < end:
            return start <= mins <= end
        return mins >= start or mins <= end        # wraps midnight

    def _cooled(self, rule: Dict[str, Any], ieee: str) -> bool:
        cd = rule.get("cooldownMinutes", 0) * 60
        last = self._fired_at.get(f"{rule['id']}|{ieee}")
        return not cd or last is None or self._clock() - last >= cd

    # persistence of cooldowns / last firings

    def _load_state(self) -> None:
        if not self._state_path:
            return
        try:
            raw = json.loads(self._state_path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("[notification_rules] ignoring unreadable %s: %s", self._state_path, e)
            return
        self._fired_at = {k: float(v) for k, v in (raw.get("fired_at") or {}).items()}
        self._last = dict(raw.get("last") or {})

    def _state_snapshot(self) -> Dict[str, Any]:
        """What's worth keeping: rules that still exist, cooldowns that haven't run out."""
        live = set(self.store.rules)
        horizon = self._clock() - max(COOLDOWN_MINUTES) * 60
        return {
            "fired_at": {k: v for k, v in self._fired_at.items()
                         if k.split("|", 1)[0] in live and v >= horizon},
            "last": {k: v for k, v in self._last.items() if k in live},
        }

    def _write_state(self, snapshot: Dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot))
        tmp.replace(self._state_path)

    async def _save_state_soon(self) -> None:
        try:
            await asyncio.sleep(STATE_SAVE_DELAY_S)
            self._save_pending = False
            # Off the event loop: firings arrive on it.
            await asyncio.to_thread(self._write_state, self._state_snapshot())
        except Exception as e:
            self._save_pending = False
            logger.warning("[notification_rules] saving cooldowns failed: %s", e)

    def _dispatch(self, firings: List[Tuple[str, Dict[str, Any]]]) -> None:
        if firings and self._state_path and not self._save_pending:
            self._save_pending = True
            task = asyncio.get_running_loop().create_task(self._save_state_soon())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        for owner, payload in firings:
            task = asyncio.get_running_loop().create_task(self._safe_deliver(owner, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe_deliver(self, owner: str, payload: Dict[str, Any]) -> None:
        try:
            await self._deliver(owner, payload)
        except Exception as e:
            logger.warning("[notification_rules] delivery to %s failed: %s", owner, e)


_engine: Optional[NotificationRuleEngine] = None


def get_rule_engine() -> Optional[NotificationRuleEngine]:
    return _engine


def set_rule_engine(engine: NotificationRuleEngine) -> None:
    global _engine
    _engine = engine
