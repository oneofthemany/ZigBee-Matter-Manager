"""
Journeys — drive tracking from the companion app's drive mode.

While the phone is connected to the car's Bluetooth, DriveService streams
fixes tagged with a trip_id (plus GPS speed and bearing). This module stores
those fixes, segments them into trips, and computes per-trip statistics:
distance, duration, and average / max / min / standard deviation of speed.

Driving behaviour
    Each fix may also carry an inertial summary of the interval that ended at
    it, and any discrete events the phone detected within it — see the
    companion app's MotionSampler for how those are produced. GPS answers
    where and how fast; the accelerometer answers how, which is the part of a
    drive that position alone cannot show. From it a closed trip gains harsh
    braking / acceleration / cornering counts, peak longitudinal and lateral
    acceleration, road roughness, and a smoothness score.

    Everything inertial is optional at every level. A phone with no gyroscope
    reports no cornering; one with no barometer falls back to GNSS altitude
    for climb; one with no motion sensing at all still records a perfectly
    good journey, and the behaviour columns stay NULL rather than zero — the
    distinction between "smooth" and "not measured" has to survive into the
    UI, or a trip with no data reads as a perfect drive.

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
    deliberately does not. Raw track points and individual events are purged
    after TRACK_RETENTION_DAYS; the per-trip summary rows (no coordinates)
    are kept indefinitely.
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

#: Below this the car is stopped, not crawling (m/s; ~1.1 mph). GPS speed does
#: not settle to exactly zero, so "stationary" needs a floor rather than a
#: comparison with 0.
STOPPED_SPEED_MPS = 0.5

#: Idle time is only accrued across gaps no longer than this (s). A longer gap
#: is lost signal — a tunnel, a multi-storey car park — and counting it as
#: idling would invent minutes of stationary time that never happened.
MAX_IDLE_SEGMENT_S = 30.0

#: Metres of altitude per hPa near sea level. The barometric relation is
#: exponential, but over the few hundred metres a road covers the linear term
#: is accurate to well under a metre, and the phone's sensor noise is larger
#: than the error this discards.
METRES_PER_HPA = 8.3

#: Deadbands for climb. Both sensors are noisy in a way that summing only
#: positive deltas turns into phantom altitude gain — a flat motorway would
#: otherwise "climb" tens of metres an hour. A real gradient clears both
#: comfortably: 2% at 20 m/s is ~4 m (0.5 hPa) per fix interval.
CLIMB_DEADBAND_HPA = 0.1
CLIMB_DEADBAND_M = 3.0

#: Trips shorter than this get no smoothness score (m). One firm brake on a
#: 500 m drive is not a driving style, but per-distance normalisation would
#: score it as if it were.
MIN_SCORE_DISTANCE_M = 2000.0

#: Score decay constant, in harsh events per 100 km.
#:
#: Calibrated against what the phone's 3.5 m/s² threshold actually fires on:
#: a motorway run produces almost nothing, an ordinary urban drive one to
#: four events in ten kilometres, and hard city driving several times that.
#: At 100 that maps to 90 / 75 / 55 / 25 across the range, which keeps the
#: resolution where the drives being compared sit. A smaller constant looks
#: sharper but pushes everyday driving into the fifties, at which point the
#: number stops distinguishing anything.
#:
#: The score is a relative indicator of how smoothly a car was driven —
#: deliberately not an insurance-style risk rating, which would need speed
#: limits, road class and time of day this hub has no access to.
SCORE_DECAY_EVENTS_PER_100KM = 100.0

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
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS is a no-op
# on a database that already has the table, so a hub that recorded journeys
# before motion sensing existed would keep the old, narrower tables forever
# and every insert below would fail. These run on every open; ADD COLUMN IF
# NOT EXISTS makes that idempotent and costs nothing on an already-current
# database.
_MIGRATIONS = (
    # Per-fix inertial summary of the interval ending at that fix.
    ("trip_fixes", "altitude_m", "DOUBLE"),
    ("trip_fixes", "pressure_hpa", "DOUBLE"),
    ("trip_fixes", "long_peak_mps2", "DOUBLE"),
    ("trip_fixes", "lat_peak_mps2", "DOUBLE"),
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
    ("trips", "smoothness_score", "DOUBLE"),
    ("trips", "motion_fix_count", "BIGINT"),
)

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

# Driving behaviour for one trip, from the per-fix inertial summaries.
#
# Kept separate from _FINALIZE_SQL rather than bolted onto it: that query is
# about the track and this one about the sensors, they fail independently
# (a phone with no accelerometer produces all-NULL here and a perfectly good
# trip there), and one window function over ten columns is harder to read than
# two over five.
#
# Roughness is the mean of the per-window RMS values. Averaging RMS values is
# not the RMS of the whole trip, but the windows are equal-length by
# construction (one fix interval each), which makes the two equal to within
# the rounding the phone already applied.
_MOTION_SQL = f"""
WITH seq AS (
    SELECT ts, speed_mps, altitude_m, pressure_hpa,
           long_peak_mps2, lat_peak_mps2, vert_rms_mps2,
           LAG(ts)           OVER w AS pts,
           LAG(speed_mps)    OVER w AS pspeed,
           LAG(altitude_m)   OVER w AS palt,
           LAG(pressure_hpa) OVER w AS ppress
    FROM trip_fixes
    WHERE trip_id = ?
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
                THEN altitude_m - palt END)       AS climb_gnss_m
