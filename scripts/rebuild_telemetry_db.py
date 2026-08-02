#!/usr/bin/env python3
"""
Rebuild a damaged telemetry DuckDB into a fresh, clean file.

Manual front-end for modules/telemetry_rebuild.py, the same engine the app runs
automatically at startup. The original is opened READ-ONLY, so a dry run cannot
make anything worse, and nothing is swapped in without --install. Stop the app
before using --install. See docs/telemetry_database.md.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.telemetry_rebuild import install, rebuild, verify  # noqa: E402

DEFAULT_SRC = "./data/telemetry.duckdb"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=DEFAULT_SRC, help=f"damaged DB (default {DEFAULT_SRC})")
    ap.add_argument("--out", default=None, help="output file (default <src>.rebuilt.duckdb)")
    ap.add_argument("--install", action="store_true",
                    help="swap the rebuild in, keeping the original")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output file")
    ap.add_argument("--in-place", action="store_true",
                    help="repair the database in place by dropping and rebuilding "
                         "only the damaged tables (no file swap; app must be stopped)")
    ap.add_argument("--cleanup-quarantine", action="store_true",
                    help="delete quarantined WALs and pre-rebuild copies older "
                         "than the retention window (the app also does this "
                         "automatically at startup once the DB is healthy)")
    ap.add_argument("--retention-days", type=float, default=None,
                    help="override the quarantine retention window for --cleanup-quarantine")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --cleanup-quarantine, list what would be deleted")
    args = ap.parse_args(argv)

    if args.cleanup_quarantine:
        from modules.telemetry_db import cleanup_quarantined
        res = cleanup_quarantined(retention_days=args.retention_days,
                                  dry_run=args.dry_run)
        if res["skipped_reason"]:
            print(f"Skipped: {res['skipped_reason']}")
            return 1
        if not res["removed"]:
            print(f"Nothing to clean up ({res['kept']} file(s) still within "
                  f"the retention window).")
            return 0
        verb = "Would delete" if args.dry_run else "Deleted"
        for path in res["removed"]:
            print(f"  {path}")
        print(f"{verb} {len(res['removed'])} file(s), "
              f"{res['bytes_freed'] / (1024 * 1024):.1f} MB freed. "
              f"{res['kept']} kept (still within retention).")
        return 0

    if args.in_place:
        if not os.path.exists(args.src):
            print(f"error: {args.src} does not exist")
            return 2
        print(f"Repairing {args.src} in place ({os.path.getsize(args.src):,} bytes)")
        print("The application MUST be stopped — this opens the database read-write.\n")
        from modules.telemetry_rebuild import repair_in_place
        try:
            s = repair_in_place(args.src)
        except Exception as e:
            print(f"\nin-place repair failed: {e}")
            return 1
        if not s["damaged"]:
            print("\nNothing was damaged — no changes made.")
            return 0
        print(f"\nRepaired {', '.join(s['damaged'])}: {s['salvaged']:,} rows kept, "
              f"~{s['dropped_rows']:,} unreadable rows discarded.")
        print("Checkpoint succeeded — the WAL is folded into the database."
              if s["checkpointed"] else "WARNING: checkpoint did not succeed.")
        print("Restart the application.")
        return 0

    src = args.src
    out = args.out or f"{os.path.splitext(src)[0]}.rebuilt.duckdb"

    if not os.path.exists(src):
        print(f"error: {src} does not exist")
        return 2
    if os.path.exists(out):
        if not args.force:
            print(f"error: {out} already exists (use --force to overwrite)")
            return 2
        os.remove(out)

    print(f"Rebuilding {src} ({os.path.getsize(src):,} bytes) -> {out}")
    print("The source is opened read-only and will not be modified.\n")

    started = time.time()
    try:
        reports = rebuild(src, out)
    except Exception as e:
        print(f"\nrebuild failed: {e}")
        print(f"{src} is untouched.")
        return 1

    print("\nVerifying the rebuilt database:")
    if not verify(out):
        print("\nThe rebuilt file did not verify — leaving everything as it was.")
        return 1

    total = sum(r.copied for r in reports)
    lost = sum(r.lost_estimate for r in reports)
    damaged = [r for r in reports if not r.clean]

    print(f"\nRecovered {total:,} rows in {time.time() - started:.1f}s.")
    if damaged:
        print(f"Approximately {lost:,} rows were unreadable, in:")
        for r in damaged:
            print(f"  {r.table}: ~{r.lost_estimate:,} rows across "
                  f"{len(r.lost_ranges)} span(s)")
    else:
        print("Every table copied completely — no rows were lost.")

    if args.install:
        print("\nInstalling:")
        install(src, out)
        print("\nDone. Restart the application to pick up the rebuilt database.")
    else:
        print(f"\nRebuilt file: {out}")
        print("Nothing has been swapped. Stop the app and re-run with --install "
              "to put it in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
