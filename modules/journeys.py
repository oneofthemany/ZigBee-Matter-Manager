"""
Journeys — drive tracking from the companion app's drive mode.

While the phone is connected to the car's Bluetooth, DriveService streams
fixes tagged with a trip_id (plus GPS speed and bearing). This module stores
those fixes, segments them into trips, and computes per-trip statistics:
distance, duration, and average / max / min / standard deviation of speed.

Storage:
    data/journeys.duckdb — a NEW database dedicated to this module. DuckDB is
    single-writer per file, so journeys must never share a .duckdb with any
    other subsystem; and all access goes through one dedicated worker thread
    that owns the connection (one DB, one thread — the project convention).
    Async callers reach it via run_in_executor.

Trip lifecycle:
    - First fix with an unseen trip_id opens a trip.
    - The phone stops streaming when the car's Bluetooth disconnects; there is
      no explicit "trip ended" call (a network call in onDestroy is not
      reliable). A closer loop finalises any open trip whose last fix is older
      than TRIP_CLOSE_GAP_S.
    - Finalisation computes distance (haversine over consecutive fixes) and
      speed statistics in SQL, resolves start/end places, then marks the trip
      closed. Trips with fewer than MIN_TRIP_FIXES fixes are discarded as
      noise (a Bluetooth blip, a parked reconnect).

Speeds are stored and aggregated in m/s (the phone reports GPS doppler speed
in m/s); the UI converts for display. Where the phone sent no speed, a speed
is derived from consecutive fixes as a fallback.

Privacy:
    Recording is opt-in per presence user (UserConfig.journeys_enabled) —
    this module persists movement history, which presence_users.py
    deliberately does not. Raw track points are purged after
    TRACK_RETENTION_DAYS; the per-trip summary rows (no coordinates) are
    kept indefinitely.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

logger = logging.getLogger("modules.journeys")

DB_PATH = Path("./data/journeys.duckdb")

#: Drive-mode fix cadence the hub asks journey-enabled phones to use, seconds.
#: At 60 s a bendy road loses real distance and the speed distribution is
#: undersampled; at 10 s both are honest. Served to the phone via
#: GET /api/presence/users/{id} so retuning is a hub-side edit.
DRIVE_FIX_INTERVAL_S = 10

#: An open trip whose newest fix is older than this is over — the car
#: disconnected and the service stopped. Comfortably larger than the fix
#: interval so a tunnel or signal gap doesn't split one drive into two.
TRIP_CLOSE_GAP_S = 300

#: Trips with fewer fixes than this are discarded at close: one or two fixes
#: is a Bluetooth blip or a parked engine-start, not a journey.
MIN_TRIP_FIXES = 3

#: Raw track points older than this are deleted (summary rows are kept).
TRACK_RETENTION_DAYS = 90

#: Speeds above this (m/s; ~200 mph) are GPS glitches, not driving. They stay
#: in the stored track but are excluded from statistics rather than allowed
#: to poison max/stddev.
MAX_PLAUSIBLE_SPEED_MPS = 90.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trip_fixes (
    trip_id     TEXT   NOT NULL,
    user_id     TEXT   NOT NULL,
    ts          DOUBLE NOT NULL,
    lat         DOUBLE NOT NULL,
    lon         DOUBLE NOT NULL,
    speed_mps   DOUBLE,
    bearing_deg DOUBLE,
    accuracy_m  DOUBLE
);
CREATE INDEX IF NOT EXISTS idx_trip_fixes_trip ON trip_fixes (trip_id);
CREATE TABLE IF NOT EXISTS trips (
    trip_id          TEXT PRIMARY KEY,
    user_id          TEXT   NOT NULL,
    started_at       DOUBLE NOT NULL,
    ended_at         DOUBLE,
    status           TEXT   NOT NULL DEFAULT 'open',
    fix_count        BIGINT,
    distance_m       DOUBLE,
    duration_s       DOUBLE,
    avg_speed_mps    DOUBLE,
    max_speed_mps    DOUBLE,
    min_speed_mps    DOUBLE,
    stddev_speed_mps DOUBLE,
    start_place      TEXT,
    end_place        TEXT
);
"""

