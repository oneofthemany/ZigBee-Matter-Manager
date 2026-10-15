"""
Alike targets are one suggestion, and a group can stand for them.

    python3 tests/swarm/test_alike_targets.py

docs/swarm-intelligence.md, "What varies": five kitchen lights doing the same
thing are one card with five ticked lights, not five cards, and where a group
holds them the card can send to the group instead — one command on the radio.
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, FakeCapabilities, FakeDevice  # noqa: E402

import test_real_house as rh  # noqa: E402
from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.compiler import CompileError  # noqa: E402
from modules.swarm.dedupe import _walk_steps  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402

KITCHEN = [("40:30:59:ff:fe:dd:ea:be", "Light - Kitchen Balcony"),
           ("40:30:59:ff:fe:f0:8d:8d", "Light - Kitchen Cooker"),
           ("40:30:59:ff:fe:f0:bb:ef", "Light - Kitchen Island"),
           ("40:30:59:ff:fe:00:00:01", "Light - Kitchen Sink"),
           ("40:30:59:ff:fe:00:00:02", "Light - Kitchen Washing")]
LIGHTS = {ieee for ieee, _ in KITCHEN}
GROUPS = {"group:13": {"id": "group:13", "name": "Kitchen Lights", "members": sorted(LIGHTS)},
          # Two of the five: offered, but not as the card's default.
          "group:20": {"id": "group:20", "name": "Kitchen Island Pair",
                       "members": ["40:30:59:ff:fe:f0:bb:ef", "40:30:59:ff:fe:00:00:01"]},
          # A light and a socket: no fit, since it would switch a socket.
          "group:21": {"id": "group:21", "name": "Mixed",
                       "members": ["40:30:59:ff:fe:dd:ea:be", "5c:02:72:ff:fe:c6:18:f5"]}}


def _network():
    devices = rh._house()
    for ieee, name in KITCHEN:
        devices[ieee] = FakeDevice(ieee, name, {"state": "OFF", "brightness": 200},
                                   commands=rh.DIM,
                                   capabilities=FakeCapabilities(["light", "on_off", "level_control"]))
        devices[ieee]._room = "kitchen"
    names = {i: d.friendly_name for i, d in devices.items()}
    settings = {i: {"chamber": d._room} for i, d in devices.items() if d._room}
    return describe_network(devices, names, settings, rh.ROOMS)["devices"], names


def _targets(seq):
    return sorted({s["target_ieee"] for s in _walk_steps(seq)
                   if s.get("type") == "command" and s.get("target_ieee")})


def run() -> Checker:
    c = Checker("test_alike_targets")
    described, names = _network()
    patterns = rh.STORE.all()
    built = sg.build(described, rules=[], rooms=rh.ROOMS, names=names,
                     patterns=patterns, groups=GROUPS)
    kitchen = [s for s in built["suggestions"] if s.get("room") == "kitchen"
               and any(d["ieee"] in LIGHTS for d in s["devices"])]

    c.section("five alike lights are one card")
    cards = [s for s in kitchen if s["pattern_id"] == "presence_light_when_dark"]
    c.check("one card for lights on when someone comes into a dark kitchen", len(cards) == 1,
            [s["sentence"] for s in cards])
    card = cards[0]
    listed = {d["ieee"] for d in card["devices"] if d["slot"] in card["choosable"]}
    c.check("every kitchen light is on it, to be unticked", listed == LIGHTS, listed)
    c.check("and listed once, though the rule also turns them off",
            sum(1 for d in card["devices"] if d["ieee"] in LIGHTS and not d.get("follows")) == 5)
    c.check("the sentence is short", "and 3 more" in card["sentence"], card["sentence"])
    per_pattern = {}
    for s in kitchen:
        per_pattern[s["pattern_id"]] = per_pattern.get(s["pattern_id"], 0) + 1
    c.check("no pattern offers the kitchen lights more than once",
            all(n == 1 for n in per_pattern.values()), per_pattern)
    c.check("a socket is not merged with the lights",
            not any(d["name"].startswith("Socket") for s in kitchen for d in s["devices"]
                    if d["slot"] in s["choosable"]))

    c.section("the groups that fit are offered")
    offered = card["groups"].get(card["choosable"][0], [])
    c.check("the group holding exactly these lights first, as the default",
            offered and offered[0]["id"] == "group:13" and offered[0]["covers_all"], offered)
    c.check("a group holding some of them too", any(g["id"] == "group:20" for g in offered))
    c.check("never one that would switch something else", all(g["id"] != "group:21" for g in offered))

    pattern = next(p for p in patterns if p["id"] == card["pattern_id"])
    slot = card["choosable"][0]

    c.section("sent to the group")
    rule = sg.recompile(pattern, card, described, rooms=rh.ROOMS,
                        chosen_groups={slot: "group:13"}, groups=GROUPS)
    c.check("on is one command, to the group", _targets(rule["then_sequence"]) == ["group:13"],
            rule["then_sequence"])
    c.check("and so is off", _targets(rule["else_sequence"]) == ["group:13"], rule["else_sequence"])
    c.check("a group step has no endpoint",
            all(s.get("endpoint_id") is None for s in _walk_steps(rule["then_sequence"])
                if s.get("target_ieee") == "group:13"))
    part = sg.recompile(pattern, card, described, rooms=rh.ROOMS,
                        chosen_groups={slot: "group:20"}, groups=GROUPS)
    c.check("a smaller group, and the other lights each",
            _targets(part["then_sequence"]) == sorted({"group:20"} | (LIGHTS - set(GROUPS["group:20"]["members"]))),
            _targets(part["then_sequence"]))
    try:
        sg.recompile(pattern, card, described, rooms=rh.ROOMS,
                     chosen_groups={slot: "group:21"}, groups=GROUPS)
        forged = False
    except CompileError:
        forged = True
    c.check("a group the card did not offer is refused", forged)

    c.section("light by light, some unticked")
    sink = "40:30:59:ff:fe:00:00:01"
    each = sg.recompile(pattern, card, described, rooms=rh.ROOMS, exclude=[sink], groups=GROUPS)
    c.check("on drives the four still ticked", _targets(each["then_sequence"]) == sorted(LIGHTS - {sink}))
    c.check("and off the same four", _targets(each["else_sequence"]) == sorted(LIGHTS - {sink}))
    one = sg.recompile(pattern, card, described, rooms=rh.ROOMS,
                       exclude=sorted(LIGHTS - {sink}), groups=GROUPS)
    c.check("one light alone is a single-light automation", _targets(one["then_sequence"]) == [sink])

    c.section("a rule aimed at the group is that suggestion, built")
    again = sg.build(described, rules=[{**rule, "id": "auto_kitchen"}], rooms=rh.ROOMS,
                     names=names, patterns=patterns, groups=GROUPS)
    same = sg.find(again, card["id"])
    c.check("the card is marked built", same and same["status"] == "active"
            and same["rule_id"] == "auto_kitchen", same and same["status"])
    covered = {g["ieee"] for g in again["coverage"]["gaps"]}
    c.check("and the group's lights count as automated", not (LIGHTS & covered))

    c.section("ids are stable")
    rebuilt = sg.build(described, rules=[], rooms=rh.ROOMS, names=names,
                       patterns=patterns, groups=GROUPS)
    c.check("the same network gives the same card ids",
            [s["id"] for s in rebuilt["suggestions"]] == [s["id"] for s in built["suggestions"]])
    c.check("nothing is withheld", not built["rejected"], built["rejected"][:2])
    return c


if __name__ == "__main__":
    run()
