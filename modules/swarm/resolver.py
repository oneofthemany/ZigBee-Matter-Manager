"""
Swarm Intelligence — the resolver.

Turns a live device of any protocol into the offers it contributes to the
swarm: what it can trigger on, what it can be asked about, and what it can be
told to do. One function, no protocol branches beyond the four places the
underlying registries genuinely differ.

Capability detection is deliberately belt-and-braces. A device's declared
capabilities are folded from whichever vocabulary its stack uses, and then the
live state is sniffed independently. Either source alone is enough to produce an
offer: a profile that claims `presence` but reports no matching attribute yields
nothing, and an unprofiled radar reporting `presence` yields the full presence
offer set without anyone having described it first. That is what keeps a device
nobody anticipated wired into the swarm.

Nothing here mutates device state, sends a command, or touches a database.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.automation import VALID_COMMANDS
from modules.swarm.capabilities import (
    ACTION,
    CAPABILITIES,
    CONDITION,
    SCOPE_HOUSE,
    SCOPE_ROOM,
    TRIGGER,
    canonical_capability,
    classify,
    resolve_param,
)

logger = logging.getLogger("modules.swarm.resolver")

# Attributes that describe the radio rather than the thing the device measures.
# Never a useful trigger, and they crowd out the ones that are.
DIAGNOSTIC_ATTRS = {
    "last_seen", "linkquality", "lqi", "rssi", "manufacturer", "model",
    "power_source", "sw_version", "date_code", "application_version",
    "stack_version", "hw_version", "manufacturer_id", "ieee", "nwk",
    "friendly_name", "device_type", "update_available", "update_state",
    "last_update", "source", "accuracy_m",
}

# String renderings of a boolean, so an offer written as True/False binds to
# whatever vocabulary the device actually reports.
TRUTHY_STRINGS = {
    "on", "true", "yes", "open", "opened", "detected", "occupied", "present",
    "locked", "alarm", "wet", "active", "running", "1",
}
FALSY_STRINGS = {
    "off", "false", "no", "closed", "clear", "undetected", "not_detected",
    "unoccupied", "absent", "unlocked", "idle", "dry", "inactive", "0",
}


# Live-value inspection

def _value_options(value: Any) -> Optional[List[Any]]:
    """The discrete values an attribute plausibly takes, or None if continuous.

    Mirrors what AutomationEngine.get_source_attributes offers the rule builder,
    so a swarm offer and a hand-built rule agree on what a value may be.
    """
    if isinstance(value, bool):
        return [True, False]
    if isinstance(value, str) and value.upper() in ("ON", "OFF"):
        return ["ON", "OFF"]
    return None


def _pick_attr(state: Dict[str, Any], candidates: Sequence[str]) -> Optional[str]:
    """First candidate attribute the device actually reports."""
    for name in candidates:
        if name in state and name not in DIAGNOSTIC_ATTRS:
            return name
    return None


def _coerce_bool(truthy: bool, sample: Any) -> Any:
    """Render a boolean offer value in the device's own vocabulary.

    A rule comparing `state == True` against a device reporting `"ON"` never
    matches, so the literal is rewritten to whatever shape the live value has.
    """
    if isinstance(sample, bool) or sample is None:
        return truthy
    if isinstance(sample, str):
        low = sample.strip().lower()
        if low in TRUTHY_STRINGS or low in FALSY_STRINGS:
            # Reuse the device's own casing for the matching polarity.
            return "ON" if (truthy and sample.isupper()) else \
                   "OFF" if (not truthy and sample.isupper()) else \
                   ("true" if truthy else "false") if low in ("true", "false") else \
                   _string_for(truthy, low)
        return truthy
    if isinstance(sample, (int, float)):
        return 1 if truthy else 0
    return truthy


def _string_for(truthy: bool, sample_low: str) -> str:
    """Pick the opposite-polarity string of the same family as the sample."""
    pairs = [("on", "off"), ("open", "closed"), ("opened", "closed"),
             ("detected", "clear"), ("occupied", "unoccupied"),
             ("locked", "unlocked"), ("present", "absent"),
             ("active", "inactive"), ("running", "idle"), ("wet", "dry")]
    for pos, neg in pairs:
        if sample_low in (pos, neg):
            return pos if truthy else neg
    return "on" if truthy else "off"


def _contact_values(attr: str, sample: Any) -> Tuple[Any, Any]:
    """(open_value, closed_value) for a contact sensor.

    Polarity is genuinely device-specific rather than derivable: the Zigbee
    `contact` attribute is True when the door is *shut*, while an `is_open`
    attribute means what it says.
    """
    name = attr.lower()
    if isinstance(sample, str):
        low = sample.strip().lower()
        if low in ("open", "closed", "opened"):
            return ("open", "closed")
    if name.startswith("is_closed"):
        return (_coerce_bool(False, sample), _coerce_bool(True, sample))
    if name.startswith("is_open") or name in ("opening", "door", "window"):
        return (_coerce_bool(True, sample), _coerce_bool(False, sample))
    # Bare `contact`: True == closed.
    return (_coerce_bool(False, sample), _coerce_bool(True, sample))


# Capability detection

def _declared_capabilities(dev: Any) -> List[str]:
    """Capabilities the device's own stack claims, in that stack's vocabulary.

    Four shapes exist: a Zigbee DeviceCapabilities object, the Matter list
    accessor, a duck-typed capabilities object (Nuki), and a plain list on the
    device. All are optional — sniffing covers anything that reports none.
    """
    caps = getattr(dev, "capabilities", None)

    if caps is not None and hasattr(caps, "get_capabilities"):
        try:
            return list(caps.get_capabilities())
        except Exception:
            pass
    if isinstance(caps, (list, tuple, set)):
        return list(caps)
    if hasattr(dev, "_get_capabilities"):
        try:
            return list(dev._get_capabilities())
        except Exception:
            pass
    return []


def _sniffed_capabilities(state: Dict[str, Any],
                         commands: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    """Capabilities evidenced by what the device reports or can be told to do.

    Sensing is inferred from attributes, but actuation is inferred from
    commands: a `state` attribute is claimed by locks, switches and covers
    alike, so reading one proves nothing about what the device can be asked to
    do. A dispatchable `lock` command does.
    """
    found = []
    for cap_id, spec in CAPABILITIES.items():
        if spec.get("kind") == "actuator":
            wanted = {a.get("command") for a in spec.get("actions", [])}
            if wanted & set(commands):
                found.append(cap_id)
            continue
        attrs = spec.get("attrs") or []
        if attrs and _pick_attr(state, attrs):
            found.append(cap_id)
    return found


def _profile_capabilities(dev: Any, ieee: str) -> List[str]:
    """Capabilities from a matched device profile, when one exists.

    The profile is the user's explicit answer about what a device is, so it is
    additive to detection rather than filtered by it.
    """
    try:
        from modules.device_profiles import DEVICE_TYPES, get_profile_store
    except Exception:
        return []
    try:
        store = get_profile_store()
        profile = store.get_profile_for_device(
            ieee=ieee,
            model=getattr(dev, "model", None),
            manufacturer=getattr(dev, "manufacturer", None),
        )
    except Exception:
        return []
    if not profile:
        return []
    caps = list(profile.get("capabilities") or [])
    dtype = profile.get("device_type")
    if dtype and dtype in DEVICE_TYPES:
        caps.extend(DEVICE_TYPES[dtype].get("capabilities", []))
    return caps


def device_capabilities(ieee: str, dev: Any, state: Dict[str, Any],
                        commands: Optional[Dict[str, List[Dict[str, Any]]]] = None
                        ) -> List[str]:
    """Canonical capability ids for one device, from every available source."""
    commands = commands if commands is not None else _command_index(dev)
    raw = (_declared_capabilities(dev)
           + _profile_capabilities(dev, ieee)
           + _sniffed_capabilities(state, commands))
    out: List[str] = []
    for name in raw:
        cap = canonical_capability(str(name))
        if cap and cap not in out:
            out.append(cap)

    # A capability may entail another that nothing in the device's state or
    # command set would reveal — a person is messageable by being a person.
    for cap in list(out):
        for implied in CAPABILITIES.get(cap, {}).get("implies", []):
            if implied not in out:
                out.append(implied)

    # And a capability may rule one out: a person's `presence` names a place,
    # so sniffing it as an occupancy flag makes the person a motion sensor.
    excluded = {e for cap in out
                for e in CAPABILITIES.get(cap, {}).get("excludes", [])}
    return [c for c in out if c not in excluded]


# Command inspection

def _command_index(dev: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Executable commands, keyed by command name.

    A command the engine has no dispatch path for is dropped here rather than
    surfacing as an offer that would compile into a silently inert rule.
    """
    try:
        cmds = dev.get_control_commands() if hasattr(dev, "get_control_commands") else []
    except Exception:
        cmds = []
    index: Dict[str, List[Dict[str, Any]]] = {}
    for c in cmds or []:
        name = str(c.get("command", "")).lower()
        if name not in VALID_COMMANDS:
            continue
        index.setdefault(name, []).append(c)
    return index


