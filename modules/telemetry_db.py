"""
DuckDB-backed time-series storage — system metrics, packet stats, device states
and spectrum scans, with per-table retention.

Chosen over SQLite for columnar aggregation, ZSTD compression, non-blocking
reads and time-bucket functions. Octopus and host metrics live in separate
files; see docs/telemetry_database.md for why, and for the fatal-state latch,
backend reconciliation and rebuild paths.
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.telemetry_db")

DB_PATH = "./data/telemetry.duckdb"
DEFAULT_RETENTION_DAYS = 90
# Its own file: it write-collides with the high-frequency telemetry writers,
# and a corrupted telemetry.duckdb must not take a year of energy history with
# it. Retention is long and user-configurable. See docs/telemetry_database.md.
OCTOPUS_DB_PATH = "./data/octopus.duckdb"
OCTOPUS_RETENTION_DAYS = 400

# Its own file too: one damaged block makes a whole DuckDB uncheckpointable,
# and host samples are the least valuable rows here — they must not be able to
# take device history down with them. See docs/telemetry_database.md.
SYSTEM_DB_PATH = "./data/system_metrics.duckdb"

# Lazy import — duckdb is only needed when this module is used
_db = None


# Backend precedence: ZMM_TELEMETRY_BACKEND=python forces the Python fallback,
# else the zmm_telemetry wheel if installed, else Python. Schema is identical,
# so switching needs no rebuild. See docs/telemetry_database.md.
_FORCE_PY = os.environ.get("ZMM_TELEMETRY_BACKEND", "").strip().lower() == "python"
try:
    if _FORCE_PY:
        raise ImportError("ZMM_TELEMETRY_BACKEND=python — forcing Python fallback")
    import zmm_telemetry as _zt
    _USE_RUST = True
except ImportError as _imp_err:
    _zt = None
    _USE_RUST = False
    if _FORCE_PY:
        logger.info("zmm_telemetry disabled by ZMM_TELEMETRY_BACKEND=python — using Python executemany fallback")
    else:
        logger.info("zmm_telemetry not available — using Python executemany fallback")

_appender = None  # zmm_telemetry.Appender singleton

# Reentrant: _get_db() -> _finish_db_init() may re-enter _get_db(). One shared
# connection per file for reads and writes.
_db_lock = threading.RLock()


def _is_unreplayable_wal_error(err: Exception) -> bool:
    """True for DuckDB's "can't replay this WAL" open failures.

    DuckDB words this two ways and both must be caught:
      - Failure while replaying WAL file "..."
      - Failure while replaying checkpoint WAL file "...": checkpoint WAL
        cannot contain a checkpoint marker

    The second form is what a WAL written by the *other* write backend looks
    like (see the appender/executemany note on _reconcile_worker). Matching
    only the first left it unhandled, so the open raised instead of healing —
    which is what turned a recoverable WAL into a wedged DB, a failed warm(),
    and a loop stall the watchdog answered with an exit-70 restart loop.
    """
    msg = str(err).lower()
    return "replaying" in msg and "wal" in msg


def _connect_local_db(path: str):
    """`duckdb.connect` for a local DB file, healing the two failure modes a
    bind-mounted / concurrently-written DuckDB hits in the container:
      1. 0-byte stub — the container runtime materialises a bind-mount target
         as an empty file, which DuckDB refuses to open. Replace it in place
         with a valid empty database.
      2. Unreplayable WAL — a killed/again-opened writer can leave a WAL DuckDB
         can't replay ('Failure while replaying WAL file …'), which then wedges
         every telemetry request. The main DB file is intact, so move the WAL
         aside and reconnect (the WAL held only uncommitted writes).
    """
    import duckdb
    # (1) heal a 0-byte stub before trying to open it
    try:
        if os.path.exists(path) and os.path.getsize(path) == 0:
            wal = path + ".wal"
            try:
                os.remove(path)                     # plain file — just drop it
            except OSError:
                import tempfile
                import shutil
                fd, tmp = tempfile.mkstemp(suffix=".duckdb")
                os.close(fd)
                os.remove(tmp)
                duckdb.connect(tmp).close()         # a valid empty database
                shutil.copyfile(tmp, path)          # overwrite contents in place
                os.remove(tmp)
            try:
                if os.path.exists(wal):
                    os.remove(wal)
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"could not heal possibly-empty DB {path}: {e}")

    # (2) open, recovering from an unreplayable WAL
    try:
        return duckdb.connect(path)
    except Exception as e:
        wal = path + ".wal"
        if _is_unreplayable_wal_error(e) and os.path.exists(wal):
            # "unreplayable", not "corrupt" — usually a backend switch left a WAL
            # the incoming engine will not accept. Quarantined, not deleted: it may
            # hold the only copy of rows that never reached the main file.
            bak = f"{wal}.unreplayable-{int(time.time())}"
            try:
                size = os.path.getsize(wal)
            except OSError:
                size = 0
            try:
                os.rename(wal, bak)
            except OSError:
                raise
            logger.warning(
                f"{path}: WAL could not be replayed ({e}). "
                f"Quarantined to {bak} ({size} bytes) and reconnecting. "
                f"The reconciler will report this."
            )
            _quarantined_wals.append({"db": path, "wal": bak, "bytes": size,
                                      "error": str(e)})
            return duckdb.connect(path)
        raise


def _get_db():
    """Get or create the single shared DuckDB connection (lazy singleton).

    This one connection is the *sole* engine touching telemetry.duckdb in this
    process — writes go through it (under _db_lock) and reads run on short-lived
    cursors off it (see read_cursor()). Never open a second engine (e.g. an
    in-process Rust appender) on this file: POSIX fcntl locks are per-PID, so a
    second in-process DuckDB instance is NOT blocked and you get two split-brain
    views of the same file. See _finish_db_init().
    """
    global _db
    if _db is None:
        with _db_lock:
            if _db is not None:
                return _db
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            db = _connect_local_db(DB_PATH)
            _init_tables(db)
            _db = db
            _finish_db_init()
    return _db


def read_cursor():
    """Short-lived READ cursor on the shared connection. DuckDB allows
    concurrent reads across cursors of one connection, so readers using this
    don't serialise behind _db_lock (the lock is only held to init/fetch the
    connection). Writers and DDL keep using _db_lock + _get_db() directly.

    Use as a context manager:

        with read_cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
    """
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        with _db_lock:
            conn = _get_db()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:
                pass
    return _cm()


def _rcursor():
    """Fresh READ cursor on the shared connection, created under the brief init
    lock. Non-context-manager form of read_cursor() for one-line reader swaps
    (`db = _rcursor()`); the short-lived cursor is closed by GC when the query
    function returns. Each caller gets its own handle, so readers never share
    the base connection across threads.
    """
    with _db_lock:
        return _get_db().cursor()


def _write_exec(sql: str, params: List[tuple]):
    """Write via a per-call cursor on the shared connection.

    Every telemetry write lands here — including calls made ON THE EVENT-LOOP
    THREAD (device/packet/metric writes from zigpy callbacks). So the lock is
    held ONLY long enough to create a fresh cursor; the executemany itself runs
    OUTSIDE the lock. DuckDB serialises concurrent writes from separate cursors
    internally (verified: parallel INSERTs + an overlapping DELETE → no errors,
    no corruption), so we don't need to hold _db_lock across the write — and
    must not, or a slow writer (a big snapshot batch, prune's multi-second
    DELETEs, or the one-time WAL replay at open) would block every loop-thread
    write behind it and stall the event loop. Each caller gets its own cursor,
    so no handle is shared across threads.
    """
    if not params:
        return
    cur = None
    try:
        # Cursor creation is INSIDE the try: on an invalidated database .cursor()
        # is what raises, not execute. Outside, every write after the first fatal
        # escaped the latch and the next boot never self-repaired.
        with _db_lock:
            cur = _get_db().cursor()      # brief: init + cursor creation only
        cur.executemany(sql, params)      # outside the lock — DuckDB serialises
    except Exception as e:
        _note_fatal(e)
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass


# Fatal-state latch. Once DuckDB reports "database has been invalidated" every
# statement raises, so without this the log fills with identical lines. Latches
# the first, reports it once, lets callers skip impossible work.

_db_fatal: Optional[str] = None

# Dropped when the DB goes terminal, read next boot by auto_rebuild_if_needed().
# A corrupt DuckDB cannot be rebuilt from inside the process stuck on it.
REBUILD_SENTINEL = "./data/.telemetry_rebuild_needed"


def is_fatal() -> bool:
    """True once the database has entered an unusable state (until restart)."""
    return _db_fatal is not None


def fatal_reason() -> Optional[str]:
    return _db_fatal


def _note_fatal(err: Exception) -> bool:
    """Latch a terminal DuckDB error. True if this call did the latching."""
    global _db_fatal
    msg = str(err)
    terminal = ("has been invalidated" in msg
                or "must be restarted prior to being used" in msg
                or "Corrupt database file" in msg)
    if not terminal or _db_fatal is not None:
        return False

    _db_fatal = msg
    logger.error(
        f"Telemetry database is unusable and will stay that way until the app "
        f"restarts: {msg}"
    )

    # Flag it for the next boot to rebuild, while nothing holds the file open.
    try:
        os.makedirs(os.path.dirname(REBUILD_SENTINEL), exist_ok=True)
        with open(REBUILD_SENTINEL, "w") as fh:
            fh.write(msg)
    except OSError as e:
        logger.debug(f"Could not write rebuild sentinel: {e}")
    try:
        from modules.app_alerts import raise_alert
        raise_alert(
            severity="error",
            source="telemetry_db",
            title="Telemetry database unusable — rebuild required",
            message=(
                f"{msg}\n\n"
                "DuckDB has invalidated the database, so history and trends "
                "will not record until the app restarts. Everything else — "
                "devices, automations, heating control — is unaffected.\n\n"
                "This has been flagged for automatic repair: on the next "
                "restart the database will be rebuilt from whatever is still "
                "readable, keeping the damaged file alongside it. No action is "
                "needed beyond restarting when convenient."
            ),
            dedupe_key="telemetry_db:fatal",
        )
    except Exception as e:
        logger.debug(f"Could not raise telemetry fatal alert: {e}")
    return True


def warm():
    """Open + migrate the DB (seconds on first touch after boot).

    Call via asyncio.to_thread early in startup, before any service runs:
    whichever thread first calls _get_db() holds _db_lock for the whole
    open/migration, and every other caller — including loop-thread appender
    writes — queues behind it and stalls the event loop.

    NOTE: this fails open. If it raises, `_db` is left unset and the *next*
    caller pays the whole open — so callers must surface the failure (main.py
    raises an app alert) and must not assume the DB is ready afterwards.
    """
    _get_db()


# The write backend can change under a database already on disk, so the engine
# that wrote the WAL may not be the one replaying it. Reconciliation runs on its
# own thread — every step is slow and none may reach the event loop.

_BACKEND_MARKER = "./data/.telemetry_backend"

# Quarantine records appended by _connect_local_db, consumed by the reconciler.
_quarantined_wals: List[Dict[str, Any]] = []
_reconciler_thread: Optional[threading.Thread] = None


def _current_backend() -> str:
    """Which engine actually writes — call only after _get_db().

    Keyed on the live `_appender`, not on `_USE_RUST`: the wheel being
    importable says nothing about who writes, because _finish_db_init() may
    decline to open the appender in-process (it currently always does, to
    avoid split-braining the file). WAL compatibility is decided by the
    engine that did the writing, so that is what gets recorded.
    """
    return "rust-appender" if _appender is not None else "python-executemany"


def _read_backend_marker() -> Optional[str]:
    try:
        with open(_BACKEND_MARKER) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _write_backend_marker(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(_BACKEND_MARKER), exist_ok=True)
        with open(_BACKEND_MARKER, "w") as fh:
            fh.write(name)
    except OSError as e:
        logger.debug(f"Could not persist telemetry backend marker: {e}")


# DO NOT add an explicit CHECKPOINT here. DuckDB checkpoints on its own when it
# can; an unbounded WAL means the database needs rebuilding, not that the
# failing operation should be forced. See docs/telemetry_database.md.


# Quarantined files are kept on purpose — they may hold rows the live file no
# longer has — but they are large, so they expire rather than persist forever.
QUARANTINE_RETENTION_DAYS = float(
    os.environ.get("ZMM_QUARANTINE_RETENTION_DAYS", "7"))


def cleanup_quarantined(retention_days: Optional[float] = None,
                        dry_run: bool = False) -> Dict[str, Any]:
    """Remove quarantine artifacts once the live database has proven itself.

    Refuses to run while the database is in a fatal state: if the live file is
    broken, a quarantined copy may well be the better one, and deleting it would
    throw away the only remaining route to that data.

    Age is the proof. A file still inside the retention window is left alone, so
    a WAL quarantined during this very boot is never swept by the same boot.
    """
    import glob

    days = QUARANTINE_RETENTION_DAYS if retention_days is None else retention_days
    result: Dict[str, Any] = {"removed": [], "kept": 0, "bytes_freed": 0,
                              "skipped_reason": None}

    if is_fatal():
        result["skipped_reason"] = "database is in a fatal state"
        logger.info("Quarantine cleanup skipped — the database is unusable, so "
                    "the quarantined copies may be better than the live one")
        return result

    cutoff = time.time() - days * 86400
    patterns = []
    for base in (DB_PATH, SYSTEM_DB_PATH, OCTOPUS_DB_PATH):
        patterns += [
            f"{base}.wal.unreplayable-*",   # WAL the engine would not replay
            f"{base}.wal.corrupt-*",        # same thing under its former name
            f"{base}.damaged-*",            # pre-rebuild copy (and its .wal)
        ]

    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime > cutoff:
                result["kept"] += 1
                continue
            size = st.st_size
            if not dry_run:
                try:
                    os.remove(path)
                except OSError as e:
                    logger.warning(f"Could not remove {path}: {e}")
                    continue
            result["removed"].append(path)
            result["bytes_freed"] += size

    if result["removed"]:
        verb = "would remove" if dry_run else "removed"
        logger.info(
            f"Quarantine cleanup: {verb} {len(result['removed'])} file(s), "
            f"{result['bytes_freed'] / (1024 * 1024):.1f} MB "
            f"(older than {days:g} days); {result['kept']} still within retention")
    return result


def _reconcile_worker() -> None:
    """Body of the reconciler thread. Never raises."""
    try:
        previous = _read_backend_marker()

        # Open before reading the backend: which engine writes is only settled
        # once the connection is up. Slow, but this thread is the only waiter.
        _get_db()

        backend = _current_backend()
        switched = previous is not None and previous != backend

        if switched:
            logger.warning(
                f"Telemetry write backend changed: {previous} → {backend}. "
                f"Reconciling {DB_PATH}."
            )

        _write_backend_marker(backend)

        # The database opened and is usable, which is the proof this waits for.
        try:
            cleanup_quarantined()
        except Exception as e:
            logger.debug(f"Quarantine cleanup failed: {e}")

        quarantined = list(_quarantined_wals)
        if not quarantined and not switched:
            return

        from modules.app_alerts import raise_alert

        if quarantined:
            total = sum(q.get("bytes", 0) for q in quarantined)
            paths = "\n".join(f"  • {q['wal']} ({q.get('bytes', 0):,} bytes)"
                              for q in quarantined)
            raise_alert(
                severity="warning",
                source="telemetry_db",
                title="Telemetry WAL could not be replayed",
                message=(
                    f"The write-ahead log could not be replayed"
                    + (f" after the backend changed ({previous} → {backend})"
                       if switched else "")
                    + f". It has been set aside so the database could open, and the "
                      f"app is running normally.\n\n"
                    f"Quarantined ({total:,} bytes total) — not deleted:\n{paths}\n\n"
                    "Rows that were only in that WAL are not in the database. "
                    "Everything already written is intact, and new telemetry is "
                    "recording normally. Delete the file once you're satisfied "
                    "nothing is needed from it."
                ),
                dedupe_key="telemetry_db:wal_quarantined",
            )
        elif switched:
            raise_alert(
                severity="info",
                source="telemetry_db",
                title="Telemetry write backend changed",
                message=(
                    f"Switched from {previous} to {backend}. The database "
                    f"opened cleanly and is recording normally."
                ),
                dedupe_key=f"telemetry_db:backend_switch:{previous}->{backend}",
            )
    except Exception as e:
        # Reconciliation is best-effort; the app runs regardless.
        logger.warning(f"Telemetry reconciliation failed: {e}")


def start_reconciler() -> None:
    """Start the reconciliation thread (idempotent, non-blocking)."""
    global _reconciler_thread
    if _reconciler_thread is not None and _reconciler_thread.is_alive():
        return
    _reconciler_thread = threading.Thread(
        target=_reconcile_worker,
        name="telemetry-reconciler",
        daemon=True,
    )
    _reconciler_thread.start()


def _finish_db_init():
    # Deliberately DO NOT open the in-process Rust appender against DB_PATH.
    global _appender
    _appender = None
    if _USE_RUST:
        logger.info(
            "Telemetry: zmm_telemetry present but NOT opened in-process "
            "(would split-brain the DB); using the shared Python connection")
    logger.info(f"Telemetry database opened: {DB_PATH}")


def _init_tables(db):
    """Create tables if they don't exist."""
    # system_metrics now lives in its own file — see _init_system_tables().

    db.execute("""
        CREATE TABLE IF NOT EXISTS packet_stats (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            ieee        VARCHAR NOT NULL,
            rx_packets  BIGINT DEFAULT 0,
            tx_packets  BIGINT DEFAULT 0,
            rx_bytes    BIGINT DEFAULT 0,
            tx_bytes    BIGINT DEFAULT 0,
            errors      INTEGER DEFAULT 0,
            retries     INTEGER DEFAULT 0,
            lqi         INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS device_states (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            ieee        VARCHAR NOT NULL,
            attribute   VARCHAR NOT NULL,
            value       VARCHAR,
            numeric_val DOUBLE
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS spectrum_scans (
            ts          TIMESTAMP NOT NULL DEFAULT now(),
            channel     INTEGER NOT NULL,
            energy      INTEGER NOT NULL
        )
    """)

    db.execute("""
            CREATE TABLE IF NOT EXISTS heating_tick_rooms (
                ts                  TIMESTAMP NOT NULL DEFAULT now(),
                circuit_id          VARCHAR NOT NULL,
                room_id             VARCHAR NOT NULL,
                classification      VARCHAR,
                current_temp_c      DOUBLE,
                setpoint_c          DOUBLE,
                outdoor_temp_c      DOUBLE,
                calling_for_heat    BOOLEAN,
                trv_setpoint_c      DOUBLE,
                trv_valve_open_pct  DOUBLE,
                dry_run             BOOLEAN DEFAULT FALSE,
                reason              VARCHAR
            )
        """)

    db.execute("""
            CREATE TABLE IF NOT EXISTS heating_tick_boiler (
                ts                  TIMESTAMP NOT NULL DEFAULT now(),
                circuit_id          VARCHAR NOT NULL,
                boiler_called       BOOLEAN NOT NULL,
                rooms_cold          INTEGER DEFAULT 0,
                rooms_ontarget      INTEGER DEFAULT 0,
                rooms_hot           INTEGER DEFAULT 0,
                receiver_command    VARCHAR,
                dry_run             BOOLEAN DEFAULT FALSE
            )
        """)

    logger.debug("Telemetry tables initialised")


