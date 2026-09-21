"""
Swarm Intelligence — suggestions.

Matches every stigmergy pattern against the live network, compiles each fill to
a rule, checks it against the rules that already exist, and returns the result
grouped by room.

Every suggestion is validated through the engine's own validator before it is
returned. A suggestion that would fail at save is a bug in this layer, and it is
better caught here — where the trace says which pattern and which slot produced
it — than by a user pressing Create. Suggestions that fail validation are
withheld and reported to diagnostics rather than silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from modules.automation import RUN_MODES, TIME_SOURCE
from modules.swarm.capabilities import WORKER_TEMPLATES, worker_payload, worker_satisfies
from modules.swarm.compiler import (
    CompileError, compile_rule, describe_candidate, effective_params,
)
from modules.swarm.resolver import hub_device, proposed_worker_device
from modules.swarm.dedupe import coverage, index_rules, status_for
from modules.swarm.matcher import match_pattern
from modules.swarm.stigmergy import get_stigmergy_store

logger = logging.getLogger("modules.swarm.suggestions")

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def suggestion_id(pattern_id: str, fills: Dict[str, Dict[str, Any]]) -> str:
    """Stable across restarts: the same pattern on the same devices is the same
    suggestion, so a dismissal sticks and a preview URL keeps working."""
    payload = json.dumps(
        {"p": pattern_id,
         "f": {k: _fill_key(v) for k, v in sorted(fills.items())}},
        sort_keys=True)
    return "sg_" + hashlib.sha1(payload.encode()).hexdigest()[:12]


def _fill_key(fill: Dict[str, Any]) -> List[Any]:
    key: List[Any] = [fill["ieee"], fill["offer"]["key"]]
    if fill.get("members"):
        # A collected slot is every device it gathered: one more window in the
        # room is a different rule. Only added here, so existing ids are stable.
        key.append(sorted(m["ieee"] for m in fill["members"]))
    return key


def with_hub(described: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The network plus the hub itself.

    No registry lists the hub, but patterns fill slots from it — the hub
    starting, a season, quiet hours — so every pool a pattern matches against
    includes it. Kept out of the network view and coverage, where a device the
    user cannot see or place would only confuse.
    """
    if any(d.get("ieee") == TIME_SOURCE for d in described):
        return list(described)
    return list(described) + [hub_device()]


def templates_used(patterns: Iterable[Dict[str, Any]]) -> List[str]:
    """Every worker template a slot in these patterns asks for."""
    return sorted({spec["worker"] for p in patterns
                   for spec in (p.get("slots") or {}).values()
                   if isinstance(spec, dict) and spec.get("worker") in WORKER_TEMPLATES})


def with_synthetic(described: List[Dict[str, Any]],
                   patterns: Optional[Iterable[Dict[str, Any]]] = None
                   ) -> List[Dict[str, Any]]:
    """The network plus the hub, plus every worker a pattern needs and the house
    does not have yet.

    A proposed worker is only ever added where nothing satisfies its template —
    so an existing "House Mode" is used as it is — and never where a worker
    already holds its id with a different type, which creating it would clash
    with. Only a slot naming that template may fill from it.
    """
    pool = with_hub(described)
    wanted = templates_used(patterns) if patterns is not None else sorted(WORKER_TEMPLATES)
    for tid in wanted:
        if any(d.get("worker_id") == tid or worker_satisfies(d, tid) for d in pool):
            continue
        try:
            pool.append(proposed_worker_device(tid))
        except Exception:                                       # noqa: BLE001
            logger.exception(f"Could not propose worker {tid}")
    return pool


def _uses_the_network(fills: Dict[str, Dict[str, Any]]) -> bool:
    """Whether a candidate involves something real: a device, a person, or a
    worker that already exists — anything but the hub and a proposal."""
    return any(member["ieee"] != TIME_SOURCE and not member["device"].get("proposed_template")
               for fill in fills.values() for member in (fill.get("members") or [fill]))


