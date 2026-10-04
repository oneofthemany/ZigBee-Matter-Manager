"""
A drive in progress is visible before it closes: totals so far, current speed
and the gradient under the car, and no coordinates.

    python3 tests/journeys/test_live.py

Needs duckdb — run it from the lockfile venv (AGENTS.md).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules import journeys  # noqa: E402
from modules.journeys import JourneyManager  # noqa: E402

FAILS: list = []
PASSED = [0]


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"    ok   {label}")
    else:
        FAILS.append(label)
        print(f"    FAIL {label}  <- {detail!r}"[:400])


def fix(mgr, trip_id, i, t0, speed, hpa, user="u"):
    mgr._record_fix(user, trip_id, 51.0 + i * 0.00135, -1.0, t0 + i * 10,
                    speed, 0.0, 5.0, None, {"vert_rms": 0.3, "pressure": hpa}, [], None)


def main() -> int:
    mgr = JourneyManager(Path(tempfile.mkdtemp()) / "journeys.duckdb")
    mgr._open()
    now = time.time()
    t0 = now - 200

    check("nothing is live before anyone drives", mgr._live_trips() == [])

    fix(mgr, "blip", 0, t0, 10.0, 1000.0, user="other")
    check("a trip too short to survive closing is not shown as a drive", mgr._live_trips() == [])

    # 15 m/s up a steady 6% climb: 9 m, so 9 / 8.3 hPa, per 10 s fix.
    step = 15.0 * 10 * 0.06 / journeys.METRES_PER_HPA
    for i in range(20):
        fix(mgr, "now", i, t0, 15.0, 1000.0 - i * step)
    live = mgr._live_trips()
    check("a drive in progress is listed", [t["trip_id"] for t in live] == ["now"], live)
    t = live[0]
    check("it reports the speed of the newest fix", t["speed_mps"] == 15.0, t["speed_mps"])
    check("it reports the gradient the car is on", t["gradient_pct"] is not None and abs(t["gradient_pct"] - 6.0) < 0.2,
          t["gradient_pct"])
    check("distance so far is what has been driven", 2700 < t["distance_m"] < 3000, t["distance_m"])
    check("the newest fix time is exposed so a stalled feed can be seen",
          abs(t["last_fix_at"] - (t0 + 190)) < 1e-6, t["last_fix_at"])
    check("no coordinates leave with the live summary", not ({"lat", "lon", "track"} & set(t)), sorted(t))

    # Crawling: too slow for a gradient of its own, but the hill has not gone.
    fix(mgr, "now", 20, t0, 2.0, 1000.0 - 20 * step)
    t = mgr._live_trips()[0]
    check("slow traffic keeps the gradient of the road just driven",
          t["speed_mps"] == 2.0 and t["gradient_pct"] is not None and t["gradient_pct"] > 5, t)

    mgr._con.execute("INSERT INTO drivers (driver_id, name, user_id, active, created_at) "
                     "VALUES ('kate', 'Kate', 'u', TRUE, 0)")
    check("it names the driver the trip will default to", mgr._live_trips()[0]["driver_id"] == "kate")

    track = mgr._get_trip("now", True)
    check("the open trip's track is readable while it is driven", len(track["track"]) == 21 and track["status"] == "open",
          (len(track["track"]), track["status"]))

    print("\n  two phones")
    for i in range(21):
        # The passenger's phone: same road, fixes a couple of seconds out of step.
        mgr._record_fix("passenger", "phone2", 51.0 + i * 0.00135 + 0.00002, -1.0, t0 + i * 10 + 2,
                        15.0, 0.0, 5.0, None, {}, [], None)
    live = mgr._live_trips()
    check("two phones in one car are one live drive", len(live) == 1, [t["trip_id"] for t in live])
    check("the phone with motion data is the one followed", live[0]["trip_id"] == "now", live[0]["trip_id"])
    check("the other occupant is named", live[0]["also_recorded_by"] == ["passenger"]
          and live[0]["duplicate_trip_ids"] == ["phone2"], live[0])
    for i in range(21, 33):
        mgr._record_fix("passenger", "phone2", 51.0 + i * 0.00135, -1.0, t0 + i * 10 + 2,
                        15.0, 0.0, 5.0, None, {}, [], None)
    check("a phone whose fixes arrive late is still in the same car, and still the one followed",
          [t["trip_id"] for t in mgr._live_trips()] == ["now"],
          [t["trip_id"] for t in mgr._live_trips()])

    for i in range(20):
        mgr._record_fix("neighbour", "elsewhere", 52.0 + i * 0.00135, 0.5, t0 + i * 10,
                        15.0, 0.0, 5.0, None, {}, [], None)
    ids = sorted(t["trip_id"] for t in mgr._live_trips())
    check("a drive somewhere else stays its own card", "elsewhere" in ids and len(ids) >= 2, ids)
    for i in range(20):
        mgr._record_fix("u", "samephone", 51.0 + i * 0.00135, -1.0, t0 + i * 10 + 1,
                        15.0, 0.0, 5.0, None, {}, [], None)
    solo = [t for t in mgr._live_trips() if t["trip_id"] == "samephone"]
    check("two trips from one phone are a recording fault, not two occupants",
          len(solo) == 1 and solo[0]["also_recorded_by"] == [], [t["trip_id"] for t in mgr._live_trips()])

    mgr._con.execute("UPDATE trip_fixes SET ts = ts - 1000")
    mgr._close_idle_trips()
    check("a closed trip is no longer live", mgr._live_trips() == [])
    mgr._close()

    print(f"\n{PASSED[0]} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
