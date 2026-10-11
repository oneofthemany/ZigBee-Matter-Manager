"""Manual config ↔ floor plan: projection and its inverse must round-trip
settings and bindings, and carry the plan's geometry into the manual side."""

from __future__ import annotations

import copy

from harness import Checker, two_room_plan

from modules.floor_plan import (
    apply_circuits_to_plan,
    clean_floor_plan,
    contact_rooms,
    geometry_change_notes,
    project_floor_plan_to_circuits,
)
from modules.solar_gain import window_facing_deg

TRV_A = "tt:00:00:00:00:00:00:0a"
TRV_B = "tt:00:00:00:00:00:00:0b"
SENSOR_H = "aa:00:00:00:00:00:00:02"


def project(plan, base=None):
    return project_floor_plan_to_circuits(clean_floor_plan(plan), base or [])


def rooms(circuits):
    return {r["id"]: r for c in circuits for r in c["rooms"]}


def round_trip(plan, manual):
    applied, notes = apply_circuits_to_plan(clean_floor_plan(plan), manual)
    out, warns = project_floor_plan_to_circuits(clean_floor_plan(applied), manual)
    return clean_floor_plan(applied), out, notes + warns


def run() -> Checker:
    c = Checker("plan_sync")
    plan = two_room_plan()

    c.section("plan → manual")
    circuits, _ = project(plan)
    r = rooms(circuits)
    lvl = clean_floor_plan(plan)["levels"][0]
    con = lvl["contacts"][0]
    c.check("an internal-door contact guards both rooms it opens into",
            sorted(contact_rooms(lvl, con)) == ["hall", "lounge"], contact_rooms(lvl, con))
    c.check("so both rooms get it in the controller",
            all(any(x["ieee"] == con["ieee"] for x in r[k]["contact_sensors"])
                for k in ("lounge", "hall")))
    win = r["lounge"]["dimensions"]["windows"][0]
    c.check("a window carries its exact bearing", win.get("facing_deg") == 180.0, win)
    c.check("which solar gain uses", window_facing_deg(win, None) == 180.0)
    c.check("floor area is the polygon's", r["lounge"]["dimensions"]["floor_area_m2"] == 20.0)

    c.section("manual → plan → manual")
    manual = copy.deepcopy(circuits)
    lounge = rooms(manual)["lounge"]
    lounge["target_temp"] = 19.0
    lounge["schedule"] = [{"days": ["mon"], "start": "07:00", "end": "09:00", "temp": 21.0}]
    lounge["trvs"] = [{"ieee": TRV_A, "window_detection": True, "min_setpoint": 7.0},
                      {"ieee": TRV_B}]
    hall = rooms(manual)["hall"]
    hall["temperature_sensors"] = [{"ieee": SENSOR_H, "height_m": 1.1, "primary": True}]
    hall["contact_sensors"] = []
    manual[0]["operating_hours"] = False
    plan2, out, notes = round_trip(plan, manual)
    r = rooms(out)
    c.check("settings written in manual come back from the plan",
            r["lounge"]["target_temp"] == 19.0 and r["lounge"]["schedule"] == lounge["schedule"],
            {k: r["lounge"].get(k) for k in ("target_temp", "schedule")})
    c.check("both TRVs survive the round trip",
            sorted(t["ieee"] for t in r["lounge"]["trvs"]) == [TRV_A, TRV_B], r["lounge"]["trvs"])
    a = next(t for t in r["lounge"]["trvs"] if t["ieee"] == TRV_A)
    c.check("with their flags and min_setpoint",
            a.get("window_detection") is True and a.get("min_setpoint") == 7.0, a)
    rads = [rd for rd in plan2["levels"][0]["radiators"] if rd["room_id"] == "lounge"]
    c.check("the first TRV takes the room's radiator, the second gets an unplaced one",
            len(rads) == 2 and rads[0]["id"] == "rad_l" and rads[0]["trv_ieee"] == TRV_A,
            rads)
    c.check("and the user is told to place it", any("had no radiator" in n for n in notes), notes)
    c.check("a sensor bound in manual lands on the plan with its height",
            any(s["ieee"] == SENSOR_H and s.get("height_m") == 1.1
                for s in plan2["levels"][0]["sensors"]))
    c.check("the door contact stays while the lounge still lists it",
            any(x["ieee"] == con["ieee"] for x in plan2["levels"][0]["contacts"]))
    c.check("circuit overrides reach the plan and come back",
            plan2["circuits"][0].get("operating_hours") is False
            and out[0].get("operating_hours") is False)
    c.check("geometry is still the plan's",
            r["lounge"]["dimensions"]["floor_area_m2"] == 20.0)

    manual2 = copy.deepcopy(out)
    for k in ("lounge", "hall"):
        rooms(manual2)[k]["contact_sensors"] = []
    plan3, _, _ = round_trip(plan2, manual2)
    c.check("a contact removed from every room it guards leaves the plan",
            plan3["levels"][0]["contacts"] == [], plan3["levels"][0]["contacts"])

    manual3 = copy.deepcopy(out)
    rooms(manual3)["lounge"]["trvs"] = [{"ieee": TRV_B}]
    plan4, out4, _ = round_trip(plan2, manual3)
    c.check("unbinding a TRV in manual unbinds it on the plan",
            [t["ieee"] for t in rooms(out4)["lounge"]["trvs"]] == [TRV_B],
            rooms(out4)["lounge"]["trvs"])

    c.section("plan authority")
    p5 = clean_floor_plan(plan2)
    for rd in p5["levels"][0]["radiators"]:
        rd.pop("trv_ieee", None)
    out5, _ = project_floor_plan_to_circuits(p5, out)
    c.check("a TRV removed in the plan editor leaves the controller",
            rooms(out5)["lounge"]["trvs"] == [], rooms(out5)["lounge"]["trvs"])
    p6 = clean_floor_plan(plan2)
    p6["levels"][0]["rooms"][0]["target_temp"] = 22.0
    out6, _ = project_floor_plan_to_circuits(p6, out)
    c.check("a target set in the plan editor wins over the stale copy",
            rooms(out6)["lounge"]["target_temp"] == 22.0)

    c.section("rooms only one side knows")
    base = copy.deepcopy(out)
    base[0]["rooms"].append({"id": "loft", "name": "Loft", "target_temp": 18.0,
                             "temperature_sensor_ieee": "aa:ff", "trvs": []})
    out7, warns = project_floor_plan_to_circuits(clean_floor_plan(plan2), base)
    c.check("a manual room not on the plan is kept with its settings",
            rooms(out7).get("loft", {}).get("target_temp") == 18.0, list(rooms(out7)))
    p8 = clean_floor_plan(plan2)
    p8["levels"][0]["rooms"] = [x for x in p8["levels"][0]["rooms"] if x["id"] != "hall"]
    out8, warns = project_floor_plan_to_circuits(p8, out)
    c.check("a room deleted from the plan leaves the controller",
            "hall" not in rooms(out8) and any("deleted" in w for w in warns), warns)
    manual9 = copy.deepcopy(out)
    manual9[0]["rooms"] = [x for x in manual9[0]["rooms"] if x["id"] != "hall"]
    plan9, _, notes9 = round_trip(plan2, manual9)
    hall_fp = next(x for x in plan9["levels"][0]["rooms"] if x["id"] == "hall")
    c.check("a room dropped in manual keeps its drawing but leaves its circuit",
            "circuit_id" not in hall_fp, hall_fp)

    c.section("geometry notes")
    old = copy.deepcopy(out)
    rooms(old)["lounge"]["dimensions"] = {"width_m": 4, "depth_m": 4, "floor_area_m2": 16.0,
                                          "ceiling_height_m": 2.4, "windows": []}
    notes = geometry_change_notes(old, out)
    c.check("a manual room the plan resizes is reported",
            any("Lounge" in n and "16.0 → 20.0" in n and "windows" in n for n in notes), notes)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
