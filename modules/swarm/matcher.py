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

from modules.swarm.capabilities import SCOPE_HOUSE, worker_satisfies

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
# House-wide, the trigger is the only partition there is: "tell me when a
# battery runs low" is one automation per battery device, and capping that at
# the room cap silently dropped two thirds of a real house's batteries.
MAX_SOURCE_VARIANTS = 24
# Two varying slots multiply, so the product is capped as well as each factor.
MAX_CANDIDATES_PER_SCOPE = 8
MAX_CANDIDATES_PER_HOUSE = 48
# A collected slot gathers every matching device into one rule. A trigger or
# condition compiles to a group, capped by the engine's MAX_CONDITIONS_PER_GROUP;
# an action compiles to steps, which run together once there are several.
MAX_COLLECT = 5
MAX_COLLECT_STEPS = 12


def _offers_of(device: Dict[str, Any], role: str) -> List[Dict[str, Any]]:
    return device.get(role + "s", [])


def _offer_matches(offer: Dict[str, Any], key: Any) -> bool:
    """Whether an offer satisfies a slot's `offer` declaration.

    A slot may name several alternatives, any of which will do — a battery
    percentage or a bare low flag express the same intent, and a device has one
    or the other rather than both.

    Suffixes are matched as a prefix, because offers fan out: a button's press
    types become `button:pressed:single`, and a dual-gang socket's second
    outlet becomes `power:started:ep2`.
    """
    keys = key if isinstance(key, list) else [key]
    return any(offer["key"] == k or offer["key"].startswith(str(k) + ":")
               for k in keys)


def _candidates(devices: List[Dict[str, Any]], spec: Dict[str, Any]
                ) -> Tuple[List[Tuple[Dict, Dict]], Optional[str]]:
    """Every (device, offer) pair that could fill this slot, and why not if none."""
    role, key = spec["role"], spec["offer"]
    wanted_classes = spec.get("device_class")
    template = spec.get("worker")
    # A worker's name is the only thing that says what it is for.
    names = [str(n).lower() for n in spec.get("name_match") or []]

    offered, class_rejected = [], False
    for dev in devices:
        proposed = dev.get("proposed_template")
        if template:
            # The worker the slot asks for: one that exists and does the job,
            # or — only where none does — the one the swarm proposes.
            if proposed != template and (proposed or not worker_satisfies(dev, template)):
                continue
        elif proposed:
            continue
        if names and not any(n in str(dev.get("name") or "").lower() for n in names):
            continue
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
    """Best filler first: highest-weighted offer, a measurement before an estimate, then name."""
    return sorted(pairs, key=lambda p: (-p[1].get("weight", 0),
                                        bool(p[0].get("estimated")), p[0]["name"]))


def _endpoint_of(offer: Dict[str, Any]) -> Any:
    """The endpoint an offer acts on, wherever it happens to be recorded."""
    if offer.get("endpoint_id") is not None:
        return offer["endpoint_id"]
    step = offer.get("step") or {}
    return step.get("endpoint_id")


def _rank_anchored(pairs: List[Tuple[Dict, Dict]], name: str,
                   slots: Dict[str, Dict[str, Any]],
                   pool: List[Dict[str, Any]]) -> List[Tuple[Dict, Dict]]:
    """Put candidates that let an anchored optional slot fill first.

    A pattern like "lights on when someone gets home, if that room is dark"
    reads far better where the room can actually answer the question. Without
    this the variant cap can spend all its slots on rooms with no light sensor,
    and every suggestion loses its condition.

    Only reorders — a room that cannot answer is still offered, just later.
    """
    anchored = [n for n, sp in slots.items()
                if sp.get("prefer_slot") == name and sp.get("prefer") == "same_room"]
    if not anchored:
        return pairs

    def answers(dev: Dict[str, Any]) -> int:
        room = dev.get("room")
        if not room:
            return 0
        for other in anchored:
            cands, _ = _candidates([d for d in pool if d.get("room") == room],
                                   slots[other])
            if cands:
                return 1
        return 0

    return sorted(pairs, key=lambda p: -answers(p[0]))


def _prefer_filter(pairs: List[Tuple[Dict, Dict]], mode: str,
                   anchor: Dict[str, Any],
                   anchor_offer: Optional[Dict[str, Any]] = None
                   ) -> List[Tuple[Dict, Dict]]:
    """Narrow a slot's candidates to those near the slot it is anchored to.

    `same_device` is for a reading that belongs with its own trigger — a radar
    reporting both presence and lux answers "is it dark here" about the room it
    is watching.

    `same_room` is for a house-scoped pattern whose condition should still be
    local: "is it dark" asked of a bathroom sensor while switching a living-room
    lamp is technically an answer and reads as a mistake.
    """
    if mode == "same_device":
        same = [p for p in pairs if p[0]["ieee"] == anchor["ieee"]]
        # On a dual-gang socket "the same device" means the same outlet: outlet
        # 2's switch belongs with outlet 2's power reading, not outlet 1's.
        if anchor_offer is not None and len(same) > 1:
            outlet = _endpoint_of(anchor_offer)
            same.sort(key=lambda p: _endpoint_of(p[1]) != outlet)
        return same
    if mode == "same_room":
        room = anchor.get("room")
        return [p for p in pairs if room and p[0].get("room") == room]
    return []


