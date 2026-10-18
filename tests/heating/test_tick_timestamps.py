"""Heating ticks share device_states' clock, and are pruned with it."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from harness import Checker

import duckdb

from modules import telemetry_db as t


def _container_like(path: str):
    """Python local time is London and DuckDB's session is UTC, as in the container."""
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    con = duckdb.connect(path)
    con.execute("SET TimeZone='UTC'")
    t._init_tables(con)
    return con


def _migration(c: Checker) -> None:
    c.section("old ticks are re-stamped once, across a clock change")
    with tempfile.TemporaryDirectory() as tmp:
        con = _container_like(os.path.join(tmp, "t.duckdb"))
        local = ["2026-03-29 00:30:00", "2026-03-29 03:30:00", "2026-07-17 15:09:48"]
        for ts in local:
            con.execute("INSERT INTO heating_tick_rooms (ts, circuit_id, room_id) VALUES (?, 'c', 'r')", [ts])
            con.execute("INSERT INTO heating_tick_boiler (ts, circuit_id, boiler_called) VALUES (?, 'c', TRUE)", [ts])
        t._migrate_tick_timestamps(con)
        rooms = [str(r[0]) for r in con.execute("SELECT ts FROM heating_tick_rooms ORDER BY ts").fetchall()]
        c.check("a GMT tick (before the March change) is unchanged",
                rooms[0] == "2026-03-29 00:30:00", rooms)
        c.check("a BST tick just after the change moves back one hour to UTC",
                rooms[1] == "2026-03-29 02:30:00", rooms)
        c.check("a summer tick moves back one hour to UTC",
                rooms[2] == "2026-07-17 14:09:48", rooms)
        boiler = [str(r[0]) for r in con.execute("SELECT ts FROM heating_tick_boiler ORDER BY ts").fetchall()]
        c.check("the boiler table is re-stamped the same way", boiler == rooms, boiler)

        t._migrate_tick_timestamps(con)
        again = [str(r[0]) for r in con.execute("SELECT ts FROM heating_tick_rooms ORDER BY ts").fetchall()]
        c.check("a second open leaves already re-stamped ticks alone", again == rooms, again)
        con.close()

    c.section("a host whose Python and DuckDB agree needs no change")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TZ"] = "Europe/London"
        time.tzset()
        con = duckdb.connect(os.path.join(tmp, "t.duckdb"))
        con.execute("SET TimeZone='Europe/London'")
        t._init_tables(con)
        con.execute("INSERT INTO heating_tick_rooms (ts, circuit_id, room_id) VALUES ('2026-07-17 15:09:48', 'c', 'r')")
        t._migrate_tick_timestamps(con)
        ts = str(con.execute("SELECT ts FROM heating_tick_rooms").fetchone()[0])
        c.check("ticks already in the session zone are not shifted", ts == "2026-07-17 15:09:48", ts)
        con.close()


def _writer_and_prune(c: Checker) -> None:
    c.section("new ticks are stamped in the session zone and pruned")
    with tempfile.TemporaryDirectory() as tmp:
        saved = (t.DB_PATH, t._db, t._tick_zone_cached)
        t.DB_PATH, t._db, t._tick_zone_cached = os.path.join(tmp, "telemetry.duckdb"), None, None
        try:
            tick = time.time()
            t.write_heating_tick(tick, False, [{
                "id": "c1", "calling_for_heat": True,
                "rooms": [{"room_id": "r1", "status": "cold"}],
            }])
            con = t._get_db().cursor()
            zone = ZoneInfo(con.execute("SELECT current_setting('TimeZone')").fetchone()[0])
            stored = con.execute("SELECT ts FROM heating_tick_rooms").fetchone()[0]
            expected = datetime.fromtimestamp(tick, zone).replace(tzinfo=None)
            c.check("a written tick is the session's wall time, like DEFAULT now() rows",
                    abs((stored - expected).total_seconds()) < 1, (stored, expected))

            con.execute("INSERT INTO heating_tick_rooms (ts, circuit_id, room_id) "
                        "VALUES (now() - INTERVAL 40 DAY, 'old', 'r')")
            con.execute("INSERT INTO heating_tick_boiler (ts, circuit_id, boiler_called) "
                        "VALUES (now() - INTERVAL 40 DAY, 'old', TRUE)")
            t.prune(retention_days=30)
            for table in t.HEATING_TICK_TABLES:
                kept = con.execute(f"SELECT circuit_id FROM {table}").fetchall()
                c.check(f"prune drops {table} rows past retention and keeps recent ones",
                        kept == [("c1",)], kept)
            c.check("db stats count the heating tick tables",
                    set(t.HEATING_TICK_TABLES) <= set(t.get_db_stats()), t.get_db_stats())
        finally:
            t.close()
            t.DB_PATH, t._db, t._tick_zone_cached = saved


def run() -> Checker:
    c = Checker("tick_timestamps")
    _migration(c)
    _writer_and_prune(c)
    return c
