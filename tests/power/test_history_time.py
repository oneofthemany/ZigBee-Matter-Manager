"""
History chart times: device_states.ts is DuckDB session wall time (UTC in the
container), but the chart's carry-forward row was stamped with Python's local
clock and every ts went out zone-less, so a browser read UTC as local. On BST
the last reading drew an hour early and was carried flat for an hour to "now".
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from harness import Checker

import duckdb

import modules.telemetry_db as telemetry_db
from modules.telemetry_api import _with_utc_ts

IEEE = "00:15:8d:00:02:56:f8:bf"


def run() -> Checker:
    c = Checker("history_time")
    tmp = tempfile.mkdtemp(prefix="zmm_hist_")
    saved = telemetry_db._db
    db = duckdb.connect(str(Path(tmp) / "t.duckdb"))
    db.execute("SET GLOBAL TimeZone = 'UTC'")     # as in the container
    db.execute("""CREATE TABLE device_states (ts TIMESTAMP NOT NULL DEFAULT now(),
                  ieee VARCHAR NOT NULL, attribute VARCHAR NOT NULL,
                  value VARCHAR, numeric_val DOUBLE)""")
    db.execute("INSERT INTO device_states (ts, ieee, attribute, value, numeric_val) "
               "VALUES (now()::TIMESTAMP - INTERVAL 30 MINUTE, ?, 'power_1', '36.0', 36.0)", [IEEE])
    telemetry_db._db = db
    try:
        c.section("the carry-forward row is on the stored rows' clock")
        rows = telemetry_db.query_device_state_bucketed(IEEE, "power_1", hours=24, bucket_minutes=5)
        real = [r for r in rows if r["samples"]]
        extended = rows[-1]
        gap = extended["ts"] - real[-1]["ts"]
        c.check("a reading 30 minutes old is carried 30 minutes, not 90",
                timedelta(minutes=25) <= gap <= timedelta(minutes=35), gap)
        start = rows[0]["ts"]
        c.check("the window starts 24 hours before the database's now",
                abs((extended["ts"] - start) - timedelta(hours=24)) < timedelta(minutes=1)
                or rows[0]["samples"], (start, extended["ts"]))

        c.section("timestamps leave as UTC")
        sent = _with_utc_ts(telemetry_db.query_device_state_bucketed, ieee=IEEE,
                            attribute="power_1", hours=24, bucket_minutes=5)
        c.check("every ts is ISO-8601 with Z", all(r["ts"].endswith("Z") and "T" in r["ts"]
                                                  for r in sent), [r["ts"] for r in sent])
        now_utc = datetime.now(timezone.utc)
        last = datetime.fromisoformat(sent[-1]["ts"].replace("Z", "+00:00"))
        c.check("and 'now' is the real now, whatever the host's zone",
                abs(last - now_utc) < timedelta(minutes=1), (last, now_utc))

        c.section("a host whose DuckDB runs on local time")
        london = ZoneInfo("Europe/London")
        c.check("summer wall time converts to UTC",
                telemetry_db.to_utc_iso(datetime(2026, 9, 27, 13, 35, 10, 980360), london)
                == "2026-09-27T12:35:10.980Z")
        c.check("winter wall time is already UTC",
                telemetry_db.to_utc_iso(datetime(2026, 12, 27, 13, 35), london)
                == "2026-12-27T13:35:00.000Z")
        c.check("the session zone is read from DuckDB", telemetry_db.session_zone() == ZoneInfo("UTC"),
                telemetry_db.session_zone())
    finally:
        telemetry_db._db = saved
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
