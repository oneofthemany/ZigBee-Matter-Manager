"""
Swarm Intelligence — the network view.

Where resolver.py describes one device, this assembles the whole swarm: every
device's offers, the room index they sit in, and the wiring between them.

Pairing is a cross-product, not a catalogue. Every trigger and condition on
every device composes with every action on every other, because they are all
expressed in one vocabulary. What this module adds is *ranking* — a radar and a
bulb in the same room score far above the same radar and a bulb two floors up,
and a leak sensor scores highest against anything that can be switched off or
send a message. Without that the cross-product is technically complete and
practically unusable; with it, the top of the list is what a person would have
chosen anyway.

Nothing here is a whitelist. A low-scoring pair is still returned, just further
down, so an unanticipated combination remains one scroll away rather than
impossible.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from modules.swarm.capabilities import SCOPE_HOUSE
from modules.swarm.resolver import describe_device

logger = logging.getLogger("modules.swarm.network")

CONFIG_PATH = "./config/config.yaml"

# How strongly a trigger capability wants a given action tag. Scores are
# deliberately coarse: they order a list, they do not decide correctness.
#
# A capability absent from this table still pairs with everything — it simply
# starts from zero and is ordered by room proximity alone.
CAPABILITY_AFFINITY: Dict[str, Dict[str, int]] = {
    "presence":      {"lighting": 3, "switchable": 2, "climate": 1},
    "illuminance":   {"lighting": 3, "shading": 2},
    "contact":       {"lighting": 2, "security": 2, "climate": 2, "access": 1, "messaging": 1},
    "temperature":   {"climate": 3, "heating": 3, "shading": 1, "switchable": 1},
    "humidity":      {"climate": 2, "switchable": 2},
    "co2":           {"climate": 2, "switchable": 2, "messaging": 1},
    "air_quality":   {"climate": 2, "switchable": 2, "messaging": 1},
    "water_leak":    {"messaging": 3, "switchable": 3},
    "smoke":         {"messaging": 3, "lighting": 2, "access": 2},
    "co_detector":   {"messaging": 3, "access": 2},
    "gas":           {"messaging": 3, "access": 2},
    "vibration":     {"messaging": 2, "lighting": 1},
    "tamper":        {"messaging": 3},
    "power":         {"messaging": 3, "switchable": 2},
    "energy":        {"messaging": 1},
    "battery":       {"messaging": 2},
    "availability":  {"messaging": 3},
    "soil_moisture": {"messaging": 2, "switchable": 2},
    "button":        {"lighting": 3, "switchable": 3, "shading": 2, "access": 2, "climate": 1},
    "rotary":        {"lighting": 3, "switchable": 2},
    "on_off":        {"lighting": 1, "switchable": 1, "messaging": 1},
    "brightness":    {"lighting": 1},
    "cover":         {"climate": 1, "lighting": 1},
    "lock":          {"lighting": 2, "messaging": 2, "security": 2},
    "thermostat":    {"climate": 2, "heating": 2},
    "person":        {"access": 3, "climate": 3, "lighting": 2, "switchable": 2, "messaging": 1},
    "household":     {"access": 3, "climate": 3, "lighting": 2, "switchable": 2, "messaging": 1},
    "weather":       {"climate": 3, "heating": 3, "shading": 2, "lighting": 2},
    "house":         {"climate": 3, "heating": 3, "switchable": 2, "messaging": 2},
    "tariff":        {"switchable": 3, "climate": 2, "heating": 2, "messaging": 1},
}

# Affinity outweighs proximity: a leak sensor's best pairing is telling someone,
# wherever they are, while a battery warning is worth offering but should never
# outrank the primary automation for the room it sits in. Weighting affinity
# above the room bonus is what separates those two.
AFFINITY_WEIGHT = 2
# What a device *is* colours what its actions are for. `on_off` is generic
# switching on a plug and lighting on a bulb, so the device's class contributes
# tags that the capability alone cannot know.
DEVICE_CLASS_TAGS: Dict[str, Tuple[str, ...]] = {
    "light":           ("lighting", "switchable"),
    "color_light":     ("lighting", "switchable"),
    "plug":            ("switchable",),
    "switch":          ("switchable",),
    "cover":           ("shading", "openings"),
    "lock":            ("access", "security"),
    "thermostat":      ("climate", "heating"),
    "person":          ("messaging",),
    "household":       ("messaging",),
    "weather":         (),
    "house":           (),
    "tariff":          (),
}

SAME_ROOM_BONUS = 3
# A person or the household applies everywhere, so proximity cannot be scored
# against them; a house-scope *source* gets the room bonus unconditionally.
# A house-scope *target* does not — anyone can always be messaged, so the fact
# that they can is no evidence this is a pairing worth making.
SOURCE_HOUSE_BONUS = 3
# A device wiring to itself is legitimate — a plug that switches off when the
# appliance on it finishes — but it is never the pairing being looked for, and
# a trigger reacting to the very state its action sets is circular. The penalty
# is large enough that self-wiring always sorts below wiring to another device.
SAME_DEVICE_PENALTY = 5
# Wiring an activating edge to an activating action, or a deactivating edge to
# a deactivating one. The mismatch penalty is larger than the match bonus so an
# inverted pair drops a whole confidence band rather than merely sorting lower:
# "someone is detected -> turn the light off" is a rule someone might want, but
# never the one being looked for.
POLARITY_MATCH_BONUS = 2
POLARITY_MISMATCH_PENALTY = 3

CONFIDENCE_HIGH = 7
CONFIDENCE_MEDIUM = 4


# Room registry

_rooms_cache: Dict[str, Any] = {"mtime": None, "rooms": {}, "meta": {}}
_rooms_lock = threading.Lock()


def load_rooms(config_path: str = CONFIG_PATH) -> Dict[str, str]:
    """Chamber id → display name, cached against the config file's mtime.

    Read on every capability request, so it is cached; the registry is derived
    from config plus the heating and floor-plan definitions, which is more work
    than a request should repeat.
    """
    try:
        mtime = os.path.getmtime(config_path)
    except OSError:
        return {}
    with _rooms_lock:
        if _rooms_cache["mtime"] == mtime:
            return _rooms_cache["rooms"]
    rooms, meta = {}, {}
    try:
        import yaml
        from modules.chambers import build_registry
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        for c in build_registry(cfg):
            rooms[c["id"]] = c.get("name") or c["id"]
            # Which subsystem defined it. The registry unions configured
            # chambers with rooms adopted from heating and from the floor plan,
            # so two subsystems naming one room differently produce two
            # chambers — and without the source, an empty one is a mystery.
            meta[c["id"]] = {"id": c["id"], "name": rooms[c["id"]],
                             "source": c.get("source"), "level": c.get("level"),
                             "adopted": bool(c.get("adopted"))}
    except Exception as e:
        logger.debug(f"Room registry unavailable: {e}")
        rooms, meta = {}, {}
    with _rooms_lock:
        _rooms_cache["mtime"] = mtime
        _rooms_cache["rooms"] = rooms
        _rooms_cache["meta"] = meta
    return rooms


def load_room_meta(config_path: str = CONFIG_PATH) -> Dict[str, Dict[str, Any]]:
    """Chamber id -> its registry entry, including which subsystem defined it.

    Shares load_rooms()'s cache; the label map is what everything else needs,
    and only diagnostics needs the provenance.
    """
    load_rooms(config_path)
    with _rooms_lock:
        return dict(_rooms_cache["meta"])


def room_assignments(settings: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """ieee → chamber id, from the device settings the Frames UI already writes."""
    out: Dict[str, str] = {}
    for ieee, s in (settings or {}).items():
        if isinstance(s, dict) and s.get("chamber"):
            out[ieee] = s["chamber"]
    return out


# Network description

def describe_network(devices: Dict[str, Any],
                     names: Optional[Dict[str, str]] = None,
                     settings: Optional[Dict[str, Any]] = None,
                     rooms: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Describe every device in the swarm, with the room index they sit in."""
    names = names or {}
    rooms = rooms if rooms is not None else load_rooms()
    assigned = room_assignments(settings)

    described: List[Dict[str, Any]] = []
    for ieee, dev in devices.items():
        try:
            room = assigned.get(ieee)
            described.append(describe_device(
                ieee, dev,
                name=names.get(ieee),
                room=room,
                room_label=rooms.get(room) if room else None,
            ))
        except Exception as e:
            logger.warning(f"Could not describe {ieee}: {e}")

    described.sort(key=lambda d: (d.get("room_label") or "￿", d["name"]))
    return {
        "devices": described,
        "rooms": rooms,
        "summary": summarise(described),
    }


