"""
Swarm Intelligence — the compiler.

Turns a matched stigmergy pattern into the rule dict `AutomationEngine.add_rule()`
accepts. Nothing here is a new execution path: the output is the same JSON a
hand-built rule produces, and it goes through the engine's own validation.

The one piece of real judgement is where a check belongs. The engine evaluates
`conditions` against the *source* device's state and `prerequisites` against any
other device, so a condition drawn from the trigger's own device is a condition,
and the identical check drawn from a second device is a prerequisite. Getting
that wrong produces a rule that validates and never fires, which is exactly the
failure this layer exists to prevent — so it is decided here, from the resolved
devices, rather than written into each pattern by hand.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional

from modules.swarm.capabilities import PARAMS, param_display, resolve_param
from modules.swarm.stigmergy import RESERVED_PLACEHOLDERS

logger = logging.getLogger("modules.swarm.compiler")


class CompileError(Exception):
    """A pattern that cannot produce a valid rule from these fills."""


def effective_params(pattern: Dict[str, Any],
                     overrides: Optional[Dict[str, Any]] = None,
                     slot: Optional[str] = None) -> Dict[str, Any]:
    """Parameter values for one compile: defaults, pattern, slot, then the user.

    A slot may override a parameter the rest of the pattern shares. Some
    thresholds mean different things in different places — `cold_c` is a cold
    snap at 5 degrees outdoors and an unheated room at 18 indoors — and a
    pattern comparing the two would otherwise have to pick one and be wrong
    about the other.
    """
    out = {pid: spec["default"] for pid, spec in PARAMS.items()}
    out.update(pattern.get("params") or {})
    if slot:
        spec = (pattern.get("slots") or {}).get(slot) or {}
        out.update(spec.get("params") or {})
    out.update(overrides or {})
    return out


def _apply_param(offer: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Re-resolve an offer's tunable value against this compile's parameters.

    Offers are built with the vocabulary defaults, so a pattern that raises
    `dark_lux` to 11 has to re-substitute rather than inherit the 10 the offer
    was born with.
    """
    cond = copy.deepcopy(offer.get("condition") or {})
    pid = offer.get("param")
    if pid and pid in params and "value" in cond:
        cond["value"] = params[pid]
    spid = offer.get("sustain_param")
    if spid and spid in params:
        cond["sustain"] = int(params[spid])
    elif offer.get("sustain_param") is None and "sustain" in cond:
        pass
    return cond


def _substitute(value: Any, fills: Dict[str, Dict[str, Any]],
                params: Dict[str, Any], trigger_device: str,
                trigger_room: Optional[str]) -> Any:
    """Resolve `$slot`, `{"slot": id}`, reserved placeholders and `{"param": id}`.

    `$slot` inside a string resolves to that slot's device address — a message
    step naming its recipient. `{"slot": id}` is the step itself, for a nested
    sequence such as an offer's accept branch. Two markers rather than one,
    because "the address of the thing" and "the action on the thing" are
    different substitutions and conflating them reads ambiguously.
    """
    if isinstance(value, dict):
        if set(value) == {"param"}:
            pid = value["param"]
            if pid not in params:
                raise CompileError(f"unknown parameter {pid!r}")
            return params[pid]
        if set(value) == {"slot"}:
            fill = fills.get(value["slot"])
            if not fill:
                return None                 # dropped by the caller
            step = copy.deepcopy(fill["offer"].get("step") or {})
            pid = fill["offer"].get("param")
            if pid and pid in params and "value" in step:
                step["value"] = params[pid]
            return step
        return {k: _substitute(v, fills, params, trigger_device, trigger_room)
                for k, v in value.items()}
    if isinstance(value, list):
        # A slot marker that did not resolve leaves a hole, not a null step.
        return [x for x in
                (_substitute(v, fills, params, trigger_device, trigger_room)
                 for v in value) if x is not None]
    if isinstance(value, str):
        out = (value
               .replace("$trigger_device", trigger_device)
               .replace("$trigger_room", trigger_room or trigger_device))
        for token in re.findall(r"\$[a-zA-Z_][a-zA-Z0-9_]*", out):
            if token in RESERVED_PLACEHOLDERS:
                continue
            slot = token[1:]
            fill = fills.get(slot)
            if not fill:
                raise CompileError(f"literal references ${slot}, which is unfilled")
            out = out.replace(token, fill["ieee"])
        return out
    return value


