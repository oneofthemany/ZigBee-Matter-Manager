"""
Drive tracking: trip segmentation, per-trip statistics, driving behaviour from
the phone's inertial summaries, and driver attribution. See docs/journeys.md.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

logger = logging.getLogger("modules.journeys")

DB_PATH = Path("./data/journeys.duckdb")

#: Fix cadence asked of drive-mode phones, s. Served via GET /api/presence/users/{id}.
DRIVE_FIX_INTERVAL_S = 10

#: An open trip whose newest fix is older than this is over.
TRIP_CLOSE_GAP_S = 300

#: Fewer fixes than this at close is a blip, not a journey — discarded.
MIN_TRIP_FIXES = 3

#: Raw track points older than this are deleted (summary rows are kept).
TRACK_RETENTION_DAYS = 90

#: m/s (~200 mph). Above this is a GPS glitch: stored, but excluded from stats.
MAX_PLAUSIBLE_SPEED_MPS = 90.0

#: m/s (~1.1 mph). GPS speed never settles to 0, so "stopped" needs a floor.
STOPPED_SPEED_MPS = 0.5

#: Idle only accrues across gaps up to this (s); longer is lost signal.
MAX_IDLE_SEGMENT_S = 30.0

#: Linear approximation, accurate to well under a metre over road altitudes.
METRES_PER_HPA = 8.3

#: Climb deadbands — without them sensor noise sums into phantom altitude gain.
CLIMB_DEADBAND_HPA = 0.1
CLIMB_DEADBAND_M = 3.0

#: m/s (~11 mph). Below this the gradient divisor turns noise into a cliff face.
MIN_GRADIENT_SPEED_MPS = 5.0

#: Gradient is only computed across gaps up to this (s).
MAX_GRADIENT_GAP_S = 30.0

#: Steeper than this (%) is a cabin-pressure artefact, not a road.
MAX_PLAUSIBLE_GRADIENT_PCT = 25.0

#: Not the car moving. Kept in the table, excluded from aggregates and the
#: track. "still" is absent deliberately: a car at a red light reports it.
NON_VEHICLE_ACTIVITIES = ("walking", "running", "on_foot", "on_bicycle")

_VEHICLE_FILTER = "(activity IS NULL OR activity NOT IN ({}))".format(
    ", ".join(f"'{a}'" for a in NON_VEHICLE_ACTIVITIES)
)

#: Trips shorter than this (m) get no smoothness score.
MIN_SCORE_DISTANCE_M = 2000.0

#: Score decay constant, harsh events per 100 km. See docs/journeys.md.
SCORE_DECAY_EVENTS_PER_100KM = 100.0

#: Measured distance before a driver is ranked (m). Below this: listed, unranked.
MIN_LEADERBOARD_DISTANCE_M = 40000.0

#: How far back the co-presence pass looks (s).
COPRESENCE_LOOKBACK_S = 6 * 3600

#: Fraction of the shorter trip that must overlap in time to be one drive.
COPRESENCE_MIN_OVERLAP = 0.6

#: How close two trips' starts, and their ends, must be to be one drive (m).
COPRESENCE_ENDPOINT_M = 500.0

#: How far two trips' distances may differ and still be one drive (fraction).
COPRESENCE_DISTANCE_TOL = 0.25

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
CREATE TABLE IF NOT EXISTS trip_events (
    trip_id    TEXT   NOT NULL,
    user_id    TEXT   NOT NULL,
    ts         DOUBLE NOT NULL,
    kind       TEXT   NOT NULL,
    peak_mps2  DOUBLE,
    duration_s DOUBLE
);
CREATE INDEX IF NOT EXISTS idx_trip_events_trip ON trip_events (trip_id);
CREATE TABLE IF NOT EXISTS drivers (
    driver_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    -- Presence user whose trips attribute here by default. NULL is valid.
    -- NB: _open splits this schema on semicolons, so no comment may contain one.
    user_id    TEXT,
    colour     TEXT,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DOUBLE  NOT NULL
);
"""

