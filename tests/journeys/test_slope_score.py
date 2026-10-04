"""
The smoothness score weighs each harsh event by how much of it was the hill's.

    python3 tests/journeys/test_slope_score.py

Needs duckdb — run it from the lockfile venv (AGENTS.md).
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules import journeys  # noqa: E402
from modules.journeys import JourneyManager, slope_weight  # noqa: E402

FAILS: list = []
PASSED = [0]


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"    ok   {label}")
    else:
        FAILS.append(label)
        print(f"    FAIL {label}  <- {detail!r}"[:400])


T0 = 1_700_000_000.0      # long past, so the closer takes every trip at once
SPEED = 15.0              # m/s
STEP_M = SPEED * journeys.DRIVE_FIX_INTERVAL_S


def drive(mgr, trip_id, gradient_pct, events, barometer=True, fixes=40):
    """A steady drive up (or down) a constant gradient, with events at fix 20."""
    hpa_per_fix = STEP_M * gradient_pct / 100.0 / journeys.METRES_PER_HPA
    for i in range(fixes):
        motion = {"vert_rms": 0.3}
        if barometer:
            motion["pressure"] = 1000.0 - i * hpa_per_fix
        evs = [{"t": T0 + i * 10 - 4, "kind": k, "peak": p, "dur": 1.0}
               for k, p in events] if i == 20 else []
        mgr._record_fix("u", trip_id, 51.0 + i * STEP_M / 111_320.0, -1.0, T0 + i * 10,
                        SPEED, 0.0, 5.0, None, motion, evs, None)


def main() -> int:
    print("\n  slope_weight")
    check("a threshold brake on the level counts in full", slope_weight("brake", 3.5, 0.0) == 1.0)
    check("braking on a climb is partly the hill's", 0 < slope_weight("brake", 3.6, 6.0) < 1,
          slope_weight("brake", 3.6, 6.0))
    check("a threshold brake on a 10% climb is all but forgiven", slope_weight("brake", 3.5, 10.0) < 0.05,
          slope_weight("brake", 3.5, 10.0))
    check("braking hard enough to be harsh without the hill still counts in full",
          slope_weight("brake", 5.0, 6.0) == 1.0)
    check("braking on a descent is not forgiven", slope_weight("brake", 3.6, -6.0) == 1.0)
    check("accelerating on a descent is partly the hill's", 0 < slope_weight("accel", 3.6, -6.0) < 1)
    check("accelerating up a climb is not forgiven", slope_weight("accel", 3.6, 6.0) == 1.0)
    check("a steeper climb forgives more", slope_weight("brake", 3.6, 8.0) < slope_weight("brake", 3.6, 4.0))
    check("cornering is never slope-weighted", slope_weight("corner", 3.6, 10.0) == 1.0)
    check("an unattributed event is never slope-weighted", slope_weight("harsh", 3.6, 10.0) == 1.0)
    check("an unknown gradient forgives nothing", slope_weight("brake", 3.6, None) == 1.0)
    check("a gradient inside the level band forgives nothing", slope_weight("brake", 3.6, 1.9) == 1.0)
    check("a stored negative braking peak weighs as its magnitude",
          slope_weight("brake", -3.6, 6.0) == slope_weight("brake", 3.6, 6.0))

    print("\n  trips")
    db = Path(tempfile.mkdtemp()) / "journeys.duckdb"
    mgr = JourneyManager(db)
    mgr._open()
    drive(mgr, "flat", 0.0, [("brake", 3.6)])
    drive(mgr, "climb", 6.0, [("brake", 3.6)])
    drive(mgr, "descent", -6.0, [("brake", 3.6), ("accel", 3.6)])
    drive(mgr, "nobaro", 6.0, [("brake", 3.6)], barometer=False)
    drive(mgr, "corner", 6.0, [("corner", 3.6)])
    mgr._close_idle_trips()
    t = {k: mgr._get_trip(k, False) for k in ("flat", "climb", "descent", "nobaro", "corner")}

    flat, climb = t["flat"], t["climb"]
    per_100km = 1 / (flat["distance_m"] / 100_000.0)
    expected = round(100 * math.exp(-per_100km / journeys.SCORE_DECAY_EVENTS_PER_100KM), 1)
    check("a level trip scores exactly as its raw event count", flat["smoothness_score"] == expected,
          (flat["smoothness_score"], expected))
    check("the same brake on a climb scores higher than on the level",
          climb["smoothness_score"] > flat["smoothness_score"],
          (climb["smoothness_score"], flat["smoothness_score"]))
    check("the raw count still reports the event that happened",
          climb["harsh_event_count"] == 1 and climb["harsh_brake_count"] == 1, climb["harsh_event_count"])
    check("the weighted count is the event's slope weight",
          climb["weighted_event_count"] == climb["events"][0]["slope_weight"]
          and 0 < climb["weighted_event_count"] < 1, climb["weighted_event_count"])
    g = climb["events"][0]["gradient_pct"]
    check("the event carries the gradient it was weighed against", g is not None and abs(g - 6.0) < 0.2, g)

    d = t["descent"]
    by_kind = {e["kind"]: e["slope_weight"] for e in d["events"]}
    check("on a descent the brake counts in full and the acceleration does not",
          by_kind["brake"] == 1.0 and 0 < by_kind["accel"] < 1, by_kind)
    check("a trip's weighted count is the sum over its events",
          abs(d["weighted_event_count"] - sum(by_kind.values())) < 1e-6, d["weighted_event_count"])
    check("without a barometer the climb forgives nothing",
          t["nobaro"]["weighted_event_count"] == 1.0
          and t["nobaro"]["smoothness_score"] == flat["smoothness_score"], t["nobaro"]["weighted_event_count"])
    check("cornering on a climb counts in full", t["corner"]["weighted_event_count"] == 1.0)

    print("\n  rescoring on open")
    mgr._con.execute("UPDATE trips SET weighted_event_count = NULL, smoothness_score = ? "
                     "WHERE trip_id = 'climb'", [flat["smoothness_score"]])
    mgr._con.execute("UPDATE trip_events SET slope_weight = NULL, gradient_pct = NULL "
                     "WHERE trip_id = 'climb'")
    mgr._close()
    mgr._open()
    again = mgr._get_trip("climb", False)
    check("a trip scored before weighting is rescored when its events survive",
          again["smoothness_score"] == climb["smoothness_score"]
          and again["weighted_event_count"] == climb["weighted_event_count"],
          (again["smoothness_score"], again["weighted_event_count"]))

    mgr._con.execute("UPDATE trips SET weighted_event_count = NULL, smoothness_score = 12.3 "
                     "WHERE trip_id = 'flat'")
    mgr._con.execute("DELETE FROM trip_events WHERE trip_id = 'flat'")
    mgr._close()
    mgr._open()
    check("a trip whose events were purged keeps the score it had",
          mgr._get_trip("flat", False)["smoothness_score"] == 12.3)
    mgr._close()

    print(f"\n{PASSED[0]} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
