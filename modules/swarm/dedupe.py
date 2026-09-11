"""
Swarm Intelligence — matching suggestions against rules that already exist.

Without this the suggestion list re-offers everything already built, which makes
it noise rather than a to-do list. With it, a candidate whose wiring is already
live comes back marked `active` and pointing at the rule, so the same list is
also a coverage report: what the swarm could do, minus what it already does.

Matching is by *wiring*, not by text. Two rules are the same automation when
they watch the same attributes on the same source and drive the same commands at
the same targets, whatever they are named and whatever thresholds they use — a
rule firing at 11 lux and a suggestion at 10 are the same automation, and
offering the second is not useful.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Set, Tuple

from modules.automation import TIME_SOURCE, TRIGGER_OPERATORS, iter_leaf_conditions

logger = logging.getLogger("modules.swarm.dedupe")

Signature = Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...], Tuple[str, ...]]

# Step types that change what an automation *is*, not just how it is tuned. A
# reminder repeating until a door shuts and a single message about the same door
# are different automations with the same trigger and recipient; a delay or a
# gate added to a hand-built rule is not.
SHAPE_STEP_TYPES = ("repeat", "offer", "snapshot", "restore", "wait_for")


def _walk_steps(steps: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    """Every step in a sequence, descending into branches and parallel arms."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        yield step
        for field in ("then_steps", "else_steps", "steps"):
            yield from _walk_steps(step.get(field) or [])
        for branch in step.get("branches") or []:
            yield from _walk_steps(branch)


