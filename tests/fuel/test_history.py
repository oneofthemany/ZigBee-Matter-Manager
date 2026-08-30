"""
Price history: the regional schema, its migration, and per-region scoping.

Needs duckdb, which the app depends on anyway. run_all.py skips this module
with a note rather than failing if it is not installed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
import time
from pathlib import Path

import duckdb
from harness import Checker

from modules.fuel.history import FuelHistoryManager

#: The table exactly as it was before regions existed.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    site_id     TEXT   NOT NULL,
    fuel        TEXT   NOT NULL,
    feed_day    DATE   NOT NULL,
    price       DOUBLE NOT NULL,
    brand       TEXT,
    postcode    TEXT,
    recorded_at DOUBLE NOT NULL,
    PRIMARY KEY (site_id, fuel, feed_day)
);
"""

STATION = {"site_id": "4711", "brand": "Aral", "postcode": "10115",
           "last_updated": "2026-08-30", "prices": {"E10": 1.719}}


def _legacy_db(days: int = 3):
    path = Path(tempfile.mkdtemp()) / "fuel_prices.duckdb"
    con = duckdb.connect(str(path))
    con.execute(LEGACY_SCHEMA)
    today = dt.date.today()
    rows = []
    for offset in range(days):
        day = today - dt.timedelta(days=offset)
        for site, brand, postcode in (("a", "Shell", "SW1"), ("b", "BP", "N1")):
            for fuel, price in (("E10", 1.35 + offset / 100),
                                ("B7", 1.45 + offset / 100)):
                rows.append((site, fuel, day, price, brand, postcode, time.time()))
    con.executemany("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)", rows)
    con.close()
    return path, len(rows)


def _columns(path: Path):
    con = duckdb.connect(str(path))
    try:
        return {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'price_history'").fetchall()}
    finally:
        con.close()


def _scalar(path: Path, sql: str):
    con = duckdb.connect(str(path))
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


async def _migration(c: Checker) -> None:
    c.section("migration from the pre-region schema")
    path, expected = _legacy_db()
    manager = FuelHistoryManager(path)
    status = await manager.status(region="GB")
    await manager.stop()

    c.check("row count preserved", status["rows"] == expected,
            (status["rows"], expected))
    c.check("stations preserved", status["stations"] == 2, status["stations"])
    columns = _columns(path)
    c.check("region column added", "region" in columns, columns)
    c.check("currency column added", "currency" in columns, columns)
    c.check("every migrated row is GB", _scalar(
        path, "SELECT COUNT(*) FROM price_history WHERE region='GB'") == expected)
    c.check("every migrated row is GBP", _scalar(
        path, "SELECT COUNT(*) FROM price_history WHERE currency='GBP'") == expected)
    c.check("no leftover rebuild table", _scalar(
        path, "SELECT COUNT(*) FROM information_schema.tables "
              "WHERE table_name='price_history_v2'") == 0)
    c.check("prices unchanged by the copy", abs(_scalar(
        path, "SELECT MIN(price) FROM price_history") - 1.35) < 1e-9)

    again = FuelHistoryManager(path)
    status = await again.status(region="GB")
    await again.stop()
    c.check("migration is idempotent", status["rows"] == expected, status["rows"])


async def _regions(c: Checker) -> None:
    c.section("region is part of the key")
    path = Path(tempfile.mkdtemp()) / "f.duckdb"
    manager = FuelHistoryManager(path)

    await manager.record_stations([STATION], region="DE", currency="EUR")
    await manager.record_stations([{**STATION, "prices": {"E10": 1.899}}],
                                  region="FR", currency="EUR")
    de = await manager.status(region="DE")
    fr = await manager.status(region="FR")
    c.check("the same site id is stored once per region",
            de["rows"] == 1 and fr["rows"] == 1, (de["rows"], fr["rows"]))
    c.check("the breakdown lists both regions",
            de["rows_by_region"] == {"DE": 1, "FR": 1}, de["rows_by_region"])

    await manager.record_stations([STATION], region="DE", currency="EUR")
    c.check("re-recording the same day is a no-op",
            (await manager.status(region="DE"))["rows"] == 1)

    trend_de = await manager.daily_trend("E10", 30, region="DE")
    trend_fr = await manager.daily_trend("E10", 30, region="FR")
    c.check("a trend sees only its own region",
            trend_de["series"][0]["median"] == 1.719, trend_de["series"])
    c.check("and the other sees only its own",
            trend_fr["series"][0]["median"] == 1.899, trend_fr["series"])
    c.check("the trend reports its region", trend_de["region"] == "DE")
    c.check("cheapest_seen carries the currency",
            trend_de["cheapest_seen"]["currency"] == "EUR", trend_de["cheapest_seen"])

    history = await manager.station_history("4711", 90, region="DE")
    c.check("station history is scoped",
            len(history) == 1 and history[0]["price"] == 1.719, history)
    c.check("and empty for a region with no such row",
            await manager.station_history("4711", 90, region="GB") == [])
    c.check("a region never recorded reports zero",
            (await manager.status(region="IT"))["rows"] == 0)

    # Australia: cents on the pump, dollars in the row.
    await manager.record_stations(
        [{"site_id": "42", "brand": "BP", "prices": {"U91": 2.039}}],
        region="AU-NSW", currency="AUD")
    au = await manager.status(region="AU-NSW")
    c.check("a subdivided region key is stored whole", au["rows"] == 1, au)
    trend = await manager.daily_trend("U91", 30, region="AU-NSW")
    c.check("stored in dollars, not cents",
            trend["series"][0]["median"] == 2.039, trend["series"])
    c.check("currency recorded as AUD",
            trend["cheapest_seen"]["currency"] == "AUD", trend["cheapest_seen"])
    await manager.stop()

    c.section("a fresh database")
    fresh = Path(tempfile.mkdtemp()) / "g.duckdb"
    new = FuelHistoryManager(fresh)
    await new.status(region="GB")
    await new.stop()
    c.check("gets the regional schema directly", "region" in _columns(fresh))


def run() -> Checker:
    c = Checker("history")
    asyncio.run(_migration(c))
    asyncio.run(_regions(c))
    return c
