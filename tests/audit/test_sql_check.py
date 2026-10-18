"""sql_check: queries bound against the schema the code declares."""

from __future__ import annotations

from harness import Checker, tree


SRC = {
    "modules/store.py": '''
        import duckdb
        _MIGRATIONS = [("trips", "driver_id", "VARCHAR")]

        def init(con):
            con.execute("""
                CREATE TABLE IF NOT EXISTS trips (trip_id INTEGER, started_at TIMESTAMP);
                CREATE INDEX IF NOT EXISTS idx_trips ON trips (started_at)
            """)
            for table, column, coltype in _MIGRATIONS:
                con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")

        def ok(con):
            return con.execute("SELECT trip_id, driver_id FROM trips WHERE trip_id = ?", [1])

        def typo(con):
            return con.execute("SELECT trip_id, drvier_id FROM trips")

        def commented(con):
            return con.execute("""
                SELECT trip_id -- the driver's trip
                FROM trips WHERE started_at > ?""", [0])

        def ghost(con):
            return con.execute("DELETE FROM journeys WHERE trip_id = ?", [1])

        def log(cur):
            cur.execute("SELECT 1", [] ); print("Update the cache")
        ''',
    "modules/legacy.py": '''
        import sqlite3
        def init(c):
            c.execute("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL)")
        def recent(c):
            return c.execute("SELECT id FROM q WHERE ts >= ?", [0])
        ''',
}


def run() -> Checker:
    c = Checker("sql_check")
    try:
        import sql_check
    except ImportError as e:
        print(f"    skipped: {e} (run under the lockfile venv)")
        return c
    with tree(SRC) as root:
        res = sql_check.check(root, [])
        errs = {r["sql"].split(" FROM")[0]: r for r in res}

        c.check("a misspelt column is a high-confidence bind error",
                any("drvier_id" in r["error"] and r["confidence"] == "high" for r in res), res)
        c.check("a column added by a looped f-string migration binds",
                not any("driver_id" in r["error"] for r in res), res)
        c.check("an apostrophe inside a -- comment does not break binding",
                not any("driver's" in r["sql"] for r in res), res)
        c.check("a table created nowhere is reported",
                any(r["kind"] == "table" and "journeys" in r["error"] for r in res), res)
        c.check("SQLite DDL in a sqlite3 file is run as SQLite",
                not any(r["file"] == "modules/legacy.py" for r in res), res)
        c.check("prose starting with an SQL verb is not taken for SQL",
                not any("Update the cache" in r["sql"] for r in res), errs)
    return c
