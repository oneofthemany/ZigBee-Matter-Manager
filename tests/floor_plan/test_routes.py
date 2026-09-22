"""
Floor-plan route tests — the same plan whichever address saves it.

    <venv>/bin/python tests/floor_plan/test_routes.py

Drives the real routes through Starlette's TestClient against temp files. The
claim is the phase-2a one: a save through /api/floor-plan (Topology's address)
is what /api/heating/floor-plan, the heating controller, heating's mode switch
and the chamber registry all see next, and config.yaml stops carrying a copy.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, sample_plan  # noqa: E402

import routes.floor_plan_routes as fpr  # noqa: E402
import routes.heating_controller_routes as hcr  # noqa: E402
from modules import chambers, floor_plan_store as store  # noqa: E402
from modules.floor_plan import clean_floor_plan  # noqa: E402
from modules.heating_controller import HeatingController  # noqa: E402


class FakeController:
    def __init__(self):
        self.applied = []

    async def apply_config(self, heating, *a, **k):
        self.applied.append(heating)
        return {"success": True}


def run() -> Checker:
    c = Checker("test_routes")
    d = tempfile.mkdtemp()
    cfg_path = os.path.join(d, "config.yaml")
    plan_path = os.path.join(d, "data", "floor_plan.json")
    fpr.CONFIG_PATH = hcr.CONFIG_PATH = cfg_path
    fpr.IMAGE_DIR = os.path.join(d, "data", "floor_plans")

    legacy = clean_floor_plan(sample_plan("Old Lounge"))
    circuits = [{"id": "zone1", "name": "Zone 1",
                 "rooms": [{"id": "lounge", "name": "Lounge", "trvs": []}]}]
    with open(cfg_path, "w") as f:
        yaml.safe_dump({"heating": {"floor_plan": legacy,
                                    "controller": {"config_mode": "floor_plan",
                                                   "circuits": circuits}}}, f)
    store.reset(plan_path, cfg_path)

    ctrl = FakeController()
    app = FastAPI()

    @app.middleware("http")
    async def as_admin(request, call_next):
        request.state.principal = SimpleNamespace(scopes=["admin"])
        return await call_next(request)

    fpr.register_floor_plan_routes(app, lambda: ctrl)
    hcr.register_heating_controller_routes(app, lambda: ctrl)
    client = TestClient(app)

    c.section("a hub upgraded with its plan in config.yaml")
    new = client.get("/api/floor-plan").json()
    old = client.get("/api/heating/floor-plan").json()
    c.check("the new address serves it", new["plan"] == legacy, new)
    c.check("the old address serves the same plan", old == new)

    c.section("saving through the new address")
    fresh = sample_plan("New Lounge")
    r = client.post("/api/floor-plan", json=fresh).json()
    c.check("the save succeeds", r.get("success"), r)
    name = lambda p: p["levels"][0]["rooms"][0]["name"]      # noqa: E731
    c.check("the old address sees it",
            name(client.get("/api/heating/floor-plan").json()["plan"]) == "New Lounge")
    c.check("the heating controller sees it",
            name(HeatingController._config_floor_plan(None)) == "New Lounge")
    c.check("the chamber registry sees it",
            any(x["name"] == "New Lounge" for x in chambers.build_registry(
                yaml.safe_load(open(cfg_path)))))
    cfg = yaml.safe_load(open(cfg_path))
    c.check("config.yaml no longer carries a copy", "floor_plan" not in cfg["heating"], cfg)
    c.check("heating's circuits were projected from it",
            cfg["heating"]["controller"]["circuits"][0]["rooms"][0].get("floor_plan_ref"),
            cfg["heating"]["controller"]["circuits"])
    c.check("the running controller was told", len(ctrl.applied) == 1)
    c.check("without a private copy of the plan riding along",
            "_floor_plan_for_thermal" not in ctrl.applied[0], list(ctrl.applied[0]))

    c.section("heating's mode switch re-projects from the same plan")
    r = client.post("/api/heating/controller/config-mode", json={"mode": "manual"}).json()
    c.check("to manual", r.get("success"), r)
    r = client.post("/api/heating/controller/config-mode", json={"mode": "floor_plan"}).json()
    c.check("and back re-projects", r.get("success") and r.get("reprojected"), r)
    c.check("the plan survived the round trip",
            name(client.get("/api/floor-plan").json()["plan"]) == "New Lounge")

    c.section("a save can't add overlapping rooms")
    clash = sample_plan("New Lounge")
    clash["levels"][0]["rooms"].append({"id": "den", "name": "Den",
                                        "polygon": [[3, 0], [8, 0], [8, 4], [3, 4]]})
    res = client.post("/api/floor-plan", json=clash)
    body = res.json()
    c.check("it is refused, naming the rooms and the overlap",
            res.status_code == 422 and not body["success"]
            and body["error"].startswith("New Lounge and Den overlap by 8.0 m²"), body)
    c.check("and the saved plan is untouched",
            len(client.get("/api/floor-plan").json()["plan"]["levels"][0]["rooms"]) == 1)
    clash["levels"][0]["rooms"][1]["polygon"] = [[5, 0], [8, 0], [8, 4], [5, 4]]
    c.check("next door instead, it saves",
            client.post("/api/floor-plan", json=clash).json().get("success"))
    # An overlap from before the rule: written straight to the store.
    legacy_clash = clean_floor_plan(clash)
    legacy_clash["levels"][0]["rooms"][1]["polygon"] = [[3, 0], [8, 0], [8, 4], [3, 4]]
    store.save_plan(legacy_clash)
    legacy_clash["levels"][0]["rooms"][1]["name"] = "Study"
    c.check("an old plan that already overlaps can still be edited",
            client.post("/api/floor-plan", json=legacy_clash).json().get("success"))

    c.section("preview, images and delete answer on both addresses")
    c.check("preview", client.get("/api/floor-plan/preview").json().get("success")
            and client.get("/api/heating/floor-plan/preview").json().get("success"))
    up = client.post("/api/floor-plan/image/ground",
                     files={"file": ("g.png", b"\x89PNG\r\n\x1a\nxx", "image/png")}).json()
    c.check("an upload names the new address", up.get("url") == "/api/floor-plan/image/ground", up)
    c.check("and is served on the old one too",
            client.get("/api/heating/floor-plan/image/ground").status_code == 200)
    schema_paths = client.get("/openapi.json").json()["paths"]
    c.check("only the new address is documented",
            "/api/floor-plan" in schema_paths and "/api/heating/floor-plan" not in schema_paths)
    c.check("delete succeeds", client.delete("/api/heating/floor-plan").json().get("success"))
    c.check("and the plan is gone everywhere",
            client.get("/api/floor-plan").json()["plan"] is None
            and HeatingController._config_floor_plan(None) is None
            and not os.path.exists(plan_path))

    store.reset(os.path.join(tempfile.mkdtemp(), "x.json"))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