# Applied on every open; CREATE TABLE IF NOT EXISTS will not widen an existing
# table, so a pre-motion-sensing database needs these. See docs/journeys.md.
_MIGRATIONS = (
    # Per-fix inertial summary of the interval ending at that fix.
    ("trip_fixes", "altitude_m", "DOUBLE"),
    ("trip_fixes", "pressure_hpa", "DOUBLE"),
    ("trip_fixes", "long_peak_mps2", "DOUBLE"),
    ("trip_fixes", "lat_peak_mps2", "DOUBLE"),
    # Unsigned horizontal magnitude; not gated on the phone's forward axis.
    ("trip_fixes", "horiz_peak_mps2", "DOUBLE"),
    # What the phone said it was doing. NULL means it had no opinion.
    ("trip_fixes", "activity", "TEXT"),
    ("trip_fixes", "vert_rms_mps2", "DOUBLE"),
    ("trip_fixes", "jerk_peak_mps3", "DOUBLE"),
    ("trip_fixes", "yaw_peak_rads", "DOUBLE"),
    # Per-trip behaviour, computed at close.
    ("trips", "harsh_brake_count", "BIGINT"),
    ("trips", "harsh_accel_count", "BIGINT"),
    ("trips", "harsh_corner_count", "BIGINT"),
    ("trips", "harsh_event_count", "BIGINT"),
    ("trips", "max_brake_mps2", "DOUBLE"),
    ("trips", "max_accel_mps2", "DOUBLE"),
    ("trips", "max_lat_mps2", "DOUBLE"),
    ("trips", "roughness_mps2", "DOUBLE"),
    ("trips", "idle_s", "DOUBLE"),
    ("trips", "stop_count", "BIGINT"),
    ("trips", "climb_m", "DOUBLE"),
    # Positive magnitude, separate from climb (a round trip nets to zero).
    ("trips", "descent_m", "DOUBLE"),
    ("trips", "smoothness_score", "DOUBLE"),
    ("trips", "motion_fix_count", "BIGINT"),
    # Attribution. NULL driver_id means unclaimed, never "the phone's owner".
    ("trips", "driver_id", "TEXT"),
    ("trips", "attribution", "TEXT"),
    ("trips", "confidence", "TEXT"),
    # Which vehicle. NULL on trips recorded before the app sent it.
    ("trips", "car_bt_address", "TEXT"),
    # Set on the redundant copy when two phones recorded one drive.
    ("trips", "primary_trip_id", "TEXT"),
)

# Distance + speed stats for one trip. Implausible samples are excluded from the
# aggregates, not the table.
_FINALIZE_SQL = f"""
WITH seq AS (
    SELECT ts, lat, lon, speed_mps,
           LAG(ts)  OVER w AS pts,
           LAG(lat) OVER w AS plat,
           LAG(lon) OVER w AS plon
    FROM trip_fixes
    WHERE trip_id = ? AND {_VEHICLE_FILTER}
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

# Driving behaviour from the per-fix inertial summaries. See docs/journeys.md.
_MOTION_SQL = f"""
WITH seq AS (
    SELECT ts, speed_mps, altitude_m, pressure_hpa,
           long_peak_mps2, lat_peak_mps2, vert_rms_mps2,
           LAG(ts)           OVER w AS pts,
           LAG(speed_mps)    OVER w AS pspeed,
           LAG(altitude_m)   OVER w AS palt,
           LAG(pressure_hpa) OVER w AS ppress
    FROM trip_fixes
    WHERE trip_id = ? AND {_VEHICLE_FILTER}
    WINDOW w AS (ORDER BY ts)
)
SELECT COUNT(vert_rms_mps2)                       AS motion_fixes,
       AVG(vert_rms_mps2)                         AS roughness,
       MAX(lat_peak_mps2)                         AS max_lat,
       MIN(long_peak_mps2)                        AS max_brake,
       MAX(long_peak_mps2)                        AS max_accel,
       SUM(CASE WHEN pts IS NOT NULL
                 AND ts - pts <= {MAX_IDLE_SEGMENT_S}
                 AND speed_mps < {STOPPED_SPEED_MPS}
                THEN ts - pts END)                AS idle_s,
       SUM(CASE WHEN pspeed >= {STOPPED_SPEED_MPS}
                 AND speed_mps < {STOPPED_SPEED_MPS}
                THEN 1 ELSE 0 END)                AS stop_count,
       -- Pressure FALLS as the car climbs, hence the reversed subtraction.
       SUM(CASE WHEN ppress - pressure_hpa > {CLIMB_DEADBAND_HPA}
                THEN (ppress - pressure_hpa) * {METRES_PER_HPA} END)
                                                  AS climb_baro_m,
       SUM(CASE WHEN altitude_m - palt > {CLIMB_DEADBAND_M}
                THEN altitude_m - palt END)       AS climb_gnss_m,
       -- Descent, same deadbands reversed, summed as a positive magnitude.
       SUM(CASE WHEN pressure_hpa - ppress > {CLIMB_DEADBAND_HPA}
                THEN (pressure_hpa - ppress) * {METRES_PER_HPA} END)
                                                  AS descent_baro_m,
       SUM(CASE WHEN palt - altitude_m > {CLIMB_DEADBAND_M}
                THEN palt - altitude_m END)       AS descent_gnss_m
