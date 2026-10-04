"""
The rule API over real FastAPI: each caller sees and changes only their own rules.

Needs the app's dependencies (see AGENTS.md "The dev box"); run_all skips it
without FastAPI.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness import Checker, FakeDevice, Rig


def run() -> Checker:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from modules.auth import User
    from modules.auth_middleware import Principal
    from modules.notification_rules import set_rule_engine
    from routes.notification_rule_routes import register_notification_rule_routes

    c = Checker("rule_routes")
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(Path(tmp), {"aa": FakeDevice("Hall", {"occupancy": False})})
        set_rule_engine(rig.engine)

        app = FastAPI()

        @app.middleware("http")
        async def sign_in(request: Request, call_next):
            # Stands in for auth_middleware: X-User names who is signed in.
            user = request.headers.get("X-User")
            if user:
                request.state.principal = Principal(User(username=user), set(), auth_method="cookie")
            return await call_next(request)

        register_notification_rule_routes(app)
        api = TestClient(app)
        alice, bob = {"X-User": "alice"}, {"X-User": "bob"}

        c.section("authentication")
        c.check("listing rules needs a signed-in user", api.get("/api/notification-rules").status_code == 401)

        c.section("create, list, update, delete")
        r = api.post("/api/notification-rules", headers=alice,
                     json={"trigger": "motion_detected", "cooldownMinutes": 0, "owner": "bob"})
        rule = r.json()
        c.check("creating a rule returns it with a hub id", r.status_code == 200 and rule["id"].startswith("rule-"), r.text)
        c.check("the owner is the caller, whatever the body claims", rule.get("owner") == "alice", rule)
        c.check("a bad rule is a 400 with the reason",
                api.post("/api/notification-rules", headers=alice, json={"trigger": "nope"}).json().get("detail", "")
                .startswith("unknown trigger"))
        c.check("the owner sees it", [x["id"] for x in api.get("/api/notification-rules", headers=alice).json()["rules"]] == [rule["id"]])
        c.check("another user doesn't", api.get("/api/notification-rules", headers=bob).json()["rules"] == [])
        c.check("another user's edit is a 404",
                api.put(f"/api/notification-rules/{rule['id']}", headers=bob,
                        json={"trigger": "smoke"}).status_code == 404)
        c.check("another user's delete is a 404",
                api.delete(f"/api/notification-rules/{rule['id']}", headers=bob).status_code == 404)
        upd = api.put(f"/api/notification-rules/{rule['id']}", headers=alice,
                      json={**rule, "enabled": False})
        c.check("the owner can disable it", upd.status_code == 200 and upd.json()["enabled"] is False, upd.text)

        c.section("a rule saved through the API is what the engine evaluates")
        api.put(f"/api/notification-rules/{rule['id']}", headers=alice, json={**rule, "enabled": True})
        sent = rig.change("aa", occupancy=True)
        c.check("motion notifies alice", [o for o, _ in sent] == ["alice"], sent)

        c.section("import")
        res = api.post("/api/notification-rules/import", headers=bob,
                       json={"rules": [{"trigger": "smoke"}, {"trigger": "bogus"}]}).json()
        c.check("import reports what it took and what it refused",
                res["imported"] == 1 and len(res["errors"]) == 1, res)
        c.check("imported rules belong to the importer",
                [x["trigger"] for x in api.get("/api/notification-rules", headers=bob).json()["rules"]] == ["smoke"])

        c.section("bell settings")
        r = api.put("/api/notification-rules/bell", headers=alice,
                    json={"enabled": True, "deviceOffline": True, "lowBattery": False,
                          "thermostatReached": False, "deviceOnline": False, "suppressMinutes": 15})
        c.check("PUT /bell saves settings rather than being taken as a rule id",
                r.status_code == 200 and r.json().get("suppressMinutes") == 15, r.text)
        c.check("GET /bell returns the caller's settings",
                api.get("/api/notification-rules/bell", headers=alice).json()["deviceOffline"] is True)
        c.check("another user's bell is separate",
                api.get("/api/notification-rules/bell", headers=bob).json()["configured"] is False)
        c.check("bell rules don't appear in the caller's rule list",
                all(not x.get("source") for x in api.get("/api/notification-rules", headers=alice).json()["rules"]))
        c.check("a bad suppression time is a 400",
                api.put("/api/notification-rules/bell", headers=alice, json={"suppressMinutes": 2}).status_code == 400)

        c.check("delete by the owner works",
                api.delete(f"/api/notification-rules/{rule['id']}", headers=alice).status_code == 200
                and api.get("/api/notification-rules", headers=alice).json()["rules"] == [])
    return c
