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

Drivers:
    A trip is recorded by a phone, not by a person: user_id says whose phone
    was in the car, which is only the same thing as who was driving when the
    owner drove. Pooling every recording user's trips into one score, or
    crediting a passenger's phone with the driving, both produce a number that
    describes nobody. So attribution is a separate concept — a roster of
    drivers, one of whom owns each trip.

    A driver may be linked to a presence user, in which case that user's trips
    are attributed to them automatically at close; the link is optional so a
    household member who carries no tracked phone can still be scored on trips
    reassigned to them. driver_id stays NULL until someone claims the trip, and
    unattributed trips are counted separately rather than folded into whoever
    happened to be carrying the phone.

    Attribution is automatic where the evidence allows and records how it was
    decided, because a guess presented as a fact is worse than no attribution:
    `attribution` says which rule fired and `confidence` how far to trust it, so
    the UI can mark a trip as needing confirmation rather than silently
    crediting the wrong person. A manual assignment is always 'high' — someone
    who was there said so, which outranks every inference here.

Co-presence:
    trip_id is minted on the phone, so two journey-enabled phones in one car
    record the same physical drive as two unrelated trips. Left alone that
    double-counts distance and averages one drive into the aggregate twice.
    A pass over recently-closed trips pairs them up by time overlap and
    endpoints, keeps one as the journey, and points the other at it through
    primary_trip_id — duplicates stay in the table (they are that phone's own
    record) but are excluded from every aggregate and from the trip list.

    Which of the two occupants was driving is not knowable from anything the
    hub can see, so a collapsed pair is attributed at 'low' confidence and
    flagged for confirmation rather than guessed at silently.

    car_bt_address identifies the vehicle and never the driver. A household
    with one car has every driver sharing that address, so the two facts are
    independent by construction: the same car appears under every name on the
    leaderboard, and a trip is attributed by who recorded it, not by what they
    drove. The column is here for per-vehicle reporting and to key a future
    learned prior — which must be conditioned on time and occupancy as well,
    never on the car alone.

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

#: Below this speed (m/s; ~11 mph) no gradient is reported. Gradient is a
#: vertical rate divided by a horizontal one, so the divisor shrinking towards
#: zero turns barometer noise into a cliff face. Crawling traffic gets NULL,
#: which the UI can show as unknown; it cannot show a number as wrong.
MIN_GRADIENT_SPEED_MPS = 5.0

#: Gradient is only computed across gaps no longer than this (s). Wider than a
#: fix interval to survive a dropped fix, short enough that the two pressures
#: still describe one stretch of road.
MAX_GRADIENT_GAP_S = 30.0

#: Steeper than this (%) is not a public road — the steepest in the country are
#: around a third, and a motorway is under 4. This catches gross artefacts only.
#: The barometer sits in the cabin, so a window or the HVAC opening steps the
#: pressure in a way no hill does, and a step small enough to land inside this
#: range still reads as a hill for one fix interval. A sustained gradient is
#: the trustworthy signal here; a single steep sample is not.
MAX_PLAUSIBLE_GRADIENT_PCT = 25.0

#: Activities that are certainly not the car moving. Fixes carrying one are
#: kept in the table but excluded from every aggregate and from the drawn
#: track — the walk from a parking space is otherwise distance the driver is
#: credited with, and the drift while sat in a parked car is a journey that
#: never happened.
#:
#: "still" is deliberately absent: a car at a red light reports it, and
#: dropping those fixes would delete the idling and stops being measured.
#: Anything unrecognised, and a NULL from a phone that cannot report at all,
#: counts — the fix is trusted unless the phone actively says otherwise.
NON_VEHICLE_ACTIVITIES = ("walking", "running", "on_foot", "on_bicycle")

_VEHICLE_FILTER = "(activity IS NULL OR activity NOT IN ({}))".format(
    ", ".join(f"'{a}'" for a in NON_VEHICLE_ACTIVITIES)
)

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

#: Measured distance a driver must have covered before they are ranked (m;
#: ~25 miles). The per-trip score is already distance-normalised, so a single
#: clean three-mile run scores as well as a careful month of commuting and
#: would take first place on a leaderboard that ranked everyone. Drivers below
#: this are listed but unranked — held back rather than hidden, because "not
#: enough data yet" is the honest reading and a missing name looks like a bug.
MIN_LEADERBOARD_DISTANCE_M = 40000.0