def summarise(described: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Headline counts — what the swarm can see and what it can act on."""
    described = list(described)
    by_room: Dict[str, int] = {}
    caps: Dict[str, int] = {}
    for d in described:
        key = d.get("room_label") or "Unassigned"
        by_room[key] = by_room.get(key, 0) + 1
        for c in d["capabilities"]:
            caps[c] = caps.get(c, 0) + 1
    return {
        "devices": len(described),
        "trigger_sources": sum(1 for d in described if d["is_trigger_source"]),
        "actuators": sum(1 for d in described if d["is_controllable"]),
        "notify_targets": sum(1 for d in described
                              if d["is_actuator"] and not d["is_controllable"]),
        "unplaced": sum(1 for d in described if not d.get("room")),
        "by_room": by_room,
        "capabilities": caps,
    }


# Pairing

def _score(source: Dict[str, Any], offer: Dict[str, Any],
           target: Dict[str, Any], action: Dict[str, Any]) -> int:
    """How strongly this trigger wants this action."""
    affinity = CAPABILITY_AFFINITY.get(offer["capability"], {})
    tags = set(action["tags"]) | set(
        DEVICE_CLASS_TAGS.get(target.get("device_class", ""), ()))
    score = AFFINITY_WEIGHT * max(
        (affinity.get(tag, 0) for tag in tags), default=0)

    if source["scope"] == SCOPE_HOUSE:
        score += SOURCE_HOUSE_BONUS
    elif source.get("room") and source["room"] == target.get("room"):
        score += SAME_ROOM_BONUS

    tp, ap = offer.get("polarity", 0), action.get("polarity", 0)
    if tp and ap:
        score += POLARITY_MATCH_BONUS if tp == ap else -POLARITY_MISMATCH_PENALTY

    if source["ieee"] == target["ieee"]:
        score -= SAME_DEVICE_PENALTY

    return score


def _confidence(score: int) -> str:
    if score >= CONFIDENCE_HIGH:
        return "high"
    if score >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def pairings(ieee: str, described: List[Dict[str, Any]],
             min_confidence: str = "low",
             limit: int = 200) -> Dict[str, Any]:
    """Every wiring this device participates in, both directions.

    `outbound` is this device's triggers driving other devices' actions;
    `inbound` is other devices' triggers driving this one. A device that is both
    a sensor and an actuator appears in both, which is the point — an actuator
    being switched on is itself an edge worth triggering on.
    """
    by_ieee = {d["ieee"]: d for d in described}
    me = by_ieee.get(ieee)
    if not me:
        return {"ieee": ieee, "outbound": [], "inbound": []}

    order = {"low": 0, "medium": 1, "high": 2}
    floor = order.get(min_confidence, 0)

    def build(source: Dict[str, Any], target: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for offer in source["triggers"]:
            for action in target["actions"]:
                score = _score(source, offer, target, action)
                conf = _confidence(score)
                if order[conf] < floor:
                    continue
                out.append({
                    "source_ieee": source["ieee"],
                    "source_name": source["name"],
                    "trigger": offer,
                    "target_ieee": target["ieee"],
                    "target_name": target["name"],
                    "action": action,
                    "score": score,
                    "weight": offer.get("weight", 0) + action.get("weight", 0),
                    "confidence": conf,
                    "same_room": bool(source.get("room")
                                      and source["room"] == target.get("room")),
                    "same_device": source["ieee"] == target["ieee"],
                    "sentence": f"When {offer['label']}, {action['label']}",
                })
        return out

    outbound: List[Dict[str, Any]] = []
    inbound: List[Dict[str, Any]] = []
    for other in described:
        if me["triggers"] and other["actions"]:
            outbound += build(me, other)
        if other["triggers"] and me["actions"] and other["ieee"] != ieee:
            inbound += build(other, me)

    outbound.sort(key=lambda p: (-p["score"], -p["weight"],
                                 p["target_name"], p["trigger"]["label"]))
    inbound.sort(key=lambda p: (-p["score"], -p["weight"],
                                p["source_name"], p["trigger"]["label"]))

    return {
        "ieee": ieee,
        "name": me["name"],
        "outbound": outbound[:limit],
        "inbound": inbound[:limit],
        "outbound_total": len(outbound),
        "inbound_total": len(inbound),
    }