# Octopus database (separate file)

_octopus_db = None
_octopus_db_lock = threading.Lock()


def _get_octopus_db():
    """Get or create the Octopus DuckDB connection (lazy singleton)."""
    global _octopus_db
    if _octopus_db is None:
        with _octopus_db_lock:
            if _octopus_db is not None:
                return _octopus_db
            import duckdb
            os.makedirs(os.path.dirname(OCTOPUS_DB_PATH), exist_ok=True)
            db = duckdb.connect(OCTOPUS_DB_PATH)
            _init_octopus_tables(db)
            _migrate_octopus_from_telemetry(db)
            _octopus_db = db
            logger.info(f"Octopus database opened: {OCTOPUS_DB_PATH}")
    return _octopus_db


def _octopus_cursor():
    """
    Per-call cursor. The Octopus helpers run in asyncio.to_thread workers,
    so several can execute CONCURRENTLY — DuckDBPyConnection must not be
    shared across threads like that (crashes the process); a cursor() is a
    cheap per-thread clone and is the documented-safe pattern.
    """
    return _get_octopus_db().cursor()


def _init_octopus_tables(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS octopus_consumption (
            fuel            VARCHAR NOT NULL,
            interval_start  TIMESTAMP NOT NULL,
            interval_end    TIMESTAMP NOT NULL,
            consumption     DOUBLE,
            consumption_kwh DOUBLE,
            source          VARCHAR DEFAULT 'meter',
            PRIMARY KEY (fuel, interval_start)
        )
    """)
    # 'meter' = settlement-grade REST data, 'mini' = provisional Home Mini
    # telemetry. Pre-existing DBs predate the column; the default backfills.
    db.execute("""
        ALTER TABLE octopus_consumption
        ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'meter'
    """)
    # Home Mini demand samples (~5-min grain) — persisted so the live chart
    # survives restarts instead of rebuilding from empty over 48h.
    db.execute("""
        CREATE TABLE IF NOT EXISTS octopus_telemetry (
            ts              TIMESTAMP NOT NULL,
            read_at         TIMESTAMP,
            demand_w        DOUBLE,
            consumption_kwh DOUBLE,
            PRIMARY KEY (ts)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS octopus_rates (
            fuel            VARCHAR NOT NULL,
            rate_type       VARCHAR NOT NULL,
            tariff_code     VARCHAR,
            valid_from      TIMESTAMP NOT NULL,
            valid_to        TIMESTAMP,
            value_inc_vat_p DOUBLE,
            PRIMARY KEY (fuel, rate_type, valid_from)
        )
    """)


def _migrate_octopus_from_telemetry(odb):
    """
    One-time move: the octopus tables briefly lived inside telemetry.duckdb.
    Copy any rows across and drop the old tables so the main DB stays clean.
    Best-effort — the data is re-fetchable from the API via backfill.
    """
    try:
        tdb = _get_db()
        tables = {r[0] for r in tdb.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        # Explicit column lists: the old telemetry.duckdb tables predate the
        # `source` column, so bare VALUES would mismatch the new schema.
        col_lists = {
            "octopus_consumption":
                "fuel, interval_start, interval_end, consumption, consumption_kwh",
            "octopus_rates":
                "fuel, rate_type, tariff_code, valid_from, valid_to, value_inc_vat_p",
        }
        for table, cols in col_lists.items():
            if table not in tables:
                continue
            rows = tdb.execute(f"SELECT {cols} FROM {table}").fetchall()
            if rows:
                ph = ", ".join(["?"] * len(cols.split(",")))
                odb.executemany(
                    f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({ph})", rows)
            tdb.execute(f"DROP TABLE {table}")
            logger.info(f"Migrated {len(rows)} {table} rows into {OCTOPUS_DB_PATH}")
    except Exception as e:
        logger.warning(f"Octopus table migration skipped: {e}")


# System-metrics database (separate file)

_system_db = None
_system_db_lock = threading.Lock()
_system_appender = None   # zmm_telemetry.Appender bound to SYSTEM_DB_PATH


def _get_system_db():
    """Get or create the system-metrics DuckDB connection (lazy singleton)."""
    global _system_db
    if _system_db is None:
        with _system_db_lock:
            if _system_db is not None:
                return _system_db
            os.makedirs(os.path.dirname(SYSTEM_DB_PATH), exist_ok=True)
            # Same guarded open as the main DB: this file gets the 0-byte-stub
            # and unreplayable-WAL healing too, rather than a bare connect().
            db = _connect_local_db(SYSTEM_DB_PATH)
            _init_system_tables(db)
            # NO migration here: reached from paths that run on the event loop.
            # Migration is a startup step — see migrate_system_metrics().
            _system_db = db
            _finish_system_db_init()
            logger.info(f"System metrics database opened: {SYSTEM_DB_PATH}")
    return _system_db


def _finish_system_db_init():
    """Decide whether the Rust appender may write this file.

    Same rule as _finish_db_init(): the appender opens its OWN DuckDB instance
    on the path it is given, and POSIX fcntl locks are per-PID, so an appender
    plus this module's Python connection on one file means two unsynchronised
    engines and a split-brain view of it.

    query_system_metrics() needs a live read connection here, so the Python
    connection has to remain the sole engine and the appender stays closed.
    The hook is kept rather than deleted because write_system_metrics() uses
    the appender whenever this is set — enabling it is a one-line change if the
    read path ever moves out of process. At one sample per 30s the appender
    would buy nothing anyway; its value is millions of rows, not two a minute.
    """
    global _system_appender
    _system_appender = None
    if _USE_RUST:
        logger.debug(
            "System metrics: zmm_telemetry present but NOT opened on "
            f"{SYSTEM_DB_PATH} (would split-brain with the read connection)")


def _system_cursor():
    """Per-call cursor — the readers run in asyncio.to_thread workers, so the
    base connection must not be shared across threads (see _octopus_cursor)."""
    return _get_system_db().cursor()


_SYSTEM_METRICS_DDL = """
    CREATE TABLE IF NOT EXISTS {table} (
        ts          TIMESTAMP NOT NULL DEFAULT now(),
        cpu_percent FLOAT,
        cpu_freq    FLOAT,
        mem_total   BIGINT,
        mem_used    BIGINT,
        mem_percent FLOAT,
        swap_used   BIGINT,
        swap_percent FLOAT,
        disk_total  BIGINT,
        disk_used   BIGINT,
        disk_percent FLOAT,
        cpu_temp    FLOAT,
        gpu_temp    FLOAT,
        load_1m     FLOAT,
        load_5m     FLOAT,
        load_15m    FLOAT,
        uptime_secs BIGINT,
        process_rss BIGINT,
        process_threads INTEGER
    )
"""


def _init_system_tables(db):
    db.execute(_SYSTEM_METRICS_DDL.format(table="system_metrics"))


SYSTEM_METRIC_COLUMNS = (
    "ts, cpu_percent, cpu_freq, mem_total, mem_used, mem_percent, "
    "swap_used, swap_percent, disk_total, disk_used, disk_percent, "
    "cpu_temp, gpu_temp, load_1m, load_5m, load_15m, "
    "uptime_secs, process_rss, process_threads"
)


def migrate_system_metrics(log: Optional[Callable[[str], None]] = None,
                           budget_seconds: float = 90.0) -> Optional[Dict[str, Any]]:
    """Move system_metrics out of telemetry.duckdb into its own file.

    MUST be called from a worker thread during startup, before any service
    writes telemetry. NEVER lazily from a write.

    The first version of this ran on first use, so it fired from
    system_monitor's loop — on the EVENT LOOP. The salvage took minutes, tripped
    loop_monitor's 60s exit-70, and the app crash-looped because every restart
    started the migration from scratch and never finished it.

    Single engine by design: the telemetry connection ATTACHes the system file
    and copies in-engine (fast, and it keeps the original timestamps). Opening a
    second DuckDB instance on either file from this process would split-brain
    it — POSIX fcntl locks are per-PID.

    Doubles as a repair when the damaged blocks are inside this table. Dropping
    it frees them, which is what lets telemetry.duckdb CHECKPOINT again. Rows
    are salvaged around the damage rather than read in one pass, because a
    straight SELECT dies on a bad block and would migrate nothing.

    Returns a summary when it migrated, else None. Never raises: losing some
    host CPU samples must not stop the app booting.
    """
    import logging as _logging
    say = log or _logging.getLogger("modules.telemetry_db").info

    try:
        db = _get_db()
        tables = {r[0] for r in db.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        if "system_metrics" not in tables:
            return None                      # already migrated / fresh install

        say(f"Migrating system_metrics out of {DB_PATH} into {SYSTEM_DB_PATH}")
        os.makedirs(os.path.dirname(SYSTEM_DB_PATH), exist_ok=True)

        cols = SYSTEM_METRIC_COLUMNS
        db.execute(f"ATTACH '{SYSTEM_DB_PATH}' AS sysdb")
        try:
            db.execute(_SYSTEM_METRICS_DDL.format(table="sysdb.system_metrics"))

            def copy_range(lo: int, hi: int) -> int:
                cur = db.execute(
                    f"INSERT INTO sysdb.system_metrics ({cols}) "
                    f"SELECT {cols} FROM system_metrics "
                    f"WHERE rowid >= {lo} AND rowid < {hi}")
                res = cur.fetchall()
                return int(res[0][0]) if res and res[0] else 0

            try:
                hi = db.execute("SELECT max(rowid) FROM system_metrics").fetchone()[0]
                bound = int(hi) + 1 if hi is not None else 0
            except Exception:
                bound = 1 << 24              # damaged — let the bisection find the end

            from modules.telemetry_rebuild import salvage_table, RebuildTimeout
            try:
                rep = salvage_table("system_metrics", bound, copy_range,
                                    log=lambda m: say(m.strip()),
                                    deadline=time.monotonic() + budget_seconds)
            except RebuildTimeout as e:
                # Leave everything as it is and try again next boot rather than
                # holding up startup. Nothing has been dropped at this point.
                say(f"system_metrics migration abandoned (over budget): {e}")
                db.execute("DELETE FROM sysdb.system_metrics")
                return None

            # Freeing these blocks is what lets the main database checkpoint again,
            # and must precede any CHECKPOINT: a failed one invalidates the
            # connection and the DROP becomes impossible until restart.
            db.execute("DROP TABLE system_metrics")
        finally:
            try:
                db.execute("DETACH sysdb")
            except Exception:
                pass

        if rep.lost_estimate:
            logger.warning(
                f"Migrated {rep.copied:,} system_metrics rows into "
                f"{SYSTEM_DB_PATH}; ~{rep.lost_estimate:,} unreadable rows "
                f"discarded with the damaged blocks")
        else:
            say(f"Migrated {rep.copied:,} system_metrics rows into {SYSTEM_DB_PATH}")

        try:
            db.execute("CHECKPOINT")
            say("Telemetry database checkpointed after migration")
        except Exception as e:
            logger.warning(f"Post-migration checkpoint failed: {e}")

        return {"migrated": rep.copied, "lost": rep.lost_estimate}
    except Exception as e:
        logger.warning(f"system_metrics migration skipped: {e}")
        return None


def prune_system(retention_days: int = DEFAULT_RETENTION_DAYS):
    """Retention prune for the system-metrics database."""
    try:
        db = _system_cursor()
        try:
            db.execute("DELETE FROM system_metrics WHERE ts < now() - "
                       f"INTERVAL '{retention_days} days'")
            logger.debug(f"Pruned system_metrics: retention={retention_days}d")
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"System metrics prune failed: {e}")


# WRITE OPERATIONS

def write_system_metrics(metrics: Dict[str, Any]):
    """Insert a system metrics sample into the system-metrics database.

    Deliberately does NOT use the Rust appender: that appender is bound to
    DB_PATH, so routing these rows through it would write them straight back
    into telemetry.duckdb — undoing the split that exists to keep host metrics
    from being able to damage device history.
    """
    if _system_appender is not None:
        # Appender preferred whenever open. ts = now() is correct here: these
        # are live samples, taken as they are written.
        _system_appender.append_system_metrics(metrics)
        return

    db = _system_cursor()
    try:
        db.execute("""
            INSERT INTO system_metrics (
                cpu_percent, cpu_freq, mem_total, mem_used, mem_percent,
                swap_used, swap_percent, disk_total, disk_used, disk_percent,
                cpu_temp, gpu_temp, load_1m, load_5m, load_15m,
                uptime_secs, process_rss, process_threads
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metrics.get("cpu_percent"), metrics.get("cpu_freq"),
            metrics.get("mem_total"), metrics.get("mem_used"), metrics.get("mem_percent"),
            metrics.get("swap_used"), metrics.get("swap_percent"),
            metrics.get("disk_total"), metrics.get("disk_used"), metrics.get("disk_percent"),
            metrics.get("cpu_temp"), metrics.get("gpu_temp"),
            metrics.get("load_1m"), metrics.get("load_5m"), metrics.get("load_15m"),
            metrics.get("uptime_secs"), metrics.get("process_rss"),
            metrics.get("process_threads"),
        ))
    except Exception as e:
        _note_fatal(e)
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


def write_packet_stats(stats_batch: List[Dict[str, Any]]):
    """Bulk insert packet stats snapshot for all devices."""
    if not stats_batch:
        return
    _get_db()
    if _appender is not None:
        for s in stats_batch:
            _appender.append_packet_stats(
                s["ieee"],
                int(s.get("rx_packets", 0)), int(s.get("tx_packets", 0)),
                int(s.get("rx_bytes", 0)),   int(s.get("tx_bytes", 0)),
                int(s.get("errors", 0)),     int(s.get("retries", 0)),
                int(s.get("lqi", 0)),
            )
        return
    # Python path (shared connection, serialised)
    _write_exec("""
        INSERT INTO packet_stats (ieee, rx_packets, tx_packets, rx_bytes, tx_bytes, errors, retries, lqi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (s["ieee"], s.get("rx_packets", 0), s.get("tx_packets", 0),
         s.get("rx_bytes", 0), s.get("tx_bytes", 0),
         s.get("errors", 0), s.get("retries", 0), s.get("lqi", 0))
        for s in stats_batch
    ])


def write_device_state(ieee: str, attribute: str, value: Any):
    """Record a device attribute change."""
    _get_db()
    str_val = str(value) if value is not None else None
    num_val = None
    try:
        num_val = float(value)
    except (TypeError, ValueError):
        pass
    if _appender is not None:
        _appender.append_device_state(ieee, attribute, str_val, num_val)
        return
    # Python path (shared connection, serialised)
    _write_exec("""
        INSERT INTO device_states (ieee, attribute, value, numeric_val)
        VALUES (?, ?, ?, ?)
    """, [(ieee, attribute, str_val, num_val)])


def write_device_states_batch(rows: List[tuple]) -> int:
    """
    Bulk device-state rows [(ieee, attribute, value), ...] in ONE commit.

    For worker-thread callers (the collector's keep-alive snapshot): goes
    through _write_exec, which holds _db_lock and uses a per-call cursor —
    never bare shared-connection access, which isn't safe across threads. One
    executemany replaces hundreds of per-row commits, which is what stalled the
    loop for seconds.
    """
    if not rows:
        return 0
    data = []
    for ieee, attribute, value in rows:
        str_val = str(value) if value is not None else None
        try:
            num_val = float(value)
        except (TypeError, ValueError):
            num_val = None
        data.append((ieee, attribute, str_val, num_val))

    if _appender is not None:
        # Appender first whenever open, as in write_device_state(). This is the
        # hot path for all device telemetry, so skipping it would quietly disable
        # the appender for the highest-volume table. Both paths stamp at insert.
        for ieee, attribute, str_val, num_val in data:
            _appender.append_device_state(ieee, attribute, str_val, num_val)
        return len(data)

    _write_exec("""
        INSERT INTO device_states (ieee, attribute, value, numeric_val)
        VALUES (?, ?, ?, ?)
    """, data)
    return len(data)


def write_spectrum_scan(results: Dict[int, int]):
    """Persist a spectrum scan (channel → energy)."""
    if not results:
        return
    _get_db()
    if _appender is not None:
        for ch, e in results.items():
            _appender.append_spectrum_scan(int(ch), int(e))
        return
    # Python path (shared connection, serialised)
    _write_exec("""
        INSERT INTO spectrum_scans (channel, energy) VALUES (?, ?)
    """, [(int(ch), int(e)) for ch, e in results.items()])


def write_heating_tick(
        ts: float,
        dry_run: bool,
        circuits: List[Dict[str, Any]],
) -> None:
    """
    Persist one controller tick for later analysis.
    ... (docstring unchanged) ...
    """
    if not circuits:
        return

    _get_db()  # ensure init (creates tables, initialises appender)

    # Prepare flat row tuples in one pass. Shape is the same regardless of
    # backend — the branch below decides whether to call the Rust appender
    # or fall back to Python INSERT.
    import datetime as _dt
    tick_dt = _dt.datetime.fromtimestamp(ts)

    room_rows = []
    boiler_rows = []

    for c in circuits:
        cid = str(c.get("id") or "")
        if not cid:
            continue

        recv = c.get("receiver_action") or {}
        recv_cmd = recv.get("command") if isinstance(recv, dict) else None

        rooms = c.get("rooms") or []
        n_cold = sum(1 for r in rooms if r.get("status") == "cold")
        n_ok   = sum(1 for r in rooms if r.get("status") == "ontarget")
        n_hot  = sum(1 for r in rooms if r.get("status") == "hot")

        boiler_rows.append({
            "circuit_id": cid,
            "boiler_called": bool(c.get("calling_for_heat")),
            "rooms_cold": n_cold,
            "rooms_ontarget": n_ok,
            "rooms_hot": n_hot,
            "receiver_command": recv_cmd,
        })

        for r in rooms:
            rid = str(r.get("room_id") or "")
            if not rid:
                continue

            trvs = r.get("trvs") or []
            trv_sp = None
            trv_open = None
            if trvs:
                first = trvs[0] if isinstance(trvs[0], dict) else {}
                trv_sp = first.get("intended_setpoint")
                for k in ("valve_opening_degree", "pi_heating_demand",
                          "valve_open_degree"):
                    if first.get(k) is not None:
                        try:
                            trv_open = float(first[k])
                        except (TypeError, ValueError):
                            pass
                        break

            room_rows.append({
                "circuit_id": cid,
                "room_id": rid,
                "classification": r.get("status"),
                "current_temp_c": r.get("current_temp"),
                "setpoint_c": r.get("target_temp"),
                "outdoor_temp_c": None,
                "calling_for_heat": bool(r.get("calling_for_heat")),
                "trv_setpoint_c": trv_sp,
                "trv_valve_open_pct": trv_open,
                "reason": r.get("temp_source"),
            })

    # Fast path: Rust appender
    if _appender is not None:
        try:
            for row in room_rows:
                _appender.append_heating_room(
                    ts,
                    row["circuit_id"], row["room_id"],
                    row["classification"],
                    _to_float(row["current_temp_c"]),
                    _to_float(row["setpoint_c"]),
                    _to_float(row["outdoor_temp_c"]),
                    row["calling_for_heat"],
                    _to_float(row["trv_setpoint_c"]),
                    _to_float(row["trv_valve_open_pct"]),
                    dry_run,
                    row["reason"],
                )
            for row in boiler_rows:
                _appender.append_heating_boiler(
                    ts,
                    row["circuit_id"],
                    row["boiler_called"],
                    int(row["rooms_cold"]),
                    int(row["rooms_ontarget"]),
                    int(row["rooms_hot"]),
                    row["receiver_command"],
                    dry_run,
                )
            return
        except Exception as e:
            logger.error(f"write_heating_tick appender failed, falling back: {e}")
            # fall through to Python INSERT

    # Python path (shared connection, serialised)
    try:
        if room_rows:
            _write_exec("""
                INSERT INTO heating_tick_rooms (
                    ts, circuit_id, room_id, classification,
                    current_temp_c, setpoint_c, outdoor_temp_c,
                    calling_for_heat, trv_setpoint_c, trv_valve_open_pct,
                    dry_run, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (tick_dt, r["circuit_id"], r["room_id"], r["classification"],
                 r["current_temp_c"], r["setpoint_c"], r["outdoor_temp_c"],
                 r["calling_for_heat"], r["trv_setpoint_c"], r["trv_valve_open_pct"],
                 dry_run, r["reason"])
                for r in room_rows
            ])
        if boiler_rows:
            _write_exec("""
                INSERT INTO heating_tick_boiler (
                    ts, circuit_id, boiler_called,
                    rooms_cold, rooms_ontarget, rooms_hot,
                    receiver_command, dry_run
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (tick_dt, r["circuit_id"], r["boiler_called"],
                 r["rooms_cold"], r["rooms_ontarget"], r["rooms_hot"],
                 r["receiver_command"], dry_run)
                for r in boiler_rows
            ])
    except Exception as e:
        logger.error(f"write_heating_tick failed: {e}", exc_info=True)


def _to_float(v):
    """Helper — coerce to Optional[float] for the Rust appender."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# READ OPERATIONS

def query_system_metrics(hours: int = 1, bucket_minutes: int = 1) -> List[Dict]:
    """
    Get system metrics aggregated by time bucket.
    Returns one row per bucket with averaged values.
    """
    db = _system_cursor()
    result = db.execute(f"""
        SELECT
            time_bucket(INTERVAL '{bucket_minutes} minutes', ts) AS bucket,
            AVG(cpu_percent) AS cpu_percent,
            AVG(mem_percent) AS mem_percent,
            MAX(mem_used) AS mem_used,
            AVG(cpu_temp) AS cpu_temp,
            AVG(gpu_temp) AS gpu_temp,
            AVG(load_1m) AS load_1m,
            AVG(load_5m) AS load_5m,
            AVG(swap_percent) AS swap_percent,
            AVG(disk_percent) AS disk_percent,
            MAX(process_rss) AS process_rss,
            MAX(process_threads) AS process_threads
        FROM system_metrics
        WHERE ts >= now() - INTERVAL '{hours} hours'
        GROUP BY bucket
        ORDER BY bucket ASC
    """).fetchall()

    columns = ["ts", "cpu_percent", "mem_percent", "mem_used", "cpu_temp",
               "gpu_temp", "load_1m", "load_5m", "swap_percent", "disk_percent",
               "process_rss", "process_threads"]
    return [dict(zip(columns, row)) for row in result]


def query_packet_stats(ieee: Optional[str] = None, hours: int = 1) -> List[Dict]:
    """Get packet stats history for a device or all devices."""
    db = _rcursor()
    hours = int(hours)
    if ieee:
        result = db.execute(f"""
            SELECT ts, ieee, rx_packets, tx_packets, errors, retries, lqi
            FROM packet_stats
            WHERE ieee = ? AND ts >= now() - INTERVAL '{hours} hours'
            ORDER BY ts ASC
        """, [ieee]).fetchall()
    else:
        result = db.execute(f"""
            SELECT
                time_bucket(INTERVAL '5 minutes', ts) AS bucket,
                SUM(rx_packets) AS rx_packets,
                SUM(tx_packets) AS tx_packets,
                SUM(errors) AS errors
            FROM packet_stats
            WHERE ts >= now() - INTERVAL '{hours} hours'
            GROUP BY bucket
            ORDER BY bucket ASC
        """).fetchall()

    if ieee:
        cols = ["ts", "ieee", "rx_packets", "tx_packets", "errors", "retries", "lqi"]
    else:
        cols = ["ts", "rx_packets", "tx_packets", "errors"]
    return [dict(zip(cols, row)) for row in result]


def query_device_state_history(ieee: str, attribute: str, hours: int = 24) -> List[Dict]:
    """Get state change history for a specific device attribute."""
    db = _rcursor()
    hours = int(hours)
    result = db.execute(f"""
        SELECT ts, value, numeric_val
        FROM device_states
        WHERE ieee = ? AND attribute = ? AND ts >= now() - INTERVAL '{hours} hours'
        ORDER BY ts ASC
    """, [ieee, attribute]).fetchall()
    return [{"ts": r[0], "value": r[1], "numeric_val": r[2]} for r in result]

def query_last_report_age_sec(ieee: str, attributes: List[str],
                              hours: int = 6) -> Optional[float]:
    """
    Seconds since the most recent report of any of `attributes` for this
    IEEE, or None if nothing landed in the lookback window.

    Computed entirely inside DuckDB: `ts` is written by DEFAULT now() in
    DuckDB's session timezone, which is independent of Python's local time
    (the container mounts host /etc/localtime for Python, but DuckDB takes
    its TimeZone from the TZ env / ICU and typically lands on UTC). Naive
    `ts` values must therefore never be interpreted with Python-local
    .timestamp() — comparing ts against now() in SQL makes the session
    timezone cancel out.
    """
    if not attributes:
        return None
    db = _rcursor()
    hours = int(hours)
    placeholders = ", ".join("?" for _ in attributes)
    row = db.execute(f"""
        SELECT date_diff('second', max(ts), now()::TIMESTAMP)
        FROM device_states
        WHERE ieee = ? AND attribute IN ({placeholders})
          AND ts >= now() - INTERVAL '{hours} hours'
    """, [ieee, *attributes]).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def query_device_attributes(ieee: str, hours: int = 720) -> List[str]:
    """
    Distinct attribute names recorded for a device within lookback.
    Default lookback matches the retention window (30 days) so attributes
    only written at rare events (e.g. app restarts) still appear.
    """
    db = _rcursor()
    hours = int(hours)
    result = db.execute(f"""
        SELECT DISTINCT attribute
        FROM device_states
        WHERE ieee = ? AND ts >= now() - INTERVAL '{hours} hours'
        ORDER BY attribute
    """, [ieee]).fetchall()
    return [r[0] for r in result]


def query_device_state_bucketed(ieee: str, attribute: str,
                                hours: int = 24,
                                bucket_minutes: int = 5) -> List[Dict]:
    """
    Time-bucketed aggregation of a numeric attribute for chart rendering.
    Falls back to the last string value per bucket for non-numeric attrs.

    Carry-forward anchoring: attributes are recorded on change, so a
    slow-changing attribute can have no rows inside the window even though
    its value is known. If any row exists before the window start, a
    synthetic samples=0 row with the last-known value is prepended at the
    window start, and the newest known value is repeated at `now`, so the
    chart draws a continuous line instead of "No data in this range".
    """
    db = _rcursor()
    hours = int(hours)
    bucket_minutes = int(bucket_minutes)
    result = db.execute(f"""
        SELECT
            time_bucket(INTERVAL '{bucket_minutes} minutes', ts) AS bucket,
            AVG(numeric_val) AS avg_val,
            MIN(numeric_val) AS min_val,
            MAX(numeric_val) AS max_val,
            COUNT(*) AS samples,
            ANY_VALUE(value) AS last_str
        FROM device_states
        WHERE ieee = ? AND attribute = ?
          AND ts >= now() - INTERVAL '{hours} hours'
        GROUP BY bucket
        ORDER BY bucket ASC
    """, [ieee, attribute]).fetchall()
    cols = ["ts", "avg", "min", "max", "samples", "last_str"]
    rows = [dict(zip(cols, row)) for row in result]

    window_start = datetime.now() - timedelta(hours=hours)

    # Anchor at window start with the last value recorded before it.
    if not rows or rows[0]["ts"] > window_start:
        anchor = db.execute(f"""
            SELECT value, numeric_val
            FROM device_states
            WHERE ieee = ? AND attribute = ?
              AND ts < now() - INTERVAL '{hours} hours'
            ORDER BY ts DESC
            LIMIT 1
        """, [ieee, attribute]).fetchone()
        if anchor:
            rows.insert(0, {
                "ts": window_start,
                "avg": anchor[1], "min": anchor[1], "max": anchor[1],
                "samples": 0, "last_str": anchor[0],
            })

    # Extend the newest known value to `now` so the line spans the window.
    if rows:
        last = rows[-1]
        rows.append({
            "ts": datetime.now(),
            "avg": last["avg"], "min": last["avg"], "max": last["avg"],
            "samples": 0, "last_str": last["last_str"],
        })

    return rows


def query_spectrum_history(hours: int = 24) -> List[Dict]:
    """Get spectrum scan history."""
    db = _rcursor()
    hours = int(hours)
    result = db.execute(f"""
        SELECT ts, channel, energy
        FROM spectrum_scans
        WHERE ts >= now() - INTERVAL '{hours} hours'
        ORDER BY ts ASC
    """).fetchall()
    return [{"ts": r[0], "channel": r[1], "energy": r[2]} for r in result]


def get_db_stats() -> Dict[str, Any]:
    """Get database size and row counts per table."""
    db = _rcursor()
    stats = {}
    for table in ["packet_stats", "device_states", "spectrum_scans"]:
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        stats[table] = count

    # system_metrics lives in its own file — count via its own connection.
    try:
        sdb = _system_cursor()
        stats["system_metrics"] = sdb.execute(
            "SELECT COUNT(*) FROM system_metrics").fetchone()[0]
        stats["system_file_size_mb"] = round(
            os.path.getsize(SYSTEM_DB_PATH) / (1024 * 1024), 2)
    except Exception:
        stats.setdefault("system_metrics", 0)

    # Octopus lives in its own file — count via its own connection.
    try:
        odb = _octopus_cursor()
        for table in ["octopus_consumption", "octopus_rates"]:
            stats[table] = odb.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        stats["octopus_file_size_mb"] = round(
            os.path.getsize(OCTOPUS_DB_PATH) / (1024 * 1024), 2)
    except Exception:
        pass

    # File size
    try:
        stats["file_size_mb"] = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
    except OSError:
        stats["file_size_mb"] = 0

    return stats


def query_room_heating_state(
        circuit_id: str,
        room_id: str,
        hours: int = 14 * 24,
) -> List[Dict[str, Any]]:
    """
    Return per-tick heating state for a room over the last N hours.

    Used by the heating anomaly watcher to build a heating_state_getter(ts)
    closure, so baseline-τ fitting can reject cool-down windows that
    overlapped a period when heating was actively running.

    Rows are returned in ascending ts order (oldest first) to make bisect
    lookups cheap on the caller side. Dry-run ticks are excluded because
    their decisions didn't actually drive TRVs or the boiler.

    The 'heating_active' column is derived: a room counts as "being heated"
    if it was calling for heat OR its TRV valve was reported open. Either
    signal is sufficient — we want a conservative gate (bias toward
    rejecting windows, not including contaminated ones).
    """
    db = _rcursor()
    hours = int(hours)
    rows = db.execute(f"""
        SELECT
            ts,
            calling_for_heat,
            trv_valve_open_pct,
            classification,
            current_temp_c,
            setpoint_c,
            (
                COALESCE(calling_for_heat, FALSE)
                OR COALESCE(trv_valve_open_pct, 0) > 0
            ) AS heating_active
        FROM heating_tick_rooms
        WHERE circuit_id = ?
          AND room_id = ?
          AND ts >= now() - INTERVAL '{hours} hours'
          AND dry_run = FALSE
        ORDER BY ts ASC
    """, [circuit_id, room_id]).fetchall()

    cols = ["ts", "calling_for_heat", "trv_valve_open_pct",
            "classification", "current_temp_c", "setpoint_c", "heating_active"]
    return [dict(zip(cols, r)) for r in rows]

# OCTOPUS ENERGY
# Timestamps in octopus_consumption / octopus_rates are UTC-naive: the API
# returns ISO8601 with offsets (BST intervals arrive as +01:00), so callers
# must normalise to UTC and strip tzinfo before writing. Local-day bucketing
# converts back to Europe/London inside the query.

_LONDON_DAY = "date_trunc('day', timezone('Europe/London', {col}::TIMESTAMP AT TIME ZONE 'UTC'))"

_OCTOPUS_GROUPS = {"halfhour", "day", "week", "month"}


def _as_day(s: str) -> datetime:
    """'YYYY-MM-DD' → midnight datetime, comparable to the _LONDON_DAY bucket."""
    return datetime.strptime(str(s), "%Y-%m-%d")


def write_octopus_consumption(fuel: str, rows: List[Dict[str, Any]],
                              source: str = "meter") -> int:
    """
    Upsert half-hourly consumption intervals (idempotent on re-poll).

    source='mini' rows are provisional Home Mini telemetry filling the
    REST data lag; the next REST poll overwrites them (same PK) with the
    settlement-grade values, so the two sources never coexist per interval.
    """
    if not rows:
        return 0
    db = _octopus_cursor()
    db.executemany("""
        INSERT OR REPLACE INTO octopus_consumption
            (fuel, interval_start, interval_end, consumption, consumption_kwh, source)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        (fuel, r["interval_start"], r["interval_end"],
         r.get("consumption"), r.get("consumption_kwh"), source)
        for r in rows
    ])
    return len(rows)


def write_octopus_rates(fuel: str, rate_type: str, tariff_code: Optional[str],
                        rows: List[Dict[str, Any]]) -> int:
    """Upsert tariff rates. Agile rates can be republished, hence REPLACE."""
    if not rows:
        return 0
    db = _octopus_cursor()
    db.executemany("""
        INSERT OR REPLACE INTO octopus_rates
            (fuel, rate_type, tariff_code, valid_from, valid_to, value_inc_vat_p)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        (fuel, rate_type, tariff_code, r["valid_from"], r.get("valid_to"),
         r.get("value_inc_vat_p"))
        for r in rows
    ])
    return len(rows)


def write_octopus_telemetry(rows: List[Dict[str, Any]]) -> int:
    """Upsert Home Mini demand samples (idempotent on the sample timestamp)."""
    if not rows:
        return 0
    db = _octopus_cursor()
    db.executemany("""
        INSERT OR REPLACE INTO octopus_telemetry
            (ts, read_at, demand_w, consumption_kwh)
        VALUES (?, ?, ?, ?)
    """, [
        (r["ts"], r.get("read_at"), r.get("demand_w"), r.get("consumption_kwh"))
        for r in rows
    ])
    return len(rows)


def query_octopus_telemetry_recent(hours: int = 48) -> List[Dict[str, Any]]:
    """
    Recent demand samples, oldest first, in the live-buffer wire shape
    (ISO strings) so the service can seed its in-memory buffer on restart.
    """
    db = _octopus_cursor()
    rows = db.execute(f"""
        SELECT ts, read_at, demand_w, consumption_kwh
        FROM octopus_telemetry
        WHERE ts >= now() - INTERVAL '{int(hours)} hours'
        ORDER BY ts ASC
    """).fetchall()
    return [{
        "ts": r[0].isoformat() + "Z",
        "read_at": r[1].isoformat() + "Z" if r[1] is not None else None,
        "demand_w": r[2],
        "consumption_kwh": r[3],
    } for r in rows]


def query_octopus_telemetry_consumption(hours: int = 48) -> List[Dict]:
    """
    Fine-grained (~5-min) electricity consumption derived from the Home
    Mini's cumulative meter register: successive sample deltas. Deltas
    spanning a gap larger than 30 min are dropped — that energy is lumped
    across downtime and would render as a bogus spike; the half-hourly
    series covers those periods. Clamped at 0 for safety, cost joined per
    sample against the half-hourly unit-rate windows.
    """
    db = _octopus_cursor()
    rows = db.execute(f"""
        WITH d AS (
            SELECT ts,
                   lag(ts) OVER (ORDER BY ts) AS ts_start,
                   consumption_kwh
                   - lag(consumption_kwh) OVER (ORDER BY ts) AS kwh
            FROM octopus_telemetry
            WHERE consumption_kwh IS NOT NULL
              AND ts >= now() - INTERVAL '{int(hours)} hours'
        )
        SELECT d.ts_start, d.ts, greatest(d.kwh, 0) AS kwh,
               greatest(d.kwh, 0) * r.value_inc_vat_p AS cost_p
        FROM d
        LEFT JOIN octopus_rates r
          ON r.fuel = 'electricity' AND r.rate_type = 'unit'
         AND r.valid_from <= d.ts_start
         AND (r.valid_to IS NULL OR r.valid_to > d.ts_start)
        WHERE d.ts_start IS NOT NULL
          AND d.kwh IS NOT NULL
          AND d.ts - d.ts_start <= INTERVAL '30 minutes'
        ORDER BY d.ts ASC
    """).fetchall()
    return [{"ts_start": r[0], "ts_end": r[1], "kwh": r[2], "cost_p": r[3]}
            for r in rows]


def query_octopus_last_interval(fuel: str) -> Optional[datetime]:
    """
    Latest meter-sourced interval_end (UTC-naive) — start point for
    incremental REST polls. Provisional 'mini' rows are excluded on purpose:
    the REST fetch must re-cover their window to replace them, otherwise a
    mini-fed watermark would leave provisional data permanent.
    """
    db = _octopus_cursor()
    row = db.execute(
        "SELECT max(interval_end) FROM octopus_consumption "
        "WHERE fuel = ? AND source = 'meter'", [fuel]
    ).fetchone()
    return row[0] if row else None


def query_octopus_consumption_buckets(fuel: str, days: int = 30,
                                      group_by: str = "day",
                                      day_from: Optional[str] = None,
                                      day_to: Optional[str] = None) -> List[Dict]:
    """
    Bucketed consumption + cost for chart rendering.

    group_by 'halfhour' returns raw intervals with per-slot unit cost;
    'day'/'week'/'month' bucket on Europe/London day boundaries and add
    one standing charge per local day. cost_p is pence including VAT and
    is NULL-safe (0 contribution) when no rate covers an interval.

    day_from/day_to ('YYYY-MM-DD', inclusive, Europe/London days) select an
    explicit calendar window instead of the rolling `days` one.
    """
    if group_by not in _OCTOPUS_GROUPS:
        group_by = "day"
    db = _octopus_cursor()
    days = int(days)
    local_day = _LONDON_DAY.format(col="c.interval_start")

    if day_from and day_to:
        window_sql = f"{local_day} BETWEEN ? AND ?"
        window_args = [_as_day(day_from), _as_day(day_to)]
    else:
        window_sql = f"c.interval_start >= now() - INTERVAL '{days} days'"
        window_args = []

    if group_by == "halfhour":
        rows = db.execute(f"""
            SELECT c.interval_start, c.consumption_kwh,
                   c.consumption_kwh * r.value_inc_vat_p AS cost_p
            FROM octopus_consumption c
            LEFT JOIN octopus_rates r
              ON r.fuel = c.fuel AND r.rate_type = 'unit'
             AND r.valid_from <= c.interval_start
             AND (r.valid_to IS NULL OR r.valid_to > c.interval_start)
            WHERE c.fuel = ?
              AND {window_sql}
            ORDER BY c.interval_start ASC
        """, [fuel] + window_args).fetchall()
        return [{"ts": r[0], "kwh": r[1], "cost_p": r[2]} for r in rows]

    rows = db.execute(f"""
        WITH hh AS (
            SELECT {local_day} AS local_day,
                   c.interval_start,
                   c.consumption_kwh AS kwh,
                   COALESCE(c.consumption_kwh * r.value_inc_vat_p, 0) AS unit_cost_p
            FROM octopus_consumption c
            LEFT JOIN octopus_rates r
              ON r.fuel = c.fuel AND r.rate_type = 'unit'
             AND r.valid_from <= c.interval_start
             AND (r.valid_to IS NULL OR r.valid_to > c.interval_start)
            WHERE c.fuel = ?
              AND {window_sql}
        ),
        by_day AS (
            SELECT local_day,
                   sum(kwh) AS kwh,
                   sum(unit_cost_p) AS unit_cost_p,
                   min(interval_start) AS first_start
            FROM hh
            GROUP BY local_day
        )
        SELECT date_trunc('{group_by}', d.local_day) AS bucket,
               sum(d.kwh) AS kwh,
               sum(d.unit_cost_p + COALESCE(s.value_inc_vat_p, 0)) AS cost_p
        FROM by_day d
        LEFT JOIN octopus_rates s
          ON s.fuel = ? AND s.rate_type = 'standing'
         AND s.valid_from <= d.first_start
         AND (s.valid_to IS NULL OR s.valid_to > d.first_start)
        GROUP BY bucket
        ORDER BY bucket ASC
    """, [fuel] + window_args + [fuel]).fetchall()
    return [{"ts": r[0], "kwh": r[1], "cost_p": r[2]} for r in rows]


def query_octopus_rates_window(fuel: str, start_utc: datetime,
                               end_utc: datetime,
                               rate_type: str = "unit") -> List[Dict]:
    """Rates overlapping [start_utc, end_utc) — feeds the Agile rate chart."""
    db = _octopus_cursor()
    rows = db.execute("""
        SELECT valid_from, valid_to, value_inc_vat_p, tariff_code
        FROM octopus_rates
        WHERE fuel = ? AND rate_type = ?
          AND valid_from < ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY valid_from ASC
    """, [fuel, rate_type, end_utc, start_utc]).fetchall()
    return [{"valid_from": r[0], "valid_to": r[1],
             "value_inc_vat_p": r[2], "tariff_code": r[3]} for r in rows]


def query_octopus_current_rate(fuel: str, rate_type: str,
                               at_utc: Optional[datetime] = None) -> Optional[float]:
    """Rate (p) in force at a UTC instant, or None. Survives app restarts."""
    db = _octopus_cursor()
    at_utc = at_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    row = db.execute("""
        SELECT value_inc_vat_p FROM octopus_rates
        WHERE fuel = ? AND rate_type = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY valid_from DESC
        LIMIT 1
    """, [fuel, rate_type, at_utc, at_utc]).fetchone()
    return row[0] if row else None


def query_octopus_kwh_for_day(fuel: str, local_date) -> Optional[float]:
    """
    Total kWh for one Europe/London calendar day (datetime.date).
    None when no intervals are stored for that day (data lag, no meter).
    """
    db = _octopus_cursor()
    local_day = _LONDON_DAY.format(col="c.interval_start")
    row = db.execute(f"""
        SELECT sum(c.consumption_kwh)
        FROM octopus_consumption c
        WHERE c.fuel = ? AND {local_day} = ?
    """, [fuel, datetime(local_date.year, local_date.month, local_date.day)]).fetchone()
    return row[0] if row and row[0] is not None else None


def query_plug_energy_by_day(days: int = 7) -> List[Dict]:
    """
    Per-device daily kWh from cumulative smart-plug 'energy' counters.
    Deltas between consecutive readings are clamped at 0 so counter resets
    (device rejoin, factory reset) don't produce negative usage.
    device_states.ts is session-local like every other table here, so days
    bucket on ts directly, consistent with the existing history queries.
    Runs in worker threads → per-call cursor, never the shared connection.
    """
    db = _get_db().cursor()
    days = int(days)
    rows = db.execute(f"""
        WITH deltas AS (
            SELECT ieee, ts,
                   numeric_val - lag(numeric_val) OVER (
                       PARTITION BY ieee ORDER BY ts
                   ) AS delta
            FROM device_states
            WHERE attribute = 'energy' AND numeric_val IS NOT NULL
              AND ts >= now() - INTERVAL '{days + 1} days'
        )
        SELECT ieee, date_trunc('day', ts) AS day,
               sum(greatest(delta, 0)) AS kwh
        FROM deltas
        WHERE delta IS NOT NULL
          AND ts >= now() - INTERVAL '{days} days'
        GROUP BY ieee, day
        ORDER BY ieee, day ASC
    """).fetchall()
    return [{"ieee": r[0], "day": r[1], "kwh": r[2]} for r in rows]


# MAINTENANCE

def prune(retention_days: int = DEFAULT_RETENTION_DAYS):
    """Remove records older than retention period.

    Callers run this in a worker thread (multi-table DELETEs take seconds on a
    grown DB). Uses a per-call cursor, created under a brief _db_lock; the
    DELETEs run OUTSIDE the lock so this never blocks loop-thread writers for
    seconds (DuckDB serialises the concurrent writes internally). See
    _write_exec for the same rationale.
    """
    cutoff = f"{retention_days} days"
    with _db_lock:
        db = _get_db().cursor()           # brief: cursor creation only
    try:
        for table in ["packet_stats", "device_states", "spectrum_scans"]:
            db.execute(
                f"DELETE FROM {table} WHERE ts < now() - INTERVAL '{cutoff}'"
            )
            logger.debug(f"Pruned {table}: retention={retention_days}d")
    finally:
        try:
            db.close()
        except Exception:
            pass

    logger.info(f"Telemetry pruned (retention={retention_days} days)")


def prune_octopus(retention_days: int = OCTOPUS_RETENTION_DAYS):
    """Trim the Octopus DB to the configured local-history window."""
    retention_days = max(7, int(retention_days))
    db = _octopus_cursor()
    cutoff = f"{retention_days} days"
    db.execute(
        f"DELETE FROM octopus_consumption WHERE interval_start < now() - INTERVAL '{cutoff}'"
    )
    db.execute(
        f"DELETE FROM octopus_rates WHERE valid_to IS NOT NULL AND valid_to < now() - INTERVAL '{cutoff}'"
    )
    # Demand samples only need to outlive the 48h live window + a margin
    db.execute(
        "DELETE FROM octopus_telemetry WHERE ts < now() - INTERVAL '7 days'"
    )
    logger.info(f"Octopus data pruned (retention={retention_days} days)")


def flush_appender():
    """Drain the Rust appender's row buffers to disk. No-op when fallback active."""
    if _appender is not None:
        try:
            _appender.flush()
        except Exception as e:
            logger.warning(f"Appender flush failed: {e}")

def close():
    """Close the database connections."""
    global _db, _appender, _octopus_db, _system_db, _system_appender
    if _appender is not None:
        try:
            _appender.flush()
        except Exception as e:
            logger.warning(f"Final appender flush failed: {e}")
        _appender = None
    if _octopus_db:
        _octopus_db.close()
        _octopus_db = None
        logger.info("Octopus database closed")
    if _system_appender is not None:
        try:
            _system_appender.flush()
        except Exception as e:
            logger.warning(f"System appender flush failed: {e}")
        _system_appender = None
    if _system_db:
        _system_db.close()
        _system_db = None
        logger.info("System metrics database closed")
    if _db:
        _db.close()
        _db = None
        logger.info("Telemetry database closed")

# Outdoor weather history helpers

OUTDOOR_TEMP_IEEE = "__weather__"
OUTDOOR_TEMP_ATTR = "outdoor_temperature_c"


def query_outdoor_temperature_history(hours: int = 72) -> List[Dict]:
    """Outdoor temperature history (synthetic IEEE)."""
    return query_device_state_history(OUTDOOR_TEMP_IEEE, OUTDOOR_TEMP_ATTR, hours)


def build_outdoor_temp_getter(hours: int = 72):
    """
    Return a callable(unix_ts) -> Optional[float] that resolves the closest
    outdoor temperature reading at-or-before the given timestamp.

    Used by thermal_profile.compute_profile and similar fits — replaces the
    constant-outdoor proxy that biased fits on swingy days.
    """
    rows = query_outdoor_temperature_history(hours)
    if not rows:
        return lambda _ts: None

    import bisect
    from datetime import datetime as _dt

    ts_list: List[float] = []
    val_list: List[Optional[float]] = []
    for r in rows:
        v = r.get("numeric_val")
        if v is None:
            continue
        t = r.get("ts")
        if isinstance(t, _dt):
            ts_list.append(t.timestamp())
        else:
            try:
                ts_list.append(float(t))
            except (TypeError, ValueError):
                continue
        val_list.append(float(v))

    if not ts_list:
        return lambda _ts: None

    def _getter(ts_seconds: float) -> Optional[float]:
        idx = bisect.bisect_right(ts_list, ts_seconds) - 1
        if idx < 0:
            return val_list[0]
        return val_list[idx]

    return _getter

class _FatalWatchHandler(logging.Handler):
    """Latch a DB-fatal however it was reported.

    Every write helper catches and logs its own failure, so a fatal can surface
    from any of a dozen bespoke except blocks — and the ones that never touch
    _write_exec (prune's DELETEs, readers, module-specific writers) would
    otherwise never reach _note_fatal at all. Rather than trusting a dozen call
    sites to remember, watch what they log: if the signature goes past, latch
    it.

    Missing the latch is not cosmetic: no latch means no sentinel, which means
    the next boot does not self-repair and the database stays broken
    indefinitely.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _db_fatal is not None:
                return                      # already latched (also stops recursion)
            msg = record.getMessage()
            if "has been invalidated" in msg or "Corrupt database file" in msg:
                _note_fatal(Exception(msg))
        except Exception:
            pass                            # never let diagnostics break logging


_fatal_watch_installed = False


def install_fatal_watch() -> None:
    """Attach the catch-all fatal watcher to the root logger (idempotent)."""
    global _fatal_watch_installed
    if _fatal_watch_installed:
        return
    logging.getLogger().addHandler(_FatalWatchHandler())
    _fatal_watch_installed = True
    logger.debug("Telemetry fatal-state watcher installed")
