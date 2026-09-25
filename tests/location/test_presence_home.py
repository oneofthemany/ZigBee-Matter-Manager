"""
The home reaching the phone. Needs FastAPI (run from the lockfile venv).

    <venv>/bin/python tests/location/test_presence_home.py

The home is the hub's one position now, and a presence user reads it through
properties rather than carrying its own copy. Properties are not fields, so
`asdict()` drops them — which is how `GET /api/presence/users/<id>`, the call
the Android app makes to arm its geofence, stopped carrying home_lat/home_lon.
The stored shape must stay clean (that copy is what drifted); the API shape
must always have it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routes.presence_routes as pr  # noqa: E402
from modules import location  # noqa: E402
from modules.presence_users import PresenceUserManager, UserConfig  # noqa: E402

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


def main() -> int:
    location.reset_home(HOME)

    section("the two shapes of a presence user")
    cfg = UserConfig(user_id="sean", display_name="Sean", account="sean")
    check("the stored shape carries no home", "home_lat" not in cfg.to_dict(), cfg.to_dict())
    check("the API shape carries the hub's",
          (cfg.api_dict()["home_lat"], cfg.api_dict()["home_lon"]) == HOME, cfg.api_dict())
    location.reset_home(None)
    check("an unset home is null, not missing — the phone can tell them apart",
          "home_lat" in cfg.api_dict() and cfg.api_dict()["home_lat"] is None)
    location.reset_home(HOME)

    section("what the manager saves and lists")
    work = tempfile.mkdtemp()
    store = os.path.join(work, "presence_users.yaml")
    mgr = PresenceUserManager(config_path=store)
    asyncio.run(mgr.upsert_user({"user_id": "sean", "display_name": "Sean", "radius_m": 120}))
    listed = mgr.list_users()[0]
    check("the list gives the hub's home", (listed["home_lat"], listed["home_lon"]) == HOME, listed)
    saved = yaml.safe_load(open(store))["users"][0]
    check("the file keeps its own copy out of it",
          "home_lat" not in saved and "home_lon" not in saved, saved)

    section("the call the phone makes")
    app = FastAPI()

    @app.middleware("http")
    async def as_phone(request, call_next):
        request.state.principal = SimpleNamespace(scopes=["presence:read:sean"])
        return await call_next(request)

    pr.require_presence_mfa = lambda account: None      # policy is not under test
    dev = SimpleNamespace(cfg=UserConfig(user_id="sean", display_name="Sean", account="sean"),
                          ieee="user::sean", state={"presence": "home"}, last_seen=0)
    pr.register_presence_routes(app, lambda: SimpleNamespace(get_user=lambda uid: dev))
    body = TestClient(app).get("/api/presence/users/sean").json()
    check("it gets the home, so it can arm the geofence",
          (body.get("home_lat"), body.get("home_lon")) == HOME, body)
    check("with the radius it needs too", body.get("radius_m"), body.get("radius_m"))

    print(f"\n{PASSED[0]} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAIL {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
