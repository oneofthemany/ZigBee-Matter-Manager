"""
Per-group DuckDB stores for speaker-sync lag telemetry.

One database file per sync group (``./data/sync/<group>.duckdb``,
``adhoc.duckdb`` for sessions started from raw player ids) — deliberately
separate from telemetry.duckdb, whose write lock belongs to the
zmm_telemetry Rust appender. Each file is only ever opened by this process,
one cached connection per group, so DuckDB's single-writer model is
respected without touching the appender's file.

A device can belong to several groups; model training therefore reads
across ALL group files (per-group session aggregates in SQL, exact medians
merged in Python — medians can't be combined across files in SQL without
attaching them into one connection, which would fight the per-group locks).

Table ``lag_samples`` (one row per measurement, kinds: startup | poll |
resync | trim) is the raw history behind /api/media/sync/history and the
learned model in /api/media/sync/model.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from statistics import median
from typing import Any, Dict, List

logger = logging.getLogger("modules.media.sync_db")

DB_DIR = "./data/sync"
RETENTION_DAYS = 90

_cons: Dict[str, object] = {}          # gid -> duckdb connection
_lock = threading.Lock()


def _gid(group_id: str) -> str:
    """Filesystem-safe group id ('' -> adhoc)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", group_id or "adhoc")


def _get_con(group_id: str):
    """Cached connection to the group's DB (created + pruned on first open)."""
    gid = _gid(group_id)
    with _lock:
        con = _cons.get(gid)
        if con is not None:
            return con
        import duckdb
        os.makedirs(DB_DIR, exist_ok=True)
        con = duckdb.connect(os.path.join(DB_DIR, f"{gid}.duckdb"))
        con.execute("""
            CREATE TABLE IF NOT EXISTS lag_samples (
                ts           TIMESTAMP NOT NULL DEFAULT now(),
                session_id   VARCHAR NOT NULL,   -- one per sync start_session
                player_id    VARCHAR NOT NULL,   -- cast:<uuid>
                kind         VARCHAR NOT NULL,   -- startup | poll | resync | trim
                lag_s        DOUBLE,             -- measured lag incl. precomp
                error_ms     DOUBLE,             -- lag - session target
                rate_ppm     DOUBLE,             -- PLL rate at sample time
                trim_ms      INTEGER,
                precomp_s    DOUBLE,             -- model pre-compensation applied
                target_lag_s DOUBLE
            )
        """)
        con.execute(
            f"DELETE FROM lag_samples WHERE ts < now() - INTERVAL '{int(RETENTION_DAYS)} days'")
        _cons[gid] = con
        logger.info(f"Sync DB opened: {gid}.duckdb")
        return con