# Offer construction

def _label(template: str, device_name: str, room_label: Optional[str],
           value: Any = None) -> str:
    """Fill a sentence fragment. An unplaced device stands in for its own room."""
    return (template
            .replace("{device}", device_name)
            .replace("{room}", room_label or device_name)
            .replace("{value}", "" if value is None else str(value)))


def _offer_base(cap_id: str, spec: Dict[str, Any], offer: Dict[str, Any],
                role: str) -> Dict[str, Any]:
    return {
        "key": f"{cap_id}:{offer['id']}",
        "capability": cap_id,
        "capability_label": spec.get("label", cap_id),
        "role": role,
        "weight": offer.get("weight", 0),
        "polarity": offer.get("polarity", 0),
        "tags": list(spec.get("tags") or []),
        "scope": spec.get("scope", SCOPE_ROOM),
    }


def _build_state_offers(cap_id: str, spec: Dict[str, Any], role: str,
                        state: Dict[str, Any], device_name: str,
                        room_label: Optional[str]) -> List[Dict[str, Any]]:
    """Trigger or condition offers for one capability on one device."""
    out: List[Dict[str, Any]] = []
    for offer in spec.get(role + "s", []):
        # Zone offers read `place` and carry no comparison of their own.
        if offer.get("type") == "zone":
            if "place" not in state:
                continue
            built = _offer_base(cap_id, spec, offer, role)
            built.update({
                "label": _label(offer["label"], device_name, room_label),
                "condition": {"type": "zone", "event": offer["event"],
                              "place": offer["place"]},
            })
            out.append(built)
            continue

        attr = _pick_attr(state, offer.get("attrs") or spec.get("attrs") or [])
        if not attr:
            continue
        sample = state.get(attr)
        options = _value_options(sample)

        # A button reports its press types as discrete values; each one is a
        # separate edge worth triggering on, so the offer fans out.
        if offer.get("expand") == "value_options":
            for opt in (options or []):
                built = _offer_base(cap_id, spec, offer, role)
                built["key"] = f"{cap_id}:{offer['id']}:{opt}"
                built.update({
                    "label": _label(offer["label"], device_name, room_label) + f" ({opt})",
                    "attribute": attr,
                    "condition": {"type": "attribute", "attribute": attr,
                                  "operator": offer["operator"], "value": opt},
                })
                out.append(built)
            continue

        value = offer.get("value")
        param_id = value.get("param") if isinstance(value, dict) else None
        value = resolve_param(value)

        if value == "$open" or value == "$closed":
            open_v, closed_v = _contact_values(attr, sample)
            value = open_v if value == "$open" else closed_v
        elif isinstance(value, bool):
            if isinstance(sample, str) and \
                    sample.strip().lower() not in TRUTHY_STRINGS | FALSY_STRINGS:
                continue
            value = _coerce_bool(value, sample)

        if value is None and offer["operator"] not in ("neq", "eq"):
            continue

        built = _offer_base(cap_id, spec, offer, role)
        built.update({
            "label": _label(offer["label"], device_name, room_label, value),
            "label_template": _label(offer["label"], device_name, room_label,
                                     "{value}"),
            "attribute": attr,
            "condition": {"type": "attribute", "attribute": attr,
                          "operator": offer["operator"], "value": value},
        })
        if param_id:
            built["param"] = param_id
        sustain = resolve_param(offer.get("sustain"))
        if sustain:
            built["condition"]["sustain"] = int(sustain)
            built["sustain_param"] = offer["sustain"].get("param") \
                if isinstance(offer.get("sustain"), dict) else None
        out.append(built)
    return out


