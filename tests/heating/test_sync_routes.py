"""
Mode switching and saves through the real routes — nothing a user set on one
side is lost by switching to the other.

    <venv>/bin/python tests/heating/test_sync_routes.py

Drives Starlette's TestClient against temp config and plan files.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness import Checker, two_room_plan

import routes.floor_plan_routes as fpr
import routes.heating_controller_routes as hcr
from modules import floor_plan_store as store
from modules.floor_plan import clean_floor_plan

TRV = "tt:00:00:00:00:00:00:0a"


class FakeController:
    def __init__(self):
        self.applied = []
        self.dry_run = False

    async def apply_config(self, heating, *a, **k):
        self.applied.append(heating)
        return {"applied": True, "diff": {}}

    def update_trv_settings(self, ieee, updates):
        return True

    async def apply_trv_config(self, ieee):
        return {"success": True}

    async def set_dry_run(self, dry_run, reason=""):
        self.dry_run = dry_run


def rooms(circuits):
    return {r["id"]: r for c in circuits for r in c["rooms"]}


def run() -> Checker:
    c = Checker("sync_routes")
    d = tempfile.mkdtemp()
    cfg_path = os.path.join(d, "config.yaml")
    plan_path = os.path.join(d, "data", "floor_plan.json")
    fpr.CONFIG_PATH = hcr.CONFIG_PATH = cfg_path
    fpr.IMAGE_DIR = os.path.join(d, "data", "floor_plans")

    plan = clean_floor_plan(two_room_plan())
    for lvl in plan["levels"]:
        lvl["radiators"] = [dict(rd) for rd in lvl["radiators"]]
    # The live hub's shape: manual edits (a TRV, a lower target) that the
    # floor-plan copy never saw.
    manual = [{"id": "down", "name": "Downstairs", "receiver_ieee": "rr:00:00:00:00:00:00:01",
               "operating_hours": False,
               "rooms": [{"id": "lounge", "name": "Lounge", "target_temp": 19.0,
                          "temperature_sensor_ieee": "aa:00:00:00:00:00:00:01",
                          "trvs": [{"ieee": TRV, "child_lock": True}]},
                         {"id": "hall", "name": "Hall", "target_temp": 20.0}]}]
    stale = [{"id": "down", "name": "Downstairs",
              "rooms": [{"id": "lounge", "name": "Lounge", "target_temp": 20.0,
                         "floor_plan_ref": {"level_id": "ground", "room_id": "lounge"}}]}]
    with open(cfg_path, "w") as f:
        yaml.safe_dump({"heating": {"circuits": manual,
                                    "controller": {"config_mode": "manual",
                                                   "circuits": stale}}}, f)
    store.reset(plan_path, cfg_path)
    store.save_plan(plan)

    ctrl = FakeController()
    app = FastAPI()

    @app.middleware("http")
    async def as_admin(request, call_next):
        request.state.principal = SimpleNamespace(scopes=["admin"])
        return await call_next(request)

    fpr.register_floor_plan_routes(app, lambda: ctrl)
    hcr.register_heating_controller_routes(app, lambda: ctrl)
    client = TestClient(app)
    cfg = lambda: yaml.safe_load(open(cfg_path))["heating"]          # noqa: E731
    fp_room = lambda rid: next(r for r in store.load_plan()["levels"][0]["rooms"]  # noqa: E731
                               if r["id"] == rid)

    c.section("manual → floor plan keeps the manual edits")
    r = client.post("/api/heating/controller/config-mode", json={"mode": "floor_plan"}).json()
    h = cfg()
    lounge = rooms(h["controller"]["circuits"])["lounge"]
    c.check("the switch succeeds", r.get("success"), r)
    c.check("the manual target survives", lounge["target_temp"] == 19.0, lounge)
    c.check("the manual TRV survives, with its flag",
            [(t["ieee"], t.get("child_lock")) for t in lounge["trvs"]] == [(TRV, True)],
            lounge["trvs"])
    c.check("and is now bound on the plan",
            any(rd.get("trv_ieee") == TRV for rd in store.load_plan()["levels"][0]["radiators"]))
    c.check("the circuit override survives", h["controller"]["circuits"][0].get("operating_hours") is False)
    c.check("a door contact only the plan knew survives the first sync",
            len(store.load_plan()["levels"][0]["contacts"]) == 1
            and rooms(h["circuits"])["hall"]["contact_sensors"], store.load_plan()["levels"][0]["contacts"])
    c.check("both modes now read the same rooms", h["circuits"] == h["controller"]["circuits"])
    c.check("the manual side gained the plan's geometry",
            rooms(h["circuits"])["lounge"]["dimensions"]["floor_area_m2"] == 20.0)

    c.section("floor plan → manual keeps everything")
    r = client.post("/api/heating/controller/config-mode", json={"mode": "manual"}).json()
    h2 = cfg()
    c.check("nothing changes but the mode",
            h2["circuits"] == h["circuits"] and h2["controller"]["config_mode"] == "manual", r)

    c.section("a manual save reaches the plan")
    edited = h2["circuits"]
    rooms(edited)["hall"]["target_temp"] = 17.5
    rooms(edited)["hall"]["temperature_sensors"] = [{"ieee": "aa:00:00:00:00:00:00:09",
                                                     "height_m": 1.2, "primary": True}]
    r = client.post("/api/heating/controller/config", json={"config": {"circuits": edited}}).json()
    c.check("the save succeeds", r.get("success"), r)
    c.check("the plan room takes the target", fp_room("hall").get("target_temp") == 17.5)
    c.check("the plan gains the sensor",
            any(s["ieee"] == "aa:00:00:00:00:00:00:09"
                for s in store.load_plan()["levels"][0]["sensors"]))
    c.check("and both lists match", cfg()["circuits"] == cfg()["controller"]["circuits"])

    c.section("a plan save reaches the manual side")
    p = store.load_plan()
    p["levels"][0]["rooms"][0]["polygon"] = [[0, 0], [5, 0], [5, 5], [0, 5]]
    r = client.post("/api/floor-plan", json=p).json()
    c.check("the plan save succeeds", r.get("success"), r)
    c.check("manual mode is kept", cfg()["controller"]["config_mode"] == "manual")
    c.check("the manual room's area follows the plan",
            rooms(cfg()["circuits"])["lounge"]["dimensions"]["floor_area_m2"] == 25.0,
            rooms(cfg()["circuits"])["lounge"]["dimensions"])
    c.check("and the user is told what changed",
            any("Lounge" in w and "20.0 → 25.0" in w for w in r.get("warnings", [])),
            r.get("warnings"))
    c.check("manual settings rode through the plan save",
            rooms(cfg()["circuits"])["hall"]["target_temp"] == 17.5)

    c.section("per-device and fast-path edits")
    r = client.post("/api/heating/controller/trv/settings",
                    json={"ieee": TRV, "min_setpoint": 8.0}).json()
    c.check("min_setpoint is accepted", r.get("success"), r)
    c.check("and lands on the plan's radiator",
            any(rd.get("trv_ieee") == TRV and rd.get("min_setpoint") == 8.0
                for rd in store.load_plan()["levels"][0]["radiators"]))
    h3 = cfg()
    c.check("and in both lists",
            all(rooms(cs)["lounge"]["trvs"][0].get("min_setpoint") == 8.0
                for cs in (h3["circuits"], h3["controller"]["circuits"])))
    client.post("/api/heating/controller/config-mode", json={"mode": "floor_plan"})
    r = client.post("/api/heating/controller/room/target",
                    json={"circuit_id": "down", "room_id": "lounge", "target_temp": 21.5}).json()
    c.check("a target set in floor-plan mode reaches the plan",
            r.get("success") and fp_room("lounge").get("target_temp") == 21.5, r)

    c.section("dry run")
    client.post("/api/heating/controller/dry-run", json={"dry_run": True})
    c.check("is persisted", cfg()["controller"].get("dry_run") is True and ctrl.dry_run)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