def compile_rule(pattern: Dict[str, Any], fills: Dict[str, Dict[str, Any]],
                 overrides: Optional[Dict[str, Any]] = None,
                 room_label: Optional[str] = None) -> Dict[str, Any]:
    """Build the rule dict for one matched pattern.

    `fills` maps slot name to {"ieee", "device", "offer"} — the device chosen for
    that slot and the offer it supplies. Optional slots may be absent; every
    reference to an absent slot is dropped rather than compiled to nothing.
    """
    emits = pattern.get("emits") or {}
    params = effective_params(pattern, overrides)

    source_slot = emits.get("source")
    source_fill = fills.get(source_slot)
    if not source_fill:
        raise CompileError(f"source slot {source_slot!r} is unfilled")
    source_ieee = source_fill["ieee"]
    trigger_device = source_fill["device"]["name"]
    trigger_room = source_fill["device"].get("room_label")

    conditions: List[Dict[str, Any]] = []
    prerequisites: List[Dict[str, Any]] = []

    for slot in emits.get("conditions", []) or []:
        if not isinstance(slot, str):
            continue
        fill = fills.get(slot)
        if not fill:
            continue
        cond = _apply_param(fill["offer"],
                            effective_params(pattern, overrides, slot))
        if not cond:
            continue
        if fill["ieee"] == source_ieee:
            conditions.append(cond)
        elif cond.get("type") == "zone":
            # Zone conditions read `place`, which only the source device is
            # evaluated against; on another device the check is meaningless.
            raise CompileError(
                f"slot {slot!r} is a zone check on {fill['ieee']}, which is not "
                f"the trigger source — only the source device's place is read")
        else:
            prerequisites.append({
                "ieee": fill["ieee"],
                "attribute": cond["attribute"],
                "operator": cond["operator"],
                "value": cond["value"],
            })

    if not conditions:
        raise CompileError("no condition resolved on the source device")

    def sequence(field: str) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        for entry in emits.get(field, []) or []:
            if isinstance(entry, str):
                fill = fills.get(entry)
                if not fill:
                    continue
                step = copy.deepcopy(fill["offer"]["step"])
                pid = fill["offer"].get("param")
                slot_params = effective_params(pattern, overrides, entry)
                if pid and pid in slot_params and "value" in step:
                    step["value"] = slot_params[pid]
                steps.append(step)
            else:
                steps.append(_substitute(entry, fills, params,
                                         trigger_device, trigger_room))
        return steps

    then_seq = sequence("then")
    else_seq = sequence("else")
    if not then_seq and not else_seq:
        raise CompileError("no action step resolved")

    name = _substitute(emits.get("name") or pattern["title"], fills, params,
                       trigger_device, trigger_room)
    name = (str(name)
            .replace("{room}", room_label or trigger_room or trigger_device)
            .replace("{device}", trigger_device))[:100]

    return {
        "name": name,
        "source_ieee": source_ieee,
        "conditions": conditions,
        "condition_logic": emits.get("condition_logic", "and"),
        "prerequisites": prerequisites,
        "then_sequence": then_seq,
        "else_sequence": else_seq,
        "cooldown": emits.get("cooldown", 5),
    }