FROM seq
"""

_EVENT_COUNT_SQL = """
SELECT kind, COUNT(*) FROM trip_events WHERE trip_id = ? GROUP BY kind
"""

# The track, with a signed barometric road gradient per fix. See docs/journeys.md.
_TRACK_SQL = f"""
WITH seq AS (
    SELECT ts, lat, lon, speed_mps, bearing_deg, accuracy_m, altitude_m,
           long_peak_mps2, lat_peak_mps2, horiz_peak_mps2, vert_rms_mps2,
           jerk_peak_mps3, yaw_peak_rads, pressure_hpa, activity,
           LAG(ts)           OVER w AS pts,
           LAG(pressure_hpa) OVER w AS ppress
    FROM trip_fixes
    WHERE trip_id = ? AND {_VEHICLE_FILTER}
    WINDOW w AS (ORDER BY ts)
),
grad AS (
    SELECT *,
           CASE WHEN ppress IS NOT NULL AND pressure_hpa IS NOT NULL
                     AND pts IS NOT NULL AND ts - pts > 0
                     AND ts - pts <= {MAX_GRADIENT_GAP_S}
                     AND speed_mps >= {MIN_GRADIENT_SPEED_MPS}
                -- Pressure falls as the car climbs. Vertical over horizontal, %.
                THEN 100.0 * ((ppress - pressure_hpa) * {METRES_PER_HPA})
                     / (speed_mps * (ts - pts))
           END AS gradient_pct
    FROM seq
)
SELECT ts, lat, lon, speed_mps, bearing_deg, accuracy_m, altitude_m,
       long_peak_mps2, lat_peak_mps2, horiz_peak_mps2, vert_rms_mps2,
       jerk_peak_mps3, yaw_peak_rads, activity,
       CASE WHEN ABS(gradient_pct) <= {MAX_PLAUSIBLE_GRADIENT_PCT}
            THEN gradient_pct END AS gradient_pct
