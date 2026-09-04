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

from modules.swarm.compiler import (
    CompileError, compile_rule, describe_candidate, effective_params,
)
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
         "f": {k: [v["ieee"], v["offer"]["key"]] for k, v in sorted(fills.items())}},
        sort_keys=True)
    return "sg_" + hashlib.sha1(payload.encode()).hexdigest()[:12]


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
    names = names or {d["ieee"]: d["name"] for d in described}
    patterns = patterns if patterns is not None else get_stigmergy_store().all()

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

        for candidate in result["candidates"]:
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

            invalid = _validate(rule, validator)
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
                "devices": [{"slot": k, "ieee": v["ieee"],
                             "name": v["device"]["name"], "offer": v["offer"]["key"],
                             "label": v["offer"]["label"]}
                            for k, v in candidate["fills"].items()],
                "params": _exposed_params(pattern),
                "alternatives": candidate.get("alternatives") or {},
                "rule": rule,
                **status_for(rule, index),
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


def _validate(rule: Dict[str, Any], validator: Any) -> Optional[str]:
    """Run a compiled rule through the engine's own validation, without saving.

    Reusing the engine's validators rather than re-implementing them is the
    point: a suggestion is only trustworthy if it passes the same checks the
    save path applies.
    """
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
        err = validator._validate_zone_source(list(rule["conditions"]),
                                              rule["source_ieee"])
        if err:
            return err
    except Exception as e:                                      # noqa: BLE001
        return f"validator raised {type(e).__name__}: {e}"
    return None


def _exposed_params(pattern: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The parameters a suggestion card should offer as fields."""
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


def recompile(pattern: Dict[str, Any], suggestion: Dict[str, Any],
              described: List[Dict[str, Any]],
              overrides: Optional[Dict[str, Any]] = None,
              rooms: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Rebuild one suggestion's rule with user-supplied parameter values.

    Applying a suggestion re-matches rather than trusting a rule carried back
    from the client: the network may have changed since it was offered, and a
    client-supplied rule is a client-supplied rule.
    """
    result = match_pattern(pattern, described, rooms or {})
    for candidate in result["candidates"]:
        if suggestion_id(pattern["id"], candidate["fills"]) == suggestion["id"]:
            return compile_rule(pattern, candidate["fills"], overrides,
                                candidate.get("room_label"))
    raise CompileError(
        f"suggestion {suggestion['id']} no longer matches — the devices it used "
        f"may have moved room, been renamed, or gone offline")