# Distance + speed statistics for one trip, all in the engine. Consecutive
# fixes are paired with LAG; each segment contributes haversine metres; the
# speed sample is the GPS-reported speed, falling back to segment distance
# over segment time when the phone sent none. Implausible samples are
# excluded from the aggregates (not the table).
_FINALIZE_SQL = f"""
WITH seq AS (
    SELECT ts, lat, lon, speed_mps,
           LAG(ts)  OVER w AS pts,
           LAG(lat) OVER w AS plat,
           LAG(lon) OVER w AS plon
    FROM trip_fixes
    WHERE trip_id = ?
    WINDOW w AS (ORDER BY ts)
),
seg AS (
    SELECT ts, speed_mps, pts,
           CASE WHEN plat IS NULL THEN 0.0
                ELSE 2.0 * 6371000.0 * ASIN(SQRT(
                       POWER(SIN(RADIANS(lat - plat) / 2.0), 2)
                     + COS(RADIANS(plat)) * COS(RADIANS(lat))
                       * POWER(SIN(RADIANS(lon - plon) / 2.0), 2)))
           END AS seg_m
    FROM seq
),
sp AS (
    SELECT ts, seg_m,
           COALESCE(speed_mps,
                    CASE WHEN pts IS NOT NULL AND ts > pts
                         THEN seg_m / (ts - pts) END) AS v
    FROM seg
)
SELECT COUNT(*)   AS fix_count,
       SUM(seg_m) AS distance_m,
       AVG(v)         FILTER (WHERE v <= {MAX_PLAUSIBLE_SPEED_MPS}) AS avg_v,
       MAX(v)         FILTER (WHERE v <= {MAX_PLAUSIBLE_SPEED_MPS}) AS max_v,
       MIN(v)         FILTER (WHERE v <= {MAX_PLAUSIBLE_SPEED_MPS}) AS min_v,
       STDDEV_SAMP(v) FILTER (WHERE v <= {MAX_PLAUSIBLE_SPEED_MPS}) AS sd_v,
       MIN(ts) AS t0,
       MAX(ts) AS t1
FROM sp
"""


