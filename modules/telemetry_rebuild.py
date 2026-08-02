"""
Telemetry database salvage / rebuild.

A DuckDB file with a corrupt block cannot be repaired in place, so the only way
back is to copy what is still readable into a new file. The damaged file is
opened READ_ONLY and never written, the copy runs inside DuckDB to preserve
timestamps, and nothing is swapped in until the result is verified.
Used by auto_rebuild_if_needed() at startup and by the manual CLI.
See docs/telemetry_database.md.
"""


from __future__ import annotations

import os
import shutil
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# Below this many rows a failing range is written off rather than bisected
# further. Each bisection step costs a failed query (which is slow — DuckDB
# raises only after hitting the bad block), so chasing the last few hundred
# rows of a damaged row group costs more time than the rows are worth.
MIN_CHUNK = 2048

# Used when the row count cannot be read because the damage sits in the way.
FALLBACK_UPPER_BOUND = 1 << 40


# Budget for the automatic startup rebuild, which runs before uvicorn serves, so
# every second is one the app cannot answer /api/system/health. It must clear the
# container HEALTHCHECK (~120 s), the manager watchdog (~240 s) and upgrade.sh
# do_swap (300 s, which ROLLS BACK). An overrun is abandoned with the original
# untouched and the sentinel kept. The manual CLI has no budget.
REBUILD_BUDGET_SECONDS = float(os.environ.get("ZMM_TELEMETRY_REBUILD_BUDGET", "90"))


class RebuildTimeout(Exception):
    """Raised when a rebuild exceeds its budget and must be abandoned."""


class Report:
    """Per-table outcome, for the summary at the end."""

    def __init__(self, table: str):
        self.table = table
        self.copied = 0
        self.lost_ranges: List[Tuple[int, int]] = []
        self.error: Optional[str] = None

    @property
    def lost_estimate(self) -> int:
        return sum(hi - lo for lo, hi in self.lost_ranges)

    @property
    def clean(self) -> bool:
        return not self.lost_ranges and self.error is None


# Salvage core

def salvage_table(
    table: str,
    upper_bound: int,
    copy_range: Callable[[int, int], int],
    log: Callable[[str], None] = print,
    min_chunk: int = MIN_CHUNK,
    deadline: Optional[float] = None,
) -> Report:
    """Copy `table` via copy_range(lo, hi), bisecting around unreadable rows.

    copy_range must copy rows with lo <= rowid < hi and return how many it
    copied, or raise if DuckDB cannot read that span. Kept free of any DuckDB
    dependency so the bisection can be tested on its own.
    """
    rep = Report(table)

    # Fast path: the whole table in one statement.
    try:
        rep.copied = copy_range(0, upper_bound)
        return rep
    except Exception as e:
        log(f"    whole-table copy failed ({_brief(e)}) — bisecting to save what is readable")

    # Depth-first bisection. An explicit stack keeps the recursion bounded and
    # the traversal ordered, so the log reads front-to-back through the table.
    stack: List[Tuple[int, int]] = [(0, upper_bound)]
    while stack:
        # Checked between chunks, not inside them: a single INSERT...SELECT is
        # not interruptible, so this bounds the search, not one statement.
        if deadline is not None and time.monotonic() > deadline:
            raise RebuildTimeout(
                f"exceeded budget while salvaging {table} "
                f"({rep.copied:,} rows recovered so far)")
        lo, hi = stack.pop()
        try:
            rep.copied += copy_range(lo, hi)
            continue
        except Exception:
            pass

        if hi - lo <= min_chunk:
            rep.lost_ranges.append((lo, hi))
            log(f"    unreadable: rowid {lo:,}–{hi:,} ({hi - lo:,} rows) — skipped")
            continue

        mid = lo + (hi - lo) // 2
        # Push high half first so the low half is processed first on pop.
        stack.append((mid, hi))
        stack.append((lo, mid))

    # Damage that straddles a bisection boundary lands in two adjacent spans.
    # Merge them so the summary counts damaged *regions*, not the arbitrary
    # points at which the search happened to divide.
    rep.lost_ranges = _coalesce(rep.lost_ranges)
    return rep


