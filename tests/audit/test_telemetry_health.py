"""telemetry_health: each defect class shows up on a seeded snapshot."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from harness import Checker


def run() -> Checker:
    c = Checker("telemetry_health")
    try:
        import duckdb
        import telemetry_health
    except ImportError as e:
        print(f"    skipped: {e} (run under the lockfile venv)")
        return c
    tmp = Path(tempfile.mkdtemp(prefix="zmm_audit_db_"))
    try:
        path = str(tmp / "t.duckdb")
        con = duckdb.connect(path)
        con.execute("CREATE TABLE device_states (ts TIMESTAMP, ieee VARCHAR, attribute VARCHAR, "
                    "value VARCHAR, numeric_val DOUBLE)")
        now = "TIMESTAMP '2026-10-18 12:00'"
        con.execute(f"""INSERT INTO device_states VALUES
            ({now} - INTERVAL 1 HOUR, 'a', 'temperature', '21.5', 21.5),
            ({now} - INTERVAL 1 HOUR, 'a', 'temperature', '21.5', 21.5),
            ({now} - INTERVAL 2 HOUR, 'b', 'humidity', '140', 140),
            ({now} - INTERVAL 100 DAY, 'c', 'battery', '55', NULL)""")
        con.execute("CREATE TABLE clean (ts TIMESTAMP, ieee VARCHAR)")
        con.execute(f"INSERT INTO clean VALUES ({now} - INTERVAL 5 MINUTE, 'a')")
        con.close()
        ro = duckdb.connect(path, read_only=True)
        ref = "TIMESTAMP '2026-10-18 12:00'"
        t = telemetry_health.check_table(ro, "device_states", 24, ref)
        issues = " | ".join(t["issues"])
        c.check("identical rows within a day are counted as dupes", "dupes: 1" in issues, issues)
        c.check("a humidity reading over 100 is flagged", "humidity: 1 reading" in issues, issues)
        c.check("a numeric value stored with NULL numeric_val is flagged",
                "NULL numeric_val" in issues, issues)
        c.check("rows past the 90-day prune window are flagged", "retention: 1" in issues, issues)
        c.check("an ieee with nothing in 24 h is listed as silent",
                [s["ieee"] for s in t.get("silent", [])] == ["c"], t.get("silent"))
        c.check("no clock issue when the newest row is an hour old and nothing is ahead",
                "clock:" not in issues or "behind" in issues, issues)
        ahead = telemetry_health.check_table(ro, "clean", 24, f"{ref} - INTERVAL 1 HOUR")
        c.check("a newest row a whole hour ahead of now reads as a timezone offset",
                any("ahead" in i for i in ahead["issues"]), ahead["issues"])
        clean = telemetry_health.check_table(ro, "clean", 24, ref)
        c.check("a healthy table reports no issues", clean["issues"] == [], clean["issues"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return c
