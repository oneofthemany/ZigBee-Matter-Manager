"""
Swarm Intelligence — the compiler.

Turns a matched stigmergy pattern into the rule dict `AutomationEngine.add_rule()`
accepts. Nothing here is a new execution path: the output is the same JSON a
hand-built rule produces, and it goes through the engine's own validation.

The one piece of real judgement is where a check belongs. A condition drawn from
the trigger's own device is a condition. The identical check drawn from a second
device is, by default, a prerequisite — checked when the rule fires, but never
itself a reason to fire. A slot may ask to be *reactive* instead, and then it
compiles to a trigger condition naming its own device, so that device changing
re-evaluates the rule too. Getting this wrong produces a rule that validates and
never fires — or fires when nobody meant it to — so it is decided here, from the
resolved devices and the slot's declaration, rather than written by hand.

Beyond one trigger and one action, a pattern can now use the whole engine:

  collect          a slot filled with *every* matching device in scope — an OR
                   (or AND) group of their conditions, or one step per device
  reactive         another device's condition as a live trigger condition
  sustain          a hold on a trigger ("open for 5 minutes")
  run_mode         what firing again while running does
  literal markers  in `emits` steps, see _substitute()
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from modules.automation import RUN_MODES, TRIGGER_OPERATORS
from modules.swarm.capabilities import PARAMS, coerce_param, param_display, resolve_param
from modules.swarm.stigmergy import RESERVED_PLACEHOLDERS

logger = logging.getLogger("modules.swarm.compiler")

# Condition types that read no device, and moments that are not a device update.
# Neither belongs to any device, so neither is ever a prerequisite or carries an
# `ieee`: they are conditions of the rule itself.
TEMPORAL_TYPES = ("date", "time_window", "sun", "time")
EVENT_TYPES = ("startup", "webhook")
# Readings only a trigger condition can make: a crossing, silence, or a change.
# On another device these are always reactive — as a prerequisite they would be
# checked at a moment that cannot contain them.
ALWAYS_REACTIVE_TYPES = ("zone", "offline")

_SLOT_PART = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)@(value|name)")
_SLOT_ALL = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)@all")
_SLOT_TOKEN = re.compile(r"\$[a-zA-Z_][a-zA-Z0-9_]*")


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

    A user override is coerced to the parameter's declared type and bounds, and
    one that cannot be is ignored rather than compiled: a card posting "6" for a
    repeat count, or "13-45" for a date, would otherwise reach the engine as a
    rule it refuses.
    """
    out = {pid: spec["default"] for pid, spec in PARAMS.items()}
    out.update(pattern.get("params") or {})
    if slot:
        spec = (pattern.get("slots") or {}).get(slot) or {}
        out.update(spec.get("params") or {})
    for pid, raw in (overrides or {}).items():
        value = coerce_param(pid, raw)
        if value is not None:
            out[pid] = value
    return out