FROM seq
"""

_EVENT_COUNT_SQL = """
SELECT kind, COUNT(*) FROM trip_events WHERE trip_id = ? GROUP BY kind
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
        for table, column, coltype in _MIGRATIONS:
            self._con.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}"
            )
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
            altitude_m: Optional[float] = None,
            motion: Optional[Dict[str, Any]] = None,
            events: Optional[List[Dict[str, Any]]] = None,
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
                        motion or {}, events or [])

    def _record_fix(self, user_id, trip_id, lat, lon, ts,
                    speed_mps, bearing_deg, accuracy_m, altitude_m,
                    motion, events) -> None:
        self._con.execute(
            "INSERT INTO trips (trip_id, user_id, started_at) VALUES (?, ?, ?) "
            "ON CONFLICT (trip_id) DO NOTHING",
            [trip_id, user_id, ts],
        )
        # A fix arriving for a trip already marked closed means the closer
        # called it early: the phone went quiet for longer than
        # TRIP_CLOSE_GAP_S — a tunnel, a dead spot, a spell with no mobile
        # data — and then came back mid-drive. Without this the trip stays
        # closed forever, every later fix lands in trip_fixes where nothing
        # will ever aggregate it, and the summary is frozen on the handful of
        # fixes that beat the gap. That is a drive reported as three fixes and
        # zero miles while the rest of it sits in the table unread.
        #
        # Reopening is enough to repair it. Finalisation recomputes distance,
        # duration, speed, behaviour and endpoints from every fix the trip
        # has, so the next closer pass produces the same answer it would have
        # if the gap had never happened.
        self._con.execute(
            "UPDATE trips SET status = 'open' "
            "WHERE trip_id = ? AND status = 'closed'",
            [trip_id],
        )
        self._con.execute(
            "INSERT INTO trip_fixes "
            "(trip_id, user_id, ts, lat, lon, speed_mps, bearing_deg, accuracy_m, "
            " altitude_m, pressure_hpa, long_peak_mps2, lat_peak_mps2, "
            " vert_rms_mps2, jerk_peak_mps3, yaw_peak_rads) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [trip_id, user_id, ts, lat, lon, speed_mps, bearing_deg, accuracy_m,
             altitude_m,
             motion.get("pressure"), motion.get("long_peak"),
             motion.get("lat_peak"), motion.get("vert_rms"),
             motion.get("jerk_peak"), motion.get("yaw_peak")],
        )
        for e in events:
            # The phone timestamps events itself, from the same clock as the
            # fix, so a manoeuvre keeps its position within the interval rather
            # than being collapsed onto the fix that carried it.
            self._con.execute(
                "INSERT INTO trip_events "
                "(trip_id, user_id, ts, kind, peak_mps2, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [trip_id, user_id, e.get("t", ts), e.get("kind", "harsh"),
                 e.get("peak"), e.get("dur")],
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
                self._con.execute("DELETE FROM trip_events WHERE trip_id = ?", [trip_id])
                self._con.execute("DELETE FROM trips WHERE trip_id = ?", [trip_id])
                logger.info(f"[journeys] discarded blip trip {trip_id} ({fix_count} fixes)")
                continue
            _, distance_m, avg_v, max_v, min_v, sd_v, t0, t1 = row
            b = self._behaviour(trip_id, distance_m)
            self._con.execute(
                "UPDATE trips SET status = 'closed', fix_count = ?, "
                "distance_m = ?, duration_s = ?, avg_speed_mps = ?, "
                "max_speed_mps = ?, min_speed_mps = ?, stddev_speed_mps = ?, "
                "started_at = ?, ended_at = ?, "
                "harsh_brake_count = ?, harsh_accel_count = ?, "
                "harsh_corner_count = ?, harsh_event_count = ?, "
                "max_brake_mps2 = ?, max_accel_mps2 = ?, max_lat_mps2 = ?, "
                "roughness_mps2 = ?, idle_s = ?, stop_count = ?, climb_m = ?, "
                "smoothness_score = ?, motion_fix_count = ? "
                "WHERE trip_id = ?",
                [fix_count, distance_m, (t1 - t0) if t0 is not None else None,
                 avg_v, max_v, min_v, sd_v, t0, t1,
                 b["harsh_brake_count"], b["harsh_accel_count"],
                 b["harsh_corner_count"], b["harsh_event_count"],
                 b["max_brake_mps2"], b["max_accel_mps2"], b["max_lat_mps2"],
                 b["roughness_mps2"], b["idle_s"], b["stop_count"],
                 b["climb_m"], b["smoothness_score"], b["motion_fix_count"],
                 trip_id],
            )
            closed.append((trip_id, user_id))
            logger.info(
                f"[journeys] closed trip {trip_id} for {user_id}: "
                f"{(distance_m or 0) / 1000:.1f} km, {fix_count} fixes"
            )
        return closed

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
         idle_s, stop_count, climb_baro, climb_gnss) = \
            self._con.execute(_MOTION_SQL, [trip_id]).fetchone()

        measured = bool(motion_fixes)

        brake = counts.get("brake")
        accel = counts.get("accel")
        corner = counts.get("corner")
        # "harsh" is an event the phone detected before it had learned the
        # car's forward axis. It counts toward the total — it was a real
        # excursion — but cannot be attributed to braking or cornering.
        total = (sum(counts.values()) if counts else 0) if measured else None

        # max_brake comes out of MIN(long_peak), so it is negative or NULL.
        # Reported as the magnitude it is spoken about as ("braked at 4 m/s²").
        brake_mag = abs(max_brake) if max_brake is not None and max_brake < 0 else None
        accel_mag = max_accel if max_accel is not None and max_accel > 0 else None

        # Prefer the barometer: over the few hundred metres a road covers it
        # resolves a metre where GNSS altitude is good to tens of them. GNSS is
        # the fallback for phones without one, and is why climb is reported at
        # all rather than only for the subset of devices with a barometer.
        climb = climb_baro if climb_baro is not None else climb_gnss

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
        # Individual events go with the track rather than with the summary:
        # each one is a timestamped record of a moment, and the counts that
        # make them worth keeping are already denormalised onto the trip row.
        self._con.execute(f"DELETE FROM trip_events WHERE trip_id IN {old}", [cutoff])

    # ------------------------------------------------------------------
    # Queries (for the API)
    # ------------------------------------------------------------------
    _TRIP_COLS = ("trip_id", "user_id", "started_at", "ended_at", "status",
                  "fix_count", "distance_m", "duration_s", "avg_speed_mps",
                  "max_speed_mps", "min_speed_mps", "stddev_speed_mps",
                  "start_place", "end_place",
                  "harsh_brake_count", "harsh_accel_count",
                  "harsh_corner_count", "harsh_event_count",
                  "max_brake_mps2", "max_accel_mps2", "max_lat_mps2",
                  "roughness_mps2", "idle_s", "stop_count", "climb_m",
                  "smoothness_score", "motion_fix_count")

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

        # Events carry no coordinates, so unlike the track they are not behind
        # the admin gate: "braked hard four minutes in" says how someone drove,
        # which presence:read already sees in aggregate, not where they were.
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
            pts = self._con.execute(
                "SELECT ts, lat, lon, speed_mps, bearing_deg, accuracy_m, "
                "       altitude_m, long_peak_mps2, lat_peak_mps2, "
                "       vert_rms_mps2, jerk_peak_mps3, yaw_peak_rads "
                "FROM trip_fixes WHERE trip_id = ? ORDER BY ts",
                [trip_id],
            ).fetchall()
            trip["track"] = [
                {"ts": p[0], "lat": p[1], "lon": p[2], "speed_mps": p[3],
                 "bearing_deg": p[4], "accuracy_m": p[5], "altitude_m": p[6],
                 "long_peak_mps2": p[7], "lat_peak_mps2": p[8],
                 "vert_rms_mps2": p[9], "jerk_peak_mps3": p[10],
                 "yaw_peak_rads": p[11]}
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
            "quantile_cont(distance_m, 0.5), "
            # Behaviour across every trip that measured it. Trips recorded
            # without motion sensing contribute NULL and are skipped by these
            # aggregates rather than diluting them with zeroes.
            "SUM(harsh_event_count), SUM(harsh_brake_count), "
            "SUM(harsh_accel_count), SUM(harsh_corner_count), "
            "MAX(max_brake_mps2), MAX(max_accel_mps2), MAX(max_lat_mps2), "
            # Distance-weighted, for the same reason as average speed: a
            # two-mile trip's score should not weigh as much as a fifty-mile
            # one when describing how someone drives.
            "CASE WHEN SUM(distance_m) FILTER (WHERE smoothness_score IS NOT NULL) > 0 "
            "     THEN SUM(smoothness_score * distance_m) "
            "          / SUM(distance_m) FILTER (WHERE smoothness_score IS NOT NULL) END, "
            "SUM(idle_s), SUM(climb_m), "
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
            # How many of those trips had motion sensing at all, so the UI can
            # say "no data" instead of drawing an empty behaviour panel.
            "measured_trip_count": row[16] or 0,
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


# ---------------------------------------------------------------------------
# Singleton helper (same pattern as presence_users / places)
# ---------------------------------------------------------------------------

_manager: Optional[JourneyManager] = None


def get_journey_manager() -> Optional[JourneyManager]:
    return _manager


def set_journey_manager(mgr: JourneyManager) -> None:
    global _manager
    _manager = mgr