class JourneyManager:
    """
    Owns data/journeys.duckdb and the trip lifecycle.

    Every DB touch runs on `_executor` — a single dedicated thread that is
    the only holder of the connection. Public methods are async and marshal
    onto that thread; nothing else may call `self._con` directly.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="journeys-db")
        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._closer_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self._run(self._open)
        self._closer_task = asyncio.create_task(self._closer_loop())
        logger.info(f"Journey manager started ({self.db_path})")

    async def stop(self) -> None:
        if self._closer_task:
            self._closer_task.cancel()
            try:
                await self._closer_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self._run(self._close)
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    def _open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path))
        for stmt in _SCHEMA.strip().split(";"):
            if stmt.strip():
                self._con.execute(stmt)
        # A crash mid-drive leaves trips open with no more fixes coming;
        # they will be swept by the first closer pass rather than special-
        # cased here, since "no fix for TRIP_CLOSE_GAP_S" already covers it.

    def _close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    async def record_fix(
            self,
            user_id: str,
            trip_id: str,
            lat: float,
            lon: float,
            ts: float,
            speed_mps: Optional[float] = None,
            bearing_deg: Optional[float] = None,
            accuracy_m: Optional[float] = None,
    ) -> None:
        """Append one drive fix; opens the trip on first sight of trip_id."""
        await self._run(self._record_fix, user_id, trip_id, lat, lon, ts,
                        speed_mps, bearing_deg, accuracy_m)

    def _record_fix(self, user_id, trip_id, lat, lon, ts,
                    speed_mps, bearing_deg, accuracy_m) -> None:
        self._con.execute(
            "INSERT INTO trips (trip_id, user_id, started_at) VALUES (?, ?, ?) "
            "ON CONFLICT (trip_id) DO NOTHING",
            [trip_id, user_id, ts],
        )
        self._con.execute(
            "INSERT INTO trip_fixes "
            "(trip_id, user_id, ts, lat, lon, speed_mps, bearing_deg, accuracy_m) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [trip_id, user_id, ts, lat, lon, speed_mps, bearing_deg, accuracy_m],
        )

    # ------------------------------------------------------------------
    # Trip closure
    # ------------------------------------------------------------------
    async def _closer_loop(self) -> None:
        last_purge = 0.0
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    closed = await self._run(self._close_idle_trips)
                    for trip_id, user_id in closed:
                        await self._resolve_places(trip_id, user_id)
                    if time.time() - last_purge > 24 * 3600:
                        await self._run(self._purge_old_tracks)
                        last_purge = time.time()
                except Exception as e:               # noqa: BLE001
                    # The loop must survive any single pass failing; a trip
                    # left open is closed by a later pass.
                    logger.error(f"Journey closer pass failed: {e}")
        except asyncio.CancelledError:
            return

    def _close_idle_trips(self) -> List[tuple]:
        """Finalise open trips gone quiet. Returns [(trip_id, user_id), ...]."""
        cutoff = time.time() - TRIP_CLOSE_GAP_S
        idle = self._con.execute(
            "SELECT t.trip_id, t.user_id FROM trips t WHERE t.status = 'open' "
            "AND COALESCE((SELECT MAX(f.ts) FROM trip_fixes f "
            "              WHERE f.trip_id = t.trip_id), t.started_at) < ?",
            [cutoff],
        ).fetchall()

        closed: List[tuple] = []
        for trip_id, user_id in idle:
            row = self._con.execute(_FINALIZE_SQL, [trip_id]).fetchone()
            fix_count = row[0] or 0
            if fix_count < MIN_TRIP_FIXES:
                # A blip, not a journey — remove it entirely.
                self._con.execute("DELETE FROM trip_fixes WHERE trip_id = ?", [trip_id])
                self._con.execute("DELETE FROM trips WHERE trip_id = ?", [trip_id])
                logger.info(f"[journeys] discarded blip trip {trip_id} ({fix_count} fixes)")
                continue
            _, distance_m, avg_v, max_v, min_v, sd_v, t0, t1 = row
            self._con.execute(
                "UPDATE trips SET status = 'closed', fix_count = ?, "
                "distance_m = ?, duration_s = ?, avg_speed_mps = ?, "
                "max_speed_mps = ?, min_speed_mps = ?, stddev_speed_mps = ?, "
                "started_at = ?, ended_at = ? WHERE trip_id = ?",
                [fix_count, distance_m, (t1 - t0) if t0 is not None else None,
                 avg_v, max_v, min_v, sd_v, t0, t1, trip_id],
            )
            closed.append((trip_id, user_id))
            logger.info(
                f"[journeys] closed trip {trip_id} for {user_id}: "
                f"{(distance_m or 0) / 1000:.1f} km, {fix_count} fixes"
            )
        return closed

    async def _resolve_places(self, trip_id: str, user_id: str) -> None:
        """
        Name the endpoints of a closed trip ("home", a place id, or "away").

        Runs outside the DB thread because it consults the presence and place
        managers; coordinates leave the database only long enough to be turned
        into names, and only the names are written back.
        """
        try:
            ends = await self._run(self._trip_endpoints, trip_id)
            if not ends:
                return
            (slat, slon), (elat, elon) = ends
            start_place = self._name_for(user_id, slat, slon)
            end_place = self._name_for(user_id, elat, elon)
            await self._run(
                lambda: self._con.execute(
                    "UPDATE trips SET start_place = ?, end_place = ? WHERE trip_id = ?",
                    [start_place, end_place, trip_id])
            )
        except Exception as e:                       # noqa: BLE001
            logger.debug(f"[journeys] place resolve failed for {trip_id}: {e}")

    def _trip_endpoints(self, trip_id: str):
        rows = self._con.execute(
            "SELECT arg_min(lat, ts), arg_min(lon, ts), "
            "       arg_max(lat, ts), arg_max(lon, ts) "
            "FROM trip_fixes WHERE trip_id = ?",
            [trip_id],
        ).fetchone()
        if not rows or rows[0] is None:
            return None
        return (rows[0], rows[1]), (rows[2], rows[3])

    @staticmethod
    def _name_for(user_id: str, lat: float, lon: float) -> str:
        """Same resolution order as live presence: home wins, then places."""
        try:
            from modules.presence_users import get_presence_manager, _haversine_m
            pmgr = get_presence_manager()
            dev = pmgr.get_user(user_id) if pmgr else None
            if dev and dev.cfg.home_lat is not None and dev.cfg.home_lon is not None:
                if _haversine_m(lat, lon, dev.cfg.home_lat, dev.cfg.home_lon) \
                        <= dev.cfg.radius_m + dev.cfg.hysteresis_m:
                    return "home"
        except Exception:                            # noqa: BLE001
            pass
        try:
            from modules.places import get_place_manager
            plm = get_place_manager()
            hit = plm.resolve(lat, lon) if plm else None
            if hit:
                return hit.id
        except Exception:                            # noqa: BLE001
            pass
        return "away"

    def _purge_old_tracks(self) -> None:
        cutoff = time.time() - TRACK_RETENTION_DAYS * 24 * 3600
        self._con.execute(
            "DELETE FROM trip_fixes WHERE trip_id IN "
            "(SELECT trip_id FROM trips WHERE status = 'closed' AND ended_at < ?)",
            [cutoff],
        )

    # ------------------------------------------------------------------
    # Queries (for the API)
    # ------------------------------------------------------------------
    _TRIP_COLS = ("trip_id", "user_id", "started_at", "ended_at", "status",
                  "fix_count", "distance_m", "duration_s", "avg_speed_mps",
                  "max_speed_mps", "min_speed_mps", "stddev_speed_mps",
                  "start_place", "end_place")

    async def list_trips(self, user_id: Optional[str] = None,
                         limit: int = 50) -> List[Dict[str, Any]]:
        return await self._run(self._list_trips, user_id, limit)

    def _list_trips(self, user_id, limit) -> List[Dict[str, Any]]:
        sql = (f"SELECT {', '.join(self._TRIP_COLS)} FROM trips "
               "WHERE status = 'closed'")
        params: List[Any] = []
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._con.execute(sql, params).fetchall()
        return [dict(zip(self._TRIP_COLS, r)) for r in rows]

    async def get_trip(self, trip_id: str,
                       include_track: bool = False) -> Optional[Dict[str, Any]]:
        return await self._run(self._get_trip, trip_id, include_track)

    def _get_trip(self, trip_id, include_track) -> Optional[Dict[str, Any]]:
        row = self._con.execute(
            f"SELECT {', '.join(self._TRIP_COLS)} FROM trips WHERE trip_id = ?",
            [trip_id],
        ).fetchone()
        if not row:
            return None
        trip = dict(zip(self._TRIP_COLS, row))
        if include_track:
            pts = self._con.execute(
                "SELECT ts, lat, lon, speed_mps, bearing_deg, accuracy_m "
                "FROM trip_fixes WHERE trip_id = ? ORDER BY ts",
                [trip_id],
            ).fetchall()
            trip["track"] = [
                {"ts": p[0], "lat": p[1], "lon": p[2], "speed_mps": p[3],
                 "bearing_deg": p[4], "accuracy_m": p[5]}
                for p in pts
            ]
        return trip

    async def user_stats(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        return await self._run(self._user_stats, user_id)

    def _user_stats(self, user_id) -> Dict[str, Any]:
        where = "WHERE status = 'closed'"
        params: List[Any] = []
        if user_id:
            where += " AND user_id = ?"
            params.append(user_id)
        row = self._con.execute(
            "SELECT COUNT(*), SUM(distance_m), SUM(duration_s), "
            # Overall average speed as total distance over total time —
            # a mean of per-trip means would overweight short trips.
            "CASE WHEN SUM(duration_s) > 0 "
            "     THEN SUM(distance_m) / SUM(duration_s) END, "
            "MAX(max_speed_mps), "
            "quantile_cont(distance_m, 0.5) "
            f"FROM trips {where}",
            params,
        ).fetchone()
        return {
            "trip_count": row[0] or 0,
            "total_distance_m": row[1] or 0.0,
            "total_duration_s": row[2] or 0.0,
            "overall_avg_speed_mps": row[3],
            "top_speed_mps": row[4],
            "median_trip_distance_m": row[5],
        }

    async def delete_trip(self, trip_id: str) -> bool:
        return await self._run(self._delete_trip, trip_id)

    def _delete_trip(self, trip_id) -> bool:
        found = self._con.execute(
            "SELECT 1 FROM trips WHERE trip_id = ?", [trip_id]).fetchone()
        self._con.execute("DELETE FROM trip_fixes WHERE trip_id = ?", [trip_id])
        self._con.execute("DELETE FROM trips WHERE trip_id = ?", [trip_id])
        return bool(found)


# ---------------------------------------------------------------------------
# Singleton helper (same pattern as presence_users / places)
# ---------------------------------------------------------------------------

_manager: Optional[JourneyManager] = None


def get_journey_manager() -> Optional[JourneyManager]:
    return _manager


def set_journey_manager(mgr: JourneyManager) -> None:
    global _manager
    _manager = mgr
