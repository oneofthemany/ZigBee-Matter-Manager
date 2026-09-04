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

logger = logging.getLogger("modules.swarm.dedupe")

Signature = Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...]]


def _walk_steps(steps: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    """Every step in a sequence, descending into branches and parallel arms."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        yield step
        for field in ("then_steps", "else_steps"):
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
                out.add((str(step["target_ieee"]), str(step.get("command", ""))))
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
    for c in rule.get("conditions") or []:
        ctype = c.get("type", "attribute")
        if ctype == "zone":
            out.add(f"zone:{c.get('event')}:{c.get('place')}")
        elif ctype in ("time_window", "time", "sun"):
            out.add(ctype)
        elif c.get("attribute"):
            out.add(str(c["attribute"]))
    return tuple(sorted(out))


def signature(rule: Dict[str, Any]) -> Signature:
    """The wiring a rule represents. Thresholds and names are deliberately out."""
    return (str(rule.get("source_ieee", "")), _watched(rule), _targets(rule))


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
    sources = {str(r.get("source_ieee")) for r in rules}
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