def _targets(rule: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
    """(target, command) pairs a rule drives, from both sequences.

    A message counts as a target so that "leak detected -> tell Sean" is not
    re-offered; the recipient is the target and "message" the command.
    """
    out: Set[Tuple[str, str]] = set()
    for field in ("then_sequence", "else_sequence"):
        for step in _walk_steps(rule.get(field) or []):
            kind = step.get("type")
            if kind == "command" and step.get("target_ieee"):
                command = str(step.get("command", ""))
                # A word is part of the wiring where a number is not: setting
                # House mode to "away" and to "home" are two automations, while
                # a setpoint of 21 and one of 21.5 are the same one.
                if isinstance(step.get("value"), str) and step["value"]:
                    command = f"{command}:{step['value'].lower()}"
                out.add((str(step["target_ieee"]), command))
            elif kind == "request" and step.get("to_user"):
                out.add((str(step["to_user"]), "message"))
            elif kind == "media" and step.get("player_id"):
                out.add((str(step["player_id"]), str(step.get("media_action", ""))))
    return tuple(sorted(out))


def _watched(rule: Dict[str, Any]) -> Tuple[str, ...]:
    """What the rule's conditions read on its source device.

    Zone conditions read a place rather than an attribute, so they are recorded
    as the event they watch for — otherwise every zone rule on one person
    collapses to the same signature.
    """
    out: Set[str] = set()
    source = rule.get("source_ieee")
    for c in iter_leaf_conditions(rule.get("conditions")):
        ctype = c.get("type", "attribute")
        # A condition reading another device is qualified by it, so a rule on
        # A that watches B's occupancy does not sign the same as A's own.
        other = c.get("ieee") if c.get("ieee") not in (None, "", source) else None
        prefix = f"{other}:" if other else ""
        if ctype == "zone":
            out.add(f"{prefix}zone:{c.get('event')}:{c.get('place')}")
        elif ctype == "offline":
            out.add(f"{prefix}{'online' if c.get('negate') else 'offline'}")
        elif ctype == "time":
            out.add(f"time:{_part_of_day(c.get('at'))}")
        elif ctype == "time_window":
            out.add(f"time_window:{_part_of_day(c.get('time_from'))}-"
                    f"{_part_of_day(c.get('time_to'))}{_days(c)}")
        elif ctype == "sun":
            out.add(f"sun:{_sun_end(c.get('from'))}-{_sun_end(c.get('to'))}{_days(c)}")
        elif ctype == "webhook":
            out.add(f"webhook:{c.get('hook')}")
        elif ctype in ("date", "startup"):
            out.add(ctype)
        elif c.get("attribute"):
            # The operator is part of the wiring: "starts drawing power" and
            # "finishes" watch one attribute and are two automations. The
            # threshold is not, so a rule at 11 lux still matches one at 10.
            out.add(f"{prefix}{c['attribute']}:{c.get('operator')}")
    return tuple(sorted(out))


def _part_of_day(hhmm: Any) -> str:
    """Morning, afternoon, evening or night, for a clock time.

    When a schedule runs is part of what it is — heating up in the morning and
    down at bedtime are two automations on the same radiators — but the minute
    is a threshold: a rule at 07:05 is the suggestion at 07:00.
    """
    try:
        hour = int(str(hhmm).split(":")[0])
    except (TypeError, ValueError):
        return "any"
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _sun_end(end: Any) -> str:
    return str(end) if end in ("sunrise", "sunset") else _part_of_day(end)


def _days(cond: Dict[str, Any]) -> str:
    days = cond.get("days")
    if days is None or sorted(days) == list(range(7)):
        return ""
    return ":" + "".join(str(d) for d in sorted(days))


def _shape(rule: Dict[str, Any]) -> Tuple[str, ...]:
    """Which SHAPE_STEP_TYPES a rule's sequences use, anywhere in them."""
    found: Set[str] = set()
    for field in ("then_sequence", "else_sequence"):
        for step in _walk_steps(rule.get(field) or []):
            if step.get("type") in SHAPE_STEP_TYPES:
                found.add(step["type"])
            for nested in step.get("accept_steps") or []:
                if isinstance(nested, dict) and nested.get("type") in SHAPE_STEP_TYPES:
                    found.add(nested["type"])
    return tuple(sorted(found))


def signature(rule: Dict[str, Any]) -> Signature:
    """The wiring a rule represents. Thresholds and names are deliberately out."""
    return (str(rule.get("source_ieee", "")), _watched(rule), _targets(rule),
            _shape(rule))


def index_rules(rules: Iterable[Dict[str, Any]]) -> Dict[Signature, List[Dict[str, Any]]]:
    """Existing rules keyed by wiring, so a candidate can be looked up directly."""
    index: Dict[Signature, List[Dict[str, Any]]] = {}
    for rule in rules or []:
        try:
            index.setdefault(signature(rule), []).append(rule)
        except Exception as e:                                  # noqa: BLE001
            # A malformed saved rule must not take the whole suggestion list
            # down with it; it is reported by diagnostics instead.
            logger.warning(f"Could not sign rule {rule.get('id')}: {e}")
    return index


def status_for(compiled: Dict[str, Any],
               index: Dict[Signature, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Whether this compiled rule is already live, and which rule it matches."""
    matches = index.get(signature(compiled)) or []
    if not matches:
        return {"status": "available"}
    rule = matches[0]
    return {
        "status": "active" if rule.get("enabled", True) else "disabled",
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
    }


def coverage(described: List[Dict[str, Any]],
             rules: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Which devices participate in at least one rule, and which do not.

    A device counts as covered whether it triggers a rule or is driven by one —
    a bulb nobody has automated is a gap even though it can trigger nothing
    interesting on its own.
    """
    rules = list(rules or [])
    # The hub is not a device anyone owns or places; it is never a coverage gap.
    # Nor is a worker the swarm has only proposed.
    described = [d for d in described
                 if d.get("ieee") != TIME_SOURCE and not d.get("proposed_template")]
    sources = {str(r.get("source_ieee")) for r in rules}
    # A device a trigger condition names takes part as much as the source does.
    sources |= {str(c["ieee"]) for r in rules
                for c in iter_leaf_conditions(r.get("conditions")) if c.get("ieee")}
    targets = {t for r in rules for t, _ in _targets(r)}
    involved = sources | targets

    covered, uncovered = [], []
    for d in described:
        entry = {"ieee": d["ieee"], "name": d["name"],
                 "room_label": d.get("room_label"),
                 "device_class": d.get("device_class")}
        (covered if d["ieee"] in involved else uncovered).append(entry)

    return {
        "devices": len(described),
        "covered": len(covered),
        "uncovered": len(uncovered),
        "percent": round(100 * len(covered) / len(described)) if described else 0,
        "gaps": sorted(uncovered, key=lambda d: (d["room_label"] or "￿", d["name"])),
    }