def _creates_workers(fills: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The proposed workers a candidate uses — what applying it will create."""
    out: List[Dict[str, Any]] = []
    for fill in fills.values():
        for member in fill.get("members") or [fill]:
            tid = member["device"].get("proposed_template")
            if tid and all(w["id"] != tid for w in out):
                template = WORKER_TEMPLATES[tid]
                out.append({"id": tid, "ieee": member["ieee"], "name": template["name"],
                            "type": template["type"],
                            "options": list(template.get("options") or []),
                            "description": template.get("description")})
    return out


def worker_payloads(pattern: Dict[str, Any], suggestion: Dict[str, Any],
                    overrides: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """WorkerManager.create() payloads for the workers applying a suggestion
    needs, each starting at the card's parameter values."""
    params = effective_params(pattern, overrides)
    return [worker_payload(w["id"], params)
            for w in suggestion.get("creates_workers") or []]


def _confidence(pattern: Dict[str, Any], candidate: Dict[str, Any]) -> str:
    """How strongly this fill is the pattern working as intended.

    Every optional slot that filled is evidence: the dark check found a lux
    sensor, the off-branch found the same light. A pattern reduced to its
    mandatory slots still works, but it is a weaker suggestion than the whole
    shape landing.
    """
    slots = pattern["slots"]
    optional = [n for n, s in slots.items() if s.get("optional")]
    if not optional:
        return "high"
    filled = sum(1 for n in optional if n in candidate["fills"])
    if filled == len(optional):
        return "high"
    return "medium" if filled else "low"


def build(described: List[Dict[str, Any]],
          rules: Optional[Iterable[Dict[str, Any]]] = None,
          rooms: Optional[Dict[str, str]] = None,
          names: Optional[Dict[str, str]] = None,
          validator: Optional[Any] = None,
          patterns: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Every suggestion the swarm can make, with the trace behind it.

    `validator` is the AutomationEngine; when supplied, each compiled rule is put
    through its validation and anything rejected is withheld.
    """
    rules = list(rules or [])
    rooms = rooms or {}
    patterns = patterns if patterns is not None else get_stigmergy_store().all()
    described = with_synthetic(described, patterns)
    names = names or {d["ieee"]: d["name"] for d in described}
    proposed = {d["ieee"] for d in described if d.get("proposed_template")}

    index = index_rules(rules)
    suggestions: List[Dict[str, Any]] = []
    traces: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for pattern in patterns:
        try:
            result = match_pattern(pattern, described, rooms)
        except Exception as e:                                  # noqa: BLE001
            logger.exception(f"Pattern {pattern['id']} failed to match")
            rejected.append({"pattern": pattern["id"], "stage": "match",
                             "error": f"{type(e).__name__}: {e}"})
            continue

        traces.extend(result["trace"])

        # The swarm suggests from the network. A candidate made only of the hub
        # and workers it would itself create — "switch House mode to night at
        # bedtime" in a house with nothing that reads House mode — is withheld,
        # and its trace says why rather than claiming a match.
        candidates = [c for c in result["candidates"] if _uses_the_network(c["fills"])]
        if result["candidates"] and not candidates:
            for t in result["trace"]:
                if t.get("outcome") == "matched":
                    t.update(outcome="no_match", candidates=0,
                             reason="it would use only the hub and workers the swarm "
                                    "proposes, and nothing on the network")

        for candidate in candidates:
            try:
                rule = compile_rule(pattern, candidate["fills"],
                                    room_label=candidate.get("room_label"))
            except CompileError as e:
                rejected.append({"pattern": pattern["id"],
                                 "room": candidate.get("room"),
                                 "stage": "compile", "error": str(e)})
                continue
            except Exception as e:                              # noqa: BLE001
                logger.exception(f"Pattern {pattern['id']} failed to compile")
                rejected.append({"pattern": pattern["id"],
                                 "room": candidate.get("room"), "stage": "compile",
                                 "error": f"{type(e).__name__}: {e}"})
                continue

            invalid = _validate(rule, validator, proposed)
            if invalid:
                rejected.append({"pattern": pattern["id"],
                                 "room": candidate.get("room"),
                                 "stage": "validate", "error": invalid,
                                 "rule": rule})
                continue

            sid = suggestion_id(pattern["id"], candidate["fills"])
            suggestions.append({
                "id": sid,
                "pattern_id": pattern["id"],
                "title": pattern["title"],
                "description": pattern.get("description"),
                "category": pattern.get("category"),
                "room": candidate.get("room"),
                "room_label": candidate.get("room_label"),
                "confidence": _confidence(pattern, candidate),
                "sentence": describe_candidate(pattern, candidate["fills"]),
                "devices": [{"slot": k, "ieee": m["ieee"],
                             "name": m["device"]["name"], "offer": m["offer"]["key"],
                             "label": m["offer"]["label"],
                             "proposed": bool(m["device"].get("proposed_template"))}
                            for k, v in candidate["fills"].items()
                            for m in (v.get("members") or [v])],
                "creates_workers": _creates_workers(candidate["fills"]),
                "params": _exposed_params(pattern),
                "alternatives": candidate.get("alternatives") or {},
                "choosable": choosable_slots(pattern),
                "rule": rule,
                **status_for(rule, index, subset_ok=bool(choosable_slots(pattern))),
            })

    suggestions.sort(key=lambda s: (
        s["status"] != "available",
        -CONFIDENCE_ORDER.get(s["confidence"], 0),
        s.get("room_label") or "￿",
        s["title"],
    ))

    return {
        "suggestions": suggestions,
        "coverage": coverage(described, rules),
        "summary": _summarise(suggestions, patterns, traces),
        "trace": traces,
        "rejected": rejected,
    }


def _validate(rule: Dict[str, Any], validator: Any,
              proposed: Iterable[str] = ()) -> Optional[str]:
    """Run a compiled rule through the engine's own validation, without saving.

    Reusing the engine's validators rather than re-implementing them is the
    point: a suggestion is only trustworthy if it passes the same checks the
    save path applies.

    The one check a proposed worker cannot pass yet is that it exists — applying
    creates it before the rule — so that complaint, about one of those, is not
    a defect.
    """
    proposed = tuple(proposed)
    if validator is None:
        return None
    try:
        err = validator._validate_conditions(list(rule["conditions"]))
        if err:
            return err
        err = validator._validate_prerequisites(list(rule["prerequisites"]))
        if err:
            return err
        err = validator._validate_sequence(list(rule["then_sequence"]), "THEN")
        if err:
            return err
        err = validator._validate_sequence(list(rule["else_sequence"]), "ELSE")
        if err:
            return err
        if rule.get("run_mode", "restart") not in RUN_MODES:
            return f"run_mode {rule.get('run_mode')!r} is not one of {RUN_MODES}"
        err = validator._validate_zone_source(list(rule["conditions"]),
                                              rule["source_ieee"])
        if err and not any(ieee in err for ieee in proposed):
            return err
    except Exception as e:                                      # noqa: BLE001
        return f"validator raised {type(e).__name__}: {e}"
    return None


def _exposed_params(pattern: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The parameters a suggestion card should offer as fields.

    Only pattern-level ones: a slot override exists because that slot needs a
    different value from the rest of the pattern, so exposing it as one shared
    field would re-introduce the conflict it was added to resolve.
    """
    from modules.swarm.capabilities import PARAMS
    values = effective_params(pattern)
    return [{"id": pid, **PARAMS[pid], "value": values[pid]}
            for pid in sorted(pattern.get("params") or {}) if pid in PARAMS]


def _summarise(suggestions: List[Dict[str, Any]], patterns: List[Dict[str, Any]],
               traces: List[Dict[str, Any]]) -> Dict[str, Any]:
    matched_patterns = {t["pattern"] for t in traces if t["outcome"] == "matched"}
    by_category: Dict[str, int] = {}
    for s in suggestions:
        if s["status"] == "available":
            by_category[s.get("category") or "other"] = \
                by_category.get(s.get("category") or "other", 0) + 1
    return {
        "patterns": len(patterns),
        "patterns_matched": len(matched_patterns),
        "patterns_unmatched": len(patterns) - len(matched_patterns),
        "total": len(suggestions),
        "available": sum(1 for s in suggestions if s["status"] == "available"),
        "active": sum(1 for s in suggestions if s["status"] == "active"),
        "disabled": sum(1 for s in suggestions if s["status"] == "disabled"),
        "by_category": by_category,
    }


def find(built: Dict[str, Any], suggestion_id_: str) -> Optional[Dict[str, Any]]:
    for s in built["suggestions"]:
        if s["id"] == suggestion_id_:
            return s
    return None


def choosable_slots(pattern: Dict[str, Any]) -> List[str]:
    """Collected action slots: every light, say, of which the user may untick some."""
    return sorted(name for name, spec in (pattern.get("slots") or {}).items()
                  if spec.get("role") == "action" and spec.get("collect"))


def without_members(pattern: Dict[str, Any], fills: Dict[str, Dict[str, Any]],
                    exclude: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """``fills`` with the excluded devices dropped from choosable slots only.

    Emptying a required slot is refused rather than compiled to a rule that
    does nothing; emptying an optional one drops the slot.
    """
    exclude = {str(e).lower() for e in exclude or ()}
    if not exclude:
        return fills
    slots = pattern.get("slots") or {}
    out = dict(fills)
    for name in choosable_slots(pattern):
        fill = out.get(name)
        if not fill:
            continue
        kept = [m for m in (fill.get("members") or [fill])
                if str(m["ieee"]).lower() not in exclude]
        if not kept:
            if not slots[name].get("optional"):
                raise CompileError(f"leave at least one device in {name!r}")
            out.pop(name)
            continue
        out[name] = {"ieee": kept[0]["ieee"], "device": kept[0]["device"],
                     "offer": kept[0]["offer"], "members": kept}
    return out


def recompile(pattern: Dict[str, Any], suggestion: Dict[str, Any],
              described: List[Dict[str, Any]],
              overrides: Optional[Dict[str, Any]] = None,
              rooms: Optional[Dict[str, str]] = None,
              exclude: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Rebuild one suggestion's rule with user-supplied parameter values.

    Applying a suggestion re-matches rather than trusting a rule carried back
    from the client: the network may have changed since it was offered, and a
    client-supplied rule is a client-supplied rule. ``exclude`` only ever
    narrows a choosable slot, so it cannot add a device the match did not.
    """
    result = match_pattern(pattern, with_synthetic(described, [pattern]), rooms or {})
    for candidate in result["candidates"]:
        if suggestion_id(pattern["id"], candidate["fills"]) == suggestion["id"]:
            fills = without_members(pattern, candidate["fills"], exclude or ())
            return compile_rule(pattern, fills, overrides,
                                candidate.get("room_label"))
    raise CompileError(
        f"suggestion {suggestion['id']} no longer matches — the devices it used "
        f"may have moved room, been renamed, or gone offline")