def _coalesce(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[Tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _brief(e: Exception, limit: int = 110) -> str:
    s = " ".join(str(e).split())
    return s if len(s) <= limit else s[:limit] + "…"


# DuckDB plumbing

def _src_tables(con) -> List[str]:
    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = 'src' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _src_columns(con, table: str) -> List[str]:
    rows = con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE database_name = 'src' AND table_name = ? ORDER BY column_index",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _dest_columns(con, table: str) -> List[str]:
    # Anything that isn't the attached source is the output database. Its
    # catalog name is derived from the filename, so match by exclusion rather
    # than trying to reconstruct it.
    rows = con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE database_name <> 'src' AND table_name = ? ORDER BY column_index",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _upper_bound(con, table: str, log: Callable[[str], None]) -> int:
    """Highest rowid + 1, without letting the damage stop us."""
    try:
        hi = con.execute(f'SELECT max(rowid) FROM src."{table}"').fetchone()[0]
        if hi is not None:
            return int(hi) + 1
        return 0
    except Exception:
        pass

    # max() has to scan; if the damage blocks it, probe upward instead. Each
    # probe only touches high row groups, which DuckDB prunes to cheaply.
    log("    row count unreadable — probing for the end of the table")
    bound = 1 << 16
    while bound < FALLBACK_UPPER_BOUND:
        try:
            row = con.execute(
                f'SELECT 1 FROM src."{table}" WHERE rowid >= {bound} LIMIT 1'
            ).fetchone()
            if row is None:
                return bound
            bound <<= 1
        except Exception:
            bound <<= 1
    return FALLBACK_UPPER_BOUND


def rebuild(src: str, out: str, log: Callable[[str], None] = print,
            deadline: Optional[float] = None) -> List[Report]:
    import duckdb

    # Schema comes from the application itself, so the rebuilt file matches
    # exactly what the app expects — defaults, types and all.
    from modules.telemetry_db import _init_tables

    con = duckdb.connect(out)
    try:
        _init_tables(con)
        # READ_ONLY is the load-bearing word: the damaged file is only ever
        # read, so a failed rebuild leaves it exactly as it was.
        con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")

        reports: List[Report] = []
        for table in _src_tables(con):
            dest_cols = _dest_columns(con, table)
            if not dest_cols:
                log(f"  {table}: not part of the current schema — skipped")
                continue

            src_cols = _src_columns(con, table)
            cols = [c for c in dest_cols if c in src_cols]
            if not cols:
                log(f"  {table}: no columns in common — skipped")
                continue

            collist = ", ".join(f'"{c}"' for c in cols)
            log(f"  {table}: copying {len(cols)} column(s)")

            def copy_range(lo: int, hi: int, _t=table, _c=collist) -> int:
                cur = con.execute(
                    f'INSERT INTO main."{_t}" ({_c}) '
                    f'SELECT {_c} FROM src."{_t}" '
                    f"WHERE rowid >= {lo} AND rowid < {hi}"
                )
                # DuckDB reports affected rows via the statement result.
                res = cur.fetchall()
                return int(res[0][0]) if res and res[0] else 0

            bound = _upper_bound(con, table, log)
            rep = salvage_table(table, bound, copy_range, log=log, deadline=deadline)
            reports.append(rep)

            if rep.clean:
                log(f"    recovered {rep.copied:,} rows (complete)")
            else:
                log(f"    recovered {rep.copied:,} rows, "
                    f"~{rep.lost_estimate:,} unreadable in "
                    f"{len(rep.lost_ranges)} span(s)")

        con.execute("DETACH src")
        return reports
    finally:
        con.close()


def verify(path: str, log: Callable[[str], None] = print) -> bool:
    """Reopen the rebuilt file read-only and read every table back."""
    import duckdb
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as e:
        log(f"  FAILED to reopen: {_brief(e)}")
        return False
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY table_name"
        ).fetchall()]
        for t in tables:
            n = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            log(f"  {t}: {n:,} rows")
        return True
    except Exception as e:
        log(f"  FAILED reading back: {_brief(e)}")
        return False
    finally:
        con.close()