def _apply_param(offer: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Re-resolve an offer's tunable values against this compile's parameters.

    Offers are built with the vocabulary defaults, so a pattern that raises
    `dark_lux` to 11 has to re-substitute rather than inherit the 10 the offer
    was born with — and the same goes for a trend's window and every parameter
    a typed condition (a date range, quiet hours) is made of.
    """
    cond = copy.deepcopy(offer.get("condition") or {})
    pid = offer.get("param")
    if pid and pid in params and "value" in cond:
        cond["value"] = params[pid]
    spid = offer.get("sustain_param")
    if spid and spid in params:
        cond["sustain"] = int(params[spid])
    wpid = offer.get("within_param")
    if wpid and wpid in params:
        cond["within"] = int(float(params[wpid]) * 60)      # minutes on a card
    for field, fpid in (offer.get("condition_params") or {}).items():
        if fpid in params:
            cond[field] = params[fpid]
    return cond


def _number(value: Any, params: Dict[str, Any]) -> Any:
    """A literal, or {"param": id} / {"param": id, "scale": n} resolved.

    A scale converts a card's unit to the engine's — minutes to the seconds a
    wait or a sustain takes — and makes the result a whole number, since the
    engine counts repeats and seconds in integers.
    """
    if not (isinstance(value, dict) and "param" in value
            and set(value) <= {"param", "scale"}):
        return value
    pid = value["param"]
    if pid not in params:
        raise CompileError(f"unknown parameter {pid!r}")
    resolved = params[pid]
    if value.get("scale") is not None:
        return int(round(float(resolved) * float(value["scale"])))
    return resolved


class _Splice(list):
    """Steps (or device ids) to be spliced into the list they appear in."""


def _members(fill: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every device a slot filled with: all of them for a collected slot."""
    return fill.get("members") or [fill]


class _Context:
    def __init__(self, pattern: Dict[str, Any], fills: Dict[str, Dict[str, Any]],
                 overrides: Optional[Dict[str, Any]], trigger_device: str,
                 trigger_room: Optional[str]) -> None:
        self.pattern = pattern
        self.slots = pattern.get("slots") or {}
        self.fills = fills
        self.overrides = overrides
        self.params = effective_params(pattern, overrides)
        self.trigger_device = trigger_device
        self.trigger_room = trigger_room

    def slot_params(self, slot: str) -> Dict[str, Any]:
        return effective_params(self.pattern, self.overrides, slot)

    def steps(self, slot: str) -> List[Dict[str, Any]]:
        """The action step of a slot — one per device for a collected slot."""
        fill = self.fills.get(slot)
        if not fill:
            return []
        params = self.slot_params(slot)
        out = []
        for member in _members(fill):
            step = copy.deepcopy(member["offer"].get("step") or {})
            pid = member["offer"].get("param")
            if pid and pid in params and "value" in step:
                step["value"] = params[pid]
            out.append(step)
        return out

    def condition(self, slot: str, member: Dict[str, Any]) -> Dict[str, Any]:
        """One device's condition for a slot, at this compile's parameters."""
        cond = _apply_param(member["offer"], self.slot_params(slot))
        sustain = (self.slots.get(slot) or {}).get("sustain")
        if sustain is not None and cond.get("type", "attribute") == "attribute":
            cond["sustain"] = int(_number(sustain, self.slot_params(slot)))
        return cond


def _place(cond: Dict[str, Any], ieee: str, source_ieee: str,
           reactive: bool) -> Tuple[str, Dict[str, Any]]:
    """Condition or prerequisite, for one device's reading. See the module doc."""
    ctype = cond.get("type", "attribute")
    if ctype in TEMPORAL_TYPES or ctype in EVENT_TYPES:
        cond.pop("ieee", None)
        return "condition", cond
    if ieee == source_ieee:
        return "condition", cond
    if reactive or ctype in ALWAYS_REACTIVE_TYPES \
            or cond.get("operator") in TRIGGER_OPERATORS:
        return "condition", {**cond, "ieee": ieee}
    return "prerequisite", {"ieee": ieee, "attribute": cond["attribute"],
                            "operator": cond["operator"], "value": cond["value"]}


def _substitute_text(text: str, ctx: _Context) -> str:
    """`$slot` → its address, `$slot@name` → its device's name, `$slot@value` →
    a live `{ieee.attribute}` placeholder the engine fills when the step runs,
    and the reserved `$trigger_device` / `$trigger_room`."""
    out = (text
           .replace("$trigger_device", ctx.trigger_device)
           .replace("$trigger_room", ctx.trigger_room or ctx.trigger_device))

    def part(match: "re.Match") -> str:
        slot, which = match.group(1), match.group(2)
        fill = ctx.fills.get(slot)
        if not fill:
            raise CompileError(f"literal references ${slot}, which is unfilled")
        first = _members(fill)[0]
        if which == "name":
            return first["device"]["name"]
        attribute = first["offer"].get("attribute") \
            or (first["offer"].get("condition") or {}).get("attribute")
        if not attribute:
            raise CompileError(f"${slot}@value: slot {slot!r} reads no attribute")
        return "{%s.%s}" % (first["ieee"], attribute)

    out = _SLOT_PART.sub(part, out)
    for token in _SLOT_TOKEN.findall(out):
        if token in RESERVED_PLACEHOLDERS:
            continue
        slot = token[1:]
        fill = ctx.fills.get(slot)
        if not fill:
            raise CompileError(f"literal references ${slot}, which is unfilled")
        out = out.replace(token, fill["ieee"])
    return out


def _substitute(value: Any, ctx: _Context) -> Any:
    """Resolve the markers a literal step may carry.

    `{"param": id}`, `{"param": id, "scale": n}`
        a parameter's value, optionally scaled (minutes → seconds)
    `{"slot": id}`
        that slot's step — every device's, for a collected slot
    `{"$cond": id, ...}`
        that slot's reading as `ieee`/`attribute`/`operator`/`value`, merged into
        the dict — a wait_for step, or a repeat's inline condition
    `"$slot"`, `"$slot@name"`, `"$slot@value"`
        in a string, see _substitute_text()
    `"$slot@all"`
        as a list element, every device id the slot collected

    A marker naming an unfilled optional slot drops out of the list it is in,
    rather than compiling to an empty step.
    """
    if isinstance(value, dict):
        keys = set(value)
        if "param" in keys and keys <= {"param", "scale"}:
            return _number(value, ctx.params)
        if keys == {"slot"}:
            steps = ctx.steps(value["slot"])
            return _Splice(steps) if steps else None
        if "$cond" in value:
            slot = value["$cond"]
            fill = ctx.fills.get(slot)
            if not fill:
                return None
            member = _members(fill)[0]
            cond = ctx.condition(slot, member)
            if cond.get("type", "attribute") != "attribute":
                raise CompileError(f"$cond {slot!r} is not a device reading, so it "
                                   f"cannot be waited for or tested inline")
            rest = {k: _substitute(v, ctx) for k, v in value.items() if k != "$cond"}
            return {**rest, "ieee": member["ieee"], "attribute": cond["attribute"],
                    "operator": cond["operator"], "value": cond["value"]}
        return {k: _substitute(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        out: List[Any] = []
        for item in value:
            if isinstance(item, str) and _SLOT_ALL.fullmatch(item):
                slot = item[1:-4]
                fill = ctx.fills.get(slot)
                if not fill:
                    raise CompileError(f"literal references ${slot}, which is unfilled")
                out.extend(m["ieee"] for m in _members(fill))
                continue
            resolved = _substitute(item, ctx)
            if resolved is None:
                continue
            if isinstance(resolved, _Splice):
                out.extend(resolved)
            else:
                out.append(resolved)
        return out
    if isinstance(value, str):
        return _substitute_text(value, ctx)
    return value


def compile_rule(pattern: Dict[str, Any], fills: Dict[str, Dict[str, Any]],
                 overrides: Optional[Dict[str, Any]] = None,
                 room_label: Optional[str] = None) -> Dict[str, Any]:
    """Build the rule dict for one matched pattern.

    `fills` maps slot name to {"ieee", "device", "offer"} — the device chosen for
    that slot and the offer it supplies — plus "members" for a collected slot.
    Optional slots may be absent; every reference to an absent slot is dropped
    rather than compiled to nothing.
    """
    emits = pattern.get("emits") or {}
    slots = pattern.get("slots") or {}

    source_slot = emits.get("source")
    source_fill = fills.get(source_slot)
    if not source_fill:
        raise CompileError(f"source slot {source_slot!r} is unfilled")
    source = _members(source_fill)[0]
    source_ieee = source["ieee"]
    ctx = _Context(pattern, fills, overrides, source["device"]["name"],
                   source["device"].get("room_label"))

    conditions: List[Dict[str, Any]] = []
    prerequisites: List[Dict[str, Any]] = []

    for slot in emits.get("conditions", []) or []:
        if not isinstance(slot, str):
            continue
        fill = fills.get(slot)
        if not fill:
            continue
        spec = slots.get(slot) or {}
        # A collected slot is watched as one: any of its devices changing must
        # re-evaluate the rule, so its members are always trigger conditions.
        reactive = bool(spec.get("reactive") or spec.get("collect"))
        placed = [_place(ctx.condition(slot, m), m["ieee"], source_ieee, reactive)
                  for m in _members(fill)]
        if spec.get("collect") and len(placed) > 1:
            conditions.append({"type": "group",
                               "condition_logic": spec.get("collect_logic", "or"),
                               "conditions": [cond for _, cond in placed]})
            continue
        for kind, cond in placed:
            (conditions if kind == "condition" else prerequisites).append(cond)

    if not conditions:
        raise CompileError("no condition resolved on the source device")

    def sequence(field: str) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        for entry in emits.get(field, []) or []:
            if isinstance(entry, str):
                steps.extend(ctx.steps(entry))
                continue
            resolved = _substitute(entry, ctx)
            if resolved is None:
                continue
            if isinstance(resolved, _Splice):
                steps.extend(resolved)
            else:
                steps.append(resolved)
        return steps

    then_seq = sequence("then")
    else_seq = sequence("else")
    if not then_seq and not else_seq:
        raise CompileError("no action step resolved")

    run_mode = emits.get("run_mode", "restart")
    if run_mode not in RUN_MODES:
        raise CompileError(f"run_mode {run_mode!r} is not one of {RUN_MODES}")

    name = _substitute(emits.get("name") or pattern["title"], ctx)
    name = (str(name)
            .replace("{room}", room_label or ctx.trigger_room or ctx.trigger_device)
            .replace("{device}", ctx.trigger_device))[:100]

    return {
        "name": name,
        "source_ieee": source_ieee,
        "conditions": conditions,
        "condition_logic": emits.get("condition_logic", "and"),
        "prerequisites": prerequisites,
        "then_sequence": then_seq,
        "else_sequence": else_seq,
        "cooldown": emits.get("cooldown", 5),
        "run_mode": run_mode,
    }


# Sentences

def _render_label(offer: Dict[str, Any], params: Dict[str, Any]) -> str:
    """An offer's sentence, re-rendered at this pattern's parameters.

    Offers are built with the vocabulary defaults, so a pattern raising `cold_c`
    to 5 would otherwise describe itself as firing at 18 while compiling a rule
    that fires at 5 — and the same for a trend's window or a date range.
    """
    template = offer.get("label_template")
    if not template:
        return offer["label"]
    text = template
    pid = offer.get("param")
    if pid and pid in params:
        text = text.replace("{value}", param_display(pid, params[pid]))
    wpid = offer.get("within_param")
    if wpid and wpid in params:
        text = text.replace("{window}", param_display(wpid, params[wpid]))
    for field, fpid in (offer.get("condition_params") or {}).items():
        if fpid in params:
            text = text.replace("{%s}" % field, param_display(fpid, params[fpid]))
    # A placeholder nothing filled means the template and offer disagree; the
    # sentence the offer was built with is at least true.
    return offer["label"] if re.search(r"\{[a-z_]+\}", text) else text


def _duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if s and s % 3600 == 0:
        return f"{s // 3600} h"
    if s >= 60 and s % 60 == 0:
        return f"{s // 60} min"
    return f"{s}s"


def describe_candidate(pattern: Dict[str, Any],
                       fills: Dict[str, Dict[str, Any]]) -> str:
    """A plain-English reading of a matched pattern, built from offer labels.

    The offers already carry sentence fragments written for a person — "someone
    is detected in Hallway", "turn on Light - Hallway" — so the reading is
    assembled from those rather than from the compiled rule. Reading the rule
    back gives "contact eq False", which is accurate and useless.
    """
    emits = pattern.get("emits") or {}
    slots = pattern.get("slots") or {}

    def label(slot: str, joiner: Optional[str] = None) -> Optional[str]:
        fill = fills.get(slot)
        if not fill:
            return None
        spec = slots.get(slot) or {}
        params = effective_params(pattern, slot=slot)
        members = _members(fill)
        texts = [_render_label(m["offer"], params) for m in members]
        if joiner is None and spec.get("collect") and len(texts) > 1:
            # A group reads in brackets, as the builder's humanizer shows one:
            # "leaves and (A is open, B is open or C is open)" is not ambiguous.
            logic = spec.get("collect_logic", "or")
            text = f"({', '.join(texts[:-1])} {logic} {texts[-1]})"
        else:
            text = (joiner or ", ").join(texts)
        if spec.get("sustain") is not None:
            text += f" for {_duration(_number(spec['sustain'], params))}"
        return text

    source = emits.get("source")
    clauses = [label(source)]
    for slot in emits.get("conditions", []) or []:
        if isinstance(slot, str) and slot != source:
            lbl = label(slot)
            if lbl:
                clauses.append(lbl)
    joiner = " and " if emits.get("condition_logic", "and") == "and" else " or "
    when = joiner.join(c for c in clauses if c)
    pattern_params = effective_params(pattern)

    def recipient(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return fills.get(_recipient_slot(step))

    def steps(entries: Any) -> List[str]:
        out: List[str] = []
        for entry in entries or []:
            if isinstance(entry, str):
                lbl = label(entry, joiner=", ")
                if lbl:
                    out.append(lbl)
                continue
            if not isinstance(entry, dict):
                continue
            if set(entry) == {"slot"}:
                lbl = label(entry["slot"], joiner=", ")
                if lbl:
                    out.append(lbl)
                continue
            kind = entry.get("type")
            if kind == "delay":
                out.append(f"wait {_duration(_number(entry.get('seconds'), pattern_params))}")
            elif kind == "request":
                who = recipient(entry)
                out.append(f"message {who['device']['name']}" if who else "send a message")
            elif kind == "offer":
                # What happens on acceptance is the whole point of an offer, so
                # the sentence says it rather than stopping at "ask someone".
                who = recipient(entry)
                asked = f"ask {who['device']['name']}" if who else "ask somebody"
                accepts = steps(entry.get("accept_steps"))
                out.append(f"{asked} first, and only then {' and '.join(accepts)}"
                           if accepts else asked)
            elif kind == "wait_for":
                slot = entry.get("$cond")
                out.append(f"wait until {label(slot)}" if slot and fills.get(slot)
                           else "wait")
            elif kind == "repeat":
                inner = ", then ".join(steps(entry.get("steps")))
                mode = entry.get("mode", "count")
                if mode == "count":
                    head = f"{_number(entry.get('count'), pattern_params)} times"
                else:
                    conds = [label(c.get("$cond")) for c in entry.get("inline_conditions") or []
                             if isinstance(c, dict) and fills.get(c.get("$cond"))]
                    cap = _number(entry.get("max_iterations", 20), pattern_params)
                    head = f"{mode} {' and '.join(c for c in conds if c) or 'done'} " \
                           f"(at most {cap} times)"
                out.append(f"repeat {head}: {inner}")
            elif kind == "snapshot":
                out.append("remember how the lights are")
            elif kind == "restore":
                out.append("put them back as they were")
        return out

    text = f"When {when}, " + ", then ".join(steps(emits.get("then")))
    undo = steps(emits.get("else"))
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

    def cond_text(c: Dict[str, Any]) -> str:
        if c.get("type") == "group":
            inner = f" {c.get('condition_logic', 'and')} ".join(
                cond_text(x) for x in c.get("conditions") or [])
            return f"({inner})"
        if c.get("type") == "zone":
            return f"{c['event']}s {c['place']}"
        if c.get("type") in TEMPORAL_TYPES + EVENT_TYPES + ("offline",):
            return c["type"]
        who = f"{dev(c['ieee'])} " if c.get("ieee") else ""
        return f"{who}{c.get('attribute')} {c.get('operator')} {c.get('value')}"

    parts = [f"When {dev(rule['source_ieee'])} "]
    joiner = " and " if rule.get("condition_logic", "and") == "and" else " or "
    parts.append(joiner.join(cond_text(c) for c in rule["conditions"]))
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


__all__ = ["CompileError", "compile_rule", "describe_candidate", "describe_rule",
           "effective_params", "resolve_param"]
