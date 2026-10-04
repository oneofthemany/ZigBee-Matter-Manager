"""
A corrected divisor is carried back into the history stored at the old scale.

The H2 outlet's power was stored a tenth of its real value; the one-off rescales
that device's power rows up to the moment the fix went live, and nothing else.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import tempfile
from pathlib import Path

import duckdb

from harness import REPO, Checker

HALLWAY = "54:ef:44:10:01:5a:14:eb"
MEDIA = "00:15:8d:00:02:56:f8:bf"

_spec = importlib.util.spec_from_file_location(
    "rescale_device_history", REPO / "scripts" / "rescale_device_history.py")
rescale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rescale)


def _run(*argv) -> tuple:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rescale.main(list(argv))
    return code, out.getvalue()


def run() -> Checker:
    c = Checker("rescale_history")
    tmp = tempfile.mkdtemp(prefix="zmm_rescale_")
    path = str(Path(tmp) / "telemetry.duckdb")
    db = duckdb.connect(path)
    db.execute("""CREATE TABLE device_states (ts TIMESTAMP NOT NULL DEFAULT now(),
                  ieee VARCHAR NOT NULL, attribute VARCHAR NOT NULL,
                  value VARCHAR, numeric_val DOUBLE)""")
    rows = [("2026-10-04 12:00", HALLWAY, "power", 11.0),
            ("2026-10-04 12:00", HALLWAY, "power_2", 10.9),
            ("2026-10-04 14:00", HALLWAY, "power", 24.1),
            ("2026-10-04 16:00", HALLWAY, "power", 241.0),      # after the fix
            ("2026-10-04 12:00", HALLWAY, "energy", 16.373),
            ("2026-10-04 12:00", HALLWAY, "power_factor", 0.9),
            ("2026-10-04 12:00", MEDIA, "power", 69.0)]
    for ts, ieee, attr, v in rows:
        db.execute("INSERT INTO device_states VALUES (?, ?, ?, ?, ?)", [ts, ieee, attr, str(v), v])
    db.close()

    def stored():
        con = duckdb.connect(path, read_only=True)
        try:
            return {(str(r[0])[:16], r[1], r[2]): (r[3], r[4]) for r in con.execute(
                "SELECT ts, ieee, attribute, value, numeric_val FROM device_states").fetchall()}
        finally:
            con.close()

    args = ("--db", path, "--ieee", HALLWAY, "--factor", "10", "--before", "2026-10-04 15:30")
    try:
        c.section("without --apply it only reports")
        before = stored()
        code, out = _run(*args)
        c.check("the report names what would change", code == 0 and "power_2" in out
                and "Report only" in out, out)
        c.check("and changes nothing", stored() == before)

        c.section("--apply rescales that device's power, up to the fix")
        code, out = _run(*args, "--apply")
        now = stored()
        c.check("three rows are rescaled", code == 0 and "Rescaled 3 rows" in out, out)
        c.check("11 W becomes 110 W, in both columns",
                now[("2026-10-04 12:00", HALLWAY, "power")] == ("110.0", 110.0), now)
        c.check("per-socket power goes with it",
                now[("2026-10-04 12:00", HALLWAY, "power_2")][1] == 109.0, now)
        c.check("24.1 W becomes 241 W", now[("2026-10-04 14:00", HALLWAY, "power")][1] == 241.0)
        c.check("a reading taken after the fix is left alone",
                now[("2026-10-04 16:00", HALLWAY, "power")][1] == 241.0, now)
        c.check("energy and power factor are not power",
                now[("2026-10-04 12:00", HALLWAY, "energy")][1] == 16.373
                and now[("2026-10-04 12:00", HALLWAY, "power_factor")][1] == 0.9, now)
        c.check("another device is untouched",
                now[("2026-10-04 12:00", MEDIA, "power")][1] == 69.0, now)

        c.section("it cannot be applied twice by accident")
        code, out = _run(*args, "--apply")
        c.check("a second run is refused", code == 1 and "Refusing" in out, out)
        c.check("and leaves the history as it was", stored() == now)

        c.section("a database the app holds open is reported, not half-written")
        real = duckdb.connect       # DuckDB's lock is per process, so raise its error here

        def locked(*a, **k):
            raise duckdb.IOException("Could not set lock on file")
        duckdb.connect = locked
        try:
            code, out = _run(*args, "--apply", "--force")
        finally:
            duckdb.connect = real
        c.check("it says to stop the app", code == 1 and "Stop it first" in out, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return c
