"""
Floor-plan scope tests — one save, two owners.

    <venv>/bin/python tests/floor_plan/test_scopes.py

Walls, rooms and device positions need device:write; radiators, sensor roles,
contacts and circuits need heating:write. A save is checked for what it
changes, and refused whole if the caller lacks a scope for any part of it.
Admins and the shipped users group hold both.
"""

from __future__ import annotations

import copy
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
from modules import floor_plan_store as store  # noqa: E402
from modules.auth import DEFAULT_GROUPS  # noqa: E402
from modules.auth_scopes import AUTHENTICATED, scope_for_path  # noqa: E402
from modules.floor_plan import clean_floor_plan  # noqa: E402

DEVICE_ONLY = "device:read,device:write"
HEATING_ONLY = "heating:read,heating:write"


def _client():
    app = FastAPI()

    @app.middleware("http")
    async def as_caller(request, call_next):
        scopes = request.headers.get("x-scopes")
        request.state.principal = (None if scopes is None
                                   else SimpleNamespace(scopes=scopes.split(",")))
        return await call_next(request)

    fpr.register_floor_plan_routes(app, lambda: None, get_mesh=lambda: {
        "nodes": [{"id": "0xa", "friendly_name": "A", "role": "Coordinator", "online": True},
                  {"id": "0xb", "friendly_name": "B", "role": "Router", "online": True}],
        "links": [{"source": "0xa", "target": "0xb", "lqi": 180}]})
    return TestClient(app)


