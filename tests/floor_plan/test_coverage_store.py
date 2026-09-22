"""
Saved signal heatmaps — kept, deduplicated, pruned, and compared.

    python3 tests/floor_plan/test_coverage_store.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, sample_plan  # noqa: E402

from modules import coverage_store as store  # noqa: E402
from modules import radio_model as rm  # noqa: E402


def _result(dbm: float = -70.0, weak_dbm: float = -90.0) -> dict:
    levels = [{"level_id": "ground", "field": {
        "x0": 0, "y0": 0, "h": 0.5, "nx": 2, "ny": 2,
        "data": [dbm, dbm - 10, dbm - 20, -95.0], "inside": [1, 1, 1, 0]}}]
    return {"model": {"p0": -40.0, "samples": 12}, "calibration": {"fitted": True},
            "weak": [{"ieee": "s"}] if weak_dbm < rm.WEAK_DBM else [],
            "suggestions": [], "thresholds": {"weak_dbm": rm.WEAK_DBM},
            "device_signal": [{"ieee": "s", "dbm": weak_dbm, "weak": weak_dbm < rm.WEAK_DBM}],
            "levels": levels, "summary": rm.field_summary(levels)}


def run() -> Checker:
    c = Checker("test_coverage_store")
    store.reset(tempfile.mkdtemp())
    plan = sample_plan()

    c.section("the headline figures")
    s = rm.field_summary(_result()["levels"])
    c.check("only the floor inside rooms counts", s["area_m2"] == 0.8, s)       # 3 cells of 0.25 m², to 1 dp
    c.check("usable is at or above the weak line", s["usable_pct"] == 66.7 and s["median_dbm"] == -80.0, s)
    c.check("no field, no figures", rm.field_summary([])["usable_pct"] is None)

    c.section("keeping them")
    c.check("nothing saved yet", store.latest() is None and store.history() == [])
    first = store.save(_result(), plan, now=1000.0)
    c.check("the first estimate is kept", first["new"] and store.latest()["id"] == first["id"], first)
    again = store.save(_result(), plan, now=1060.0)
    c.check("the same again only moves its check time",
            not again["new"] and again["id"] == first["id"]
            and store.latest()["checked_at"] == 1060.0 and len(store.history()) == 1, again)
    changed = store.save(_result(dbm=-60.0), plan, now=1120.0)
    c.check("a changed signal is a new snapshot", changed["new"] and len(store.history()) == 2)
    moved = store.save(_result(dbm=-60.0), {**plan, "north_offset_deg": 10}, now=1180.0)
    c.check("so is the same signal on a changed plan", moved["new"] and len(store.history()) == 3)
    h = store.history()
    c.check("history is newest first, with its headline figures",
            [x["id"] for x in h] == [moved["id"], changed["id"], first["id"]]
            and h[0]["summary"]["median_dbm"] == -70.0 and h[0]["samples"] == 12, h[0])
    c.check("a snapshot comes back whole",
            store.get(first["id"])["levels"][0]["field"]["data"][0] == -70.0)
    c.check("an id that isn't one is refused", store.get("../floor_plan") is None)

    c.section("only the newest are kept")
    for k in range(store.KEEP + 5):
        store.save(_result(dbm=-50.0 - k), plan, now=2000.0 + k)
    ids = [x["id"] for x in store.history()]
    c.check(f"at most {store.KEEP}", len(ids) == store.KEEP, len(ids))
    c.check("the oldest went first", first["id"] not in ids and ids[0] > ids[-1])
    c.check("two in one second don't overwrite each other",
            store.save(_result(dbm=-1.0), plan, now=9000.0)["id"]
            != store.save(_result(dbm=-2.0), plan, now=9000.0)["id"])
    stray = os.path.join(store.DIR, "notes.json")
    open(stray, "w").write("{}")
    c.check("files that aren't snapshots are ignored", all(x["id"] != "notes" for x in store.history()))
    return c


if __name__ == "__main__":
    ck = run()
    print(f"\n{ck.passed} passed, {len(ck.failures)} failed")
    sys.exit(1 if ck.failures else 0)
