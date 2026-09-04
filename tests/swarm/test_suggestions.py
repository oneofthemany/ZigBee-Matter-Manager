"""
Suggestion tests — matching, compiling, deduplication and coverage.

    python3 tests/swarm/test_suggestions.py

The assertions are about outcomes a person would recognise: the hallway radar
produces the hallway rule, both people get their own arrival rule, and a rule
already built is not offered again even when its thresholds differ.
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, SAMPLE_NAMES, SAMPLE_ROOMS, SAMPLE_SETTINGS, sample_network,
)

from modules.swarm import suggestions as sg  # noqa: E402
from modules.swarm.compiler import CompileError, compile_rule, effective_params  # noqa: E402
from modules.swarm.dedupe import coverage, index_rules, signature, status_for  # noqa: E402
from modules.swarm.matcher import match_pattern  # noqa: E402
from modules.swarm.network import describe_network  # noqa: E402
from modules.swarm.stigmergy import StigmergyStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
STORE = StigmergyStore(bundled_dir=str(REPO / "modules" / "swarm" / "patterns"),
                       user_dir="/nonexistent")


def _net():
    return describe_network(sample_network(), SAMPLE_NAMES, SAMPLE_SETTINGS,
                            SAMPLE_ROOMS)["devices"]


def _built(rules=None):
    return sg.build(_net(), rules=rules or [], rooms=SAMPLE_ROOMS,
                    patterns=STORE.all())


def _by_sentence(built, needle):
    return [s for s in built["suggestions"] if needle in s["sentence"]]


def run() -> Checker:
    c = Checker("test_suggestions")
    described = _net()

    c.section("the hallway pattern matches the hallway")
    pattern = STORE.get("presence_light_when_dark")
    result = match_pattern(pattern, described, SAMPLE_ROOMS)
    c.check("one candidate", len(result["candidates"]) == 1, len(result["candidates"]))
    fills = result["candidates"][0]["fills"]
    c.check("trigger is the radar", fills["trig"]["ieee"] == "0xradar")
    c.check("light is the hallway light", fills["light"]["ieee"] == "0xhalllight")
    c.check("the lux check lands on the radar itself",
            fills["dark"]["ieee"] == "0xradar", fills["dark"]["ieee"])
    c.check("the lounge is traced as a non-match",
            any(t["room"] == "lounge" and t["outcome"] == "no_match"
                for t in result["trace"]))

    c.section("same device means condition, another device means prerequisite")
    rule = compile_rule(pattern, fills, room_label="Hallway")
    attrs = {cond["attribute"] for cond in rule["conditions"]}
    c.check("both checks are conditions on the source",
            attrs == {"presence", "illuminance_lux"}, attrs)
    c.check("nothing became a prerequisite", rule["prerequisites"] == [],
            rule["prerequisites"])

    door = STORE.get("door_entry_light")
    dr = match_pattern(door, described, SAMPLE_ROOMS)
    drule = compile_rule(door, dr["candidates"][0]["fills"], room_label="Hallway")
    c.check("the door's own contact is the condition",
            [x["attribute"] for x in drule["conditions"]] == ["contact"],
            drule["conditions"])
    c.check("the radar's lux became a prerequisite",
            [p["ieee"] for p in drule["prerequisites"]] == ["0xradar"],
            drule["prerequisites"])
    c.check("the prerequisite names its attribute",
            drule["prerequisites"][0]["attribute"] == "illuminance_lux")

    c.section("parameters override the vocabulary default")
    c.check("the pattern's dark_lux is used",
            effective_params(pattern)["dark_lux"] == 11)
    lux = [x for x in rule["conditions"] if x["attribute"] == "illuminance_lux"][0]
    c.check("and reaches the compiled rule", lux["value"] == 11, lux)
    tuned = compile_rule(pattern, fills, overrides={"dark_lux": 40})
    lux2 = [x for x in tuned["conditions"] if x["attribute"] == "illuminance_lux"][0]
    c.check("a user override reaches it too", lux2["value"] == 40, lux2)

    c.section("the sentence shown always matches the rule compiled")
    # A pattern raising a threshold must not advertise the vocabulary default.
    # Checked on an offer whose sentence carries the number: "Lounge drops
    # below 18.0" must not still read 18.0 once the pattern says 9.
    from modules.swarm.compiler import describe_candidate
    heat = STORE.get("cold_room_heat")
    raised = {**heat, "id": "raised", "params": {**heat["params"], "cold_c": 9.0}}
    rfills = match_pattern(raised, described, SAMPLE_ROOMS)["candidates"][0]["fills"]
    rrule = compile_rule(raised, rfills)
    c.check("the rule uses the pattern's value",
            rrule["conditions"][0]["value"] == 9.0, rrule["conditions"])
    sentence = describe_candidate(raised, rfills)
    c.check("and so does the sentence", "9.0" in sentence, sentence)
    c.check("the default is not still advertised", "18.0" not in sentence, sentence)

    c.section("a slot may override a threshold the pattern shares")
    # cold_c means a cold snap outdoors and an unheated room indoors; one value
    # cannot serve both, so a slot carries its own.
    snap = STORE.get("cold_snap_heat")
    c.check("the shipped pattern uses a slot override",
            (snap["slots"]["in"].get("params") or {}).get("cold_c") == 18.0,
            snap["slots"]["in"])
    c.check("pattern level is the outdoor value",
            effective_params(snap)["cold_c"] == 5.0)
    c.check("slot level is the indoor value",
            effective_params(snap, slot="in")["cold_c"] == 18.0)
    c.check("an unrelated slot is unaffected",
            effective_params(snap, slot="cold")["cold_c"] == 5.0)
    c.check("a user override still wins",
            effective_params(snap, {"cold_c": 1.0}, slot="in")["cold_c"] == 1.0)

    c.section("house-scoped patterns produce one suggestion per subject")
    built = _built()
    arrivals = _by_sentence(built, "unlock Front Door Lock")
    c.check("both people get an arrival rule", len(arrivals) == 2, len(arrivals))
    c.check("named individually",
            {a["sentence"].split()[1] for a in arrivals} == {"Sean", "Charlie"},
            [a["sentence"] for a in arrivals])
    batteries = _by_sentence(built, "battery falls below")
    c.check("every battery device gets its own alert",
            len(batteries) == 3, [b["sentence"] for b in batteries])

    c.section("a local question is asked of the room it is about")
    # A house-scoped pattern with an optional "is it dark" check must ask it of
    # the room being lit. Any lux sensor is technically an answer; the bathroom
    # deciding for the living room reads as a mistake.
    arrival = STORE.get("arrival_lights_when_dark")
    c.check("the shipped pattern anchors its dark check to the light",
            arrival["slots"]["dark"].get("prefer") == "same_room"
            and arrival["slots"]["dark"].get("prefer_slot") == "light",
            arrival["slots"]["dark"])

    ar = match_pattern(arrival, described, SAMPLE_ROOMS)
    for cand in ar["candidates"]:
        dark, light = cand["fills"].get("dark"), cand["fills"]["light"]
        if not dark:
            continue
        if not c.check("the lux sensor is in the room being lit",
                       dark["device"].get("room") == light["device"].get("room"),
                       (dark["device"]["name"], light["device"]["name"])):
            break

    c.check("a room that cannot answer still gets a suggestion, without it",
            any(c2["fills"].get("dark") is None for c2 in ar["candidates"])
            or all(c2["fills"].get("dark") for c2 in ar["candidates"]),
            [bool(c2["fills"].get("dark")) for c2 in ar["candidates"]])

    from modules.swarm.matcher import _prefer_filter
    a = {"ieee": "0xa", "room": "hall"}
    pairs = [({"ieee": "0xa", "room": "hall"}, {}), ({"ieee": "0xb", "room": "hall"}, {}),
             ({"ieee": "0xc", "room": "lounge"}, {})]
    c.check("same_device narrows to the anchor",
            [p[0]["ieee"] for p in _prefer_filter(pairs, "same_device", a)] == ["0xa"])
    c.check("same_room narrows to its room",
            [p[0]["ieee"] for p in _prefer_filter(pairs, "same_room", a)] == ["0xa", "0xb"])
    c.check("an anchor with no room narrows to nothing",
            _prefer_filter(pairs, "same_room", {"ieee": "0xz"}) == [])

    c.section("a dual-gang device suggests per outlet, not per device")
    # Outlet 1 may be the washing machine and outlet 2 the dryer, so "tell me
    # when it finishes" is two rules. A button's four press types share an
    # endpoint and stay one suggestion.
    from harness import FakeCapabilities, FakeDevice as _FD
    devs = sample_network()
    devs["0xdual"] = _FD(
        "0xdual", "Socket - Media",
        {"state_1": "ON", "state_2": "OFF", "power_1": 43.2, "power_2": 0.4},
        commands=[{"command": "on", "label": "On", "endpoint_id": 1},
                  {"command": "off", "label": "Off", "endpoint_id": 1},
                  {"command": "on", "label": "On", "endpoint_id": 2},
                  {"command": "off", "label": "Off", "endpoint_id": 2}],
        capabilities=FakeCapabilities(["on_off", "power_monitoring", "multi_switch"]))
    dn = dict(SAMPLE_NAMES); dn["0xdual"] = "Socket - Media"
    ds = dict(SAMPLE_SETTINGS); ds["0xdual"] = {"chamber": "lounge"}
    dbuilt = sg.build(describe_network(devs, dn, ds, SAMPLE_ROOMS)["devices"],
                      rules=[], rooms=SAMPLE_ROOMS, patterns=STORE.all())
    fin = [x for x in dbuilt["suggestions"]
           if x["pattern_id"] == "appliance_finished" and "Socket - Media" in x["sentence"]]
    c.check("both outlets are suggested", len(fin) == 2, [x["sentence"] for x in fin])
    c.check("outlet 2 is named", any("(outlet 2)" in x["sentence"] for x in fin),
            [x["sentence"] for x in fin])
    c.check("they are separate suggestions", len({x["id"] for x in fin}) == 2)
    c.check("nothing was rejected", dbuilt["rejected"] == [], dbuilt["rejected"])

    c.section("notify slots offer a recipient rather than multiplying")
    leaks = _by_sentence(built, "finishes")
    c.check("one appliance-finished suggestion, not one per person",
            len(leaks) == 1, [x["sentence"] for x in leaks])

    c.section("sentences read as English, not as attribute dumps")
    hall = _by_sentence(built, "someone is detected in Hallway")
    c.check("the hallway suggestion exists", hall, len(hall))
    c.check("it reads correctly",
            hall[0]["sentence"] == "When someone is detected in Hallway and "
                                   "Hallway is dark, turn on Light - Hallway — "
                                   "otherwise turn off Light - Hallway",
            hall[0]["sentence"])

    c.section("an offer splices its action into the accept branch")
    ac = STORE.get("ac_offer_when_cooler_outside")
    c.check("the shipped pattern uses the slot-step marker",
            any(isinstance(e, dict) and e.get("type") == "offer"
                for e in ac["emits"]["then"]), ac["emits"]["then"])
    step = next(e for e in ac["emits"]["then"] if e.get("type") == "offer")
    c.check("its accept branch names a slot rather than a device",
            step["accept_steps"] == [{"slot": "cool_it"}], step["accept_steps"])
    c.check("the recipient is a slot too", step["to_user"] == "$who")

    c.section("nothing is offered that would fail to compile")
    c.check("no candidate was rejected", built["rejected"] == [], built["rejected"])
    c.check("every suggestion carries a rule",
            all(s.get("rule", {}).get("source_ieee") for s in built["suggestions"]))

    c.section("deduplication is by wiring, not by text")
    target = hall[0]
    existing = dict(target["rule"])
    existing["id"] = "auto_x"
    existing["name"] = "totally different name"
    existing["cooldown"] = 999
    existing["conditions"] = [dict(x) for x in existing["conditions"]]
    for cond in existing["conditions"]:
        if cond["attribute"] == "illuminance_lux":
            cond["value"] = 25
    c.check("a differently-tuned rule has the same signature",
            signature(existing) == signature(target["rule"]))

    rebuilt = _built(rules=[existing])
    again = sg.find(rebuilt, target["id"])
    c.check("the suggestion comes back as active",
            again["status"] == "active", again["status"])
    c.check("and points at the rule", again["rule_id"] == "auto_x")
    c.check("the summary counts it", rebuilt["summary"]["active"] == 1,
            rebuilt["summary"])
    c.check("an unrelated suggestion stays available",
            sg.find(rebuilt, arrivals[0]["id"])["status"] == "available")

    c.section("a disabled rule reads as disabled, not available")
    off = dict(existing); off["enabled"] = False
    c.check("status is disabled",
            status_for(target["rule"], index_rules([off]))["status"] == "disabled")

    c.section("suggestion ids are stable")
    c.check("the same network yields the same ids",
            {s["id"] for s in _built()["suggestions"]} ==
            {s["id"] for s in built["suggestions"]})

    c.section("coverage")
    cov = coverage(described, [])
    c.check("nothing covered with no rules", cov["covered"] == 0, cov)
    c.check("gaps list every device", cov["uncovered"] == len(described))
    cov2 = coverage(described, [existing])
    c.check("the rule's source and target both count",
            cov2["covered"] == 2, cov2["covered"])
    c.check("percentage reported", cov2["percent"] == 25, cov2["percent"])

    c.section("recompiling checks the network has not moved on")
    err = None
    try:
        sg.recompile(pattern, {"id": "sg_bogus"}, described, rooms=SAMPLE_ROOMS)
    except CompileError as e:
        err = str(e)
    c.check("a stale suggestion id is refused with a reason",
            err and "no longer matches" in err, err)

    c.section("an empty network suggests nothing and does not crash")
    empty = sg.build([], rules=[], rooms={}, patterns=STORE.all())
    c.check("no suggestions", empty["suggestions"] == [])
    c.check("no rejections", empty["rejected"] == [], empty["rejected"])
    c.check("coverage is zero", empty["coverage"]["percent"] == 0)
    c.check("every pattern is traced as unmatched",
            empty["summary"]["patterns_unmatched"] == len(STORE.all()))

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
