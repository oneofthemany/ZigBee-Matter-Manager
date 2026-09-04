"""
Swarm Intelligence — the matcher.

Fills a stigmergy pattern's slots from the live network and reports, in full,
why each slot did or did not fill.

The trace is not an afterthought. A pattern that matches nothing and a pattern
that failed to load look identical from the outside, and so do "no device in
this room offers presence" and "a device offers presence but is in no room at
all". Every rejection is recorded with its reason, so triage is reading a
report rather than adding print statements.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from modules.swarm.capabilities import SCOPE_HOUSE

logger = logging.getLogger("modules.swarm.matcher")

# Why a slot did not fill. Distinguishing these is the whole point of the trace:
# each one has a different fix.
NO_OFFER = "no_offer"                # nothing on the network makes this offer
NO_OFFER_IN_SCOPE = "no_offer_in_scope"   # it exists, but not in this room
CLASS_MISMATCH = "class_mismatch"    # offered, but by the wrong kind of device
PREFERENCE_UNMET = "preference_unmet"     # required same-device pairing absent

# A room with many lights would otherwise produce a suggestion per light, which
# reads as noise. The cap keeps the obvious ones and drops the tail.
MAX_VARIANTS_PER_SLOT = 4
# Two varying slots multiply, so the product is capped as well as each factor.
MAX_CANDIDATES_PER_SCOPE = 8


def _offers_of(device: Dict[str, Any], role: str) -> List[Dict[str, Any]]:
    return device.get(role + "s", [])


def _offer_matches(offer: Dict[str, Any], key: str) -> bool:
    """A slot's offer key, allowing for offers that fan out per value.

    A button's press offers are emitted as `button:pressed:single`,
    `button:pressed:double` and so on, so a pattern asking for `button:pressed`
    matches any of them.
    """
    return offer["key"] == key or offer["key"].startswith(key + ":")


def _candidates(devices: List[Dict[str, Any]], spec: Dict[str, Any]
                ) -> Tuple[List[Tuple[Dict, Dict]], Optional[str]]:
    """Every (device, offer) pair that could fill this slot, and why not if none."""
    role, key = spec["role"], spec["offer"]
    wanted_classes = spec.get("device_class")

    offered, class_rejected = [], False
    for dev in devices:
        for offer in _offers_of(dev, role):
            if not _offer_matches(offer, key):
                continue
            if wanted_classes and dev.get("device_class") not in wanted_classes:
                class_rejected = True
                continue
            offered.append((dev, offer))

    if offered:
        return offered, None
    return [], CLASS_MISMATCH if class_rejected else NO_OFFER_IN_SCOPE


def _rank_fills(pairs: List[Tuple[Dict, Dict]]) -> List[Tuple[Dict, Dict]]:
    """Best filler first: the offer the vocabulary weights highest, then by name."""
    return sorted(pairs, key=lambda p: (-p[1].get("weight", 0), p[0]["name"]))


def _distinct_devices(pairs: List[Tuple[Dict, Dict]]) -> List[Tuple[Dict, Dict]]:
    """One entry per device, keeping its best offer.

    A slot varies over *devices*; a device offering several matching values —
    a button with four press types — is one variant, not four suggestions.
    """
    seen, out = set(), []
    for dev, offer in pairs:
        if dev["ieee"] in seen:
            continue
        seen.add(dev["ieee"])
        out.append((dev, offer))
    return out


def _scope_pool(described: List[Dict[str, Any]], room: Optional[str]
                ) -> List[Dict[str, Any]]:
    """Devices a room-scoped slot may draw on: that room, plus house-scope devices.

    House-scope devices — a person, the household — have no room and apply
    everywhere, so excluding them from a room match would make "lights on when
    someone gets home" impossible to express per room.
    """
    return [d for d in described
            if d.get("room") == room or d["scope"] == SCOPE_HOUSE]


def _slot_order(slots: Dict[str, Dict[str, Any]]) -> List[str]:
    """Slots without a preference first, so the slots that depend on them can see
    what was chosen."""
    independent = [n for n, s in slots.items() if not s.get("prefer_slot")]
    dependent = [n for n, s in slots.items() if s.get("prefer_slot")]
    return independent + dependent


def match_pattern(pattern: Dict[str, Any], described: List[Dict[str, Any]],
                  rooms: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Fill one pattern everywhere it can be filled.

    Returns {"candidates": [...], "trace": [...]} — the trace carries an entry
    per attempted scope, matched or not.
    """
    rooms = rooms or {}
    scope = pattern.get("scope", "room")

    if scope == SCOPE_HOUSE:
        attempts: List[Tuple[Optional[str], List[Dict[str, Any]]]] = [(None, described)]
    else:
        occupied = sorted({d["room"] for d in described if d.get("room")})
        attempts = [(r, _scope_pool(described, r)) for r in occupied]
        if not occupied:
            return {"candidates": [], "trace": [{
                "pattern": pattern["id"], "room": None, "outcome": "no_match",
                "reason": "no device is assigned to a room, so a room-scoped "
                          "pattern has nothing to match against",
                "slots": {},
            }]}

    candidates: List[Dict[str, Any]] = []
    trace: List[Dict[str, Any]] = []

    for room, pool in attempts:
        result = _match_one(pattern, pool, room,
                            (rooms.get(room) or room) if room else None)
        trace.append(result["trace"])
        candidates.extend(result["candidates"])

    return {"candidates": candidates, "trace": trace}


