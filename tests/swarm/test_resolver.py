"""
Resolver tests — one device, of any protocol, reduced to its offers.

    python3 tests/swarm/test_resolver.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Checker, FakeCapabilities, FakeDevice, LOCK_COMMANDS, ONOFF_COMMANDS,
    SAMPLE_NAMES, SAMPLE_ROOMS, SAMPLE_SETTINGS, offer, sample_network,
)

from modules.swarm.capabilities import CAPABILITIES, PARAMS  # noqa: E402
from modules.swarm.resolver import (  # noqa: E402
    _coerce_bool, _contact_values, describe_device, device_capabilities,
)


def run() -> Checker:
    c = Checker("test_resolver")
    net = sample_network()

    c.section("capability folding across the three legacy vocabularies")
    radar_caps = device_capabilities("0xradar", net["0xradar"], net["0xradar"].state)
    c.check("radar folds presence_sensor/radar_sensor/occupancy_sensing to one id",
            radar_caps.count("presence") == 1, radar_caps)
    c.check("radar picks up illuminance by sniffing alone",
            "illuminance" in radar_caps, radar_caps)
    c.check("transport markers produce no capability",
            "tuya" not in radar_caps and "matter" not in radar_caps, radar_caps)

    plug_caps = device_capabilities("matter_plug", net["matter_plug"],
                                    net["matter_plug"].state)
    c.check("matter list accessor resolves", "on_off" in plug_caps, plug_caps)
    c.check("power_monitoring folds to power", "power" in plug_caps, plug_caps)

    c.section("a device with no declared capabilities still resolves")
    bare = FakeDevice("0xbare", "Unknown Sensor",
                      {"occupancy": False, "illuminance": 3, "temperature": 19.0})
    bare_caps = device_capabilities("0xbare", bare, bare.state)
    c.check("sniffing alone finds presence", "presence" in bare_caps, bare_caps)
    c.check("sniffing alone finds illuminance", "illuminance" in bare_caps, bare_caps)
    c.check("sniffing alone finds temperature", "temperature" in bare_caps, bare_caps)

    c.section("offers are backed by attributes that exist")
    radar = describe_device("0xradar", net["0xradar"], "Radar Sensor - Hallway",
                            "hallway", "Hallway")
    detected = offer(radar["triggers"], "presence:detected")
    c.check("presence trigger present", detected is not None)
    c.check("presence trigger binds to the real attribute",
            detected and detected["attribute"] == "presence", detected)
    c.check("presence trigger reads as a sentence",
            detected and detected["label"] == "someone is detected in Hallway",
            detected and detected["label"])
    dark = offer(radar["conditions"], "illuminance:is_dark")
    c.check("dark condition uses the shared lux default",
            dark and dark["condition"]["value"] == PARAMS["dark_lux"]["default"], dark)
    c.check("dark condition is tunable", dark and dark.get("param") == "dark_lux", dark)
    c.check("a sensor offers no actions", radar["actions"] == [], radar["actions"])
    c.check("radar classifies as a presence sensor",
            radar["device_class"] == "presence_sensor", radar["device_class"])

    c.section("actuators offer actions, and are themselves trigger sources")
    light = describe_device("0xhalllight", net["0xhalllight"], "Light - Hallway",
                            "hallway", "Hallway")
    c.check("turn_on offered", offer(light["actions"], "on_off:turn_on") is not None)
    c.check("brightness offered",
            offer(light["actions"], "brightness:set_brightness") is not None)
    turned_on = offer(light["triggers"], "on_off:turned_on")
    c.check("an actuator being switched on is itself a trigger",
            turned_on is not None)
    c.check("boolean offer rendered in the device's own vocabulary",
            turned_on and turned_on["condition"]["value"] == "ON", turned_on)
    c.check("action carries the endpoint the command lives on",
            offer(light["actions"], "on_off:turn_on")["step"]["endpoint_id"] == 1)
    c.check("light classifies as a light",
            light["device_class"] == "light", light["device_class"])

    c.section("commands the engine cannot dispatch are never offered")
    lock = describe_device("nuki_1", net["nuki_1"], "Front Door Lock")
    cmds = {a["step"]["command"] for a in lock["actions"] if "command" in a["step"]}
    c.check("lock and unlock offered", {"lock", "unlock"} <= cmds, cmds)
    c.check("unlatch and lock_n_go dropped",
            not ({"unlatch", "lock_n_go"} & cmds), cmds)

    c.section("contact polarity")
    c.check("bare `contact` True means closed",
            _contact_values("contact", True) == (False, True))
    c.check("`is_open` means what it says",
            _contact_values("is_open", True) == (True, False))
    c.check("`is_closed` inverts",
            _contact_values("is_closed", True) == (False, True))
    c.check("string contacts stay strings",
            _contact_values("contact", "open") == ("open", "closed"))

    door = describe_device("0xfrontdoor", net["0xfrontdoor"], "Front Door Contact",
                           "hallway", "Hallway")
    opened = offer(door["triggers"], "contact:opened")
    c.check("opened trigger compiles to the un-intuitive polarity",
            opened and opened["condition"]["value"] is False, opened)

    c.section("boolean coercion")
    c.check("bool device keeps bools", _coerce_bool(True, False) is True)
    c.check("ON/OFF device gets ON", _coerce_bool(True, "OFF") == "ON")
    c.check("ON/OFF device gets OFF", _coerce_bool(False, "ON") == "OFF")
    c.check("open/closed device gets the right word",
            _coerce_bool(False, "open") == "closed")
    c.check("numeric device gets 1/0", _coerce_bool(True, 0) == 1)

    c.section("presence users are house-scoped and zone-triggered")
    sean = describe_device("user::sean", net["user::sean"], "Sean")
    c.check("person scope is house", sean["scope"] == "house", sean["scope"])
    arrived = offer(sean["triggers"], "person:arrived_home")
    c.check("arrival trigger present", arrived is not None)
    c.check("arrival compiles to a zone condition",
            arrived and arrived["condition"] == {"type": "zone", "event": "enter",
                                                 "place": "home"}, arrived)
    c.check("a person can be messaged",
            offer(sean["actions"], "notify:message") is not None, sean["actions"])
    c.check("person is not an actuator target for commands",
            all("command" not in a["step"] for a in sean["actions"]), sean["actions"])

    c.section("endpoint-suffixed attributes resolve")
    # A multi-endpoint device spells its attributes power_1, state_1 and so on.
    # Matching only the bare name silently excluded a whole class of devices.
    from modules.swarm.resolver import _pick_attr
    c.check("an exact name still wins",
            _pick_attr({"power": 1, "power_1": 2}, ["power"]) == "power")
    c.check("a suffixed name is accepted",
            _pick_attr({"power_1": 2}, ["power"]) == "power_1")
    c.check("the lowest endpoint wins",
            _pick_attr({"power_3": 1, "power_1": 2}, ["power"]) == "power_1")
    c.check("a different base is not matched",
            _pick_attr({"power_demand_1": 5}, ["power"]) is None)
    c.check("diagnostics are still skipped",
            _pick_attr({"linkquality": 90}, ["linkquality"]) is None)
    c.check("a non-numeric suffix is not treated as an endpoint",
            _pick_attr({"state_l1": "ON"}, ["state"]) is None)

    socket = FakeDevice(
        "0xsock", "Socket - Media",
        {"state_1": "ON", "power_1": 43.2, "energy_1": 1.8, "voltage_1": 241},
        commands=[{"command": "on", "label": "On", "endpoint_id": 1},
                  {"command": "off", "label": "Off", "endpoint_id": 1}],
        capabilities=FakeCapabilities(["on_off", "metering", "power_monitoring"]))
    sd = describe_device("0xsock", socket, "Socket - Media")
    started = offer(sd["triggers"], "power:started")
    c.check("the socket's power trigger resolves", started is not None,
            [t["key"] for t in sd["triggers"]])
    c.check("bound to the suffixed attribute",
            started and started["attribute"] == "power_1", started)
    on = offer(sd["triggers"], "on_off:turned_on")
    c.check("and so does its on/off trigger",
            on and on["attribute"] == "state_1", on)

    c.section("a dual-gang device offers both outlets independently")
    # handlers/power.py spells a two-gang socket state_1/state_2, power_1/power_2.
    # Both outlets switch and draw power independently, so both are separately
    # triggerable — the action side has always fanned out, and the trigger side
    # collapsing to outlet 1 made the two halves disagree.
    dual = FakeDevice(
        "0xdual", "Socket - Media",
        {"state_1": "ON", "state_2": "OFF", "power_1": 43.2, "power_2": 0.4},
        commands=[{"command": "on", "label": "On", "endpoint_id": 1},
                  {"command": "off", "label": "Off", "endpoint_id": 1},
                  {"command": "on", "label": "On", "endpoint_id": 2},
                  {"command": "off", "label": "Off", "endpoint_id": 2}],
        capabilities=FakeCapabilities(["on_off", "power_monitoring", "multi_switch"]))
    dd = describe_device("0xdual", dual, "Socket - Media")
    tkeys = {t["key"] for t in dd["triggers"]}
    c.check("outlet 1 keeps the bare key", "power:started" in tkeys, tkeys)
    c.check("outlet 2 gets its own offer", "power:started:ep2" in tkeys, tkeys)
    c.check("on/off fans out too",
            {"on_off:turned_on", "on_off:turned_on:ep2"} <= tkeys, tkeys)

    ep1 = offer(dd["triggers"], "power:started")
    ep2 = offer(dd["triggers"], "power:started:ep2")
    c.check("each binds to its own attribute",
            ep1["attribute"] == "power_1" and ep2["attribute"] == "power_2",
            (ep1["attribute"], ep2["attribute"]))
    c.check("the endpoint is recorded",
            ep1["endpoint_id"] == 1 and ep2["endpoint_id"] == 2)
    c.check("outlet 1 is not annotated — it is the default one",
            "outlet" not in ep1["label"], ep1["label"])
    c.check("outlet 2 is annotated", "(outlet 2)" in ep2["label"], ep2["label"])

    akeys = {a["key"] for a in dd["actions"]}
    c.check("actions use the same key convention",
            {"on_off:turn_on", "on_off:turn_on:ep2"} <= akeys, akeys)
    c.check("and the same label convention",
            "(outlet 2)" in offer(dd["actions"], "on_off:turn_on:ep2")["label"])
    c.check("each action targets its own endpoint",
            offer(dd["actions"], "on_off:turn_on")["step"]["endpoint_id"] == 1
            and offer(dd["actions"], "on_off:turn_on:ep2")["step"]["endpoint_id"] == 2)

    c.section("a single-gang device is unaffected")
    lamp = FakeDevice("0xlamp", "Lamp", {"state": "OFF"},
                      commands=[{"command": "on", "label": "On", "endpoint_id": 1},
                                {"command": "off", "label": "Off", "endpoint_id": 1}],
                      capabilities=FakeCapabilities(["on_off"]))
    ld = describe_device("0xlamp", lamp, "Lamp")
    c.check("one trigger per edge, no endpoint suffix",
            {t["key"] for t in ld["triggers"]} == {"on_off:turned_on", "on_off:turned_off"},
            {t["key"] for t in ld["triggers"]})
    c.check("no outlet annotation anywhere",
            not any("outlet" in o["label"] for o in ld["triggers"] + ld["actions"]))
    c.check("a bare attribute reports no endpoint",
            offer(ld["triggers"], "on_off:turned_on")["endpoint_id"] is None)

    c.section("an exact bare name is not treated as an endpoint")
    from modules.swarm.resolver import _pick_attrs
    c.check("a plain attribute short-circuits the scan",
            _pick_attrs({"power": 5, "power_2": 1}, ["power"]) == [(None, "power")],
            _pick_attrs({"power": 5, "power_2": 1}, ["power"]))
    c.check("suffixed forms come back in endpoint order",
            _pick_attrs({"power_3": 1, "power_1": 2}, ["power"])
            == [(1, "power_1"), (3, "power_3")])
    c.check("nothing matching returns empty",
            _pick_attrs({"humidity": 5}, ["power"]) == [])

    c.section("a device reports what it says, for diagnostics")
    c.check("state keys are exposed",
            set(sd["state_keys"]) == {"state_1", "power_1", "energy_1", "voltage_1"},
            sd["state_keys"])
    noisy = FakeDevice("0xnoise", "Noisy", {"presence": True, "attr_1024_0": 5,
                                            "presence_raw": 1, "linkquality": 80})
    c.check("raw and diagnostic keys are excluded",
            describe_device("0xnoise", noisy, "Noisy")["state_keys"] == ["presence"],
            describe_device("0xnoise", noisy, "Noisy")["state_keys"])

    c.section("a person is not an occupancy sensor")
    c.check("presence excluded on a presence user",
            "presence" not in sean["capabilities"], sean["capabilities"])
    c.check("no occupancy offers on a person",
            not any(t["capability"] == "presence" for t in sean["triggers"]),
            [t["key"] for t in sean["triggers"]])
    c.check("person classifies as a person",
            sean["device_class"] == "person", sean["device_class"])

    c.section("offers the device cannot express are dropped")
    odd = FakeDevice("0xodd", "Odd Sensor", {"occupancy": "intermittent"},
                     capabilities=FakeCapabilities(["occupancy_sensing"]))
    d = describe_device("0xodd", odd, "Odd Sensor")
    c.check("a boolean offer against an unrecognised string is not emitted",
            not any(t["capability"] == "presence" for t in d["triggers"]),
            [t["key"] for t in d["triggers"]])

    c.section("polarity")
    c.check("detection is activating", detected["polarity"] == 1, detected)
    cleared = offer(radar["triggers"], "presence:cleared")
    c.check("clearing is deactivating", cleared["polarity"] == -1, cleared)
    c.check("turn_on is activating",
            offer(light["actions"], "on_off:turn_on")["polarity"] == 1)
    c.check("turn_off is deactivating",
            offer(light["actions"], "on_off:turn_off")["polarity"] == -1)
    c.check("locking is deactivating",
            offer(lock["actions"], "lock:lock")["polarity"] == -1)
    c.check("unlocking is activating",
            offer(lock["actions"], "lock:unlock")["polarity"] == 1)
    c.check("safety sensors carry no polarity",
            all(o.get("polarity", 0) == 0 for cap in ("water_leak", "smoke")
                for o in CAPABILITIES[cap]["triggers"]))

    c.section("every offer compiles to a shape the engine validates")
    for ieee, dev in net.items():
        d = describe_device(ieee, dev, SAMPLE_NAMES.get(ieee))
        for o in d["triggers"] + d["conditions"]:
            cond = o["condition"]
            if cond.get("type") == "zone":
                ok = cond.get("event") in ("enter", "leave") and bool(cond.get("place"))
            else:
                ok = all(k in cond for k in ("attribute", "operator", "value"))
            if not c.check(f"{ieee} {o['key']} compiles", ok, cond):
                break
        for a in d["actions"]:
            step = a["step"]
            ok = step.get("type") in ("command", "request") and (
                "command" not in step or step.get("target_ieee"))
            if not c.check(f"{ieee} {a['key']} step compiles", ok, step):
                break

    c.section("vocabulary integrity")
    for cap_id, spec in CAPABILITIES.items():
        for role in ("triggers", "conditions", "actions"):
            ids = [o["id"] for o in spec.get(role, [])]
            c.check(f"{cap_id}.{role} ids are unique", len(ids) == len(set(ids)), ids)
        for o in spec.get("actions", []):
            vf = o.get("value_from")
            c.check(f"{cap_id}.{o['id']} value_from is a known param",
                    vf is None or vf in PARAMS, vf)

    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