def install(src: str, out: str, log: Callable[[str], None] = print) -> None:
    """Swap the rebuild in, preserving the original and its stale WAL.

    Ordered so that a crash at any point leaves a valid database at `src` — it
    runs during startup, where the watchdog may restart the app underneath it.
    Renaming the original out of the way first (the obvious order) would leave
    a window with no database at all.
    """
    stamp = int(time.time())
    kept = f"{src}.damaged-{stamp}"

    # 1. Preserve the original under a second name. A hard link is a new name
    #    for the same inode — nothing is copied, and `src` stays valid.
    try:
        os.link(src, kept)
    except OSError:
        shutil.copy2(src, kept)          # different filesystem / no link support
    log(f"  original preserved: {kept}")

    # 2. Move the stale WAL aside BEFORE the swap, never after. Crashing
    #    between the swap and the WAL move would leave DuckDB replaying the old
    #    file's WAL against the new database on the next boot — the exact
    #    failure this whole exercise started with.
    wal = src + ".wal"
    if os.path.exists(wal):
        os.replace(wal, f"{kept}.wal")
        log(f"  stale WAL moved aside: {kept}.wal")

    # 3. Single atomic swap. `src` is a valid database before and after, and is
    #    never absent in between.
    os.replace(out, src)
    log(f"  rebuilt database installed: {src}")


# Automatic startup recovery

