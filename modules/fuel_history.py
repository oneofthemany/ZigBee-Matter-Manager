"""
Fuel price history — snapshots what the price feeds said, when we asked.

Retailers publish a daily number with no archive, so today's price is gone once
tomorrow's replaces it. One row per (site_id, fuel, feed day) bounds growth
regardless of search frequency. Its own DuckDB file and worker thread. Rows
describe stations, not people — where the user searched is not stored.
See docs/journeys.md.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

logger = logging.getLogger("modules.fuel_history")

DB_PATH = Path("./data/fuel_prices.duckdb")

#: Snapshots older than this are purged (a year of daily prices per station
#: is small, and longer serves no query the UI asks).
RETENTION_DAYS = 365

_SCHEMA = """
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


class FuelHistoryManager:
    """
    Owns data/fuel_prices.duckdb. Same shape as JourneyManager: a single
    worker thread holds the only connection; public methods are async and
    marshal onto it. Opened lazily on first use — fuel history has no
    background loops, so there is nothing to start eagerly.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fuel-db")
        self._con: Optional[duckdb.DuckDBPyConnection] = None

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _ensure_open(self) -> duckdb.DuckDBPyConnection:
        """Worker-thread only. Opens the DB and purges old rows once."""
        if self._con is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._con = duckdb.connect(str(self.db_path))
            self._con.execute(_SCHEMA)
            self._con.execute(
                "DELETE FROM price_history WHERE feed_day < ?",
                [dt.date.today() - dt.timedelta(days=RETENTION_DAYS)],
            )
        return self._con

    async def stop(self) -> None:
        def _close():
            if self._con is not None:
                self._con.close()
                self._con = None
        try:
            await self._run(_close)
        except Exception:                                # noqa: BLE001
            pass
        self._executor.shutdown(wait=False)

    # Recording
    async def record_stations(self, stations: List[Dict[str, Any]]) -> None:
        """
        Snapshot the stations a nearby-query returned (all four fuel prices
        each, not just the fuel searched for — they arrive together and cost
        nothing extra to keep). Best-effort: history failing must never fail
        the price query the user is waiting on.
        """
        rows: List[tuple] = []
        now = time.time()
        for s in stations:
            site_id = s.get("site_id")
            if not site_id:
                continue
            feed_day = _feed_day(s.get("last_updated"), now)
            for fuel, price in (s.get("prices") or {}).items():
                if not isinstance(price, (int, float)) or price <= 0:
                    continue
                rows.append((str(site_id), str(fuel), feed_day,
                             round(float(price), 3), s.get("brand"),
                             s.get("postcode"), now))
        if not rows:
            return
        try:
            await self._run(self._insert_rows, rows)
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"fuel history record failed: {e}")

    def _insert_rows(self, rows: List[tuple]) -> None:
        self._ensure_open().executemany(
            "INSERT INTO price_history "
            "(site_id, fuel, feed_day, price, brand, postcode, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (site_id, fuel, feed_day) DO NOTHING",
            rows,
        )

    # Queries
    async def daily_trend(self, fuel: str, days: int = 30,
                          site_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-day min/median/max across recorded stations (or one station)."""
        return await self._run(self._daily_trend, fuel, days, site_id)

    def _daily_trend(self, fuel, days, site_id) -> Dict[str, Any]:
        con = self._ensure_open()
        since = dt.date.today() - dt.timedelta(days=int(days))
        sql = ("SELECT feed_day, MIN(price), median(price), MAX(price), "
               "COUNT(DISTINCT site_id) FROM price_history "
               "WHERE fuel = ? AND feed_day >= ?")
        params: List[Any] = [fuel, since]
        if site_id:
            sql += " AND site_id = ?"
            params.append(site_id)
        sql += " GROUP BY feed_day ORDER BY feed_day"
        rows = con.execute(sql, params).fetchall()
        series = [
            {"day": r[0].isoformat(), "min": r[1], "median": r[2],
             "max": r[3], "stations": r[4]}
            for r in rows
        ]
        cheapest = con.execute(
            ("SELECT price, brand, postcode, feed_day FROM price_history "
             "WHERE fuel = ? AND feed_day >= ? "
             + ("AND site_id = ? " if site_id else "")
             + "ORDER BY price ASC, feed_day DESC LIMIT 1"),
            params,
        ).fetchone()
        return {
            "fuel": fuel,
            "days": int(days),
            "series": series,
            "cheapest_seen": None if not cheapest else {
                "price": cheapest[0], "brand": cheapest[1],
                "postcode": cheapest[2], "day": cheapest[3].isoformat(),
            },
        }

    async def station_history(self, site_id: str,
                              days: int = 90) -> List[Dict[str, Any]]:
        return await self._run(self._station_history, site_id, days)

    def _station_history(self, site_id, days) -> List[Dict[str, Any]]:
        rows = self._ensure_open().execute(
            "SELECT feed_day, fuel, price FROM price_history "
            "WHERE site_id = ? AND feed_day >= ? ORDER BY feed_day, fuel",
            [site_id, dt.date.today() - dt.timedelta(days=int(days))],
        ).fetchall()
        return [{"day": r[0].isoformat(), "fuel": r[1], "price": r[2]}
                for r in rows]

    async def status(self) -> Dict[str, Any]:
        return await self._run(self._status)

    def _status(self) -> Dict[str, Any]:
        row = self._ensure_open().execute(
            "SELECT COUNT(*), COUNT(DISTINCT site_id), MIN(feed_day), "
            "MAX(feed_day) FROM price_history"
        ).fetchone()
        return {
            "rows": row[0] or 0,
            "stations": row[1] or 0,
            "oldest_day": row[2].isoformat() if row[2] else None,
            "newest_day": row[3].isoformat() if row[3] else None,
            "retention_days": RETENTION_DAYS,
        }


def _feed_day(last_updated: Any, fallback_ts: float) -> dt.date:
    """
    The day a feed value belongs to — the dedupe key's time axis.

    Retailer `last_updated` arrives as an ISO string via the package; a feed
    that omitted it is keyed on the day we saw it, which deduplicates just as
    well for feeds that genuinely update daily.
    """
    if isinstance(last_updated, str) and last_updated:
        try:
            return dt.datetime.fromisoformat(last_updated).date()
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(fallback_ts).date()


_manager: Optional[FuelHistoryManager] = None


def get_fuel_history() -> FuelHistoryManager:
    """Lazy, like the fuel service itself — created on first use."""
    global _manager
    if _manager is None:
        _manager = FuelHistoryManager()
    return _manager
