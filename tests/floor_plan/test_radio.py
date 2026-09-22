"""
Signal-coverage tests — learning a house's attenuation and where it is weak.

    python3 tests/floor_plan/test_radio.py

The claims: the fit recovers wall and floor losses that synthetic links were
generated with; with no links it is the textbook prior; LQI maps to RSSI by
the hub's own direct neighbours when there are enough; the field falls off
through walls; and a repeater is suggested where it actually lifts a weak
device, and nowhere when nothing is weak.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

from modules import radio_model as rm  # noqa: E402
from modules.floor_plan import clean_floor_plan  # noqa: E402

TRUE = {"p0": -45.0, "n": 2.8, "ext": 12.0, "int": 5.0, "floor": 18.0}


def _house():
    """Two floors of a 12×8 m house: four rooms each, brick outside, stud inside."""
    def level(lid, index, z):
        rooms = [{"id": f"{lid}_{k}", "name": f"{lid} {k}", "polygon": p} for k, p in enumerate([
            [[0, 0], [6, 0], [6, 4], [0, 4]], [[6, 0], [12, 0], [12, 4], [6, 4]],
            [[0, 4], [6, 4], [6, 8], [0, 8]], [[6, 4], [12, 4], [12, 8], [6, 8]]])]
        walls = [{"id": f"{lid}_s", "x1": 0, "y1": 0, "x2": 12, "y2": 0, "type": "external"},
                 {"id": f"{lid}_e", "x1": 12, "y1": 0, "x2": 12, "y2": 8, "type": "external"},
                 {"id": f"{lid}_n", "x1": 12, "y1": 8, "x2": 0, "y2": 8, "type": "external"},
                 {"id": f"{lid}_w", "x1": 0, "y1": 8, "x2": 0, "y2": 0, "type": "external"},
                 {"id": f"{lid}_mx", "x1": 6, "y1": 0, "x2": 6, "y2": 8, "type": "internal"},
                 {"id": f"{lid}_my", "x1": 0, "y1": 4, "x2": 12, "y2": 4, "type": "internal"},
                 # a brick spine wall in the east half, so heavy walls are inside too
                 {"id": f"{lid}_b", "x1": 9, "y1": 0.5, "x2": 9, "y2": 7.5, "type": "party"}]
        return {"id": lid, "name": lid, "index": index, "floor_above_ground_m": z,
                "rooms": rooms, "walls": walls, "devices": []}
    return clean_floor_plan({"levels": [level("ground", 0, 0), level("first", 1, 2.7)]})


def run() -> Checker:
    c = Checker("test_radio")
    plan = _house()
    geo = rm.build_geometry(plan)

    c.section("LQI to RSSI")
    exact = [(l, -100 + 0.25 * l) for l in (40, 90, 150, 220)]
    cal = rm.fit_calibration(exact)
    c.check("the hub's direct neighbours set the line", cal["fitted"]
            and abs(cal["slope"] - 0.25) < 1e-6 and abs(cal["intercept"] + 100) < 1e-6, cal)
    c.check("too few pairs fall back to the default",
            rm.fit_calibration(exact[:3]) == {**rm.DEFAULT_CALIBRATION, "pairs": 3, "fitted": False})
    c.check("pairs all at one LQI can't pin a slope",
            not rm.fit_calibration([(200, -60)] * 6)["fitted"])
    c.check("the mapping goes both ways", rm.rssi_to_lqi(rm.lqi_to_rssi(150, cal), cal) == 150)

    c.section("walls between two points")
    g = lambda lvl, x, y: {"level_id": lvl, "x": x, "y": y}  # noqa: E731
    c.check("same room: none", rm.features(geo, g("ground", 1, 1), g("ground", 5, 3))[1:] == (0, 0, 0))
    c.check("through the stud wall: one light",
            rm.features(geo, g("ground", 5, 1), g("ground", 7, 1))[1:] == (0, 1, 0))
    c.check("through the brick spine too: one heavy, one light",
            rm.features(geo, g("ground", 5, 1), g("ground", 11, 1))[1:] == (1, 1, 0))
    c.check("a device standing on a wall does not count it",
            rm.features(geo, g("ground", 6, 1), g("ground", 5, 1))[1:] == (0, 0, 0))
    up = rm.features(geo, g("ground", 3, 2), g("first", 3, 2))
    c.check("straight up a floor: one floor, 2.7 m", up[3] == 1 and abs(up[0] - 2.7) < 1e-9, up)

    c.section("learning the house's attenuation")
    c.check("no links is the prior", {k: v for k, v in rm.fit([]).items() if k in rm.PARAMS}
            == rm.PRIOR)
    rnd = random.Random(7)
    pts = [g(lvl, rnd.uniform(0.3, 11.7), rnd.uniform(0.3, 7.7))
           for lvl in ("ground", "first") for _ in range(25)]
    samples = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            f = rm.features(geo, pts[i], pts[j])
            samples.append((f, rm.predict(TRUE, f) + rnd.gauss(0, 2.0)))
    model = rm.fit(samples)
    c.check("recovers the external-wall loss", abs(model["ext"] - TRUE["ext"]) < 1.5, model)
    c.check("recovers the internal-wall loss", abs(model["int"] - TRUE["int"]) < 1.5, model)
    c.check("recovers the floor loss", abs(model["floor"] - TRUE["floor"]) < 2.0, model)
    c.check("recovers the distance exponent", abs(model["n"] - TRUE["n"]) < 0.3, model)
    c.check("and says how well it fits", model["samples"] == len(samples)
            and 1.0 < model["rmse_db"] < 3.0, model)
    few = rm.fit(samples[:3])
    c.check("three links move it only a little from the prior",
            abs(few["ext"] - rm.PRIOR["ext"]) < 4.0, few)
    wild = rm.fit([(f, -300.0) for f, _ in samples[:40]])
    c.check("nonsense readings stay inside physical bounds",
            all(rm.BOUNDS[k][0] <= wild[k] <= rm.BOUNDS[k][1] for k in rm.PARAMS), wild)

    c.section("the coverage field")
    router = {"ieee": "r", **g("ground", 1, 1)}
    f = rm.coverage_field(geo, TRUE, [router], "ground", step=0.5)
    at = lambda x, y: f["data"][int((y - f["y0"]) / f["h"]) * f["nx"] + int((x - f["x0"]) / f["h"])]  # noqa: E731
    c.check("in the thermal fields' layout", f["nx"] == 24 and f["ny"] == 16
            and len(f["data"]) == len(f["inside"]) == 24 * 16, (f["nx"], f["ny"]))
    c.check("strong by the router, weak across the house", at(1.2, 1.2) > at(11.2, 7.2) + 20)
    c.check("a wall costs what the model says it does",
            abs((at(5.7, 1.2) - at(6.2, 1.2)) - TRUE["int"]) < 1.5, (at(5.7, 1.2), at(6.2, 1.2)))
    c.check("no routers, no field", rm.coverage_field(geo, TRUE, [], "ground") is None)
    # The grid is worked out a source at a time with numpy; the links and the
    # repeater search still use the one-pair path. They must agree to the cell.
    upstairs = {"ieee": "u", **g("first", 9.3, 5.1)}
    two = rm.coverage_field(geo, TRUE, [router, upstairs], "ground", step=0.5)
    worst = 0.0
    for k, v in enumerate(two["data"]):
        cx = two["x0"] + (k % two["nx"] + 0.5) * two["h"]
        cy = two["y0"] + (k // two["nx"] + 0.5) * two["h"]
        at = {"level_id": "ground", "x": cx, "y": cy}
        scalar = max(rm.predict(TRUE, rm.features(geo, s, at)) for s in (router, upstairs))
        worst = max(worst, abs(v - scalar))
    c.check("every cell matches the one-pair model, across floors too", worst < 0.051, worst)
    c.check("and the room mask matches too", all(
        two["inside"][k] == int(any(rm._in_poly(two["x0"] + (k % two["nx"] + 0.5) * two["h"],
                                                two["y0"] + (k // two["nx"] + 0.5) * two["h"],
                                                r["polygon"]) for r in geo["ground"]["rooms"]))
        for k in range(len(two["inside"]))))

    c.section("where a repeater would help")
    # A softer house than TRUE: one that a single repeater can actually rescue.
    mild = {"p0": -40.0, "n": 2.2, "ext": 10.0, "int": 4.0, "floor": 15.0}
    coord = {"ieee": "c", **g("ground", 1, 1)}
    behind = {"ieee": "s", "name": "Shed Sensor", "dbm": -92.0, **g("ground", 11.5, 7.5)}
    sug = rm.suggest_repeaters(geo, mild, [coord], [behind])
    c.check("one suggestion lifts the far sensor", len(sug) == 1
            and sug[0]["fixes"][0]["ieee"] == "s"
            and sug[0]["fixes"][0]["after_dbm"] >= rm.TARGET_DBM, sug)
    c.check("somewhere that still hears the mesh", sug and sug[0]["uplink_dbm"] >= rm.UPLINK_DBM, sug)
    c.check("and it's named by its room", sug and sug[0]["room_name"], sug)
    c.check("nothing weak, nothing suggested", rm.suggest_repeaters(geo, mild, [coord], []) == [])
    beside = {"ieee": "r2", **g("ground", 11.0, 7.0)}
    c.check("never right beside a device that already relays",
            all(math.hypot(x["x"] - beside["x"], x["y"] - beside["y"]) >= rm.MIN_SPACING_M
                for x in rm.suggest_repeaters(geo, mild, [coord, beside], [behind])),
            rm.suggest_repeaters(geo, mild, [coord, beside], [behind]))
    c.check("a handful of readings is flagged as rough",
            rm.fit(samples[:4])["rough"] and not model["rough"], (model["rough"], model["rmse_db"]))
    c.check("every claimed fix clears the target and is worth doing",
            all(f["after_dbm"] >= rm.TARGET_DBM and f["after_dbm"] - f["before_dbm"] >= rm.MIN_GAIN_DB
                for x in sug for f in x["fixes"]), sug)
    # Through a 18 dB floor and a brick spine, no one spot both hears the mesh
    # and reaches the loft. Saying nothing beats promising a fix that won't work.
    loft = {"ieee": "l", "name": "Loft Sensor", "dbm": -95.0, **g("first", 11, 7)}
    c.check("a device nothing can reach gets no promise",
            rm.suggest_repeaters(geo, TRUE, [coord], [loft]) == [])
    c.check("but in a softer house it does",
            len(rm.suggest_repeaters(geo, mild, [coord], [loft])) == 1)

    c.section("the whole picture from a plan and a mesh")
    placed = clean_floor_plan({**plan, "levels": [
        {**plan["levels"][0], "devices": [{"ieee": "00:c0", "x": 1, "y": 1},
                                          {"ieee": "00:r1", "x": 5, "y": 2}]},
        {**plan["levels"][1], "devices": [{"ieee": "00:s1", "x": 11, "y": 7}]}]})
    mesh = {"nodes": [{"id": "00:c0", "friendly_name": "Coordinator", "role": "Coordinator", "online": True},
                      {"id": "00:r1", "friendly_name": "Lamp", "role": "Router", "online": True,
                       "lqi": 200, "rssi": -55},
                      {"id": "00:s1", "friendly_name": "Loft Sensor", "role": "EndDevice", "online": True,
                       "lqi": 40, "rssi": -90}],
            "links": [{"source": "00:c0", "target": "00:r1", "lqi": 200},
                      {"source": "00:r1", "target": "00:c0", "lqi": 190},
                      {"source": "00:r1", "target": "00:s1", "lqi": 40}]}
    out = rm.analyse(placed, mesh)
    c.check("the loft sensor's measured link marks it weak",
            [w["ieee"] for w in out["weak"]] == ["00:s1"] and out["weak"][0]["measured"], out["weak"])
    c.check("the model learned from the measured links", out["model"]["samples"] == 4, out["model"])
    c.check("a field for each floor", {l["level_id"] for l in out["levels"]} == {"ground", "first"})
    c.check("routers and the coordinator are the sources",
            sorted(out["sources"]) == ["00:c0", "00:r1"], out["sources"])
    c.check("nothing placed, nothing to say",
            rm.analyse(plan, mesh)["weak"] == [] and rm.analyse(plan, mesh)["levels"] == [])
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
