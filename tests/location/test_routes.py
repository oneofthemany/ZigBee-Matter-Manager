"""
The home through its routes: /api/location/home, and Settings → Weather,
whose coordinates are the home's. Needs FastAPI (run from the lockfile venv).

    python3 tests/location/test_routes.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FAILS: list = []
PASSED = [0]


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"    ok   {label}")
    else:
        FAILS.append(label)
        print(f"    FAIL {label}  <- {detail!r}"[:400])


def main() -> int:
    here = os.getcwd()
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "config"))
    cfg_path = os.path.join(work, "config", "config.yaml")
    with open(cfg_path, "w") as f:
        f.write("weather:\n  enabled: true\n  latitude: 51.38\n  longitude: -0.79\n"
                "location:\n  country: GB\n")
    os.chdir(work)
    try:
        from modules import location
        from modules.weather import WeatherService
        from routes.config_routes import register_config_routes

        location.reset_home()
        location.init(yaml.safe_load(open(cfg_path)), [])
        weather = WeatherService({"enabled": False, "latitude": 1.0, "longitude": 1.0})
        location.on_change(weather.home_moved)
        app = FastAPI()
        register_config_routes(app, lambda: None)
        client = TestClient(app)

        print("\n  one home, read and moved through its route")
        check("the weather reads the home, not its own old copy",
              (weather.latitude, weather.longitude) == (51.38, -0.79))
        check("GET answers with it", client.get("/api/location/home").json()["home"] == {"lat": 51.38, "lon": -0.79})
        r = client.post("/api/location/home", json={"lat": 51.3797701, "lon": -0.790379}).json()
        check("POST moves it", r["success"] and r["home"] == {"lat": 51.3797701, "lon": -0.790379}, r)
        on_disk = yaml.safe_load(open(cfg_path))
        check("into location:, and nowhere else",
              on_disk["location"]["latitude"] == 51.3797701 and "latitude" not in on_disk["weather"], on_disk)
        check("and the weather follows at once", weather.latitude == 51.3797701)
        bad = client.post("/api/location/home", json={"lat": 95, "lon": 0})
        check("a place not on earth is a 400", bad.status_code == 400 and not bad.json()["success"])

        print("\n  Settings → Weather shows and sets the same home")
        shown = client.get("/api/config/structured").json()["config"]["weather"]
        check("its coordinates are the home's", (shown["latitude"], shown["longitude"]) == (51.3797701, -0.790379),
              shown)
        saved = client.post("/api/config/structured",
                            json={"config": {"weather": {"enabled": True, "latitude": 51.5, "longitude": -0.1,
                                                         "poll_interval_minutes": 20}}}).json()
        on_disk = yaml.safe_load(open(cfg_path))
        check("saving them moves the home", saved["success"] and location.home() == (51.5, -0.1), saved)
        check("written to location:, the weather block keeps only its own settings",
              on_disk["location"]["latitude"] == 51.5 and "latitude" not in on_disk["weather"]
              and on_disk["weather"]["poll_interval_minutes"] == 20, on_disk)
        client.post("/api/config/structured", json={"config": {"weather": {"enabled": True}}})
        check("a save without coordinates leaves the home alone", location.home() == (51.5, -0.1))
    finally:
        os.chdir(here)
    print(f"\n{PASSED[0]} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
