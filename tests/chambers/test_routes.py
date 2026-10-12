"""
Chambers through the real routes: a room made or renamed in heating, on the
floor plan or in the chamber manager is the same chamber everywhere, and what
points at a chamber follows a merge.

    <venv>/bin/python tests/chambers/test_routes.py

Drives Starlette's TestClient against temp config, plan and frame files.
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness import Checker, square

import routes.chamber_routes as chr_
import routes.floor_plan_routes as fpr
import routes.frame_routes as frr
import routes.heating_controller_routes as hcr
import routes.heating_routes as hr
from modules import floor_plan_store as store

TRV = "tt:00:00:00:00:00:00:0a"
LAMP = "11:00:00:00:00:00:00:01"
PLUG = "11:00:00:00:00:00:00:02"
RAD = "11:00:00:00:00:00:00:03"


class FakeController:
    def __init__(self):
        self.applied = []

    async def apply_config(self, heating, *a, **k):
        self.applied.append(heating)
        return {"applied": True, "diff": {}}


class FakeGroups:
    def __init__(self):
        self.groups = {1: {"id": 1, "name": "Bedroom lights", "chamber": "master_bedroom"}}
        self.saved = 0

    def save_groups(self):
        self.saved += 1


class FakeZigbee:
    def __init__(self):
        self.devices = {TRV: object(), LAMP: object(), PLUG: object(), RAD: object()}
        self.device_settings = {LAMP: {"chamber": "kitchen", "polling": 30},
                                TRV: {"chamber": "kitchen"},
                                PLUG: {"chamber": "master_bedroom"}}
        self.group_manager = FakeGroups()

    def _save_json(self, path, data):
        pass


def run() -> Checker:
    c = Checker("chamber_routes")
    d = tempfile.mkdtemp()
    cfg_path = os.path.join(d, "config.yaml")
    plan_path = os.path.join(d, "data", "floor_plan.json")
    fpr.CONFIG_PATH = hcr.CONFIG_PATH = chr_.CONFIG_PATH = hr.CONFIG_PATH = cfg_path
    fpr.IMAGE_DIR = os.path.join(d, "data", "floor_plans")
    frr.FRAMES_PATH = os.path.join(d, "data", "frames.json")
    store.reset(plan_path, cfg_path)

    circuits = [{"id": "c1", "name": "Downstairs", "rooms": [
        {"id": "room_k", "name": "Kitchen", "trvs": [{"ieee": TRV}]},
        {"id": "room_m", "name": "Master Bedrom", "trvs": []}]}]
    with open(cfg_path, "w") as f:
        yaml.safe_dump({"chambers": [{"id": "kitchen", "name": "Kitchen"},
                                     {"id": "master_bedroom", "name": "Master Bedroom"}],
                        "heating": {"circuits": circuits,
                                    "controller": {"config_mode": "manual",
                                                   "circuits": circuits}}}, f)
    store.save_plan({"version": 1, "levels": [{"id": "ground", "name": "Ground", "index": 0,
                                                "rooms": [{"id": "room_k", "name": "Kitchen",
                                                           "polygon": square(0, 0)}]}]})
    os.makedirs(os.path.dirname(frr.FRAMES_PATH), exist_ok=True)
    with open(frr.FRAMES_PATH, "w") as f:
        json.dump([{"id": "bed", "name": "Bed", "split": "chamber",
                    "chambers": ["master_bedroom"]}], f)

    ctrl, zb = FakeController(), FakeZigbee()
    app = FastAPI()

    @app.middleware("http")
    async def as_admin(request, call_next):
        request.state.principal = SimpleNamespace(scopes=["admin"])
        return await call_next(request)

    fpr.register_floor_plan_routes(app, lambda: ctrl)
    hcr.register_heating_controller_routes(app, lambda: ctrl)
    chr_.register_chamber_routes(app, lambda: zb, lambda: ctrl)
    hr.register_heating_routes(app, lambda: None, lambda: zb)
    client = TestClient(app)
    reg = lambda: {x["id"]: x for x in client.get("/api/chambers").json()["chambers"]}  # noqa: E731
    cfg = lambda: yaml.safe_load(open(cfg_path))  # noqa: E731

    c.section("a hub with each room twice")
    body = client.get("/api/chambers").json()
    pairs = {frozenset((p["a"], p["b"])) for p in body["duplicates"]}
    c.check("both pairs are offered for merging",
            pairs == {frozenset(("room_k", "kitchen")), frozenset(("room_m", "master_bedroom"))},
            body["duplicates"])

    c.section("the next heating save folds the exact twin in")
    r = client.post("/api/heating/controller/config",
                    json={"config": {"circuits": cfg()["heating"]["circuits"]}}).json()
    c.check("the save succeeds", r.get("success"), r)
    c.check("one Kitchen is left, heating's",
            "kitchen" not in reg() and reg()["room_k"]["name"] == "Kitchen", list(reg()))
    c.check("its devices moved with it, other settings kept",
            zb.device_settings[LAMP] == {"chamber": "room_k", "polling": 30}, zb.device_settings)
    c.check("the TRV is in the room heating binds it to",
            zb.device_settings[TRV]["chamber"] == "room_k")

    c.section("merging the misspelt pair")
    r = client.post("/api/chambers/merge",
                    json={"source": "room_m", "into": "master_bedroom"}).json()
    c.check("keeps heating's id under the chosen name",
            r.get("chamber") == "room_m" and reg()["room_m"]["name"] == "Master Bedroom", r)
    c.check("heating shows the name",
            cfg()["heating"]["circuits"][0]["rooms"][1]["name"] == "Master Bedroom")
    c.check("the running controller was told", ctrl.applied and
            ctrl.applied[-1]["circuits"][0]["rooms"][1]["name"] == "Master Bedroom")
    c.check("a device follows", zb.device_settings[PLUG]["chamber"] == "room_m")
    c.check("a group follows", zb.group_manager.groups[1]["chamber"] == "room_m")
    c.check("a saved frame follows",
            json.load(open(frr.FRAMES_PATH))[0]["chambers"] == ["room_m"])
    c.check("no duplicates remain", client.get("/api/chambers").json()["duplicates"] == [])

    c.section("one name, whichever side renames")
    client.post("/api/chambers", json={"id": "room_k", "name": "Kitchen Diner"})
    c.check("the chamber manager renames heating's room",
            cfg()["heating"]["controller"]["circuits"][0]["rooms"][0]["name"] == "Kitchen Diner")
    c.check("and the plan's", store.load_plan()["levels"][0]["rooms"][0]["name"] == "Kitchen Diner")

    plan = store.load_plan()
    plan["levels"][0]["rooms"][0]["name"] = "Kitchen"
    plan["levels"][0]["rooms"].append({"id": "study", "name": "Study", "polygon": square(5, 0)})
    r = client.post("/api/floor-plan", json=plan).json()
    c.check("the plan saves", r.get("success"), r)
    c.check("a rename on the plan renames the chamber", reg()["room_k"]["name"] == "Kitchen")
    c.check("and heating's room", cfg()["heating"]["circuits"][0]["rooms"][0]["name"] == "Kitchen")
    c.check("a room drawn on the plan is a chamber",
            reg().get("study", {}).get("mapped") is True, reg().get("study"))

    circ = cfg()["heating"]["circuits"]
    circ[0]["rooms"][1]["name"] = "Main Bedroom"
    client.post("/api/heating/controller/config", json={"config": {"circuits": circ}})
    c.check("a rename in heating renames the chamber", reg()["room_m"]["name"] == "Main Bedroom")

    c.section("devices")
    r = client.post("/api/chambers/assign", json={"ieee": TRV, "chamber": "room_m"}).json()
    c.check("a TRV cannot be moved away from its heating room",
            not r["success"] and "heating" in r["error"], r)
    r = client.post("/api/chambers/assign/bulk",
                    json={"ieees": [TRV, LAMP], "chamber": "study"}).json()
    c.check("a bulk move skips it and moves the rest",
            r["skipped"] == [TRV] and r["assigned"] == [LAMP], r)

    c.section("heating advisor zones are chambers")
    r = client.post("/api/heating/zones", json={"zones": [
        {"id": "zone_1", "name": "kitchen", "target_temp": 20, "devices": [RAD, LAMP]},
        {"id": "zone_2", "name": "Conservatory"}]}).json()
    zones = {z["id"]: z for z in r.get("zones") or []}
    c.check("a zone from before chambers lands on the chamber of its name",
            zones.get("room_k", {}).get("target_temp") == 20, r)
    c.check("one with no such chamber makes it",
            "conservatory" in zones and "conservatory" in reg(), r)
    c.check("zones are named by their chamber", zones["room_k"]["name"] == "Kitchen", zones)
    c.check("its devices in no chamber join it", zb.device_settings[RAD]["chamber"] == "room_k")
    c.check("a device already in a chamber stays", zb.device_settings[LAMP]["chamber"] == "study")
    c.check("no device list is stored on the zone",
            all("devices" not in z and "name" not in z for z in cfg()["heating"]["zones"]))
    r = client.post("/api/heating/zones/nowhere", json={"target_temp": 20}).json()
    c.check("a zone needs a chamber", not r["success"], r)

    c.section("creating and deleting")
    r = client.post("/api/chambers", json={"name": "main bedroom"}).json()
    c.check("a second chamber of a name is refused", not r["success"], r)
    r = client.delete("/api/chambers/room_k").json()
    c.check("a heated chamber cannot be deleted", not r["success"] and "heating" in r["error"], r)
    client.post("/api/chambers", json={"name": "Loft"})
    r = client.delete("/api/chambers/loft").json()
    c.check("an unused one can", r.get("success") and "loft" not in reg(), r)
    return c


if __name__ == "__main__":
    raise SystemExit(1 if run().failures else 0)