def describe_candidate(pattern: Dict[str, Any],
                       fills: Dict[str, Dict[str, Any]]) -> str:
    """A plain-English reading of a matched pattern, built from offer labels.

    The offers already carry sentence fragments written for a person — "someone
    is detected in Hallway", "turn on Light - Hallway" — so the reading is
    assembled from those rather than from the compiled rule. Reading the rule
    back gives "contact eq False", which is accurate and useless.
    """
    emits = pattern.get("emits") or {}

    def label(slot: str) -> Optional[str]:
        """The offer's sentence, re-rendered at this pattern's thresholds.

        Offers are built with the vocabulary defaults, so a pattern raising
        `cold_c` to 5 would otherwise describe itself as firing at 18 while
        compiling a rule that fires at 5.
        """
        fill = fills.get(slot)
        if not fill:
            return None
        offer = fill["offer"]
        pid = offer.get("param")
        template = offer.get("label_template")
        params = effective_params(pattern, slot=slot)
        if pid and template and pid in params:
            return template.replace("{value}", param_display(pid, params[pid]))
        return offer["label"]

    source = emits.get("source")
    clauses = [label(source)]
    for slot in emits.get("conditions", []) or []:
        if isinstance(slot, str) and slot != source:
            lbl = label(slot)
            if lbl:
                clauses.append(lbl)
    joiner = " and " if emits.get("condition_logic", "and") == "and" else " or "
    when = joiner.join(c for c in clauses if c)

    def steps(field: str) -> List[str]:
        out = []
        for entry in emits.get(field, []) or []:
            if isinstance(entry, str):
                lbl = label(entry)
                if lbl:
                    out.append(lbl)
            elif entry.get("type") == "delay":
                out.append(f"wait {resolve_param(entry.get('seconds'))}s")
            elif entry.get("type") == "request":
                who = fills.get(_recipient_slot(entry))
                out.append(f"message {who['device']['name']}" if who else "send a message")
            elif entry.get("type") == "offer":
                # What happens on acceptance is the whole point of an offer, so
                # the sentence says it rather than stopping at "ask someone".
                who = fills.get(_recipient_slot(entry))
                asked = f"ask {who['device']['name']}" if who else "ask somebody"
                accepts = []
                for nested in entry.get("accept_steps") or []:
                    if isinstance(nested, dict) and set(nested) == {"slot"}:
                        lbl = label(nested["slot"])
                        if lbl:
                            accepts.append(lbl)
                out.append(f"{asked} first, and only then {' and '.join(accepts)}"
                           if accepts else asked)
        return out

    text = f"When {when}, " + ", then ".join(steps("then"))
    undo = steps("else")
    if undo:
        text += " — otherwise " + ", then ".join(undo)
    return text


def _recipient_slot(step: Dict[str, Any]) -> str:
    to = str(step.get("to_user", ""))
    return to[1:] if to.startswith("$") else to


def describe_rule(rule: Dict[str, Any], names: Dict[str, str]) -> str:
    """A reading of an arbitrary compiled rule, in terms of its raw comparisons.

    The fallback for a rule with no pattern behind it — an existing hand-built
    rule, say. Where a pattern is available, describe_candidate() reads far
    better.
    """
    def dev(ieee: str) -> str:
        return names.get(ieee, ieee)

    parts = [f"When {dev(rule['source_ieee'])} "]
    conds = []
    for c in rule["conditions"]:
        if c.get("type") == "zone":
            conds.append(f"{c['event']}s {c['place']}")
        else:
            conds.append(f"{c['attribute']} {c['operator']} {c['value']}")
    joiner = " and " if rule.get("condition_logic", "and") == "and" else " or "
    parts.append(joiner.join(conds))
    for p in rule.get("prerequisites") or []:
        parts.append(f", and {dev(p['ieee'])} {p['attribute']} "
                     f"{p['operator']} {p['value']}")
    actions = []
    for s in rule.get("then_sequence") or []:
        if s.get("type") == "command":
            actions.append(f"{s['command']} {dev(s.get('target_ieee', ''))}")
        elif s.get("type") == "delay":
            actions.append(f"wait {s.get('seconds')}s")
        elif s.get("type") == "request":
            actions.append(f"message {dev(s.get('to_user', ''))}")
        else:
            actions.append(s.get("type", "?"))
    if actions:
        parts.append(" → " + ", then ".join(actions))
    return "".join(parts)
