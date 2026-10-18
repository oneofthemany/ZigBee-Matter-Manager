#!/usr/bin/env python3
"""
Data-health report for a telemetry DuckDB snapshot. Opens read-only.

Run against a copy, never the live file: the app holds the write lock, and
a read-only open from another process fails rather than waits. Copy the file
and its .wal together; if the copy won't open (a checkpoint landed mid-copy),
copy again or stop the app first. Checks, per table with a ``ts``:

  clock     rows stamped in the future, and a newest row a whole number of
            hours off now (a UTC/local mix-up, see telemetry_db.py)
  retention rows older than the prune window
  dupes     identical rows in the last day
  values    numeric_val NULL where value parses as a number; readings outside
            the physical range their attribute name implies
  silence   ieee whose newest row is older than --silent-hours
  tables    tables whose newest rows sit a whole number of hours apart: one
            writer stamps local time, another UTC

    python scripts/audit/telemetry_health.py SNAPSHOT.duckdb [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import re

import duckdb

# Physical bounds by the attribute's final name part (voltage_1 is a voltage;
# temperature_reporting_interval and color_temperature are not temperatures).
BOUNDS = [
    ("humidity", 0, 100), ("battery_percentage", 0, 100), ("battery", 0, 100),
    ("valve", 0, 100), ("temperature", -40, 85), ("pressure", 300, 1200),
    ("linkquality", 0, 255), ("lqi", 0, 255), ("illuminance", 0, 150000),
    ("voltage", 0, 500), ("power", -25000, 25000),
]
NOT_PHYSICAL = ("color_", "_interval", "_period", "_duration")
RETENTION = {"octopus_": 400}


def bound_for(attribute):
    name = re.sub(r"_\d+$", "", attribute.lower())
    if any(part in name for part in NOT_PHYSICAL):
        return None
    return next(((lo, hi) for k, lo, hi in BOUNDS if name == k or name.endswith("_" + k)), None)
DEFAULT_RETENTION = 90


def tables(con):
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name").fetchall()]


def columns(con, table):
    return {r[0]: r[1] for r in con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ?", [table]).fetchall()}


def q(table):
    return '"' + table.replace('"', '""') + '"'


def check_table(con, table, silent_hours, now_sql):
    cols = columns(con, table)
    t = q(table)
    now_sql = f"({now_sql})"  # spliced into arithmetic; keep it one operand
    n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    out = {"table": table, "rows": n, "issues": []}
    if not n or "ts" not in cols:
        return out
    tmin, tmax, ahead, skew_h = con.execute(f"""
        SELECT min(ts), max(ts),
               count(*) FILTER (WHERE ts > {now_sql} + INTERVAL 10 MINUTE),
               round(epoch(max(ts) - {now_sql}) / 3600.0, 2)
        FROM {t}""").fetchone()
    out.update(first=str(tmin), last=str(tmax))
    if ahead:
        out["issues"].append(f"clock: {ahead} row(s) stamped >10 min in the future")
    if skew_h is not None and abs(skew_h) >= 0.9 and abs(skew_h - round(skew_h)) < 0.1 \
            and abs(skew_h) <= 14:
        why = "ahead: a timezone offset" if skew_h > 0 else \
            "behind: a timezone offset, or writes stopped that long ago"
        out["issues"].append(f"clock: newest row is {skew_h:+.0f} h {why}")
    keep = next((d for p, d in RETENTION.items() if table.startswith(p)), DEFAULT_RETENTION)
    old = con.execute(f"SELECT count(*) FROM {t} WHERE ts < {now_sql} - INTERVAL {keep} DAY"
                      ).fetchone()[0]
    if old:
        out["issues"].append(f"retention: {old} row(s) older than {keep} days — prune not running?")
    names = ", ".join(q(c) for c in cols)
    dup = con.execute(f"""
        SELECT coalesce(sum(c - 1), 0) FROM (
            SELECT count(*) c FROM {t} WHERE ts > {now_sql} - INTERVAL 1 DAY
            GROUP BY {names} HAVING count(*) > 1)""").fetchone()[0]
    if dup:
        out["issues"].append(f"dupes: {dup} identical row(s) in the last day")
    if {"value", "numeric_val"} <= cols.keys():
        lost = con.execute(f"""
            SELECT count(*) FROM {t}
            WHERE numeric_val IS NULL AND TRY_CAST(value AS DOUBLE) IS NOT NULL""").fetchone()[0]
        if lost:
            out["issues"].append(f"values: {lost} row(s) with a numeric value but NULL numeric_val")
    if {"attribute", "numeric_val"} <= cols.keys():
        attrs = [r[0] for r in con.execute(f"SELECT DISTINCT attribute FROM {t}").fetchall()]
        for attr in attrs:
            bound = bound_for(attr)
            if not bound:
                continue
            bad, mn, mx = con.execute(f"""
                SELECT count(*), min(numeric_val), max(numeric_val) FROM {t}
                WHERE attribute = ? AND (numeric_val < ? OR numeric_val > ?)""",
                [attr, *bound]).fetchone()
            if bad:
                out["issues"].append(f"values: {attr}: {bad} reading(s) outside "
                                     f"{bound[0]}..{bound[1]} (seen {mn}..{mx})")
    if "ieee" in cols:
        silent = con.execute(f"""
            SELECT ieee, max(ts) FROM {t} GROUP BY ieee
            HAVING max(ts) < {now_sql} - INTERVAL {int(silent_hours)} HOUR
            ORDER BY max(ts)""").fetchall()
        if silent:
            out["silent"] = [{"ieee": i, "last": str(ts)} for i, ts in silent]
    return out


def clock_split(con, tables):
    """Groups of live tables whose newest rows differ by whole hours."""
    newest = {}
    for t in tables:
        if t.get("last") and t["rows"]:
            newest[t["table"]] = con.execute(
                f"SELECT epoch(max(ts)) FROM {q(t['table'])}").fetchone()[0]
    if len(newest) < 2:
        return None
    latest = max(newest.values())
    offsets = {name: round((latest - ts) / 3600, 2) for name, ts in newest.items()}
    hours = {round(o) for o in offsets.values() if abs(o - round(o)) < 0.1}
    return offsets if len(hours) > 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("db")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--silent-hours", type=float, default=24)
    ap.add_argument("--now", help="reference time (snapshot taken earlier), e.g. '2026-10-18 09:00'")
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    # Columns are naive TIMESTAMP; compare in the session zone the app writes with.
    now_sql = f"TIMESTAMP '{args.now}'" if args.now else "now()::TIMESTAMP"
    report = {"file": args.db, "bytes": os.path.getsize(args.db),
              "wal_bytes": os.path.getsize(args.db + ".wal") if os.path.exists(args.db + ".wal") else 0,
              "timezone": con.execute("SELECT current_setting('TimeZone')").fetchone()[0],
              "tables": [check_table(con, t, args.silent_hours, now_sql) for t in tables(con)]}
    report["clock_split"] = clock_split(con, report["tables"])
    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
        return
    print(f"{report['file']}  {report['bytes'] / 1e6:.1f} MB, WAL {report['wal_bytes'] / 1e6:.1f} MB, "
          f"session tz {report['timezone']}")
    if report["clock_split"]:
        print("\n! tables: newest rows a whole number of hours apart, so writers disagree "
              "on the timezone (hours behind the newest table):")
        for name, hours in sorted(report["clock_split"].items(), key=lambda x: x[1]):
            print(f"    {name}: {hours:+g} h")
    for t in report["tables"]:
        span = f"  {t.get('first', '')} .. {t.get('last', '')}" if t.get("first") else ""
        print(f"\n{t['table']}: {t['rows']} rows{span}")
        for i in t["issues"]:
            print(f"  ! {i}")
        if t.get("silent"):
            print(f"  ~ {len(t['silent'])} ieee silent > {args.silent_hours:g} h, oldest "
                  f"{t['silent'][0]['ieee']} at {t['silent'][0]['last']}")


if __name__ == "__main__":
    main()