def run() -> Checker:
    c = Checker("test_scopes")
    d = tempfile.mkdtemp()
    cfg_path = os.path.join(d, "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.safe_dump({"location": {"latitude": 51.5, "longitude": -0.12}, "heating": {}}, f)
    fpr.CONFIG_PATH = cfg_path
    fpr.IMAGE_DIR = os.path.join(d, "data", "floor_plans")
    store.reset(os.path.join(d, "data", "floor_plan.json"), cfg_path)

    base = sample_plan()
    base["levels"][0]["radiators"] = [{"id": "r1", "room_id": "lounge", "watts_at_dt50": 1000,
                                       "wall_id": "wn", "offset_m": 1.0}]
    base["levels"][0]["sensors"] = [{"id": "s1", "room_id": "lounge", "ieee": "0xtemp",
                                     "x": 2.5, "y": 2.0}]
    store.save_plan(clean_floor_plan(base))
    client = _client()

    def post(plan, scopes):
        return client.post("/api/floor-plan", json=plan, headers={"x-scopes": scopes})

    def edited(fn):
        p = copy.deepcopy(store.load_plan())
        fn(p["levels"][0])
        return p

    bulb = edited(lambda l: l["devices"].append({"ieee": "0xbulb", "x": 1, "y": 3}))
    bigger = edited(lambda l: l["radiators"][0].update(watts_at_dt50=2000))

    c.section("the middleware leaves the decision to the routes")
    c.check("both addresses are mapped to any signed-in caller",
            scope_for_path("/api/floor-plan", "POST") == AUTHENTICATED
            and scope_for_path("/api/heating/floor-plan", "POST") == AUTHENTICATED)
    c.check("nobody signed in is refused", client.post("/api/floor-plan", json=bulb).status_code == 401)

    c.section("device:write alone")
    r = post(bigger, DEVICE_ONLY)
    c.check("cannot resize a radiator", r.status_code == 403
            and r.json()["missing_scopes"] == ["heating:write"], r.text[:200])
    c.check("and says what it can't change", "radiators" in r.json()["error"], r.json())
    c.check("the refused save changed nothing",
            store.load_plan()["levels"][0]["radiators"][0]["watts_at_dt50"] == 1000)
    r = post(bulb, DEVICE_ONLY)
    c.check("can place a bulb", r.status_code == 200 and r.json()["success"], r.text[:200])
    moved = edited(lambda l: l["sensors"][0].update(x=1.0))
    c.check("can move a sensor", post(moved, DEVICE_ONLY).status_code == 200)
    both = edited(lambda l: (l["devices"].clear(), l["radiators"][0].update(watts_at_dt50=3000)))
    c.check("a save touching both is refused whole",
            post(both, DEVICE_ONLY).status_code == 403
            and store.load_plan()["levels"][0]["devices"])

    c.section("heating:write alone")
    r = post(edited(lambda l: l["walls"][0].update(x2=7)), HEATING_ONLY)
    c.check("cannot move a wall", r.status_code == 403
            and r.json()["missing_scopes"] == ["device:write"], r.text[:200])
    stale = post(bigger, HEATING_ONLY)
    c.check("a stale copy that would undo someone's bulb is refused",
            stale.status_code == 403 and stale.json()["missing_scopes"] == ["device:write"])
    bigger = edited(lambda l: l["radiators"][0].update(watts_at_dt50=2000))
    c.check("can resize a radiator", post(bigger, HEATING_ONLY).status_code == 200)
    fit = edited(lambda l: l["radiators"][0].update(trv_ieee="0xbulb"))
    r = post(fit, HEATING_ONLY)
    c.check("can fit a device someone placed to a radiator", r.status_code == 200, r.text[:200])
    c.check("which then has one position, at the radiator",
            store.load_plan()["levels"][0]["devices"] == [])

    c.section("admins and the shipped groups")
    wall = edited(lambda l: l["walls"][0].update(x2=6.5))
    c.check("an admin can change both at once",
            post(edited(lambda l: (l["walls"][0].update(x2=6.2),
                                   l["radiators"][0].update(watts_at_dt50=900))),
                 "admin").status_code == 200)
    c.check("the users group holds both scopes",
            post(wall, ",".join(DEFAULT_GROUPS["users"])).status_code == 200)
    c.check("the viewers group can read the plan",
            client.get("/api/floor-plan",
                       headers={"x-scopes": ",".join(DEFAULT_GROUPS["viewers"])}).status_code == 200)
    c.check("but not change it",
            post(bulb, ",".join(DEFAULT_GROUPS["viewers"])).status_code == 403)
    c.check("someone with neither read scope cannot read it",
            client.get("/api/floor-plan", headers={"x-scopes": "media:read"}).status_code == 403)

    c.section("the rest of the surface")
    got = client.get("/api/floor-plan", headers={"x-scopes": "device:read"}).json()
    c.check("the plan comes with the home's coordinates for the map",
            got["home"] == {"lat": 51.5, "lon": -0.12}, got.get("home"))
    day = client.get("/api/floor-plan/daylight", headers={"x-scopes": "heating:read"}).json()
    lounge = next((r for r in day.get("rooms", []) if r["room_id"] == "lounge"), None)
    c.check("the daylight estimate covers today in half-hour steps",
            day.get("success") and len(day["times"]) == 49 and lounge and len(lounge["lux"]) == 49, day)
    c.check("dark at midnight, lit at midday",
            lounge and lounge["lux"][0] == 0 and max(lounge["lux"]) > 50, lounge and lounge["lux"])
    mesh = client.get("/api/floor-plan/mesh", headers={"x-scopes": "system:read"}).json()
    c.check("the mesh comes merged, one link per pair",
            mesh.get("success") and len(mesh["links"]) == 1 and mesh["links"][0]["band"] == "ok", mesh)
    c.check("and needs system:read, as the network routes do",
            client.get("/api/floor-plan/mesh", headers={"x-scopes": "device:read"}).status_code == 403)
    cov = client.get("/api/floor-plan/coverage", headers={"x-scopes": "system:read"}).json()
    c.check("coverage comes with the learned model and its priors",
            cov.get("success") and set(cov["model"]) >= set(("p0", "n", "ext", "int", "floor"))
            and "weak" in cov and "suggestions" in cov, cov)
    c.check("and needs system:read too",
            client.get("/api/floor-plan/coverage", headers={"x-scopes": "device:read"}).status_code == 403)
    c.check("and needs a read scope",
            client.get("/api/floor-plan/daylight", headers={"x-scopes": "media:read"}).status_code == 403)
    png = {"file": ("g.png", b"\x89PNG\r\n\x1a\nxx", "image/png")}
    c.check("a background image is structure",
            client.post("/api/floor-plan/image/ground", files=png,
                        headers={"x-scopes": HEATING_ONLY}).status_code == 403
            and client.post("/api/floor-plan/image/ground", files=png,
                            headers={"x-scopes": DEVICE_ONLY}).status_code == 200)
    c.check("deleting the plan takes both",
            client.delete("/api/floor-plan", headers={"x-scopes": DEVICE_ONLY}).status_code == 403
            and client.delete("/api/floor-plan", headers={"x-scopes": HEATING_ONLY}).status_code == 403
            and store.load_plan() is not None)
    c.check("which an admin has",
            client.delete("/api/floor-plan", headers={"x-scopes": "admin"}).status_code == 200
            and store.load_plan() is None)

    store.reset(os.path.join(tempfile.mkdtemp(), "x.json"))
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