def _build_action_offers(cap_id: str, spec: Dict[str, Any],
                         commands: Dict[str, List[Dict[str, Any]]],
                         device_name: str, room_label: Optional[str],
                         ieee: str) -> List[Dict[str, Any]]:
    """Action offers for one capability, one per executable endpoint."""
    out: List[Dict[str, Any]] = []
    for offer in spec.get("actions", []):
        # Non-command steps (a message to a person) have no hardware to check.
        if offer.get("step"):
            built = _offer_base(cap_id, spec, offer, ACTION)
            built.update({
                "label": _label(offer["label"], device_name, room_label),
                "step": {"type": offer["step"], "to_user": ieee},
            })
            out.append(built)
            continue

        entries = commands.get(offer["command"])
        if not entries:
            continue

        param_id = offer.get("value_from")
        value = resolve_param({"param": param_id}) if param_id else None
        multi = len(entries) > 1

        for entry in entries:
            eid = entry.get("endpoint_id")
            built = _offer_base(cap_id, spec, offer, ACTION)
            label = _label(offer["label"], device_name, room_label, value)
            label_template = _label(offer["label"], device_name, room_label,
                                    "{value}")
            if multi and eid is not None:
                built["key"] = f"{cap_id}:{offer['id']}:ep{eid}"
                label += f" (EP{eid})"
            step = {"type": "command", "target_ieee": ieee,
                    "command": offer["command"], "value": value,
                    "endpoint_id": eid}
            built.update({"label": label, "label_template": label_template,
                          "step": step})
            if param_id:
                built["param"] = param_id
            out.append(built)
    return out


