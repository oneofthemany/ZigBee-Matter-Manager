"""
What the phone sends, and what the hub makes of it. Needs FastAPI (run from the lockfile venv).

    <venv>/bin/python tests/location/test_phone_reports.py

A phone's token carries only presence:write:<user>, and it needs the places list
to arm its wake-up geofences — the path table used to refuse it. Each report now
says what sent it, and the hub logs reports so a quiet path is visible: always
when one arrives after a gap, otherwise at most once per user per 10 minutes.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import routes.presence_routes as pr  # noqa: E402
from modules import location  # noqa: E402
from modules.auth_scopes import AUTHENTICATED, scope_for_path  # noqa: E402
from modules.places import PlaceManager, set_place_manager  # noqa: E402
from modules.presence_users import PresenceUserManager  # noqa: E402
from routes.place_routes import register_place_routes  # noqa: E402

FAILS: list = []
PASSED = [0]
HOME = (51.5074, -0.1278)


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"    ok   {label}")
    else:
        FAILS.append(label)
        print(f"    FAIL {label}  <- {detail!r}"[:400])


def section(t):
    print(f"\n  {t}")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))

    def info(self):
        return [m for lvl, m in self.records if lvl >= logging.INFO and "report" in m]

    def clear(self):
        self.records.clear()


def app_as(scopes):
    app = FastAPI()

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.principal = SimpleNamespace(scopes=scopes, user=SimpleNamespace(username="sean"))
        return await call_next(request)
    return app


def main() -> int:
    location.reset_home(HOME)
    work = Path(tempfile.mkdtemp())

    section("the places list, which the phone arms geofences from")
    c_get, c_post = scope_for_path("/api/places", "GET"), scope_for_path("/api/places", "POST")
    check("the path table lets any signed-in caller reach GET (the route decides)", c_get == AUTHENTICATED, c_get)
    check("changing places is still admin-only", c_post == "admin", c_post)
    pm = PlaceManager(work / "places.yaml")
    pm.upsert({"name": "Shops", "lat": 51.51, "lon": -0.13, "radius_m": 150})
    set_place_manager(pm)
    for scopes, want, label in [
        (["presence:write:sean"], 200, "a phone's token (presence:write:<user>) gets the list"),
        (["presence:read"], 200, "presence:read still does"),
        (["device:read"], 403, "a token with no presence scope doesn't"),
    ]:
        app = app_as(scopes)
        register_place_routes(app)
        r = TestClient(app).get("/api/places")
        check(label, r.status_code == want and (want != 200 or r.json()["places"][0]["name"] == "Shops"), (r.status_code, r.text[:120]))
    app = app_as(["presence:write:sean"])
    register_place_routes(app)
    r = TestClient(app).post("/api/places", json={"name": "Gym", "lat": 51.5, "lon": -0.1})
    check("…but that token can't add a place", r.status_code == 403, r.status_code)

    section("what sent a report")
    check("a report may say what sent it", pr.FixReport(lat=1, lon=1, kind="heartbeat").kind == "heartbeat")
    check("…or say nothing (older app versions)", pr.FixReport(lat=1, lon=1).kind is None)
    try:
        pr.FixReport(lat=1, lon=1, kind="anything")
        bad = False
    except ValidationError:
        bad = True
    check("an unknown kind is refused", bad)

    mgr = PresenceUserManager(config_path=str(work / "presence_users.yaml"))
    mgr.state_path = work / "presence_state.json"
    asyncio.run(mgr.upsert_user({"user_id": "sean", "display_name": "Sean", "radius_m": 120,
                                 "mode": "balanced"}))
    cap = Capture()
    log = logging.getLogger("modules.presence_users")
    log.addHandler(cap)
    log.setLevel(logging.DEBUG)

    pr.require_presence_mfa = lambda account: None      # policy is not under test
    app = app_as(["presence:write:sean"])
    pr.register_presence_routes(app, lambda: mgr)
    api = TestClient(app)
    r = api.post("/api/presence/users/sean/fix", json={"lat": HOME[0], "lon": HOME[1], "accuracy": 10, "kind": "heartbeat"})
    dev = mgr.get_user("sean")
    check("the route passes the kind through to the state", r.status_code == 200 and dev.state.get("report_kind") == "heartbeat",
          (r.status_code, r.text[:160], dev.state))
    check("…and the first report is logged at info, with what sent it",
          any("heartbeat report: home" in m for m in cap.info()), cap.records)

    section("logging is quiet in steady state, loud after a gap")
    cap.clear()
    asyncio.run(mgr.report_pwa_fix("sean", HOME[0], HOME[1], 10, kind="geofence"))
    check("a second report within 10 minutes is debug only", cap.info() == [], cap.info())
    check("…but still counts as contact", time.time() - dev.last_seen < 5)
    cap.clear()
    dev.last_seen = time.time() - 60 * 60          # an hour of silence on a 30-minute heartbeat
    dev._contact_logged_at = time.time()           # so only the gap rule can make it info
    asyncio.run(mgr.report_pwa_fix("sean", HOME[0], HOME[1], 10, kind="heartbeat"))
    check("a report after a gap is always logged, with how long it was",
          any("after 60 min without one" in m for m in cap.info()), cap.info())
    cap.clear()
    dev.last_seen = time.time() - 60 * 60
    asyncio.run(mgr.report_pwa_fix("sean", HOME[0], HOME[1], 900, kind="passive"))
    check("a report too inaccurate to move the badge still counts as contact, and says so",
          time.time() - dev.last_seen < 5 and any("passive report: contact only" in m for m in cap.info()), cap.info())
    dev.last_seen = time.time() - 60 * 60
    cap.clear()
    asyncio.run(mgr.report_pwa_fix("sean", HOME[0], HOME[1], 10))
    check("a report from an older app (no kind) is logged as unspecified",
          any("unspecified report" in m for m in cap.info()), cap.info())
    log.removeHandler(cap)

    print(f"\n{PASSED[0]} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAIL {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
