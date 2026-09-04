"""
Swarm Intelligence — diagnostics.

One report answering the question that actually gets asked: *why isn't the
suggestion I expected showing up?*

Failures in this layer are quiet by nature. A pattern that failed to load, a
pattern that loaded but matched nothing, a match that failed to compile, a
compile that failed validation, and a suggestion correctly withheld because the
rule already exists all present identically to a user — as an absence. Each has
a different fix, so each is reported separately and by name.

Nothing here changes state. It is safe to call on a live system.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from modules.swarm.capabilities import CAPABILITIES
from modules.swarm.dedupe import signature
from modules.swarm.stigmergy import get_stigmergy_store

logger = logging.getLogger("modules.swarm.diagnostics")

# Severity of a finding. `error` means something is broken; `warning` means the
# swarm is working but is blind to part of the house; `info` is context.
ERROR, WARNING, INFO = "error", "warning", "info"


def _finding(level: str, code: str, message: str, **extra) -> Dict[str, Any]:
    return {"level": level, "code": code, "message": message, **extra}


def diagnose(described: List[Dict[str, Any]],
             built: Optional[Dict[str, Any]] = None,
             rules: Optional[Iterable[Dict[str, Any]]] = None,
             rooms: Optional[Dict[str, str]] = None,
             commands_available: bool = True) -> Dict[str, Any]:
    """Full triage report for the swarm as it currently stands.

    `commands_available` is False when running against the state cache rather
    than a live registry: actuation is detected from a device's command list,
    which the cache does not hold, so every actuator would otherwise be reported
    as a device with no capabilities. A report must not raise an alarm about a
    limitation of its own input.
    """
    started = time.monotonic()
    rules = list(rules or [])
    rooms = rooms or {}
    store = get_stigmergy_store()
    findings: List[Dict[str, Any]] = []

    findings += _check_patterns_loaded(store)
    findings += _check_network(described, rooms)
    findings += _check_devices(described, commands_available)
    findings += _check_rules(rules, described)
    if built is not None:
        findings += _check_suggestions(built, store)

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    findings.sort(key=lambda f: (order.get(f["level"], 3), f["code"]))

    return {
        "generated": time.time(),
        "took_ms": round((time.monotonic() - started) * 1000, 1),
        "ok": not any(f["level"] == ERROR for f in findings),
        "counts": {
            "error": sum(1 for f in findings if f["level"] == ERROR),
            "warning": sum(1 for f in findings if f["level"] == WARNING),
            "info": sum(1 for f in findings if f["level"] == INFO),
        },
        "findings": findings,
    }


# Individual checks

def _check_patterns_loaded(store) -> List[Dict[str, Any]]:
    out = []
    errors = store.errors
    if errors:
        out.append(_finding(
            ERROR, "pattern_load_failed",
            f"{len(errors)} stigmergy pattern(s) failed to load and are not "
            f"being offered at all",
            details=errors[:20]))
    patterns = store.all()
    if not patterns:
        out.append(_finding(
            ERROR, "no_patterns",
            "No stigmergy patterns loaded, so no suggestions can be made. "
            "Check modules/swarm/patterns/ shipped with the release and holds "
            "valid JSON."))
    else:
        out.append(_finding(INFO, "patterns_loaded",
                            f"{len(patterns)} stigmergy pattern(s) loaded",
                            count=len(patterns)))
    return out


def _check_network(described: List[Dict[str, Any]],
                   rooms: Dict[str, str]) -> List[Dict[str, Any]]:
    out = []
    if not described:
        out.append(_finding(
            ERROR, "no_devices",
            "The swarm sees no devices. Either the automation engine has not "
            "started or its merged registry is empty."))
        return out

    if not rooms:
        out.append(_finding(
            WARNING, "no_rooms",
            "No chambers are defined, so every room-scoped pattern is "
            "unmatchable. Define rooms under `chambers:` in config.yaml, or "
            "adopt them from heating / the floor plan.",
            fix="Frames → chambers"))
        return out

    unplaced = [d for d in described if not d.get("room") and d["scope"] != "house"]
    if unplaced:
        level = WARNING if len(unplaced) < len(described) else ERROR
        out.append(_finding(
            level, "devices_unplaced",
            f"{len(unplaced)} device(s) are in no room, so room-scoped patterns "
            f"cannot pair them with anything",
            devices=[{"ieee": d["ieee"], "name": d["name"]} for d in unplaced[:25]],
            fix="assign a chamber on the device"))

    empty_rooms = sorted(set(rooms) - {d.get("room") for d in described})
    if empty_rooms:
        out.append(_finding(
            INFO, "rooms_empty",
            f"{len(empty_rooms)} room(s) hold no devices",
            rooms=[rooms[r] for r in empty_rooms[:25]]))
    return out


def _check_devices(described: List[Dict[str, Any]],
                   commands_available: bool = True) -> List[Dict[str, Any]]:
    """Devices the resolver could not make sense of.

    A device with no capabilities is invisible to the swarm — it can neither
    trigger nor be driven — and that is nearly always a missing profile or a
    device that has not finished interviewing rather than a device that truly
    does nothing.
    """
    out = []
    mute = [d for d in described if not d["capabilities"]]
    if mute:
        out.append(_finding(
            WARNING if commands_available else INFO,
            "devices_without_capabilities",
            f"{len(mute)} device(s) resolved to no capabilities and take no part "
            f"in the swarm. Usually an unfinished interview or a missing profile."
            if commands_available else
            f"{len(mute)} device(s) resolved to no capabilities. Reading the "
            f"state cache rather than a live registry, so actuators are expected "
            f"here — their capabilities come from a command list the cache does "
            f"not hold. Use /api/swarm/diagnostics for a definitive read.",
            devices=[{"ieee": d["ieee"], "name": d["name"],
                      "model": d.get("model")} for d in mute[:25]]))

    inert = [d for d in described
             if d["capabilities"] and not d["triggers"] and not d["actions"]]
    if inert and commands_available:
        out.append(_finding(
            INFO, "devices_without_offers",
            f"{len(inert)} device(s) have capabilities but offer no trigger or "
            f"action — they can be read as a condition only",
            devices=[{"ieee": d["ieee"], "name": d["name"],
                      "capabilities": d["capabilities"]} for d in inert[:25]]))

    # A capability that resolved but produced nothing is the harder case: it
    # counts as present, so it never appears in capabilities_absent, yet every
    # pattern needing it is blocked. Nearly always a device that declares a
    # cluster it has not reported an attribute for, or a service that is
    # configured but has no data yet.
    silent: List[Dict[str, Any]] = []
    for d in described:
        offered = {o["capability"] for o in d["triggers"] + d["conditions"] + d["actions"]}
        for cap in d["capabilities"]:
            spec = CAPABILITIES.get(cap) or {}
            if cap in offered:
                continue
            # A capability with nothing to offer in the vocabulary is silent by
            # design, not by fault.
            if not (spec.get("triggers") or spec.get("conditions") or spec.get("actions")):
                continue
            silent.append({"ieee": d["ieee"], "name": d["name"], "capability": cap,
                           "expected_attributes": list(spec.get("attrs") or []),
                           # What it does report, so a naming mismatch is
                           # visible rather than a guess. Finding that an Aqara
                           # socket reports `power_1` took reading a handler;
                           # it should have taken reading this line.
                           "reports": sorted(d.get("state_keys") or [])})
    if silent:
        caps = sorted({e["capability"] for e in silent})
        out.append(_finding(
            WARNING, "capabilities_silent",
            f"{len(silent)} capability(s) resolved on a device but produced no "
            f"trigger, condition or action ({', '.join(caps)}). They count as "
            f"present, so they do not show as absent, but every pattern needing "
            f"them is blocked. Usually a declared cluster the device has never "
            f"reported, or a service with no data yet.",
            entries=silent[:25]))

    # A capability nothing on the network provides is not a fault, but knowing
    # which ones are missing explains a whole class of absent suggestions.
    present = {c for d in described for c in d["capabilities"]}
    missing = sorted(set(CAPABILITIES) - present)
    out.append(_finding(
        INFO, "capabilities_absent",
        f"{len(missing)} capability(s) in the vocabulary are not present on any "
        f"device; patterns needing them cannot match",
        capabilities=missing))
    return out


def _check_rules(rules: List[Dict[str, Any]],
                 described: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    known = {d["ieee"] for d in described}
    unsignable, orphaned, disabled = [], [], []

    for rule in rules:
        try:
            signature(rule)
        except Exception as e:                                  # noqa: BLE001
            unsignable.append({"id": rule.get("id"), "name": rule.get("name"),
                               "error": f"{type(e).__name__}: {e}"})
            continue
        src = rule.get("source_ieee")
        if src and src != "__time__" and src not in known:
            orphaned.append({"id": rule.get("id"), "name": rule.get("name"),
                             "source_ieee": src})
        if not rule.get("enabled", True):
            disabled.append({"id": rule.get("id"), "name": rule.get("name")})

    if unsignable:
        out.append(_finding(
            ERROR, "rules_unsignable",
            f"{len(unsignable)} existing rule(s) could not be read for "
            f"deduplication, so suggestions may be re-offered for automations "
            f"that already exist",
            rules=unsignable[:25]))
    if orphaned:
        out.append(_finding(
            WARNING, "rules_orphaned",
            f"{len(orphaned)} rule(s) name a source device the swarm cannot see",
            rules=orphaned[:25]))
    if disabled:
        out.append(_finding(
            INFO, "rules_disabled",
            f"{len(disabled)} rule(s) are disabled; their suggestions come back "
            f"marked 'disabled' rather than 'available'",
            rules=disabled[:25]))
    return out


def _check_suggestions(built: Dict[str, Any], store) -> List[Dict[str, Any]]:
    out = []
    rejected = built.get("rejected") or []
    if rejected:
        by_stage: Dict[str, int] = {}
        for r in rejected:
            by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
        out.append(_finding(
            ERROR, "suggestions_rejected",
            f"{len(rejected)} candidate(s) were withheld because they failed to "
            f"compile or validate. These are defects in a pattern, not in the "
            f"network.",
            by_stage=by_stage, details=rejected[:20]))

    # A pattern that matched nowhere, with the reason from every scope it tried.
    matched = {t["pattern"] for t in built.get("trace", []) if t["outcome"] == "matched"}
    unmatched = []
    for pattern in store.all():
        if pattern["id"] in matched:
            continue
        reasons = sorted({t.get("reason") for t in built.get("trace", [])
                          if t["pattern"] == pattern["id"] and t.get("reason")})
        blocked = sorted({t.get("blocked_by") for t in built.get("trace", [])
                          if t["pattern"] == pattern["id"] and t.get("blocked_by")})
        unmatched.append({"id": pattern["id"], "title": pattern["title"],
                          "blocked_slots": blocked, "reasons": reasons[:3]})
    if unmatched:
        out.append(_finding(
            INFO, "patterns_unmatched",
            f"{len(unmatched)} pattern(s) matched nothing on this network",
            patterns=unmatched))

    summary = built.get("summary") or {}
    if summary.get("total") and not summary.get("available"):
        out.append(_finding(
            INFO, "all_suggestions_active",
            "Every suggestion the swarm can make is already built as a rule."))
    return out


# Explain one pattern

def explain(pattern_id: str, described: List[Dict[str, Any]],
            rooms: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Why one pattern matched, or did not, in every scope it was tried.

    The per-slot detail the suggestion list has no room for: which device filled
    each slot, what else could have, and for an unfilled slot which of the four
    distinct reasons applied.
    """
    from modules.swarm.matcher import match_pattern

    store = get_stigmergy_store()
    pattern = store.get(pattern_id)
    if not pattern:
        return {"error": f"unknown pattern {pattern_id!r}",
                "known": [p["id"] for p in store.all()]}

    result = match_pattern(pattern, described, rooms or {})
    return {
        "pattern": pattern,
        "outcome": "matched" if result["candidates"] else "no_match",
        "candidates": len(result["candidates"]),
        "trace": result["trace"],
    }


def offers_for_slot(slot: Dict[str, Any],
                    described: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every device anywhere that could fill this slot, ignoring room and class.

    Answers "does anything on this network make this offer at all?", which is
    the first question when a slot will not fill.
    """
    key = slot.get("offer", "")
    role = slot.get("role", "trigger")
    out = []
    for d in described:
        for offer in d.get(role + "s", []):
            if offer["key"] == key or offer["key"].startswith(key + ":"):
                out.append({"ieee": d["ieee"], "name": d["name"],
                            "room_label": d.get("room_label"),
                            "device_class": d.get("device_class"),
                            "offer": offer["key"]})
    return out