FROM grad
ORDER BY ts
"""


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle metres. The Python twin of the SQL in _FINALIZE_SQL, for
    the co-presence pairing, which compares trips rather than fixes."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2.0 * 6371000.0 * math.asin(math.sqrt(a))


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
        for table, column, coltype in _MIGRATIONS:
            self._con.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}"
            )

    def _close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

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
            altitude_m: Optional[float] = None,
            motion: Optional[Dict[str, Any]] = None,
            events: Optional[List[Dict[str, Any]]] = None,
            activity: Optional[str] = None,
    ) -> None:
        """
        Append one drive fix; opens the trip on first sight of trip_id.

        [motion] is the phone's summary of the interval ending at this fix and
        [events] the discrete manoeuvres within it, both keyed as the API
        receives them. Either may be absent — the phone omits them when it has
        no sensors, and older builds never send them at all.
        """
        await self._run(self._record_fix, user_id, trip_id, lat, lon, ts,
                        speed_mps, bearing_deg, accuracy_m, altitude_m,
                        motion or {}, events or [], activity)

    def _record_fix(self, user_id, trip_id, lat, lon, ts,
                    speed_mps, bearing_deg, accuracy_m, altitude_m,
                    motion, events, activity) -> None:
        self._con.execute(
            "INSERT INTO trips (trip_id, user_id, started_at) VALUES (?, ?, ?) "
            "ON CONFLICT (trip_id) DO NOTHING",
            [trip_id, user_id, ts],
        )
        # Reopen if the closer called it early (a signal gap mid-drive);
        # finalisation recomputes everything. See docs/journeys.md.
        self._con.execute(
            "UPDATE trips SET status = 'open' "
            "WHERE trip_id = ? AND status = 'closed'",
            [trip_id],
        )
        self._con.execute(
            "INSERT INTO trip_fixes "
            "(trip_id, user_id, ts, lat, lon, speed_mps, bearing_deg, accuracy_m, "
            " altitude_m, pressure_hpa, long_peak_mps2, lat_peak_mps2, "
            " horiz_peak_mps2, vert_rms_mps2, jerk_peak_mps3, yaw_peak_rads, "
            " activity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [trip_id, user_id, ts, lat, lon, speed_mps, bearing_deg, accuracy_m,
             altitude_m,
             motion.get("pressure"), motion.get("long_peak"),
             motion.get("lat_peak"), motion.get("horiz_peak"),
             motion.get("vert_rms"),
             motion.get("jerk_peak"), motion.get("yaw_peak"), activity],
        )
        for e in events:
            # The phone timestamps events itself, so their position survives.
            self._con.execute(
                "INSERT INTO trip_events "
                "(trip_id, user_id, ts, kind, peak_mps2, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [trip_id, user_id, e.get("t", ts), e.get("kind", "harsh"),
                 e.get("peak"), e.get("dur")],
            )

    async def _closer_loop(self) -> None:
        last_purge = 0.0
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    closed = await self._run(self._close_idle_trips)
                    for trip_id, user_id in closed:
                        await self._resolve_places(trip_id, user_id)
                    # After closing: a pair needs both trips to have endpoints.
                    await self._run(self._collapse_copresent)
                    if time.time() - last_purge > 24 * 3600:
                        await self._run(self._purge_old_tracks)
                        last_purge = time.time()
                except Exception as e:               # noqa: BLE001
                    # The loop must survive one pass failing; a later pass retries.
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
                self._con.execute("DELETE FROM trip_events WHERE trip_id = ?", [trip_id])
                self._con.execute("DELETE FROM trips WHERE trip_id = ?", [trip_id])
                logger.info(f"[journeys] discarded blip trip {trip_id} ({fix_count} fixes)")
                continue
            _, distance_m, avg_v, max_v, min_v, sd_v, t0, t1 = row
            b = self._behaviour(trip_id, distance_m)
            driver_id = self._driver_for_user(user_id)
            self._con.execute(
                "UPDATE trips SET status = 'closed', fix_count = ?, "
                "distance_m = ?, duration_s = ?, avg_speed_mps = ?, "
                "max_speed_mps = ?, min_speed_mps = ?, stddev_speed_mps = ?, "
                "started_at = ?, ended_at = ?, "
                "harsh_brake_count = ?, harsh_accel_count = ?, "
                "harsh_corner_count = ?, harsh_event_count = ?, "
                "max_brake_mps2 = ?, max_accel_mps2 = ?, max_lat_mps2 = ?, "
                "roughness_mps2 = ?, idle_s = ?, stop_count = ?, climb_m = ?, "
                "descent_m = ?, smoothness_score = ?, motion_fix_count = ?, "
                # COALESCE: a reopened trip must not lose a manual attribution.
                "driver_id = COALESCE(driver_id, ?), "
                "attribution = COALESCE(attribution, ?), "
                "confidence = COALESCE(confidence, ?) "
                "WHERE trip_id = ?",
                [fix_count, distance_m, (t1 - t0) if t0 is not None else None,
                 avg_v, max_v, min_v, sd_v, t0, t1,
                 b["harsh_brake_count"], b["harsh_accel_count"],
                 b["harsh_corner_count"], b["harsh_event_count"],
                 b["max_brake_mps2"], b["max_accel_mps2"], b["max_lat_mps2"],
                 b["roughness_mps2"], b["idle_s"], b["stop_count"],
                 b["climb_m"], b["descent_m"], b["smoothness_score"],
                 b["motion_fix_count"],
                 driver_id,
                 "sole_phone" if driver_id else None,
                 # One phone recording means one person known to be there.
                 "high" if driver_id else None,
                 trip_id],
            )
            closed.append((trip_id, user_id))
            logger.info(
                f"[journeys] closed trip {trip_id} for {user_id}: "
                f"{(distance_m or 0) / 1000:.1f} km, {fix_count} fixes"
            )
        return closed

    # Recently-closed journeys with endpoints, for co-presence pairing. Trips
    # already pointed at a primary are excluded, so settled groups stay settled.
    _COPRESENCE_SQL = """
    WITH candidate AS (
        SELECT trip_id, user_id, started_at, ended_at, distance_m,
               fix_count, motion_fix_count
        FROM trips
        WHERE status = 'closed' AND primary_trip_id IS NULL
          AND ended_at >= ? AND started_at IS NOT NULL AND ended_at IS NOT NULL
    ),
    ends AS (
        SELECT trip_id,
               arg_min(lat, ts) AS slat, arg_min(lon, ts) AS slon,
               arg_max(lat, ts) AS elat, arg_max(lon, ts) AS elon
        FROM trip_fixes
        WHERE trip_id IN (SELECT trip_id FROM candidate)
        GROUP BY trip_id
    )
    SELECT c.trip_id, c.user_id, c.started_at, c.ended_at, c.distance_m,
           c.fix_count, c.motion_fix_count,
           e.slat, e.slon, e.elat, e.elon
    FROM candidate c JOIN ends e ON e.trip_id = c.trip_id
    ORDER BY c.started_at
    """

    def _collapse_copresent(self) -> int:
        """
        Point one of each pair of same-drive trips at the other. DB thread.

        Two phones in one car produce two trips describing one journey. Keeping
        both would count the distance twice and average the drive into the
        aggregates twice, so one is kept as the journey and the other marked as
        a duplicate of it. Returns how many were newly marked.
        """
        rows = self._con.execute(
            self._COPRESENCE_SQL, [time.time() - COPRESENCE_LOOKBACK_S]
        ).fetchall()
        if len(rows) < 2:
            return 0

        # Trips that already have duplicates. A settled group keeps its primary.
        established = {
            r[0] for r in self._con.execute(
                "SELECT DISTINCT primary_trip_id FROM trips "
                "WHERE primary_trip_id IS NOT NULL"
            ).fetchall()
        }

        marked = 0
        for group in self._cluster_drives(rows):
            if len(group) < 2:
                continue
            # One journey per group, chosen once — pairwise marking would chain A→B→C.
            primary = max(group, key=lambda t: (t[0] in established,) + self._rank(t))
            for dup in group:
                if dup[0] == primary[0]:
                    continue
                self._con.execute(
                    "UPDATE trips SET primary_trip_id = ? WHERE trip_id = ?",
                    [primary[0], dup[0]],
                )
                marked += 1
                logger.info(
                    f"[journeys] co-presence: {dup[0]} ({dup[1]}) is the same "
                    f"drive as {primary[0]} ({primary[1]}) — collapsed"
                )
            # Nothing here says who drove. A manual assignment is left alone.
            self._con.execute(
                "UPDATE trips SET attribution = 'copresence', confidence = 'low' "
                "WHERE trip_id = ? AND COALESCE(attribution, '') <> 'manual'",
                [primary[0]],
            )
        return marked

    def _cluster_drives(self, rows) -> List[list]:
        """
        Group closed trips that describe the same physical drive.

        Membership is by similarity to anything already in the group rather
        than to a fixed representative, so three phones in one car land in one
        group even where the first and last of them pair only through the
        middle one.
        """
        groups: List[list] = []
        for row in rows:
            for g in groups:
                if any(self._same_drive(row, other) for other in g):
                    g.append(row)
                    break
            else:
                groups.append([row])
        return groups

    @staticmethod
    def _same_drive(a, b) -> bool:
        """Whether two closed trips are one physical journey."""
        (_, a_user, a_t0, a_t1, a_dist, _, _, a_slat, a_slon, a_elat, a_elon) = a
        (_, b_user, b_t0, b_t1, b_dist, _, _, b_slat, b_slon, b_elat, b_elon) = b

        # Two trips from one user overlapping is a recording fault, not two occupants.
        if a_user == b_user:
            return False

        overlap = min(a_t1, b_t1) - max(a_t0, b_t0)
        shorter = min(a_t1 - a_t0, b_t1 - b_t0)
        if shorter <= 0 or overlap / shorter < COPRESENCE_MIN_OVERLAP:
            return False

        if _haversine_m(a_slat, a_slon, b_slat, b_slon) > COPRESENCE_ENDPOINT_M:
            return False
        if _haversine_m(a_elat, a_elon, b_elat, b_elon) > COPRESENCE_ENDPOINT_M:
            return False

        # Endpoints agreeing while distances do not means different roads.
        if a_dist and b_dist:
            if abs(a_dist - b_dist) / max(a_dist, b_dist) > COPRESENCE_DISTANCE_TOL:
                return False
        return True

    @staticmethod
    def _rank(t):
        """
        How good a candidate a trip is for surviving as the journey.

        The richer recording wins — motion data first, then fix count — so the
        trip that is kept is the one that can be scored at all. trip_id breaks
        a tie, only so that repeated passes reach the same answer.
        """
        return (1 if (t[6] or 0) > 0 else 0, t[5] or 0, t[0])

    def _driver_for_user(self, user_id: str) -> Optional[str]:
        """
        The active driver linked to a recording presence user, if any.

        Returns None when nobody has claimed that phone, which leaves the trip
        unattributed rather than inventing a driver from the user_id — a
        leaderboard entry nobody created is worse than a missing one.
        """
        row = self._con.execute(
            "SELECT driver_id FROM drivers WHERE user_id = ? AND active "
            "ORDER BY created_at LIMIT 1",
            [user_id],
        ).fetchone()
        return row[0] if row else None

    def _behaviour(self, trip_id: str,
                   distance_m: Optional[float]) -> Dict[str, Any]:
        """
        Driving behaviour for one closing trip. Runs on the DB thread.

        Returns every key the UPDATE needs, with None wherever the phone gave
        us nothing to work with. None is load bearing here: a trip recorded by
        a phone without motion sensing must not be presented as one driven
        without a single harsh event.
        """
        counts = dict(self._con.execute(_EVENT_COUNT_SQL, [trip_id]).fetchall())
        (motion_fixes, roughness, max_lat, max_brake, max_accel,
         idle_s, stop_count, climb_baro, climb_gnss,
         descent_baro, descent_gnss) = \
            self._con.execute(_MOTION_SQL, [trip_id]).fetchone()

        measured = bool(motion_fixes)

        brake = counts.get("brake")
        accel = counts.get("accel")
        corner = counts.get("corner")
        # "harsh" predates the phone learning the forward axis: counts, unattributable.
        total = (sum(counts.values()) if counts else 0) if measured else None

        # MIN(long_peak) is negative or NULL; report the magnitude.
        brake_mag = abs(max_brake) if max_brake is not None and max_brake < 0 else None
        accel_mag = max_accel if max_accel is not None and max_accel > 0 else None

        # Barometer where available (metre resolution vs GNSS's tens); one source
        # for both directions, so climb and descent cannot disagree.
        use_baro = climb_baro is not None or descent_baro is not None
        climb = climb_baro if use_baro else climb_gnss
        descent = descent_baro if use_baro else descent_gnss

        return {
            "harsh_brake_count": brake if measured else None,
            "harsh_accel_count": accel if measured else None,
            "harsh_corner_count": corner if measured else None,
            "harsh_event_count": total,
            "max_brake_mps2": brake_mag,
            "max_accel_mps2": accel_mag,
            "max_lat_mps2": max_lat,
            "roughness_mps2": roughness,
            "idle_s": idle_s,
            "stop_count": stop_count,
            "climb_m": climb,
            "descent_m": descent,
            "motion_fix_count": motion_fixes or 0,
            "smoothness_score": self._score(total, distance_m) if measured else None,
        }

    @staticmethod
    def _score(events: Optional[int], distance_m: Optional[float]) -> Optional[float]:
        """
        Harsh events per 100 km, mapped to 0-100 by exponential decay.

        Exponential rather than a linear penalty because the interesting
        difference is at the smooth end: linear scoring compresses "one event"
        and "no events" into the same couple of points while letting a bad
        enough drive go negative and need clamping. Decay has neither problem —
        it is steepest exactly where the drives being compared usually sit, and
        approaches zero without ever reaching it.
        """
        if events is None or not distance_m or distance_m < MIN_SCORE_DISTANCE_M:
            return None
        per_100km = events / (distance_m / 100_000.0)
        return round(100.0 * math.exp(-per_100km / SCORE_DECAY_EVENTS_PER_100KM), 1)

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
        old = ("(SELECT trip_id FROM trips "
               " WHERE status = 'closed' AND ended_at < ?)")
        self._con.execute(f"DELETE FROM trip_fixes WHERE trip_id IN {old}", [cutoff])
        # Events go with the track; the counts are denormalised onto the trip row.
        self._con.execute(f"DELETE FROM trip_events WHERE trip_id IN {old}", [cutoff])

    _TRIP_COLS = ("trip_id", "user_id", "started_at", "ended_at", "status",
                  "fix_count", "distance_m", "duration_s", "avg_speed_mps",
                  "max_speed_mps", "min_speed_mps", "stddev_speed_mps",
                  "start_place", "end_place",
                  "harsh_brake_count", "harsh_accel_count",
                  "harsh_corner_count", "harsh_event_count",
                  "max_brake_mps2", "max_accel_mps2", "max_lat_mps2",
                  "roughness_mps2", "idle_s", "stop_count", "climb_m",
                  "descent_m", "smoothness_score", "motion_fix_count",
                  "driver_id", "attribution", "confidence", "car_bt_address",
                  "primary_trip_id")

    async def list_trips(self, user_id: Optional[str] = None,
                         limit: int = 50,
                         driver_id: Optional[str] = None,
                         include_duplicates: bool = False) -> List[Dict[str, Any]]:
        return await self._run(self._list_trips, user_id, limit, driver_id,
                               include_duplicates)

    def _list_trips(self, user_id, limit, driver_id,
                    include_duplicates) -> List[Dict[str, Any]]:
        sql = (f"SELECT {', '.join(self._TRIP_COLS)} FROM trips "
               "WHERE status = 'closed'")
        params: List[Any] = []
        # One row per physical journey by default; duplicates filtered, not deleted.
        if not include_duplicates:
            sql += " AND primary_trip_id IS NULL"
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        if driver_id:
            sql += " AND driver_id = ?"
            params.append(driver_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._con.execute(sql, params).fetchall()
        return [dict(zip(self._TRIP_COLS, r)) for r in rows]

    _DRIVER_COLS = ("driver_id", "name", "user_id", "colour", "active",
                    "created_at")

    async def list_drivers(self) -> List[Dict[str, Any]]:
        return await self._run(self._list_drivers)

    def _list_drivers(self) -> List[Dict[str, Any]]:
        rows = self._con.execute(
            f"SELECT {', '.join(self._DRIVER_COLS)} FROM drivers ORDER BY name"
        ).fetchall()
        return [dict(zip(self._DRIVER_COLS, r)) for r in rows]

    async def save_driver(self, driver_id: str, name: str,
                          user_id: Optional[str] = None,
                          colour: Optional[str] = None,
                          active: bool = True) -> Dict[str, Any]:
        return await self._run(self._save_driver, driver_id, name, user_id,
                               colour, active)

    def _save_driver(self, driver_id, name, user_id, colour,
                     active) -> Dict[str, Any]:
        self._con.execute(
            "INSERT INTO drivers (driver_id, name, user_id, colour, active, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (driver_id) DO UPDATE SET name = EXCLUDED.name, "
            "user_id = EXCLUDED.user_id, colour = EXCLUDED.colour, "
            "active = EXCLUDED.active",
            [driver_id, name, user_id, colour, bool(active), time.time()],
        )
        return {"driver_id": driver_id, "claimed": self._claim_history(driver_id, user_id)}

    def _claim_history(self, driver_id: str, user_id: Optional[str]) -> int:
        """
        Attribute a newly linked user's unclaimed trips to this driver.

        Without it a driver created today starts with an empty leaderboard row
        while months of their own trips sit unattributed. Only NULL driver_id
        rows are touched: linking a phone must never take a trip away from
        whoever is already credited with it.
        """
        if not user_id:
            return 0
        # Counted over journeys only — a duplicate reaches no aggregate.
        n = self._con.execute(
            "SELECT COUNT(*) FROM trips WHERE user_id = ? AND driver_id IS NULL "
            "AND primary_trip_id IS NULL",
            [user_id],
        ).fetchone()[0]
        self._con.execute(
            "UPDATE trips SET driver_id = ?, "
            # COALESCE: claiming a phone says who was in the car, not who was alone.
            "attribution = COALESCE(attribution, 'sole_phone'), "
            # 'medium': backfilled trips were never checked for a second phone.
            "confidence = COALESCE(confidence, 'medium') "
            "WHERE user_id = ? AND driver_id IS NULL",
            [driver_id, user_id],
        )
        return int(n)

    async def delete_driver(self, driver_id: str) -> bool:
        return await self._run(self._delete_driver, driver_id)

    def _delete_driver(self, driver_id) -> bool:
        found = self._con.execute(
            "SELECT 1 FROM drivers WHERE driver_id = ?", [driver_id]).fetchone()
        if not found:
            return False
        # Unattribute rather than cascade: the trips still happened.
        self._con.execute(
            "UPDATE trips SET driver_id = NULL, attribution = NULL, "
            "confidence = NULL WHERE driver_id = ?",
            [driver_id],
        )
        self._con.execute("DELETE FROM drivers WHERE driver_id = ?", [driver_id])
        return True

    async def set_trip_driver(self, trip_id: str,
                              driver_id: Optional[str]) -> bool:
        return await self._run(self._set_trip_driver, trip_id, driver_id)

    def _set_trip_driver(self, trip_id, driver_id) -> bool:
        if not self._con.execute(
                "SELECT 1 FROM trips WHERE trip_id = ?", [trip_id]).fetchone():
            return False
        if driver_id is not None and not self._con.execute(
                "SELECT 1 FROM drivers WHERE driver_id = ?", [driver_id]).fetchone():
            raise KeyError(driver_id)
        # 'manual'/'high' outranks every inference here.
        self._con.execute(
            "UPDATE trips SET driver_id = ?, attribution = ?, confidence = ? "
            "WHERE trip_id = ?",
            [driver_id,
             "manual" if driver_id else None,
             "high" if driver_id else None,
             trip_id],
        )
        return True

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

        # Who else recorded this drive — explains a low-confidence attribution.
        trip["also_recorded_by"] = [
            r[0] for r in self._con.execute(
                "SELECT user_id FROM trips WHERE primary_trip_id = ? ORDER BY user_id",
                [trip_id],
            ).fetchall()
        ]

        # Events carry no coordinates, so unlike the track they need no admin gate.
        evs = self._con.execute(
            "SELECT ts, kind, peak_mps2, duration_s FROM trip_events "
            "WHERE trip_id = ? ORDER BY ts",
            [trip_id],
        ).fetchall()
        trip["events"] = [
            {"ts": e[0], "kind": e[1], "peak_mps2": e[2], "duration_s": e[3]}
            for e in evs
        ]

        if include_track:
            pts = self._con.execute(_TRACK_SQL, [trip_id]).fetchall()
            trip["track"] = [
                {"ts": p[0], "lat": p[1], "lon": p[2], "speed_mps": p[3],
                 "bearing_deg": p[4], "accuracy_m": p[5], "altitude_m": p[6],
                 "long_peak_mps2": p[7], "lat_peak_mps2": p[8],
                 "horiz_peak_mps2": p[9], "vert_rms_mps2": p[10],
                 "jerk_peak_mps3": p[11], "yaw_peak_rads": p[12],
                 "activity": p[13], "gradient_pct": p[14]}
                for p in pts
            ]
        return trip

    # Per-driver aggregates. LEFT JOIN so a driver with no trips is a row of
    # nulls, not a missing name. Duplicates excluded as everywhere.
    _LEADERBOARD_SQL = """
    SELECT d.driver_id, d.name, d.colour, d.user_id, d.active,
           COUNT(t.trip_id),
           SUM(t.distance_m),
           SUM(t.duration_s),
           MAX(t.max_speed_mps),
           CASE WHEN SUM(t.duration_s) > 0
                THEN SUM(t.distance_m) / SUM(t.duration_s) END,
           COUNT(t.trip_id) FILTER (WHERE t.motion_fix_count > 0),
           SUM(t.distance_m) FILTER (WHERE t.smoothness_score IS NOT NULL),
           -- Distance-weighted, matching _user_stats.
           CASE WHEN SUM(t.distance_m) FILTER (WHERE t.smoothness_score IS NOT NULL) > 0
                THEN SUM(t.smoothness_score * t.distance_m)
                     / SUM(t.distance_m) FILTER (WHERE t.smoothness_score IS NOT NULL) END,
           SUM(t.harsh_event_count),
           SUM(t.harsh_brake_count), SUM(t.harsh_accel_count),
           SUM(t.harsh_corner_count),
           MAX(t.max_brake_mps2), MAX(t.max_lat_mps2),
           COUNT(t.trip_id) FILTER (WHERE t.confidence IN ('low', 'medium'))
    FROM drivers d
    LEFT JOIN trips t
           ON t.driver_id = d.driver_id
          AND t.status = 'closed'
          AND t.primary_trip_id IS NULL
    GROUP BY d.driver_id, d.name, d.colour, d.user_id, d.active
    """

    _LEADERBOARD_COLS = ("driver_id", "name", "colour", "user_id", "active",
                         "trip_count", "total_distance_m", "total_duration_s",
                         "top_speed_mps", "overall_avg_speed_mps",
                         "measured_trip_count", "measured_distance_m",
                         "smoothness_score", "harsh_event_count",
                         "harsh_brake_count", "harsh_accel_count",
                         "harsh_corner_count", "max_brake_mps2",
                         "max_lat_mps2", "unconfirmed_trip_count")

    async def leaderboard(self) -> Dict[str, Any]:
        return await self._run(self._leaderboard)

    def _leaderboard(self) -> Dict[str, Any]:
        rows = [dict(zip(self._LEADERBOARD_COLS, r))
                for r in self._con.execute(self._LEADERBOARD_SQL).fetchall()]

        for d in rows:
            if d["smoothness_score"] is not None:
                d["smoothness_score"] = round(d["smoothness_score"], 1)
            # The raw rate behind the score — checkable in a way the score is not.
            dist = d["measured_distance_m"]
            ev = d["harsh_event_count"]
            d["events_per_100km"] = (
                round(ev / (dist / 100_000.0), 1)
                if ev is not None and dist else None
            )
            d["qualified"] = bool(
                d["smoothness_score"] is not None
                and (d["measured_distance_m"] or 0) >= MIN_LEADERBOARD_DISTANCE_M
            )

        # Ranked first, best down; the rest by how close they are to qualifying.
        ranked = sorted((d for d in rows if d["qualified"]),
                        key=lambda d: -d["smoothness_score"])
        for i, d in enumerate(ranked, 1):
            d["rank"] = i
        unranked = sorted((d for d in rows if not d["qualified"]),
                          key=lambda d: -(d["measured_distance_m"] or 0))
        for d in unranked:
            d["rank"] = None

        unattributed = self._con.execute(
            "SELECT COUNT(*), SUM(distance_m) FROM trips "
            "WHERE status = 'closed' AND primary_trip_id IS NULL "
            "AND driver_id IS NULL"
        ).fetchone()

        return {
            "drivers": ranked + unranked,
            "min_distance_m": MIN_LEADERBOARD_DISTANCE_M,
            # So the UI can say how much driving sits outside the table.
            "unattributed_trip_count": unattributed[0] or 0,
            "unattributed_distance_m": unattributed[1] or 0.0,
        }

    async def user_stats(self, user_id: Optional[str] = None,
                         driver_id: Optional[str] = None) -> Dict[str, Any]:
        return await self._run(self._user_stats, user_id, driver_id)

    def _user_stats(self, user_id, driver_id=None) -> Dict[str, Any]:
        # Duplicates never reach an aggregate: one drive, counted once.
        where = "WHERE status = 'closed' AND primary_trip_id IS NULL"
        params: List[Any] = []
        if user_id:
            where += " AND user_id = ?"
            params.append(user_id)
        if driver_id:
            where += " AND driver_id = ?"
            params.append(driver_id)
        row = self._con.execute(
            "SELECT COUNT(*), SUM(distance_m), SUM(duration_s), "
            # Total distance over total time — a mean of means overweights short trips.
            "CASE WHEN SUM(duration_s) > 0 "
            "     THEN SUM(distance_m) / SUM(duration_s) END, "
            "MAX(max_speed_mps), "
            "quantile_cont(distance_m, 0.5), "
            # Trips without motion sensing contribute NULL and are skipped, not zeroed.
            "SUM(harsh_event_count), SUM(harsh_brake_count), "
            "SUM(harsh_accel_count), SUM(harsh_corner_count), "
            "MAX(max_brake_mps2), MAX(max_accel_mps2), MAX(max_lat_mps2), "
            # Distance-weighted, as with average speed.
            "CASE WHEN SUM(distance_m) FILTER (WHERE smoothness_score IS NOT NULL) > 0 "
            "     THEN SUM(smoothness_score * distance_m) "
            "          / SUM(distance_m) FILTER (WHERE smoothness_score IS NOT NULL) END, "
            "SUM(idle_s), SUM(climb_m), SUM(descent_m), "
            "COUNT(*) FILTER (WHERE motion_fix_count > 0) "
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
            "harsh_event_count": row[6],
            "harsh_brake_count": row[7],
            "harsh_accel_count": row[8],
            "harsh_corner_count": row[9],
            "max_brake_mps2": row[10],
            "max_accel_mps2": row[11],
            "max_lat_mps2": row[12],
            "smoothness_score": round(row[13], 1) if row[13] is not None else None,
            "total_idle_s": row[14],
            "total_climb_m": row[15],
            "total_descent_m": row[16],
            # So the UI can say "no data" rather than draw an empty panel.
            "measured_trip_count": row[17] or 0,
        }

    async def delete_trip(self, trip_id: str) -> bool:
        return await self._run(self._delete_trip, trip_id)

    def _delete_trip(self, trip_id) -> bool:
        found = self._con.execute(
            "SELECT 1 FROM trips WHERE trip_id = ?", [trip_id]).fetchone()
        self._con.execute("DELETE FROM trip_fixes WHERE trip_id = ?", [trip_id])
        self._con.execute("DELETE FROM trip_events WHERE trip_id = ?", [trip_id])
        self._con.execute("DELETE FROM trips WHERE trip_id = ?", [trip_id])
        return bool(found)



_manager: Optional[JourneyManager] = None


def get_journey_manager() -> Optional[JourneyManager]:
    return _manager


def set_journey_manager(mgr: JourneyManager) -> None:
    global _manager
    _manager = mgr