def auto_rebuild_if_needed(log: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """Rebuild a fatally-damaged telemetry DB, if one was flagged.

    Call this during startup and NOWHERE else. It moves database files around,
    which is only safe while nothing has the file open — and a corrupt DuckDB
    cannot be rebuilt from inside the process that is already stuck on it,
    because the first fatal error invalidates every connection until restart.

    The flow across a restart is:
      1. A write hits the damage; telemetry_db._note_fatal() latches it and
         drops a sentinel naming the reason.
      2. Everything keeps running — devices, automations, heating are all
         unaffected — but telemetry stops recording until restart.
      3. On the next boot this runs first, salvages what is readable into a
         fresh file, installs it, and clears the sentinel.

    Returns a summary dict when it rebuilt, else None. Never raises: a failed
    rebuild leaves the original in place and the app boots without telemetry
    rather than not booting at all.
    """
    import logging
    logger = logging.getLogger("modules.telemetry_rebuild")
    say = log or logger.info

    from modules.telemetry_db import DB_PATH, REBUILD_SENTINEL

    try:
        if not os.path.exists(REBUILD_SENTINEL):
            return None
        try:
            reason = open(REBUILD_SENTINEL).read().strip()
        except OSError:
            reason = "(reason unavailable)"

        if not os.path.exists(DB_PATH):
            os.remove(REBUILD_SENTINEL)          # nothing to rebuild
            return None

        say(f"Telemetry database was flagged as damaged — rebuilding. Reason: {reason}")
        out = f"{os.path.splitext(DB_PATH)[0]}.rebuilt.duckdb"
        for stale in (out, out + ".wal"):
            if os.path.exists(stale):
                os.remove(stale)

        started = time.time()
        try:
            reports = rebuild(DB_PATH, out, log=say,
                              deadline=time.monotonic() + REBUILD_BUDGET_SECONDS)
        except RebuildTimeout as e:
            # Boot healthy rather than risk the watchdog restarting us mid-swap
            # or an upgrade rolling back. The sentinel stays, so this is
            # retried next boot; the CLI can do it unbudgeted meanwhile.
            say(f"Telemetry rebuild abandoned: {e}")
            for partial in (out, out + ".wal"):
                if os.path.exists(partial):
                    try:
                        os.remove(partial)
                    except OSError:
                        pass
            _alert_rebuild_timeout(str(e))
            return None

        if not verify(out, log=say):
            say("Rebuilt database failed verification — keeping the original untouched.")
            _alert_rebuild_failed(reason, "the rebuilt file could not be read back")
            return None

        install(DB_PATH, out, log=say)
        try:
            os.remove(REBUILD_SENTINEL)
        except OSError:
            pass

        recovered = sum(r.copied for r in reports)
        lost = sum(r.lost_estimate for r in reports)
        damaged = [r for r in reports if not r.clean]
        elapsed = time.time() - started
        say(f"Telemetry rebuild complete: {recovered:,} rows recovered, "
            f"~{lost:,} unreadable, in {elapsed:.1f}s")

        _alert_rebuilt(recovered, lost, damaged, elapsed)
        return {"recovered": recovered, "lost": lost, "seconds": elapsed,
                "tables": {r.table: r.copied for r in reports}}

    except Exception as e:
        say(f"Telemetry rebuild failed: {e}")
        _alert_rebuild_failed("", str(e))
        return None


def _alert_rebuilt(recovered: int, lost: int, damaged: List[Report], elapsed: float) -> None:
    try:
        from modules.app_alerts import raise_alert
        if lost:
            detail = "\n".join(f"  • {r.table}: ~{r.lost_estimate:,} rows unreadable"
                               for r in damaged)
            message = (
                f"The telemetry database was damaged and has been rebuilt "
                f"automatically.\n\n"
                f"Recovered {recovered:,} rows in {elapsed:.1f}s. Approximately "
                f"{lost:,} rows could not be read:\n{detail}\n\n"
                f"The damaged file was kept alongside the new one, and history "
                f"is recording normally again."
            )
            severity = "warning"
        else:
            message = (
                f"The telemetry database was damaged and has been rebuilt "
                f"automatically. All {recovered:,} rows were recovered in "
                f"{elapsed:.1f}s — nothing was lost. The damaged file was kept "
                f"alongside the new one."
            )
            severity = "info"
        raise_alert(severity=severity, source="telemetry_db",
                    title="Telemetry database rebuilt", message=message,
                    dedupe_key=f"telemetry_db:rebuilt:{int(time.time())}")
    except Exception:
        pass


def _alert_rebuild_failed(reason: str, detail: str) -> None:
    try:
        from modules.app_alerts import raise_alert
        raise_alert(
            severity="error", source="telemetry_db",
            title="Telemetry database rebuild failed",
            message=(
                f"The telemetry database is damaged and the automatic rebuild "
                f"did not succeed: {detail}\n\n"
                f"The original file has not been modified. History will not "
                f"record until this is resolved. Run "
                f"scripts/rebuild_telemetry_db.py manually to inspect it."
                + (f"\n\nOriginal fault: {reason}" if reason else "")
            ),
            dedupe_key="telemetry_db:rebuild_failed",
        )
    except Exception:
        pass


def _alert_rebuild_timeout(detail: str) -> None:
    try:
        from modules.app_alerts import raise_alert
        raise_alert(
            severity="warning", source="telemetry_db",
            title="Telemetry rebuild took too long — postponed",
            message=(
                f"The automatic rebuild of the damaged telemetry database was "
                f"abandoned so it could not delay startup: {detail}\n\n"
                f"Nothing was changed — the original database is untouched and "
                f"the app has started normally. History will not record until "
                f"the rebuild completes.\n\n"
                f"Run scripts/rebuild_telemetry_db.py --install with the app "
                f"stopped (it has no time limit), or raise the budget with "
                f"ZMM_TELEMETRY_REBUILD_BUDGET (currently "
                f"{REBUILD_BUDGET_SECONDS:.0f}s)."
            ),
            dedupe_key="telemetry_db:rebuild_timeout",
        )
    except Exception:
        pass


# In-place repair

def repair_in_place(db_path: str, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Excise damage from a database in place, without rebuilding the file.

    Dropping a damaged table frees its blocks, and once nothing references them
    a CHECKPOINT succeeds again — which is the actual cure, because a database
    that cannot checkpoint grows its WAL forever and eventually leaves one that
    cannot be replayed. That is what cost 22-23 July.

    Cheaper than rebuild(): no second file, no swap, no downtime beyond the
    restart you were doing anyway, and rows in undamaged tables never move.

    ORDER IS CRITICAL, and is why this looks fussy:

      1. Salvage the readable rows of the damaged table into a staging table.
      2. DROP the damaged table.
      3. Recreate it from the app's own schema and restore the salvaged rows.
      4. Only THEN checkpoint.

    Never checkpoint before the drop. The first failed checkpoint marks the
    whole database invalidated, after which every statement — including the
    DROP that would have fixed it — raises until the process restarts.

    The caller must guarantee no other connection is open (startup, or the app
    stopped). Returns a summary; raises only if the repair could not proceed.
    """
    import duckdb
    from modules.telemetry_db import _init_tables

    con = duckdb.connect(db_path)
    summary: Dict[str, Any] = {"damaged": [], "salvaged": 0, "dropped_rows": 0,
                               "checkpointed": False}
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()]

        # Probe each table by streaming every row out of it. count(*) is NOT
        # good enough — DuckDB answers it from row-group metadata without ever
        # reading the data blocks, so a corrupt block sails straight past it.
        # Streaming in batches forces the reads while staying memory-bounded.
        damaged: List[str] = []
        for t in tables:
            try:
                cur = con.execute(f'SELECT * FROM "{t}"')
                while cur.fetchmany(10_000):
                    pass
            except Exception as e:
                if "invalidated" in str(e):
                    raise RuntimeError(
                        "database was already invalidated before the repair "
                        "began — restart the process and retry") from e
                log(f"  {t}: unreadable ({_brief(e)})")
                damaged.append(t)

        if not damaged:
            log("  no damaged tables found — nothing to repair")
            return summary
        summary["damaged"] = damaged

        for t in damaged:
            staging = f"_repair_{t}"
            con.execute(f'DROP TABLE IF EXISTS "{staging}"')
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() "
                "WHERE table_name = ? ORDER BY column_index", [t]).fetchall()]
            collist = ", ".join(f'"{c}"' for c in cols)

            con.execute(f'CREATE TABLE "{staging}" AS SELECT {collist} FROM "{t}" LIMIT 0')

            def copy_range(lo: int, hi: int, _t=t, _s=staging, _c=collist) -> int:
                cur = con.execute(
                    f'INSERT INTO "{_s}" ({_c}) SELECT {_c} FROM "{_t}" '
                    f"WHERE rowid >= {lo} AND rowid < {hi}")
                res = cur.fetchall()
                return int(res[0][0]) if res and res[0] else 0

            bound = _upper_bound_local(con, t, log)
            rep = salvage_table(t, bound, copy_range, log=log)
            log(f"  {t}: salvaged {rep.copied:,} rows, "
                f"~{rep.lost_estimate:,} unreadable")
            summary["salvaged"] += rep.copied
            summary["dropped_rows"] += rep.lost_estimate

            # Free the damaged blocks. This is the step that makes CHECKPOINT
            # possible again.
            con.execute(f'DROP TABLE "{t}"')
            _init_tables(con)                       # CREATE TABLE IF NOT EXISTS
            con.execute(f'INSERT INTO "{t}" ({collist}) SELECT {collist} FROM "{staging}"')
            con.execute(f'DROP TABLE "{staging}"')
            log(f"  {t}: rebuilt in place with {rep.copied:,} rows")

        con.execute("CHECKPOINT")
        summary["checkpointed"] = True
        log("  CHECKPOINT succeeded — WAL folded into the database")
        return summary
    finally:
        try:
            con.close()
        except Exception:
            pass


def _upper_bound_local(con, table: str, log: Callable[[str], None]) -> int:
    """_upper_bound() for a table in the primary database rather than 'src'."""
    try:
        hi = con.execute(f'SELECT max(rowid) FROM "{table}"').fetchone()[0]
        return int(hi) + 1 if hi is not None else 0
    except Exception:
        pass
    log("    row count unreadable — probing for the end of the table")
    bound = 1 << 16
    while bound < FALLBACK_UPPER_BOUND:
        try:
            if con.execute(f'SELECT 1 FROM "{table}" WHERE rowid >= {bound} '
                           f"LIMIT 1").fetchone() is None:
                return bound
            bound <<= 1
        except Exception:
            bound <<= 1
    return FALLBACK_UPPER_BOUND