#: How far back the co-presence pass looks (s). Both phones disconnect from the
#: car within seconds of each other, so their trips close on the same pass or
#: the next one; six hours is slack for a hub that was down for the afternoon,
#: while keeping the pairwise comparison over a handful of rows.
COPRESENCE_LOOKBACK_S = 6 * 3600

#: Fraction of the shorter trip that must overlap in time before two trips can
#: be the same drive. One car cannot carry two people along different roads,
#: so a genuine pair overlaps almost entirely; the slack is for the phones
#: starting and stopping their fixes at slightly different moments.
COPRESENCE_MIN_OVERLAP = 0.6

#: How close two trips' start points, and their end points, must be to be the
#: same drive (m). Wide enough for two phones acquiring GPS at different
#: moments as the car pulls away, tight enough that two cars leaving the same
#: house for different places do not pair.
COPRESENCE_ENDPOINT_M = 500.0

#: How far two trips' distances may differ and still be one drive (fraction).
#: The same road measured by two phones differs by a few percent through fix
#: timing alone; a quarter is generous and only rules out gross mismatches.
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
    -- The presence user whose trips are attributed here by default. NULL for a
    -- driver who carries no tracked phone, who is scored only on trips
    -- reassigned to them by hand. Note that _open splits this schema on
    -- semicolons, so no comment here may contain one.
    user_id    TEXT,
    colour     TEXT,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DOUBLE  NOT NULL
);
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
    # Unsigned horizontal magnitude; unlike the two above, not gated on the
    # phone's forward axis, so populated for the whole of a drive.
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
    # Descent as a positive magnitude. Separate from climb rather than netted
    # into it: a round trip nets to zero, which says nothing about the road.
    ("trips", "descent_m", "DOUBLE"),
    ("trips", "smoothness_score", "DOUBLE"),
    ("trips", "motion_fix_count", "BIGINT"),
    # Attribution. driver_id is who drove; the other two say how confidently
    # that was decided, so the UI can ask rather than assert. NULL driver_id
    # means nobody has claimed the trip — never "the phone's owner by default",
    # which is the assumption this whole column exists to stop making.
    ("trips", "driver_id", "TEXT"),
    ("trips", "attribution", "TEXT"),
    ("trips", "confidence", "TEXT"),
    # Which vehicle. Filled by the companion app once it sends the address it
    # already matches on; NULL on every trip recorded before then.
    ("trips", "car_bt_address", "TEXT"),
    # Set on the redundant copy when two phones recorded one drive; points at
    # the trip kept as the journey. NULL on a trip that is itself a journey.
    ("trips", "primary_trip_id", "TEXT"),
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
       -- Descent, same deadbands with the comparisons reversed. Summed as a
       -- positive magnitude: "220 m of descent" is how it is spoken about.
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