def write_samples(group_id: str, rows: List[Dict[str, Any]]):
    """Bulk append measurement rows to the group's DB. Rows may carry an
    explicit 'ts' (datetime) — omitted means now()."""
    if not rows:
        return
    con = _get_con(group_id)
    con.executemany("""
        INSERT INTO lag_samples (
            ts, session_id, player_id, kind,
            lag_s, error_ms, rate_ppm, trim_ms, precomp_s, target_lag_s
        ) VALUES (COALESCE(?, now()), ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (r.get("ts"), r["session_id"], r["player_id"], r["kind"],
         r.get("lag_s"), r.get("error_ms"), r.get("rate_ppm"),
         r.get("trim_ms"), r.get("precomp_s"), r.get("target_lag_s"))
        for r in rows
    ])


def _all_group_ids() -> List[str]:
    try:
        return [f[:-7] for f in os.listdir(DB_DIR) if f.endswith(".duckdb")]
    except FileNotFoundError:
        return []


def query_device_model(days: int = 30) -> Dict[str, Dict[str, Any]]:
    """Learned model per cast device, trained across EVERY group's history
    with robust aggregates (medians — one bad-WiFi session can't skew it):
      lag_s     — median startup lag across sessions, pre-compensation removed
      drift_ppm — median of each session's SETTLED PLL rate (arg_max by ts,
                  so early-session ramp-up polls don't dilute it)
    """
    days = int(days)
    lag_vals: Dict[str, List[float]] = {}
    ppm_vals: Dict[str, List[float]] = {}
    for gid in _all_group_ids():
        try:
            con = _get_con(gid)
            for pid, lag in con.execute(f"""
                SELECT player_id, lag_s - COALESCE(precomp_s, 0)
                FROM lag_samples
                WHERE kind = 'startup' AND lag_s IS NOT NULL
                  AND ts >= now() - INTERVAL '{days} days'
            """).fetchall():
                lag_vals.setdefault(pid, []).append(float(lag))
            for pid, ppm in con.execute(f"""
                SELECT player_id, arg_max(rate_ppm, ts) AS ppm
                FROM lag_samples
                WHERE kind = 'poll' AND rate_ppm IS NOT NULL
                  AND ts >= now() - INTERVAL '{days} days'
                GROUP BY player_id, session_id
            """).fetchall():
                if ppm is not None:
                    ppm_vals.setdefault(pid, []).append(float(ppm))
        except Exception as e:
            logger.warning(f"Sync model read failed for group {gid}: {e}")
    out: Dict[str, Dict[str, Any]] = {}
    for pid, lags in lag_vals.items():
        out[pid] = {"lag_s": median(lags),
                    "drift_ppm": median(ppm_vals[pid]) if ppm_vals.get(pid) else 0.0,
                    "sessions": len(lags)}
    return out


def query_history(group_id: str, hours: int = 24,
                  bucket_minutes: int = 0) -> List[Dict]:
    """Lag/hysteresis history for one group — raw rows by default,
    median-bucketed per player when bucket_minutes > 0."""
    con = _get_con(group_id)
    hours, bucket_minutes = int(hours), int(bucket_minutes)
    if bucket_minutes > 0:
        rows = con.execute(f"""
            SELECT time_bucket(INTERVAL '{bucket_minutes} minutes', ts) AS bucket,
                   player_id,
                   median(error_ms)     AS error_ms,
                   median(rate_ppm)     AS rate_ppm,
                   arg_max(trim_ms, ts) AS trim_ms,
                   count(*)             AS samples
            FROM lag_samples
            WHERE kind = 'poll' AND ts >= now() - INTERVAL '{hours} hours'
            GROUP BY bucket, player_id
            ORDER BY bucket ASC
        """).fetchall()
        cols = ["ts", "player_id", "error_ms", "rate_ppm", "trim_ms", "samples"]
    else:
        rows = con.execute(f"""
            SELECT ts, player_id, session_id, kind, error_ms, rate_ppm, trim_ms
            FROM lag_samples
            WHERE ts >= now() - INTERVAL '{hours} hours'
            ORDER BY ts ASC
        """).fetchall()
        cols = ["ts", "player_id", "session_id", "kind", "error_ms",
                "rate_ppm", "trim_ms"]
    return [dict(zip(cols, r)) for r in rows]


def query_sessions(group_id: str, days: int = 30) -> List[Dict]:
    """Session index for one group, newest first — powers the Sync Lab's
    session picker."""
    con = _get_con(group_id)
    rows = con.execute(f"""
        SELECT session_id,
               min(ts)                                   AS started,
               date_diff('second', min(ts), max(ts))     AS duration_s,
               count(DISTINCT player_id)                 AS players,
               count(*) FILTER (WHERE kind = 'poll')     AS polls,
               count(*) FILTER (WHERE kind = 'resync')   AS resyncs,
               count(*) FILTER (WHERE kind = 'trim')     AS trims,
               max(target_lag_s)                         AS target_lag_s
        FROM lag_samples
        WHERE ts >= now() - INTERVAL '{int(days)} days'
        GROUP BY session_id
        ORDER BY started DESC
    """).fetchall()
    cols = ["session_id", "started", "duration_s", "players", "polls",
            "resyncs", "trims", "target_lag_s"]
    return [dict(zip(cols, r)) for r in rows]


def query_session_detail(group_id: str, session_id: str) -> Dict[str, Any]:
    """Everything the Sync Lab needs for one session: the full measurement
    series (elapsed seconds from session start) and per-speaker summary
    stats. 'Settled' stats only count polls after the speaker first locked
    (|error| within the correction dead-band), so startup convergence
    doesn't pollute the steady-state numbers."""
    con = _get_con(group_id)
    series = con.execute("""
        WITH s AS (SELECT min(ts) AS t0 FROM lag_samples WHERE session_id = ?)
        SELECT round(date_diff('millisecond', s.t0, l.ts) / 1000.0, 1) AS elapsed_s,
               l.player_id, l.kind, l.error_ms, l.rate_ppm, l.trim_ms
        FROM lag_samples l CROSS JOIN s
        WHERE l.session_id = ?
        ORDER BY l.ts ASC
    """, [session_id, session_id]).fetchall()
    scols = ["elapsed_s", "player_id", "kind", "error_ms", "rate_ppm", "trim_ms"]

    summary = con.execute("""
        WITH s AS (SELECT min(ts) AS t0 FROM lag_samples WHERE session_id = ?),
        lock AS (
            SELECT player_id, min(ts) AS lock_ts
            FROM lag_samples
            WHERE session_id = ? AND kind = 'poll' AND abs(error_ms) <= 60
            GROUP BY player_id
        )
        SELECT l.player_id,
               any_value(l.lag_s)
                   FILTER (WHERE l.kind = 'startup')       AS startup_lag_s,
               any_value(l.precomp_s)                      AS precomp_s,
               round(min(date_diff('millisecond', s.t0, k.lock_ts)) / 1000.0, 1)
                                                           AS lock_s,
               median(abs(l.error_ms))
                   FILTER (WHERE l.kind = 'poll'
                           AND k.lock_ts IS NOT NULL
                           AND l.ts >= k.lock_ts)          AS settled_med_ms,
               quantile_cont(abs(l.error_ms), 0.95)
                   FILTER (WHERE l.kind = 'poll'
                           AND k.lock_ts IS NOT NULL
                           AND l.ts >= k.lock_ts)          AS settled_p95_ms,
               arg_max(l.rate_ppm, l.ts)
                   FILTER (WHERE l.kind = 'poll')          AS final_ppm,
               count(*) FILTER (WHERE l.kind = 'resync')   AS resyncs,
               count(*) FILTER (WHERE l.kind = 'trim')     AS trims,
               arg_max(l.trim_ms, l.ts)                    AS trim_ms
        FROM lag_samples l
        CROSS JOIN s
        LEFT JOIN lock k ON k.player_id = l.player_id
        WHERE l.session_id = ?
        GROUP BY l.player_id
        ORDER BY l.player_id
    """, [session_id, session_id, session_id]).fetchall()
    pcols = ["player_id", "startup_lag_s", "precomp_s", "lock_s",
             "settled_med_ms", "settled_p95_ms", "final_ppm",
             "resyncs", "trims", "trim_ms"]
    return {"session_id": session_id,
            "series": [dict(zip(scols, r)) for r in series],
            "players": [dict(zip(pcols, r)) for r in summary]}


def close_all():
    with _lock:
        for gid, con in _cons.items():
            try:
                con.close()
            except Exception:
                pass
        _cons.clear()