def _match_one(pattern: Dict[str, Any], pool: List[Dict[str, Any]],
               room: Optional[str], room_label: Optional[str]) -> Dict[str, Any]:
    """Fill a pattern's slots from one scope's device pool."""
    slots = pattern["slots"]
    emits = pattern.get("emits") or {}
    fills: Dict[str, Dict[str, Any]] = {}
    alternatives: Dict[str, List[Dict[str, str]]] = {}
    slot_trace: Dict[str, Any] = {}
    blocked: Optional[str] = None

    vary_slots = _vary_slots(pattern)
    vary_pairs: Dict[str, List[Tuple[Dict, Dict]]] = {}

    for name in _slot_order(slots):
        spec = slots[name]
        pairs, reason = _candidates(pool, spec)

        prefer_slot = spec.get("prefer_slot")
        if pairs and prefer_slot and spec.get("prefer") == "same_device":
            anchor = fills.get(prefer_slot)
            if anchor:
                same = [p for p in pairs if p[0]["ieee"] == anchor["ieee"]]
                if same:
                    pairs = same
                elif spec.get("require_same_device"):
                    pairs, reason = [], PREFERENCE_UNMET

        if not pairs:
            slot_trace[name] = {"status": "unfilled", "reason": reason,
                                "optional": bool(spec.get("optional"))}
            if not spec.get("optional"):
                blocked = name
                break
            continue

        ranked = _rank_fills(pairs)
        if name in vary_slots:
            vary_pairs[name] = _distinct_devices(ranked)[:MAX_VARIANTS_PER_SLOT]
        dev, offer = ranked[0]
        fills[name] = {"ieee": dev["ieee"], "device": dev, "offer": offer}
        if len(ranked) > 1:
            alternatives[name] = [{"ieee": d["ieee"], "name": d["name"],
                                   "offer": o["key"]} for d, o in ranked[1:]]
        note = None
        if prefer_slot and fills.get(prefer_slot, {}).get("ieee") == dev["ieee"]:
            note = f"same device as {prefer_slot}"
        slot_trace[name] = {"status": "filled", "ieee": dev["ieee"],
                            "device": dev["name"], "offer": offer["key"],
                            "alternatives": len(ranked) - 1, "note": note}

    if blocked:
        return {"candidates": [], "trace": {
            "pattern": pattern["id"], "room": room, "room_label": room_label,
            "outcome": "no_match", "blocked_by": blocked,
            "reason": _reason_text(blocked, slot_trace[blocked], room_label),
            "slots": slot_trace,
        }}

    # One candidate per combination of the varying slots' choices.
    active = [n for n in vary_slots if n in vary_pairs]
    combos: List[Dict[str, Tuple[Dict, Dict]]] = [{}]
    for name in active:
        combos = [{**combo, name: pair}
                  for combo in combos for pair in vary_pairs[name]]
    combos = combos[:MAX_CANDIDATES_PER_SCOPE]

    candidates = []
    for combo in combos:
        these = dict(fills)
        for name, (dev, offer) in combo.items():
            these[name] = {"ieee": dev["ieee"], "device": dev, "offer": offer}
            # A slot pinned to a varying one follows it.
            for other, spec in slots.items():
                if spec.get("prefer_slot") != name or other not in these:
                    continue
                repin = [p for p in _candidates(pool, spec)[0]
                         if p[0]["ieee"] == dev["ieee"]]
                if repin:
                    these[other] = {"ieee": repin[0][0]["ieee"],
                                    "device": repin[0][0], "offer": repin[0][1]}
                elif spec.get("optional"):
                    these.pop(other, None)
        candidates.append({
            "pattern_id": pattern["id"],
            "room": room,
            "room_label": room_label,
            "fills": these,
            "alternatives": alternatives,
        })

    return {"candidates": candidates, "trace": {
        "pattern": pattern["id"], "room": room, "room_label": room_label,
        "outcome": "matched", "candidates": len(candidates), "slots": slot_trace,
    }}


def _vary_slots(pattern: Dict[str, Any]) -> List[str]:
    """Slots whose choice makes two suggestions genuinely different automations.

    Controlling a different light is a different automation, so the action slot
    varies. Messaging a different person is the *same* automation with a
    different recipient, so notify slots do not — they offer alternatives
    instead.

    In a house-scoped pattern the trigger varies too. Nothing else partitions
    it: "tell me when a battery runs low" must produce one suggestion per
    battery device, and "unlock when someone gets home" one per person. A
    room-scoped pattern needs none of that, because the room is the partition.
    """
    out: List[str] = []
    slots = pattern["slots"]
    emits = pattern.get("emits") or {}

    for entry in emits.get("then", []) or []:
        if not isinstance(entry, str):
            continue
        spec = slots.get(entry) or {}
        if spec.get("role") == "action" and \
                not str(spec.get("offer", "")).startswith("notify:"):
            out.append(entry)
            break

    if pattern.get("scope") == SCOPE_HOUSE:
        source = emits.get("source")
        if source and source not in out:
            out.append(source)

    return out


def _reason_text(slot: str, entry: Dict[str, Any], room_label: Optional[str]) -> str:
    where = f"in {room_label}" if room_label else "on the network"
    return {
        NO_OFFER: f"nothing {where} provides what slot '{slot}' needs",
        NO_OFFER_IN_SCOPE: f"no device {where} provides what slot '{slot}' needs",
        CLASS_MISMATCH: f"a device {where} makes the offer slot '{slot}' needs, "
                        f"but is not one of the device types the pattern accepts",
        PREFERENCE_UNMET: f"slot '{slot}' must sit on the same device as its "
                          f"anchor, and none {where} does both",
    }.get(entry.get("reason"), f"slot '{slot}' could not be filled")
