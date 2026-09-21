"""
Floor-plan model tests — one position per device, and which part a save changes.

    python3 tests/floor_plan/test_model.py

A device is placed once. Its position comes from the heating object that holds
it (radiator, sensor, contact) or else from ``devices[]``, never both. And a
save is split into structure (device:write) and heating (heating:write), so
the check has to put every change on the right side.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, sample_plan  # noqa: E402

from modules.floor_plan import changed_parts, clean_floor_plan, placed_devices  # noqa: E402

TRV, SENSOR, CONTACT, BULB = "0xtrv", "0xtemp", "0xdoor", "0xbulb"


def _plan(**extra):
    p = sample_plan()
    lvl = p["levels"][0]
    lvl["radiators"] = [{"id": "r1", "room_id": "lounge", "watts_at_dt50": 1200,
                         "wall_id": "wn", "offset_m": 1.0, "length_m": 1.0}]
    lvl["sensors"] = [{"id": "s1", "room_id": "lounge", "ieee": SENSOR, "x": 2.5, "y": 2.0}]
    lvl["contacts"] = [{"id": "c1", "opening_id": "win1", "ieee": CONTACT}]
    lvl["devices"] = [{"ieee": BULB, "x": 1.0, "y": 3.0}]
    lvl.update(extra)
    return clean_floor_plan(p)


def _by_ieee(plan):
    return {d["ieee"]: d for d in placed_devices(plan)}


def run() -> Checker:
    c = Checker("test_model")

    c.section("each device's one position")
    plan = _plan()
    placed = _by_ieee(plan)
    c.check("a bulb dropped in the lounge is in the lounge",
            placed[BULB]["room_id"] == "lounge" and placed[BULB]["source"] == "device",
            placed.get(BULB))
    c.check("a sensor is where heating put it",
            (placed[SENSOR]["x"], placed[SENSOR]["y"]) == (2.5, 2.0)
            and placed[SENSOR]["source"] == "sensor")
    c.check("a contact is at the middle of its window",
            (placed[CONTACT]["x"], placed[CONTACT]["y"]) == (1.7, 0.0), placed[CONTACT])
    c.check("an unfitted TRV is not placed", TRV not in placed)
    fitted = copy.deepcopy(plan)
    fitted["levels"][0]["radiators"][0]["trv_ieee"] = TRV
    at = _by_ieee(fitted)[TRV]
    c.check("a fitted TRV is at its radiator",
            at["source"] == "radiator" and (at["x"], at["y"]) == (3.5, 4.0), at)
    c.check("a device outside every room has no room",
            _by_ieee(_plan(devices=[{"ieee": BULB, "x": 9, "y": 9}]))[BULB]["room_id"] is None)

    c.section("never two positions")
    dup = _plan(devices=[{"ieee": SENSOR, "x": 4, "y": 1}, {"ieee": BULB, "x": 1, "y": 3},
                         {"ieee": BULB, "x": 2, "y": 2}])
    ieees = [d["ieee"] for d in dup["levels"][0]["devices"]]
    c.check("a device heating holds is dropped from devices[]", SENSOR not in ieees, ieees)
    c.check("a repeat is dropped, the first kept",
            ieees == [BULB] and dup["levels"][0]["devices"][0]["x"] == 1.0, dup["levels"][0]["devices"])
    moved = copy.deepcopy(plan)
    moved["levels"][0]["devices"].append({"ieee": TRV, "x": 4, "y": 3})
    moved["levels"][0]["radiators"][0]["trv_ieee"] = TRV
    moved = clean_floor_plan(moved)
    c.check("fitting a placed TRV to a radiator moves it off devices[]",
            [d["ieee"] for d in moved["levels"][0]["devices"]] == [BULB]
            and len([d for d in placed_devices(moved) if d["ieee"] == TRV]) == 1)

    c.section("what daylight needs from the plan")
    from modules.floor_plan import daylight_geometry
    geo = daylight_geometry(plan)
    c.check("a room with an outside window is included, with its bearing",
            [g["room_id"] for g in geo] == ["lounge"] and geo[0]["windows"][0]["bearing_deg"] == 180.0, geo)
    c.check("its inner surface is floor, ceiling and walls", geo[0]["surface_m2"] == 83.2, geo[0])
    turned = copy.deepcopy(plan); turned["north_offset_deg"] = 90
    c.check("turning the compass turns the window",
            daylight_geometry(turned)[0]["windows"][0]["bearing_deg"] == 90.0,
            daylight_geometry(turned)[0]["windows"])
    shut = copy.deepcopy(plan); shut["levels"][0]["openings"] = []
    c.check("a room with no window is left out", daylight_geometry(shut) == [])
    inner = copy.deepcopy(plan)
    inner["levels"][0]["walls"][0]["type"] = "internal"
    c.check("a window onto another room is not daylight", daylight_geometry(inner) == [])

    c.section("a window belongs to the room it opens into")
    from modules.floor_plan import per_wall_breakdown_for_room, project_level_to_room_dimensions
    two = clean_floor_plan({"levels": [{"id": "g", "rooms": [
        {"id": "lounge", "name": "Lounge", "polygon": [[0, 0], [5, 0], [5, 4], [0, 4]]},
        {"id": "kitchen", "name": "Kitchen", "polygon": [[5, 0], [9, 0], [9, 4], [5, 4]]}],
        "walls": [{"id": "ws", "x1": 0, "y1": 0, "x2": 9, "y2": 0, "type": "external"},
                  {"id": "wn", "x1": 9, "y1": 4, "x2": 0, "y2": 4, "type": "external"},
                  {"id": "wi", "x1": 5, "y1": 0, "x2": 5, "y2": 4}],
        "openings": [{"id": "win1", "wall_id": "ws", "kind": "window",
                      "offset_m": 1, "width_m": 1.4, "height_m": 1.2}]}]})
    lvl = two["levels"][0]
    kitchen = next(r for r in lvl["rooms"] if r["id"] == "kitchen")
    c.check("not to every room its long wall passes",
            per_wall_breakdown_for_room(lvl, kitchen)["windows"] == []
            and project_level_to_room_dimensions(lvl)["kitchen"].get("windows", []) == [])
    c.check("so only the lounge gets its daylight",
            [g["room_id"] for g in daylight_geometry(two)] == ["lounge"])

    c.section("which part a save changes")
    def after(fn):
        new = copy.deepcopy(plan)
        fn(new["levels"][0], new)
        return changed_parts(plan, clean_floor_plan(new))
    c.check("nothing changed is nothing", changed_parts(plan, copy.deepcopy(plan)) == set())
    c.check("placing a bulb is structure",
            after(lambda l, p: l["devices"].append({"ieee": "0xnew", "x": 2, "y": 2})) == {"structure"})
    c.check("moving a wall is structure",
            after(lambda l, p: l["walls"][0].update(x2=6)) == {"structure"})
    c.check("turning the compass is structure",
            after(lambda l, p: p.update(north_offset_deg=30)) == {"structure"})
    c.check("moving a sensor is structure",
            after(lambda l, p: l["sensors"][0].update(x=1.0)) == {"structure"})
    c.check("making a sensor primary is heating",
            after(lambda l, p: l["sensors"][0].update(primary=True)) == {"heating"})
    c.check("resizing a radiator is heating",
            after(lambda l, p: l["radiators"][0].update(watts_at_dt50=2000)) == {"heating"})
    c.check("a circuit is heating",
            after(lambda l, p: p.update(circuits=[{"id": "z1", "name": "Zone 1"}])) == {"heating"})
    def fit(l, p):
        l["radiators"][0]["trv_ieee"] = BULB
    c.check("fitting a placed device to a radiator is heating only", after(fit) == {"heating"})
    c.check("deleting a room takes both",
            after(lambda l, p: (l["rooms"].clear(), l["radiators"].clear(), l["sensors"].clear()))
            == {"structure", "heating"})
    c.check("a first save is structure", changed_parts(None, plan) >= {"structure"})
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
