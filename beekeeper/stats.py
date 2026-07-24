"""Query log + aggregates, backed by SQLite.

The DNS hot path must never block on disk, so :meth:`Stats.record` only bumps
in-memory counters and drops the row onto a bounded queue; a dedicated writer
thread batch-inserts into SQLite (WAL mode, so the control API can read while
the writer writes). If logging is disabled or the queue is full the counters
still update — the dashboard's headline numbers survive even without the log.

Read methods (summary/top/recent/series) open their own short-lived read
connections and are meant to be called off the event loop via ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("beekeeper.stats")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    client    TEXT    NOT NULL,
    qname     TEXT    NOT NULL,
    qtype     INTEGER NOT NULL,
    blocked   INTEGER NOT NULL,
    reason    TEXT,
    cached    INTEGER NOT NULL DEFAULT 0,
    upstream  TEXT,
    rcode     INTEGER,
    elapsed_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts);
CREATE INDEX IF NOT EXISTS idx_queries_blocked_ts ON queries(blocked, ts);
CREATE INDEX IF NOT EXISTS idx_queries_qname ON queries(qname);
"""

_SENTINEL = object()


class Stats:
    def __init__(self, db_path: Path, retention_days: int = 7,
                 enabled: bool = True, queue_max: int = 20000,
                 batch_size: int = 200):
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        self.enabled = enabled
        self.batch_size = batch_size
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=queue_max)
        self._writer: Optional[threading.Thread] = None
        self._stopping = False
        self._lock = threading.Lock()
        # Always-on counters (independent of the sqlite log).
        self._counters = {"total": 0, "blocked": 0, "cached": 0, "dropped": 0}
        self._started_at = time.time()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        if self.enabled and (self._writer is None or not self._writer.is_alive()):
            self._writer = threading.Thread(target=self._run_writer,
                                            name="beekeeper-stats", daemon=True)
            self._writer.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._writer and self._writer.is_alive():
            try:
                self._q.put_nowait(_SENTINEL)
            except queue.Full:
                pass
            self._writer.join(timeout)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ── write path ───────────────────────────────────────────────────────────
    def record(self, *, client: str, qname: str, qtype: int, blocked: bool,
               reason: Optional[str] = None, cached: bool = False,
               upstream: Optional[str] = None, rcode: Optional[int] = None,
               elapsed_ms: Optional[float] = None, ts: Optional[float] = None) -> None:
        self._counters["total"] += 1
        if blocked:
            self._counters["blocked"] += 1
        if cached:
            self._counters["cached"] += 1
        if not self.enabled:
            return
        row = (ts if ts is not None else time.time(), client, qname, int(qtype),
               1 if blocked else 0, reason, 1 if cached else 0, upstream, rcode,
               elapsed_ms)
        try:
            self._q.put_nowait(row)
        except queue.Full:
            # Backpressure: drop the log row (counters already updated) rather
            # than stall the resolver. Surfaced via counters["dropped"].
            self._counters["dropped"] += 1

    def _run_writer(self) -> None:
        conn = self._connect()
        last_prune = 0.0
        try:
            while True:
                batch = self._drain_batch()  # may set self._stopping if sentinel seen
                if batch:
                    try:
                        conn.executemany(
                            "INSERT INTO queries (ts,client,qname,qtype,blocked,"
                            "reason,cached,upstream,rcode,elapsed_ms) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                        conn.commit()
                    except sqlite3.Error as e:
                        logger.error("stats insert failed: %s", e)
                if self._stopping:           # flush done — now exit
                    break
                now = time.time()
                if now - last_prune > 3600:  # prune at most hourly
                    self._prune(conn)
                    last_prune = now
        finally:
            conn.close()

    def _drain_batch(self) -> List[tuple]:
        """Block for the first item, then greedily take up to batch_size more.

        Sets ``self._stopping`` (rather than returning a sentinel value) when the
        stop sentinel is seen, so the writer flushes the collected batch before
        exiting and never loses in-flight rows.
        """
        batch: List[tuple] = []
        try:
            first = self._q.get(timeout=1.0)
        except queue.Empty:
            return batch
        if first is _SENTINEL:
            self._stopping = True
            return batch
        batch.append(first)
        while len(batch) < self.batch_size:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                self._stopping = True
                break
            batch.append(item)
        return batch

    def _prune(self, conn: sqlite3.Connection) -> None:
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        try:
            conn.execute("DELETE FROM queries WHERE ts < ?", (cutoff,))
            conn.commit()
        except sqlite3.Error as e:
            logger.warning("stats prune failed: %s", e)

    # ── read path (call via asyncio.to_thread) ───────────────────────────────
    def counters(self) -> Dict[str, Any]:
        c = dict(self._counters)
        total = c["total"] or 1
        c["blocked_pct"] = round(100.0 * c["blocked"] / total, 1)
        c["uptime_s"] = round(time.time() - self._started_at, 1)
        return c

    def summary(self, window_hours: float = 24.0) -> Dict[str, Any]:
        since = time.time() - window_hours * 3600
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(blocked),0), COALESCE(SUM(cached),0), "
                "COUNT(DISTINCT client) FROM queries WHERE ts >= ?", (since,)).fetchone()
        finally:
            conn.close()
        total, blocked, cached, clients = row or (0, 0, 0, 0)
        return {
            "window_hours": window_hours,
            "total": total, "blocked": blocked, "cached": cached,
            "clients": clients,
            "blocked_pct": round(100.0 * blocked / total, 1) if total else 0.0,
        }

    def top_blocked(self, limit: int = 20, window_hours: float = 24.0) -> List[Dict]:
        since = time.time() - window_hours * 3600
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT qname, COUNT(*) c FROM queries WHERE blocked=1 AND ts >= ? "
                "GROUP BY qname ORDER BY c DESC LIMIT ?", (since, limit)).fetchall()
        finally:
            conn.close()
        return [{"qname": r[0], "count": r[1]} for r in rows]

    def top_clients(self, limit: int = 20, window_hours: float = 24.0) -> List[Dict]:
        since = time.time() - window_hours * 3600
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT client, COUNT(*) total, COALESCE(SUM(blocked),0) blocked "
                "FROM queries WHERE ts >= ? GROUP BY client ORDER BY total DESC "
                "LIMIT ?", (since, limit)).fetchall()
        finally:
            conn.close()
        return [{"client": r[0], "total": r[1], "blocked": r[2]} for r in rows]

    def recent(self, limit: int = 100) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ts, client, qname, qtype, blocked, reason, cached, rcode "
                "FROM queries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return [{"ts": r[0], "client": r[1], "qname": r[2], "qtype": r[3],
                 "blocked": bool(r[4]), "reason": r[5], "cached": bool(r[6]),
                 "rcode": r[7]} for r in rows]

    def series(self, window_hours: float = 24.0, buckets: int = 24) -> List[Dict]:
        """Queries per time-bucket split into blocked/allowed, for the chart."""
        now = time.time()
        since = now - window_hours * 3600
        width = (window_hours * 3600) / max(1, buckets)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT CAST((ts - ?) / ? AS INTEGER) b, "
                "COUNT(*) total, COALESCE(SUM(blocked),0) blocked "
                "FROM queries WHERE ts >= ? GROUP BY b ORDER BY b",
                (since, width, since)).fetchall()
        finally:
            conn.close()
        by_bucket = {r[0]: (r[1], r[2]) for r in rows}
        out = []
        for b in range(buckets):
            total, blocked = by_bucket.get(b, (0, 0))
            out.append({"start": since + b * width,
                        "total": total, "blocked": blocked,
                        "allowed": total - blocked})
        return out
