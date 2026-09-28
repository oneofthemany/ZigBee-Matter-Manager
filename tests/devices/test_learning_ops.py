"""
The inference operations turn what a user did into proposals, with the
evidence in words (docs/plans/device-learning.md §5).
"""

from __future__ import annotations

from harness import Checker

from modules.learning_ops import (attribute_that_toggled, correlate_blob_tags, power_follows_switch,
                                  press_signature, scale_from_known, which_endpoint_changed,
                                  which_endpoints_moved)

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
    c.check("the socket's own reading rose and fell: it meters itself",
            paths(p) == {"endpoints.1.metering": "self"}, p)
    c.check("in watts, with how long it took to stop",
            "rose to 1985 W" in ev and "fell to 0 W 8 s after switch-off" in ev, ev)
    c.check("and no other EP is flagged", "also showed" not in ev, ev)

    talk = clean + [at(13, 2, 0x0B04, 0x050B, 19850), at(49, 2, 0x0B04, 0x050B, 0)]
    p = power_follows_switch(talk, base, step(1), ctx)
    ev = " | ".join(x["evidence"] for x in p)
    c.check("another EP showing the same load is flagged, with what it would do",
            "EP2 also showed the load on Socket 1" in ev and "history records it on 2 EPs" in ev
            and "automation waiting" in ev, ev)
    c.check("and the fix is proposed", paths(p) == {"endpoints.1.metering": "device_total",
                                                    "endpoints.2.metering": "none"}, p)

    p = power_follows_switch([at(12, 1, 0x0B04, 0x050B, 19850)], base, step(1), ctx)
    c.check("a socket not switched during the step: judged from the readings, and said so",
            any("was not switched on" in x["evidence"] for x in p))
    stuck = onoff + [at(12, 1, 0x0B04, 0x050B, 19850), at(48, 1, 0x0B04, 0x050B, 19800)]
    p = power_follows_switch(stuck, base, step(1), ctx)
    c.check("a reading that never falls after switch-off is flagged for automations",
            any("still showed power" in x["evidence"] for x in p))

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
