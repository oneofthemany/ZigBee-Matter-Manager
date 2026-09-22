"""
The home's one position — moved into `location:` once, read by everything,
changed in one place.

    python3 tests/location/test_home.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from modules import location  # noqa: E402

FAILS: list = []
PASSED = [0]


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"    ok   {label}")
    else:
        FAILS.append(label)
        print(f"    FAIL {label}  <- {detail!r}"[:400])


def cfg_file(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "config.yaml"
    p.write_text(text)
    return p


OLD = """weather:
  enabled: true
  # where the forecast is for
  latitude: 51.380509
  longitude: -0.790318
  poll_interval_minutes: 30
location:
  country: GB
"""


def main() -> int:
    print("\n  the first boot moves the weather's copy into location:")
    location.reset_home()
    p = cfg_file(OLD)
    cfg = yaml.safe_load(OLD)
    got = location.init(cfg, [(51.380537, -0.79028)], path=p)
    after = yaml.safe_load(p.read_text())
    check("the weather's coordinates become the home", got == (51.380509, -0.790318), got)
    check("written to location:", (after["location"]["latitude"], after["location"]["longitude"])
          == (51.380509, -0.790318), after["location"])
    check("and gone from weather:", "latitude" not in after["weather"] and "longitude" not in after["weather"],
          after["weather"])
    check("with the rest of the weather block, and its comments, untouched",
          after["weather"]["poll_interval_minutes"] == 30 and "# where the forecast is for" in p.read_text())
    check("the live config agrees", cfg["location"]["latitude"] == 51.380509 and "latitude" not in cfg["weather"])
    check("home() answers", location.home() == (51.380509, -0.790318))
    again = location.init(yaml.safe_load(p.read_text()), [], path=p)
    check("a second boot reads it from location: and changes nothing",
          again == (51.380509, -0.790318) and yaml.safe_load(p.read_text()) == after)

    location.reset_home()
    p = cfg_file("weather:\n  latitude: 51.3\n  longitude: -0.7\nui: {}\n")
    location.init(yaml.safe_load(p.read_text()), [], path=p)
    check("a weather block that held only coordinates is left an empty map, not null",
          yaml.safe_load(p.read_text())["weather"] == {}, p.read_text())

    print("\n  failing that, a presence user's old home")
    location.reset_home()
    p = cfg_file("weather:\n  enabled: true\n")
    got = location.init(yaml.safe_load(p.read_text()), [(None, None), (51.38054, -0.79028)], path=p)
    check("the first presence home with a value", got == (51.38054, -0.79028), got)
    check("and it's kept in location:", yaml.safe_load(p.read_text())["location"]["latitude"] == 51.38054)
    location.reset_home()
    p = cfg_file("weather:\n  enabled: true\n")
    check("nowhere at all is None, and nothing is written",
          location.init({}, [], path=p) is None and "location" not in yaml.safe_load(p.read_text()))

    print("\n  moving it")
    location.reset_home((51.0, -0.7))
    p = cfg_file("location:\n  country: GB\n  latitude: 51.0\n  longitude: -0.7\n")
    heard = []
    location.on_change(heard.append)
    live = {"location": {"country": "GB"}}
    moved = location.set_home("51.3797701", -0.790379, live, path=p)
    check("it's written, rounded to 7 places", yaml.safe_load(p.read_text())["location"]
          == {"country": "GB", "latitude": 51.3797701, "longitude": -0.790379})
    check("every listener hears it once", heard == [moved], heard)
    check("the live config is patched", live["location"]["latitude"] == 51.3797701)
    location.set_home(51.3797701, -0.790379, path=p)
    check("setting the same place again tells no one", len(heard) == 1)
    for bad in ((91, 0), (0, 181), ("north", 0), (None, None)):
        try:
            location.set_home(*bad, path=p)
            check(f"{bad} is refused", False)
        except ValueError:
            check(f"{bad} is refused", True)
    check("home_coords prefers the live home over a stale config",
          location.home_coords({"weather": {"latitude": 1, "longitude": 1}}) == (51.3797701, -0.790379))

    print("\n  presence reads the home, it doesn't keep one")
    from modules.presence_users import UserConfig
    u = UserConfig.from_dict({"user_id": "sean", "home_lat": 10.0, "home_lon": 10.0})
    check("an old per-user home is ignored", (u.home_lat, u.home_lon) == (51.3797701, -0.790379),
          (u.home_lat, u.home_lon))
    check("and not written back on save", "home_lat" not in u.to_dict() and "home_lon" not in u.to_dict())
    location.reset_home()
    check("with no home, none", UserConfig.from_dict({"user_id": "x"}).home_lat is None)

    print(f"\n{PASSED[0]} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
