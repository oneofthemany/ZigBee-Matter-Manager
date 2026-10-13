"""
The inference operations turn what a user did into proposals, with the
evidence in words (docs/plans/device-learning.md §5).
"""

from __future__ import annotations

from harness import Checker

from modules.learning_ops import (attribute_that_toggled, attribute_that_tracked, correlate_blob_tags, metering_map,
                                  moved_endpoints, occupancy_attribute, parse_known, power_follows_switch, press_signature,
                                  attributes_holding_known,
                                  scale_from_known, which_endpoint_changed, which_endpoints_moved)

AEU002_F7 = ("032800052101000921000b0a2100000d23060000006410006510016610006820006920006a20006b20"
             "0095394c37093d9839000000009739f56a6a43")


def rec(ep, cluster, attr, value, mfr=None):
    return {"t": 1.0, "ep": ep, "cluster": cluster, "attr": attr, "mfr": mfr, "value": value}


def power(*eps_values):
    return [rec(ep, 0x0B04, 0x050B, v) for ep, v in eps_values]


def step(ep=1, label="Socket 1", **inputs):
    return {"ep": ep, "label": label, "inputs": inputs}


def paths(props):
    return {p["path"]: p["value"] for p in props if p["path"]}


def run() -> Checker:
    c = Checker("learning_ops")
    base = {(1, 0x0B04, 0x050B, None): 0, (2, 0x0B04, 0x050B, None): 0, (3, 0x0B04, 0x050B, None): 0}

    c.section("which EPs a load moves")
    p = which_endpoints_moved(power((1, 20000)), base, step(1), {})
    c.check("only the loaded EP moved: it meters itself",
            paths(p) == {"endpoints.1.metering": "self"}, p)
    p = which_endpoints_moved(power((1, 20000), (2, 20040), (3, 19980)), base, step(2), {})
    c.check("every EP moved alike: one whole-device figure, the rest repeat it",
            paths(p) == {"endpoints.1.metering": "device_total", "endpoints.2.metering": "none",
                         "endpoints.3.metering": "none"}, p)
    p = which_endpoints_moved(power((1, 20000), (2, 20000)), base, step(3, "USB"), {})
    c.check("a load on the USB port that shows on the sockets alike: whole device, "
            "and the port repeats nothing", paths(p).get("endpoints.1.metering") == "device_total", p)
    p = which_endpoints_moved(power((2, 20000)), base, step(3, "USB"), {})
    c.check("a load on EP3 that shows only on EP2: EP3 does not meter itself",
            paths(p) == {"endpoints.3.metering": "none"}, p)
    p = which_endpoints_moved([], base, step(1), {})
    c.check("nothing moved: no proposal, and says why",
            not paths(p) and "load switched on" in p[0]["evidence"], p)
    p = which_endpoints_moved(power((1, 20000), (2, 9000)), base, step(1), {})
    c.check("EPs moved by different amounts: asks for a repeat", not paths(p)
            and p[0]["confidence"] == "low", p)

    c.section("a load switched on and off with the socket's button")
    def at(t, ep, cluster, attr, value):
        return {"t": t, "ep": ep, "cluster": cluster, "attr": attr, "mfr": None, "value": value}
    ctx = {"power_scale": {1: (1, 10), 2: (1, 10), 3: (1, 10)}}
    onoff = [at(1, 1, 0x0006, 0, 1), at(40, 1, 0x0006, 0, 0)]
    clean = onoff + [at(12, 1, 0x0B04, 0x050B, 19850), at(48, 1, 0x0B04, 0x050B, 0)]
    p = power_follows_switch(clean, base, step(1), ctx)
    ev = " | ".join(x["evidence"] for x in p)
    c.check("one test proposes no metering: that needs every socket (the metering map)",
            not paths(p), p)
    c.check("in watts, with how long it took to stop",
            "rose to 1985 W" in ev and "fell to 0 W 8 s after switch-off" in ev, ev)
    c.check("and no other EP is flagged", "also showed" not in ev, ev)

    talk = clean + [at(13, 2, 0x0B04, 0x050B, 19850), at(49, 2, 0x0B04, 0x050B, 0)]
    p = power_follows_switch(talk, base, step(1), ctx)
    ev = " | ".join(x["evidence"] for x in p)
    c.check("another EP showing the same load is flagged, with what it would do",
            "EP2 also showed the load on Socket 1" in ev and "history records it on 2 EPs" in ev
            and "automation waiting" in ev, ev)
    c.check("and it records which EPs moved, for the map",
            moved_endpoints(talk, base) == [1, 2], moved_endpoints(talk, base))

    p = power_follows_switch([at(12, 1, 0x0B04, 0x050B, 19850)], base, step(1), ctx)
    c.check("a socket not switched during the step: judged from the readings, and said so",
            any("was not switched on" in x["evidence"] for x in p))
    stuck = onoff + [at(12, 1, 0x0B04, 0x050B, 19850), at(48, 1, 0x0B04, 0x050B, 19800)]
    p = power_follows_switch(stuck, base, step(1), ctx)
    c.check("a reading that never falls after switch-off is flagged for automations",
            any("still showed power" in x["evidence"] for x in p))

    c.section("the metering map, across every switch test")
    m = paths(metering_map({1: [1, 2], 2: [1, 3], 3: [1, 2]}, [1, 2, 3], [1, 2, 3]))
    c.check("the Aqara outlet: EP1 the whole device, EP2 socket 1 + USB, EP3 socket 2",
            m == {"endpoints.1.metering": "device_total", "endpoints.2.metering": "measures:1,3",
                  "endpoints.3.metering": "measures:2"}, m)
    c.check("with every socket tested, it is confident",
            all(p["confidence"] == "high" for p in metering_map({1: [1, 2], 2: [1, 3], 3: [1, 2]},
                                                                [1, 2, 3], [1, 2, 3]) if p["path"]))
    m = paths(metering_map({1: [1], 2: [2]}, [1, 2], [1, 2]))
    c.check("a plug that meters each socket: each meters itself",
            m == {"endpoints.1.metering": "self", "endpoints.2.metering": "self"}, m)
    p = metering_map({1: [1, 2]}, [1, 2, 3], [1, 2, 3])
    c.check("one socket tested: provisional, and says what is left",
            all(x["confidence"] == "medium" for x in p if x["path"])
            and any("not tested yet: EP2, EP3" in x["evidence"] for x in p), p)
    c.check("no tests: no map", metering_map({}, [1, 2, 3], [1, 2, 3]) == [])

    c.section("scaling from a known load")
    p = scale_from_known(power((1, 20000)), base, step(1, rating_w=2000), {"max_power_w": 4000})
    c.check("raw 20000 for a 2000 W kettle is /10",
            paths(p) == {"zmm.measurements.active_power":
                         {"cluster": "0x0B04", "attr": "0x050B", "multiplier": 1, "divisor": 10}}, p)
    c.check("and the evidence says so", "raw 20000 for a 2000 W load: /10" in p[0]["evidence"])
    p = scale_from_known(power((1, 1850)), base, step(1, rating_w=2000), {})
    c.check("a draw a little under the rating is whole watts",
            paths(p)["zmm.measurements.active_power"]["divisor"] == 1, p)
    p = scale_from_known(power((1, 7000)), base, step(1, rating_w=2000), {})
    c.check("no power of ten fits: no proposal", not paths(p) and p[0]["confidence"] == "low", p)
    p = scale_from_known(power((1, 60000)), base, step(1, rating_w=6000), {"max_power_w": 4000})
    c.check("a result beyond what the outlet carries is refused",
            not paths(p) and "can carry" in p[0]["evidence"], p)

    c.section("buttons")
    p = press_signature([rec(2, 0x0012, 0x0055, 2)], {}, step(2, "Socket 2", press_type="double"), {})
    c.check("a double press that sends 2 names value 2",
            paths(p) == {"endpoints.2.actions": "multistate", "zmm.press_names.2": "double"}, p)
    p = press_signature([], {}, step(2, press_type="double"), {})
    c.check("no press arrived: says so", not paths(p) and "no button press" in p[0]["evidence"])
    p = which_endpoint_changed([rec(1, 0x0006, 0x0000, 1)], {(1, 0x0006, 0x0000, None): 0},
                               step(1), {})
    c.check("the EP that switched with the press is a load switch",
            paths(p) == {"endpoints.1.kind": "switch"}, p)
    p = which_endpoint_changed([rec(1, 0x0006, 0x0000, 1)], {(1, 0x0006, 0x0000, None): 0},
                               {**step(1), "expect_kind": "light"}, {})
    c.check("a step about a lamp proposes a light", paths(p) == {"endpoints.1.kind": "light"}, p)

    c.section("naming a setting")
    writable = {(1, 0xFCC0, 0x0203, 0x115F): "bool", (1, 0xFCC0, 0x0009, 0x115F): "uint8"}
    before = {(1, 0xFCC0, 0x0203, 0x115F): 1, (1, 0xFCC0, 0x0009, 0x115F): 0}
    s = {"ep": None, "label": "the device",
         "inputs": {"setting_id": "button_leds", "label": "Button LEDs",
                    "from_label": "On", "to_label": "Off"}}
    p = attribute_that_toggled([rec(1, 0xFCC0, 0x0203, 0, 0x115F)], before, s, {"writable": writable})
    c.check("the one writable attribute that changed becomes the setting",
            paths(p) == {"zmm.settings": {"id": "button_leds", "label": "Button LEDs", "type": "bool",
                                          "ep": 1, "cluster": "0xFCC0", "attr": "0x0203",
                                          "mfr": "0x115F", "values": {"1": "On", "0": "Off"}}}, p)
    p = attribute_that_toggled([rec(1, 0xFCC0, 0x0203, 0, 0x115F), rec(1, 0xFCC0, 0x0009, 1, 0x115F)],
                               before, s, {"writable": writable})
    c.check("two changed at once: asks to change just one", not paths(p) and p[0]["confidence"] == "low")
    p = attribute_that_toggled([rec(1, 0xFCC0, 0x0777, 1, 0x115F)], {(1, 0xFCC0, 0x0777, 0x115F): 0},
                               s, {"writable": writable})
    c.check("a read-only attribute that changed is not a setting", not paths(p))
    bare = {"ep": None, "label": "the device", "inputs": {"setting_id": "child_lock"}}
    p = attribute_that_toggled([rec(1, 0xFC11, 0x0000, 1)], {(1, 0xFC11, 0x0000, None): 0}, bare,
                               {"writable": {(1, 0xFC11, 0x0000, None): "bool"}})
    got = paths(p).get("zmm.settings") or {}
    c.check("left unnamed, a bool is Off/On and the label comes from the id",
            got.get("values") == {"0": "Off", "1": "On"} and got.get("label") == "Child lock", p)

    c.section("a setting found in the device's own history")
    def hist(attr, points):
        return [{"t": t, "ep": 1, "cluster": 0xFC11, "attr": attr, "mfr": None, "value": v}
                for t, v in points]
    setpoint = [(0, 19.0), (100, 16.0), (200, 19.0), (300, 15.0), (405, 19.0)]
    ctx = {"writable": {(1, 0xFC11, 0x6009, None): "int16", (1, 0xFC11, 0x600A, None): "int16"},
           "known": {"heating_setpoint": setpoint, "target_temp": setpoint,
                     "rssi": [(0, -60.0), (100, -1600.0)]},
           "texts": {"system_mode": [(0, "heat"), (400, "auto")]}}
    records = hist(0x6009, [(5, 1900), (105, 1600), (204, 1900), (303, 1500)]) \
        + hist(0x600A, [(5, 0), (410, 1900)]) + hist(0x6003, [(5, 165), (500, 165)])
    p = attribute_that_tracked(records, {}, {}, ctx)
    got = paths(p).get("zmm.settings") or {}
    c.check("the attribute that followed the setpoint at every change is proposed with its scale",
            (got.get("attr"), got.get("scale"), got.get("id")) == ("0x6009", 100, "heating_setpoint_stored"), p)
    c.check("the mode that held throughout is in the evidence",
            any("system_mode was heat" in x["evidence"] for x in p if x["path"]), p)
    c.check("one change is reported but not proposed",
            any(not x["path"] and "0x600A" in x["evidence"] for x in p), p)
    c.check("an attribute that never changed is not mentioned",
            not any("0x6003" in x["evidence"] for x in p), p)
    p = attribute_that_tracked(hist(0x6009, [(5, 1900), (105, 1601), (204, 1900), (303, 1500)]), {}, {}, ctx)
    c.check("one change that disagrees rules it out", not paths(p), p)

    c.section("the reading that follows someone walking in and out")
    def seen(t, attr, value, cluster=0xFCC0):
        return {"t": t, "ep": 1, "cluster": cluster, "attr": attr, "mfr": 0x115F, "value": value}
    PATH = "endpoints.1.clusters.0xFCC0.attributes.0x0142"
    walk = lambda expect: {"ep": None, "label": "the device", "inputs": {}, "expect": expect, "t0": 100.0}
    noise = [seen(101, 0x0000, 16721, 0x0400), seen(102, 0x0055, 1, 0x0012), seen(103, 0x0021, 1, 0x0001)]
    p = occupancy_attribute(noise + [seen(104, 0x0142, 1)], {(1, 0xFCC0, 0x0142, 0x115F): 0},
                            walk("occupied"), {})
    c.check("the reading that went 0 -> 1 on walking in is occupancy, and 1 is occupied",
            paths(p).get(PATH, {}).get("value_map") == {"1": True, "0": False}
            and paths(p)[PATH]["name"] == "occupancy", p)
    c.check("a lux reading, a button press and the battery in the same window are not offered",
            set(paths(p)) == {PATH, "device_type"}, p)
    c.check("and the device is proposed as a motion sensor", paths(p)["device_type"] == "motion_sensor", p)
    c.check("seen to change from a known value: high confidence, saying when",
            p[0]["confidence"] == "high" and "went 0 -> 1 4 s into the step" in p[0]["evidence"], p)
    p = occupancy_attribute([seen(104, 0x0142, 1)], {}, walk("occupied"), {})
    c.check("a sleepy sensor's first report cannot be compared: offered, but not as certain",
            PATH in paths(p) and p[0]["confidence"] == "medium" and "not seen before" in p[0]["evidence"], p)
    p = occupancy_attribute([seen(130, 0x0142, 0)], {(1, 0xFCC0, 0x0142, 0x115F): 1}, walk("clear"), {})
    c.check("walking out names the same states from the other side",
            paths(p)[PATH]["value_map"] == {"1": True, "0": False}, p)
    p = occupancy_attribute([seen(104, 0x0142, 0)], {(1, 0xFCC0, 0x0142, 0x115F): 1}, walk("occupied"), {})
    c.check("a sensor that reports 0 when someone is there is learned that way round",
            paths(p)[PATH]["value_map"] == {"0": True, "1": False}, p)
    p = occupancy_attribute([seen(104, 0x0142, 1)], {(1, 0xFCC0, 0x0142, 0x115F): 1}, walk("occupied"), {})
    c.check("a reading repeated at its old value is no change: nothing proposed, and says why",
            not paths(p) and "view empty" in p[0]["evidence"], p)
    p = occupancy_attribute([seen(104, 0x0142, 1), seen(110, 0x0142, 0)],
                            {(1, 0xFCC0, 0x0142, 0x115F): 0}, walk("occupied"), {})
    c.check("one that flipped and flipped back did not hold while the user stayed", not paths(p), p)
    p = occupancy_attribute([seen(104, 0x0150, 1), seen(105, 0x0150, 7), seen(106, 0x0150, 1)], {},
                            walk("occupied"), {})
    c.check("a reading that also took a third value is a measurement, not a state", not paths(p), p)
    p = occupancy_attribute([seen(104, 0x0152, 1)], {(1, 0xFCC0, 0x0152, 0x115F): 0}, walk("occupied"),
                            {"writable": {(1, 0xFCC0, 0x0152, 0x115F): "uint8"}})
    c.check("a writable attribute is a setting, not a reading", not paths(p), p)
    p = occupancy_attribute([seen(104, 0x0142, 1), seen(105, 0x0160, 1)], {}, walk("occupied"), {})
    c.check("two readings flipping together are both offered, for the user to pick one",
            len([x for x in p if x["path"].startswith("endpoints.")]) == 2
            and all(x["confidence"] == "low" and "accept only one" in x["evidence"]
                    for x in p if x["path"].startswith("endpoints.")), p)
    p = occupancy_attribute([seen(104 + i, 0x0140 + i, 1) for i in range(5)], {}, walk("occupied"), {})
    c.check("many flipping at once is not one walk past: asks for a repeat",
            not paths(p) and p[0]["confidence"] == "low", p)

    c.section("values the user knows, matched to the attributes holding them")
    # lumi.sensor_occupy.agl8's vendor attributes as it reported them on joining
    FP300 = {0x010C: 2, 0x014F: 30, 0x015D: 1, 0x0162: 600000, 0x0163: 3600000, 0x0164: 100,
             0x0165: 3, 0x016A: 3600000, 0x016B: 1500, 0x016C: 3, 0x0170: 1, 0x0192: 1,
             0x0193: 10000, 0x0194: 3600000, 0x0195: 1500, 0x0196: 3, 0x0197: 10, 0x0203: 0,
             0x023E: 589845, 0x019A: "0003ffffff"}
    held = {(1, 0xFCC0, a, 0x115F): v for a, v in FP300.items()}
    said = ('{"absence_delay_timer": 35, "battery": 100, "detection_range_composite": {"detection_range_0": true},'
            ' "humidity_reporting_threshold": 15, "led_disabled_night": false, "light_reporting_threshold": 15,'
            ' "pir_detection_interval": 2, "schedule_start_time": "21:00", "schedule_time_raw": 589845,'
            ' "temp_and_humidity_sampling_period": 600, "temp_reporting_interval": 3600,'
            ' "humidity_reporting_interval": 3600, "temperature": 24}')
    known, skipped = parse_known(said)
    c.check("a pasted JSON object gives its numbers, and names what is not one",
            known["schedule_time_raw"] == 589845 and "battery" in known
            and set(skipped) == {"detection_range_composite", "led_disabled_night", "schedule_start_time"},
            (known, skipped))
    c.check("typed pairs read the same", parse_known("Absence delay: 35, sampling_period = 600; x: on")
            == ({"absence_delay": 35.0, "sampling_period": 600.0}, ["x"]))
    ctx = {"vendor_values": held, "state_keys": {"battery", "temperature"}, "discovered": False}
    p = attributes_holding_known([], {}, {"inputs": {"known": said}}, ctx)
    named = {x["value"]["name"]: (x["path"], x["value"].get("scale"), x["confidence"])
             for x in p if x["path"]}
    c.check("the one attribute holding a distinctive value is proposed under its name",
            named.get("schedule_time_raw")
            == ("endpoints.1.clusters.0xFCC0.attributes.0x023E", None, "high"), p)
    c.check("and one in other units with its scale",
            named.get("temp_and_humidity_sampling_period")
            == ("endpoints.1.clusters.0xFCC0.attributes.0x0162", 1000, "high"), p)
    c.check("nothing else is proposed", set(named) == {"schedule_time_raw",
                                                       "temp_and_humidity_sampling_period"}, named)
    notes = " | ".join(x["evidence"] for x in p if not x["path"])
    c.check("values several attributes hold are listed with their candidates",
            "temp_reporting_interval = 3600 fits" in notes and "0x0163" in notes
            and "as does humidity_reporting_interval" in notes, notes)
    c.check("a small number that fits one attribute by chance is not proposed",
            "pir_detection_interval" not in named and any(
                "too small" in x["evidence"] and "pir_detection_interval" in x["evidence"] for x in p), p)
    c.check("a value the device no longer holds is said to be absent",
            any("no attribute holds" in x["evidence"] and "absence_delay_timer" in x["evidence"] for x in p), p)
    c.check("a reading ZMM already has under that name is not matched again",
            not any("battery" in x["evidence"] or "temperature =" in x["evidence"] for x in p), p)
    p = attributes_holding_known([], {}, {"inputs": {"known": "schedule_time_raw: 589845"}},
                                 {**ctx, "writable": {(1, 0xFCC0, 0x023E, 0x115F): "uint32"}})
    c.check("an attribute known to be writable is proposed as a setting",
            paths(p).get("zmm.settings", {}).get("attr") == "0x023E"
            and paths(p)["zmm.settings"]["type"] == "uint32", p)

    c.section("blob tags against known readings")
    p = correlate_blob_tags([rec(1, 0xFCC0, 0x00F7, AEU002_F7, 0x115F)], {}, step(1, rating_w=2000),
                            {"energy_kwh": 0.034})
    got = paths(p)
    c.check("the tag reading 234.4 is the mains voltage",
            got.get("zmm.struct_tags.0x97") == {"name": "voltage", "scale": 1}, got)
    c.check("the tag matching the energy counter is energy",
            got.get("zmm.struct_tags.0x95") == {"name": "energy", "scale": 1}, got)
    c.check("nothing else is claimed", set(got) == {"zmm.struct_tags.0x97", "zmm.struct_tags.0x95"}, got)
    return c


if __name__ == "__main__":
    run()