# The track, with a signed road gradient per fix.
#
# Gradient is a rate over a rate — metres climbed per metre travelled — so it
# needs no phone orientation at all. That is the whole reason it is derived
# this way: gravity cannot separate a cradle tilted 20 degrees from a hill of
# 20 degrees, and asking the driver to mount the phone a particular way would
# trade away the one property that makes the rest of this work anywhere.
#
# Barometric only. GNSS altitude is good to tens of metres, which over the few
# hundred metres between two fixes is larger than the height change being
# measured; a gradient from it would be noise with a plausible unit attached.
# Phones without a barometer get NULL, and the UI says unknown.
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
                -- Pressure FALLS as the car climbs, hence the reversed
                -- subtraction. Vertical metres over horizontal metres, as a
                -- percentage; speed is GPS Doppler, so the divisor is measured
                -- rather than derived from the two positions.
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
                    # After closing, not before: a pair is only detectable once
                    # both phones' trips have distance and endpoints, and the
                    # second one may not have closed until this same pass.
                    await self._run(self._collapse_copresent)
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
                # COALESCE, not assignment: a trip reopened by a late fix (see
                # _record_fix) comes back through here, and refinalising it must
                # not discard an attribution someone made by hand in between.
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
                 # High until the co-presence pass finds another phone in the
                 # car, which is the only thing that can undermine it: one
                 # phone recording means one person known to have been there.
                 "high" if driver_id else None,
                 trip_id],
            )
            closed.append((trip_id, user_id))
            logger.info(
                f"[journeys] closed trip {trip_id} for {user_id}: "
                f"{(distance_m or 0) / 1000:.1f} km, {fix_count} fixes"
            )
        return closed

    # Recently-closed journeys with their endpoints, for co-presence pairing.
    # Only trips that are still journeys in their own right are considered:
    # once a trip has been pointed at a primary it is out of the running, which
    # is what keeps repeated passes from re-pairing a settled group.
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

        # Trips that already have duplicates pointing at them. A settled group
        # must keep the primary it has: if a third phone's trip closes later
        # and outranks the current primary, promoting it would leave the old
        # primary's duplicates pointing at a trip that is no longer a journey.
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
            # One journey per group, chosen once — pairwise marking would let
            # three phones in one car chain A→B→C, and A would resolve to a
            # trip that is not itself a journey.
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
            # Several people were in the car and nothing the hub can see says
            # which one drove, so the surviving journey stops claiming to know.
            # A manual assignment is left alone: someone who was there has
            # already answered the question this is asking.
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

        # The same phone cannot be a passenger in its own car. Two trips from
        # one user that overlap are a recording fault, not two occupants, and
        # collapsing them would hide it.
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

        # Endpoints and timing agreeing while the distances do not means the
        # two phones did not travel the same roads between them.
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
        # Both directions take the same source, so a trip cannot report
        # barometric climb against GNSS descent and appear to gain height it
        # never lost.
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
        # One row per physical journey by default. The duplicate is still that
        # phone's own record of the drive, so it is filtered rather than
        # deleted, and reachable by asking for it.
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

    # ------------------------------------------------------------------
    # Drivers
    # ------------------------------------------------------------------
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
        # Counted over journeys only. A collapsed duplicate is claimed too —
        # it is still that phone's record — but it reaches no aggregate, so
        # reporting it as a claimed journey would promise history that never
        # appears in the driver's totals.
        n = self._con.execute(
            "SELECT COUNT(*) FROM trips WHERE user_id = ? AND driver_id IS NULL "
            "AND primary_trip_id IS NULL",
            [user_id],
        ).fetchone()[0]
        self._con.execute(
            "UPDATE trips SET driver_id = ?, "
            # COALESCE, so a trip the co-presence pass already labelled keeps
            # that label. Claiming a phone says who was in the car, not that
            # they were alone in it — overwriting would turn the more accurate
            # verdict into the less accurate one and raise its confidence.
            "attribution = COALESCE(attribution, 'sole_phone'), "
            # Backfill is an inference over history rather than an observation
            # of it: trips older than COPRESENCE_LOOKBACK_S were never checked
            # for a second phone, so 'high' would be claiming more than is known.
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
        # Unattribute rather than cascade: the trips happened, and deleting a
        # driver is a statement about the roster, not about the history.
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
        # 'manual' at 'high': someone who was in the car has answered the
        # question, which outranks every inference this module can make — and
        # is why the co-presence pass leaves manual attributions alone.
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

        # Who else's phone recorded this same drive. The reason a journey is
        # attributed at low confidence is usually right here, so the detail
        # panel can explain the flag instead of just showing it.
        trip["also_recorded_by"] = [
            r[0] for r in self._con.execute(
                "SELECT user_id FROM trips WHERE primary_trip_id = ? ORDER BY user_id",
                [trip_id],
            ).fetchall()
        ]

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

    # Per-driver aggregates. Deliberately a LEFT JOIN from drivers: a driver
    # with no trips yet is a row of nulls on the leaderboard, not a missing
    # name, because "registered but hasn't driven" is a state worth showing.
    #
    # Duplicates are excluded here as everywhere — the whole point of
    # collapsing them is that one drive counts once, for one driver.
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
           -- Distance-weighted, matching the single-driver figure in
           -- _user_stats: a two-mile trip's score must not weigh as much as a
           -- fifty-mile one when ranking how someone drives.
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
            # Harsh events per 100 km — the raw rate behind the score, shown
            # alongside it because "3 events in 60 miles" is checkable in a way
            # that a 0-100 number is not.
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

        # Ranked drivers first, best score down; everyone else after, ordered
        # by how close they are to qualifying rather than by an unearned score.
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
            # Surfaced so the UI can say how much history is sitting outside
            # the table; a leaderboard that quietly omits half the driving
            # invites more trust than it has earned.
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
            # How many of those trips had motion sensing at all, so the UI can
            # say "no data" instead of drawing an empty behaviour panel.
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


# ---------------------------------------------------------------------------
# Singleton helper (same pattern as presence_users / places)
# ---------------------------------------------------------------------------

_manager: Optional[JourneyManager] = None


def get_journey_manager() -> Optional[JourneyManager]:
    return _manager


def set_journey_manager(mgr: JourneyManager) -> None:
    global _manager
    _manager = mgr
