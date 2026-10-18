# Audit tools

Read-only checks for defects the tests don't reach. None of them import the
app, so they run on the dev box; the DuckDB ones need the lockfile venv
(see AGENTS.md, "The dev box").

| Tool | Finds | Needs |
|---|---|---|
| `blocking_calls.py` | blocking work reachable from the event loop: telemetry DB access, `time.sleep`, subprocess, blocking sockets, called from an `async def` or from sync code the loop runs (zigpy listeners, `call_soon`/`call_later` targets) | stdlib |
| `sql_check.py` | SQL that won't bind against the schema the code creates: missing columns, tables created nowhere. `--db` binds against a real snapshot instead | duckdb |
| `telemetry_health.py` | data problems in a telemetry snapshot: future or timezone-shifted rows, rows past retention, duplicates, lost `numeric_val`, out-of-range readings, silent devices | duckdb |
| `docs_drift.py` | frontend calls and doc-named routes no backend serves, `name.md §N` citations to missing sections, doc paths and functions that don't exist, out-of-order section numbers, `*_DEFAULTS` mirrors disagreeing with `config/config.yaml` | stdlib; PyYAML for the config check |

    python3 scripts/audit/blocking_calls.py            # --all adds the accepted zigbee_cache reads
    python3 scripts/audit/docs_drift.py                # --undocumented lists routes nothing mentions
    <venv>/bin/python scripts/audit/sql_check.py       # --all adds templated SQL it can't expand
    <venv>/bin/python scripts/audit/telemetry_health.py snapshot/telemetry.duckdb

Each prints `file:line` findings, and `--json` gives the same as a list.

## Limits

Static reachability can't see a callable that was injected (a `telemetry_query=`
lambda), and it stops at a function that checks `_on_event_loop()`, because
that function chooses its path at runtime. A clean run means no path was
found, not that none exists.

`telemetry_health.py` must run on a copy. The app holds the DuckDB write lock,
so copy the `.duckdb` file and its `.wal` together. For an evidence-grade look
at the same copy (hashes, storage, per-column statistics), the rene fork has
`rea inspect-duckdb-database COPY.duckdb --table device_states --summarize`.
It needs a DuckDB 1.x CLI in `REA_DUCKDB_COMMAND`.

Tests: `tests/audit/run_all.py`.
