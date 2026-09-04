"""
Network tests — the whole swarm, and the ranked wiring between devices.

    python3 tests/swarm/test_network.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, SAMPLE_NAMES, SAMPLE_ROOMS, SAMPLE_SETTINGS, sample_network,
)

from modules.swarm.network import (  # noqa: E402
    describe_network, pairings, room_assignments, summarise,
)


def _described():
    return describe_network(sample_network(), SAMPLE_NAMES, SAMPLE_SETTINGS,
                            SAMPLE_ROOMS)


def same_room_score(p):
    """Score of the presence-to-light pairing the whole design exists to find."""
    return _find(p["outbound"], "presence:detected", "0xhalllight",
                 "on_off:turn_on")["score"]


def _find(pairs, trigger_key, target_ieee, action_key):
    for p in pairs:
        if (p["trigger"]["key"] == trigger_key
                and p["target_ieee"] == target_ieee
                and p["action"]["key"] == action_key):
            return p
    return None


def run() -> Checker:
    c = Checker("test_network")

    c.section("network description")
    net = _described()
    devices = {d["ieee"]: d for d in net["devices"]}
    c.check("every device described", len(net["devices"]) == 8, len(net["devices"]))
    c.check("rooms resolve to labels",
            devices["0xradar"]["room_label"] == "Hallway",
            devices["0xradar"]["room_label"])
    c.check("unplaced devices carry no room",
            devices["user::sean"]["room"] is None)
    c.check("room assignment reads the chamber key",
            room_assignments(SAMPLE_SETTINGS)["0xhalllight"] == "hallway")

    c.section("coverage summary")
    s = net["summary"]
    c.check("counts devices", s["devices"] == 8, s)
    c.check("counts controllable devices", s["actuators"] == 4, s["actuators"])
    c.check("counts notify targets separately",
            s["notify_targets"] == 2, s["notify_targets"])
    c.check("counts trigger sources", s["trigger_sources"] >= 6, s["trigger_sources"])
    c.check("counts the unplaced", s["unplaced"] == 3, s["unplaced"])
    c.check("groups by room", s["by_room"].get("Hallway") == 3, s["by_room"])

    c.section("pairing is a cross-product, not a catalogue")
    p = pairings("0xradar", net["devices"])
    c.check("radar reaches everything that can be acted on",
            {x["target_ieee"] for x in p["outbound"]} ==
            {"0xhalllight", "nuki_1", "matter_plug", "0xtrv",
             "user::sean", "user::charlie"},
            {x["target_ieee"] for x in p["outbound"]})
    c.check("a radar with no actions has no inbound wiring",
            p["inbound"] == [], len(p["inbound"]))

    c.section("ranking puts the obvious pair first")
    top = p["outbound"][0]
    c.check("highest-ranked target is the light in the same room",
            top["target_ieee"] == "0xhalllight", top["target_ieee"])
    c.check("highest-ranked pair reads as a sentence",
            top["sentence"] == "When someone is detected in Hallway, "
                               "turn on Light - Hallway",
            top["sentence"])

    battery_msg = _find(p["outbound"], "battery:low", "user::charlie", "notify:message")
    c.check("a battery warning is offered", battery_msg is not None)
    c.check("but never outranks the room's primary automation",
            battery_msg["score"] < same_room_score(p), battery_msg["score"])

    same_room = _find(p["outbound"], "presence:detected", "0xhalllight", "on_off:turn_on")
    cross_room = _find(p["outbound"], "presence:detected", "matter_plug", "on_off:turn_on")
    c.check("same-room light outranks a cross-room plug",
            same_room["score"] > cross_room["score"],
            (same_room["score"], cross_room["score"]))
    c.check("same-room lighting pair is high confidence",
            same_room["confidence"] == "high", same_room)
    c.check("the cross-room pair is still offered, just lower",
            cross_room is not None and cross_room["confidence"] != "high",
            cross_room and cross_room["confidence"])
    c.check("same_room is reported", same_room["same_room"] is True)

    c.section("polarity keeps inverted pairs out of the lead")
    on_match = _find(p["outbound"], "presence:detected", "0xhalllight", "on_off:turn_on")
    on_invert = _find(p["outbound"], "presence:detected", "0xhalllight", "on_off:turn_off")
    c.check("detection-to-on beats detection-to-off",
            on_match["score"] > on_invert["score"],
            (on_match["score"], on_invert["score"]))
    c.check("the inverted pair drops a confidence band",
            on_invert["confidence"] != "high", on_invert["confidence"])
    c.check("the inverted pair is still offered", on_invert is not None)
    off_match = _find(p["outbound"], "presence:cleared", "0xhalllight", "on_off:turn_off")
    c.check("clearing-to-off is high confidence",
            off_match["confidence"] == "high", off_match["score"])

    lock_p = pairings("user::sean", net["devices"])
    arrive_unlock = _find(lock_p["outbound"], "person:arrived_home", "nuki_1", "lock:unlock")
    arrive_lock = _find(lock_p["outbound"], "person:arrived_home", "nuki_1", "lock:lock")
    leave_lock = _find(lock_p["outbound"], "person:left_home", "nuki_1", "lock:lock")
    c.check("arriving unlocks rather than locks",
            arrive_unlock["score"] > arrive_lock["score"],
            (arrive_unlock["score"], arrive_lock["score"]))
    c.check("leaving locks", leave_lock["confidence"] == "high", leave_lock["score"])

    c.section("actuators are trigger sources too")
    lp = pairings("0xhalllight", net["devices"])
    c.check("the light has inbound wiring", lp["inbound"], lp["inbound_total"])
    c.check("the light also drives other devices",
            any(x["trigger"]["capability"] == "on_off" for x in lp["outbound"]),
            [x["trigger"]["key"] for x in lp["outbound"][:3]])
    self_pairs = [x for x in lp["outbound"] if x["same_device"]]
    c.check("a device may wire to itself", self_pairs, len(self_pairs))
    c.check("self-wiring never leads",
            all(x["score"] < max(y["score"] for y in lp["outbound"]
                                 if not y["same_device"])
                for x in self_pairs),
            [x["score"] for x in self_pairs])
    plug = pairings("matter_plug", net["devices"])
    c.check("a plug does not lead with switching itself on",
            not plug["outbound"][0]["same_device"], plug["outbound"][0]["sentence"])

    c.section("house-scope devices reach everywhere")
    sp = pairings("user::sean", net["devices"])
    c.check("a person reaches every controllable device",
            {"0xhalllight", "nuki_1", "matter_plug", "0xtrv"} <=
            {x["target_ieee"] for x in sp["outbound"]},
            {x["target_ieee"] for x in sp["outbound"]})
    unlock = _find(sp["outbound"], "person:arrived_home", "nuki_1", "lock:unlock")
    c.check("arriving home pairs with unlocking the door", unlock is not None)
    c.check("that pair is high confidence despite no shared room",
            unlock and unlock["confidence"] == "high", unlock and unlock["score"])
    preheat = _find(sp["outbound"], "person:arrived_home", "0xtrv",
                    "thermostat:set_setpoint")
    c.check("arriving home pairs with the heating", preheat is not None)

    c.section("safety triggers reach the people who need telling")
    door = pairings("0xfrontdoor", net["devices"])
    msg = [x for x in door["outbound"] if x["action"]["capability"] == "notify"]
    c.check("an opening door can message a person", msg, len(msg))
    c.check("messaging targets are the presence users",
            {x["target_ieee"] for x in msg} == {"user::sean", "user::charlie"},
            {x["target_ieee"] for x in msg})

    c.section("confidence filtering")
    high = pairings("0xradar", net["devices"], min_confidence="high")
    c.check("filtering narrows the list",
            len(high["outbound"]) < p["outbound_total"],
            (len(high["outbound"]), p["outbound_total"]))
    c.check("everything returned meets the floor",
            all(x["confidence"] == "high" for x in high["outbound"]))

    c.section("limits")
    capped = pairings("0xradar", net["devices"], limit=3)
    c.check("limit caps the returned list", len(capped["outbound"]) == 3)
    c.check("the total is still reported",
            capped["outbound_total"] == p["outbound_total"],
            capped["outbound_total"])

    c.section("an empty network degrades quietly")
    empty = describe_network({}, {}, {}, {})
    c.check("no devices, no crash", empty["devices"] == [])
    c.check("summary still shaped", empty["summary"]["devices"] == 0)
    c.check("pairings for an unknown device return empty",
            pairings("nope", [])["outbound"] == [])

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