# Public API

def describe_device(ieee: str, dev: Any, name: Optional[str] = None,
                    room: Optional[str] = None,
                    room_label: Optional[str] = None) -> Dict[str, Any]:
    """Everything one device contributes to the swarm."""
    state = dict(getattr(dev, "state", None) or {})
    device_name = name or getattr(dev, "friendly_name", None) or ieee
    commands = _command_index(dev)
    caps = device_capabilities(ieee, dev, state, commands)

    triggers: List[Dict[str, Any]] = []
    conditions: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []

    for cap_id in caps:
        spec = CAPABILITIES.get(cap_id)
        if not spec:
            continue
        triggers += _build_state_offers(cap_id, spec, TRIGGER, state, device_name, room_label)
        conditions += _build_state_offers(cap_id, spec, CONDITION, state, device_name, room_label)
        actions += _build_action_offers(cap_id, spec, commands, device_name, room_label, ieee)

    scope = SCOPE_HOUSE if any(
        CAPABILITIES.get(c, {}).get("scope") == SCOPE_HOUSE for c in caps
    ) else SCOPE_ROOM

    return {
        "ieee": ieee,
        "name": device_name,
        "model": getattr(dev, "model", None) or "Unknown",
        "room": room,
        "room_label": room_label,
        "scope": scope,
        "device_class": classify(caps),
        "capabilities": caps,
        "triggers": triggers,
        "conditions": conditions,
        "actions": actions,
        "is_trigger_source": bool(triggers),
        "is_actuator": bool(actions),
        # Distinct from is_actuator: a person accepts a message but cannot be
        # commanded, and coverage counts mean something different for each.
        "is_controllable": any(a["step"].get("type") == "command" for a in actions),
    }