def _distinct_devices(pairs: List[Tuple[Dict, Dict]]) -> List[Tuple[Dict, Dict]]:
    """One entry per independently addressable thing, keeping its best offer.

    Keyed on device *and endpoint*, not device alone. A button offering four
    press types is one variant, not four — those share an endpoint. But a
    dual-gang socket's two outlets are two separate things: outlet 1 may be the
    washing machine and outlet 2 the dryer, and "tell me when it finishes"
    wants a rule for each.
    """
    seen, out = set(), []
    for dev, offer in pairs:
        key = (dev["ieee"], _endpoint_of(offer))
        if key in seen:
            continue
        seen.add(key)
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
    depends = lambda s: s.get("prefer_slot") or s.get("exclude_slot")  # noqa: E731
    independent = [n for n, s in slots.items() if not depends(s)]
    dependent = [n for n, s in slots.items() if depends(s)]
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
    house = pattern.get("scope") == SCOPE_HOUSE
    source_slot = emits.get("source")

    for name in _slot_order(slots):
        spec = slots[name]
        pairs, reason = _candidates(pool, spec)

        prefer_slot = spec.get("prefer_slot")
        if pairs and prefer_slot and spec.get("prefer"):
            anchor = fills.get(prefer_slot)
            if anchor:
                same = _prefer_filter(pairs, spec.get("prefer"), anchor["device"],
                                      anchor.get("offer"))
                if same:
                    pairs = same
                elif spec.get("require_same_device"):
                    pairs, reason = [], PREFERENCE_UNMET

        exclude = spec.get("exclude_slot")
        if pairs and exclude and fills.get(exclude):
            # "Another light than the one switched" — never that light itself.
            pairs = [p for p in pairs if p[0]["ieee"] != fills[exclude]["ieee"]]
            if not pairs:
                reason = PREFERENCE_UNMET

        if not pairs:
            slot_trace[name] = {"status": "unfilled", "reason": reason,
                                "optional": bool(spec.get("optional"))}
            if not spec.get("optional"):
                blocked = name
                break
            continue

        ranked = _rank_fills(pairs)
        if spec.get("collect"):
            # Every device in scope that makes the offer, as one fill: "any
            # window in this room", "every light in the house".
            gathered = _distinct_devices(ranked)
            kept = gathered[:MAX_COLLECT_STEPS if spec.get("role") == "action" else MAX_COLLECT]
            first_dev, first_offer = kept[0]
            fills[name] = {"ieee": first_dev["ieee"], "device": first_dev,
                           "offer": first_offer,
                           "members": [{"ieee": d["ieee"], "device": d, "offer": o}
                                       for d, o in kept]}
            slot_trace[name] = {"status": "filled", "ieee": first_dev["ieee"],
                                "device": first_dev["name"], "offer": first_offer["key"],
                                "collected": len(kept),
                                "left_out": len(gathered) - len(kept),
                                "alternatives": 0, "note": None}
            continue
        if name in vary_slots:
            cap = MAX_SOURCE_VARIANTS if house and name == source_slot else MAX_VARIANTS_PER_SLOT
            vary_pairs[name] = _distinct_devices(
                _rank_anchored(ranked, name, slots, pool))[:cap]
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
    combos = combos[:MAX_CANDIDATES_PER_HOUSE if house else MAX_CANDIDATES_PER_SCOPE]

    candidates = []
    for combo in combos:
        these = dict(fills)
        for name, (dev, offer) in combo.items():
            these[name] = {"ieee": dev["ieee"], "device": dev, "offer": offer}
            # A slot pinned to a varying one follows it.
            for other, spec in slots.items():
                if spec.get("prefer_slot") != name or other not in these:
                    continue
                repin = _prefer_filter(_candidates(pool, spec)[0],
                                       spec.get("prefer") or "same_device", dev, offer)
                if repin:
                    these[other] = {"ieee": repin[0][0]["ieee"],
                                    "device": repin[0][0], "offer": repin[0][1]}
                elif spec.get("optional"):
                    these.pop(other, None)
        if not _resolve_exclusions(these, slots, pool):
            continue
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


def _resolve_exclusions(these: Dict[str, Dict[str, Any]],
                        slots: Dict[str, Dict[str, Any]],
                        pool: List[Dict[str, Any]]) -> bool:
    """Re-pick any slot that landed on the device it must differ from.

    A varying slot can move onto the very device an `exclude_slot` rules out.
    Returns False when a required slot has nothing else to take, so the
    combination is dropped rather than compiled into a light following itself.
    """
    for name, spec in slots.items():
        other = spec.get("exclude_slot")
        if not other or name not in these or other not in these:
            continue
        if these[name]["ieee"] != these[other]["ieee"]:
            continue
        alt = [p for p in _rank_fills(_candidates(pool, spec)[0])
               if p[0]["ieee"] != these[other]["ieee"]]
        if alt:
            these[name] = {"ieee": alt[0][0]["ieee"], "device": alt[0][0], "offer": alt[0][1]}
        elif spec.get("optional"):
            these.pop(name, None)
        else:
            return False
    return True


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
        # A collected slot is already every device, so it never varies.
        if spec.get("role") == "action" and not spec.get("collect") and \
                not str(spec.get("offer", "")).startswith("notify:"):
            out.append(entry)
            break

    if pattern.get("scope") == SCOPE_HOUSE:
        source = emits.get("source")
        if source and source not in out and not (slots.get(source) or {}).get("collect"):
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
