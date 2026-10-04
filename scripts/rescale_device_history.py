#!/usr/bin/env python3
"""
Rescale one device's stored power history after its scaling was corrected.

History is stored already scaled, so a divisor fixed in a device's entry leaves
every earlier row at the old scale. This multiplies those rows by --factor, up
to the moment the corrected app started (--before, in the hub's local time, as
device_states.ts is). Covers `power` and the per-socket `power_<n>` keys.

Without --apply it only reports. Stop the app first either way: the running app
holds DuckDB's lock, which refuses even a read-only open from another process.
Run it with the app's own duckdb version (the app image), since that is what
replays the WAL. A marker beside the
database stops the same correction being applied twice.

    python3 scripts/rescale_device_history.py \\
        --ieee 54:ef:44:10:01:5a:14:eb --factor 10 --before "2026-10-04 15:30"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Optional, Sequence

DEFAULT_DB = "./data/telemetry.duckdb"
ATTRIBUTES = r"^power(_[0-9]+)?$"       # not power_factor, not power_demand_<n>
WHERE = "ieee = ? AND ts < ? AND numeric_val IS NOT NULL AND regexp_matches(attribute, ?)"


def _marker_path(db_path: str) -> str:
    return db_path + ".rescaled.json"


def _applied(db_path: str) -> list:
    try:
        with open(_marker_path(db_path)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def summary(db, ieee: str, before: datetime) -> list:
    """[(attribute, rows, min, max)] of what the correction would touch."""
    return db.execute(
        f"SELECT attribute, count(*), min(numeric_val), max(numeric_val) "
        f"FROM device_states WHERE {WHERE} GROUP BY attribute ORDER BY attribute",
        [ieee, before, ATTRIBUTES]).fetchall()


def rescale(db, ieee: str, before: datetime, factor: float) -> int:
    """Multiply the rows in place, in one transaction. Returns rows changed."""
    db.execute("BEGIN")
    try:
        n = db.execute(f"SELECT count(*) FROM device_states WHERE {WHERE}",
                       [ieee, before, ATTRIBUTES]).fetchone()[0]
        db.execute(
            f"UPDATE device_states SET numeric_val = round(numeric_val * ?, 1), "
            f"value = CAST(round(numeric_val * ?, 1) AS VARCHAR) WHERE {WHERE}",
            [factor, factor, ieee, before, ATTRIBUTES])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return n


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"telemetry DB (default {DEFAULT_DB})")
    ap.add_argument("--ieee", required=True, help="device address, as stored")
    ap.add_argument("--factor", type=float, required=True,
                    help="multiply stored values by this (10 for a divisor of 10 corrected to 1)")
    ap.add_argument("--before", required=True, type=datetime.fromisoformat,
                    help='rows earlier than this are rescaled: when the corrected app '
                         'started, hub local time, e.g. "2026-10-04 15:30"')
    ap.add_argument("--apply", action="store_true", help="write the change (default: report only)")
    ap.add_argument("--force", action="store_true",
                    help="apply even though the marker says this correction was already made")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: {args.db} does not exist")
        return 2
    if args.factor <= 0:
        print("error: --factor must be positive")
        return 2

    import duckdb
    try:
        db = duckdb.connect(args.db, read_only=not args.apply)
    except duckdb.IOException as e:
        print(f"error: cannot open {args.db}: {e}\nIs the app still running? Stop it first.")
        return 1

    try:
        rows = summary(db, args.ieee, args.before)
        if not rows:
            print(f"No power history for {args.ieee} before {args.before}. Nothing to do.")
            return 0
        print(f"{args.ieee}: power history before {args.before}, x{args.factor:g}\n")
        print(f"  {'attribute':<12}{'rows':>9}{'min':>10}{'max':>10}{'new max':>11}")
        for attr, n, lo, hi in rows:
            print(f"  {attr:<12}{n:>9}{lo:>10.1f}{hi:>10.1f}{hi * args.factor:>11.1f}")

        done = [a for a in _applied(args.db) if a["ieee"] == args.ieee]
        if done:
            print("\nAlready applied to this device:")
            for a in done:
                print(f"  x{a['factor']:g} before {a['before']} ({a['rows']} rows, at {a['at']})")

        if not args.apply:
            print("\nReport only. Re-run with --apply to write this.")
            return 0
        if done and not args.force:
            print("\nRefusing to scale the same device twice; --force overrides.")
            return 1

        n = rescale(db, args.ieee, args.before, args.factor)
        db.execute("CHECKPOINT")
    finally:
        db.close()

    record = _applied(args.db) + [{"ieee": args.ieee, "factor": args.factor,
                                   "before": args.before.isoformat(sep=" "), "rows": n,
                                   "at": datetime.now().isoformat(sep=" ", timespec="seconds")}]
    with open(_marker_path(args.db), "w") as f:
        json.dump(record, f, indent=1)
    print(f"\nRescaled {n} rows. Recorded in {_marker_path(args.db)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
