"""
House mode and alarm API over real FastAPI: who may arm, disarm and configure,
and that PIN hashes never leave the hub. Skipped without FastAPI.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from harness import Checker

import test_alarm as T


def run() -> Checker:
    c = Checker("house_routes")
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    from modules import alarm as A
    from modules.auth import User
    from modules.auth_middleware import Principal
    from modules.house_mode import HouseMode, set_house_mode
    from modules.workers import WorkerManager
    from routes.house_routes import register_house_routes

    SCOPES = {"root": {"admin"}, "alex": {"security:read", "security:write", "device:read", "device:write"},
              "guest": {"security:read", "device:read"}}

    with tempfile.TemporaryDirectory() as tmp:
        rig = T.Rig(tmp)
        A.set_alarm(rig.panel)
        wm = WorkerManager(data_file=os.path.join(tmp, "workers.json"))
        set_house_mode(HouseMode(lambda: wm, lambda: None, path=Path(tmp) / "hm.json"))
        try:
            app = FastAPI()

            @app.middleware("http")
            async def sign_in(request: Request, call_next):
                user = request.headers.get("X-User")
                if user:
                    request.state.principal = Principal(User(username=user), SCOPES[user], auth_method="cookie")
                return await call_next(request)

            register_house_routes(app)
            api = TestClient(app)
            root, alex, guest = {"X-User": "root"}, {"X-User": "alex"}, {"X-User": "guest"}

            c.section("house mode")
            c.check("only an admin creates the house mode",
                    api.post("/api/house/mode/config/create-worker", headers=alex).status_code == 403
                    and api.post("/api/house/mode/config/create-worker", headers=root).json()["configured"])
            c.check("a household member sets it", api.post("/api/house/mode", headers=alex,
                                                           json={"mode": "night"}).json()["mode"] == "night")
            c.check("a read-only member can't", api.post("/api/house/mode", headers=guest,
                                                        json={"mode": "home"}).status_code == 403)
            c.check("an unknown mode is a 400", api.post("/api/house/mode", headers=alex,
                                                        json={"mode": "party"}).status_code == 400)

            c.section("alarm")
            c.check("a read-only member can't arm",
                    api.post("/api/alarm/arm", headers=guest, json={"mode": "home"}).status_code == 403)
            rig.devs["back"].state["contact"] = False
            r = api.post("/api/alarm/arm", headers=alex, json={"mode": "home"})
            c.check("arming with a door open is a 409 listing it",
                    r.status_code == 409 and r.json()["detail"]["open"][0]["name"] == "Back door", r.text)
            r = api.post("/api/alarm/arm", headers=alex, json={"mode": "home", "force": True})
            c.check("arm anyway works", r.status_code == 200 and r.json()["status"]["state"] == "armed_home", r.text)
            r = api.post("/api/alarm/disarm", headers=alex, json={"pin": "1234"})
            c.check("disarming without a PIN set is refused", r.status_code == 400, r.text)
            c.check("a short PIN is a 400",
                    api.post("/api/alarm/pin", headers=alex, json={"pin": "1"}).status_code == 400)
            c.check("a member sets their own PIN",
                    api.post("/api/alarm/pin", headers=alex, json={"pin": "2468"}).status_code == 200)
            c.check("status says the caller has a PIN", api.get("/api/alarm", headers=alex).json()["have_pin"])
            c.check("a wrong PIN is refused",
                    api.post("/api/alarm/disarm", headers=alex, json={"pin": "0000"}).status_code == 400)
            r = api.post("/api/alarm/disarm", headers=alex, json={"pin": "2468"})
            c.check("the right PIN disarms", r.status_code == 200 and r.json()["status"]["state"] == "disarmed", r.text)

            c.section("setup")
            c.check("only an admin reads the alarm setup",
                    api.get("/api/alarm/config", headers=alex).status_code == 403)
            r = api.get("/api/alarm/config", headers=root)
            c.check("the setup lists who has a PIN but never a hash",
                    r.json()["pins_set"] == ["alex"] and "pbkdf2" not in r.text, r.text)
            c.check("a bad setting is a 400",
                    api.put("/api/alarm/config", headers=root, json={"siren_minutes": 99}).status_code == 400)
            c.check("only an admin clears someone's PIN",
                    api.delete("/api/alarm/pin/alex", headers=alex).status_code == 403
                    and api.delete("/api/alarm/pin/alex", headers=root).json()["success"])
        finally:
            A.set_alarm(None)
            set_house_mode(None)
    return c
