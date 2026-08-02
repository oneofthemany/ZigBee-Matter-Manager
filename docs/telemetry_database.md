# Telemetry Databases — Resilience, Repair & Recovery

## Overview

ZigBee Matter Manager stores history in DuckDB files under `data/`. Almost all of
what follows happens **without you doing anything** — the app detects damage,
repairs what it can, tells you what it did, and tidies up after itself a week
later.

You are reading this because an alert pointed you here, or because you want to
know what the app is doing to your data. If you only remember one thing:

> **A damaged telemetry database never stops the app.** Devices, automations,
> heating and Matter all keep working. Only history is affected.

---

## The files, and why there is more than one

| File | Holds | Retention |
|---|---|---|
| `data/telemetry.duckdb` | Device attribute history, packet stats, heating ticks, spectrum scans | 90 days |
| `data/system_metrics.duckdb` | Host CPU, memory, disk, load | 90 days |
| `data/octopus.duckdb` | Energy consumption and tariff rates | 400 days |

They are separate files on purpose, and it is the single most important design
decision here.

**A single damaged block anywhere in a DuckDB file makes the whole file
uncheckpointable.** When DuckDB cannot checkpoint, its write-ahead log (`.wal`)
grows without bound, and a WAL that grows past the point of being replayable
loses *every* table's recent writes — not just the table that was damaged.

Host CPU samples are the least valuable rows in the system. Energy history is
the most valuable, and the hardest to re-fetch. Keeping them in one file would
mean a bad block under `cpu_percent` could take a year of energy data with it.
Different write rates, different value, different lifetimes → different files.

---

## What can go wrong

There are two distinct faults. They look similar in the log and have very
different consequences.

### 1. An unreplayable WAL

The main database file is fine, but the WAL beside it cannot be replayed:

```
Failure while replaying WAL file "..."
Failure while replaying checkpoint WAL file "...": checkpoint WAL cannot
contain a checkpoint marker
```

Usually caused by the write backend changing under an existing database
(installing or removing the `zmm_telemetry` wheel, or setting
`ZMM_TELEMETRY_BACKEND=python`), because the engine that wrote the WAL is not
the engine now trying to read it.

**What the app does:** sets the WAL aside as
`<db>.wal.unreplayable-<timestamp>`, opens the database without it, and carries
on. You get one warning alert naming the quarantined file and its size.

**What it costs:** any rows that were only ever in that WAL — everything written
since the last successful checkpoint. That can be hours or days. The file is
kept, never deleted at the time, because it is the only remaining copy of those
rows.

### 2. A corrupt block

```
IO Error: Corrupt database file: computed checksum 5381 does not match
stored checksum 0 in block at location 7876608
```

A block inside the database file itself is unreadable. This is the more serious
fault, because it makes checkpointing impossible, which eventually produces
fault 1 above and loses everything since the last good checkpoint.

**What the app does:** flags the database for repair and rebuilds it on the next
restart, keeping every row it can still read.

> ### Do not "fix" this with CHECKPOINT
>
> It is the obvious idea and it makes things much worse. `CHECKPOINT` forces
> DuckDB to write blocks it otherwise leaves alone. Hitting the damaged one
> fails **fatally**: DuckDB marks the entire database invalidated, and from that
> moment every read and write in the process raises — including ones that were
> working perfectly a moment earlier — until the app restarts. On a database
> whose damage is dormant, this converts a survivable problem into a total
> outage that repeats on every boot.

---

## What happens automatically

Nothing in this section needs a human.

**On open** — an unreplayable WAL is quarantined and the database opens without
it (both wordings of the error are recognised).

**On a fatal error** — the failure is latched the first time it appears, from
whichever code path reported it, and a sentinel is written. You get one alert
instead of hundreds of identical log lines, and telemetry writes stop being
buffered for a database that cannot accept them.

**On the next startup** — if that sentinel exists, the database is rebuilt
before anything opens it: readable rows are copied into a fresh file, the
original is preserved as `<db>.damaged-<timestamp>`, and its stale WAL is moved
aside with it. The rebuild is verified by reopening it and reading every table
back *before* anything is swapped in. If verification fails, the original is
left exactly as it was.

**Startup migrations** — moving a table into its own database (as
`system_metrics` was) runs on a worker thread during startup, never lazily on
first use. It salvages around any damage rather than reading in one pass.

**Reconciliation** — a dedicated thread records which write backend is in use
(`data/.telemetry_backend`) so a future backend change is detected and reported
rather than surfacing as a mysterious WAL error.

**Cleanup** — quarantined WALs and pre-rebuild copies are deleted automatically
once they are older than 7 days, but **only while the database is healthy**. If
the live file is broken, the quarantined copy may be the better one, so nothing
is swept.

---

## Reading the alerts

| Alert | Severity | Do you need to act? |
|---|---|---|
| Telemetry WAL could not be replayed | warning | No. Rows only in that WAL are gone; everything else is intact. The file is named in the alert and kept for 7 days. |
| Telemetry database unusable — rebuild required | error | No, but **restart when convenient**. It repairs itself on the next boot. |
| Telemetry database rebuilt | info / warning | No. Reports how many rows were recovered and how many were unreadable. |
| Telemetry rebuild took too long — postponed | warning | Only if it recurs. Run the CLI with the app stopped (it has no time limit). |
| Telemetry rebuild failed | error | Yes. The original is untouched; inspect with the CLI. |
| Telemetry database warm-up failed | warning | Usually no. History may be unavailable and the first query slow. |
| Telemetry write backend changed | info | No. Confirmation that a backend switch reconciled cleanly. |

---

## Manual tooling

`scripts/rebuild_telemetry_db.py` is the same engine the app runs automatically.
Reach for it when you want to inspect before committing, when an automatic
rebuild was postponed for taking too long, or when you would rather repair
deliberately than wait for a restart.

**Which mode?**

| Situation | Command |
|---|---|
| Find out what is recoverable, change nothing | *(no flags — the default)* |
| Damage spans several tables, or you want a clean file | `--install` |
| Damage is confined to one table and you want to keep the file | `--in-place` |
| Reclaim space from old quarantined files now | `--cleanup-quarantine` |

```bash
# Inspect only. Opens the database READ-ONLY — cannot make anything worse.
python3 scripts/rebuild_telemetry_db.py

# Rebuild into a fresh file and swap it in. Original kept as .damaged-<ts>.
python3 scripts/rebuild_telemetry_db.py --install

# Repair in place: drop and rebuild only the damaged tables, keeping the file.
python3 scripts/rebuild_telemetry_db.py --in-place

# Housekeeping (the app does this itself once files pass the retention window).
python3 scripts/rebuild_telemetry_db.py --cleanup-quarantine --dry-run
python3 scripts/rebuild_telemetry_db.py --cleanup-quarantine --retention-days 1
```

**Stop the app before `--install` or `--in-place`.** Both need write access, and
swapping a file under a running process leaves it holding a deleted inode and
writing into nowhere. The default inspect mode is safe at any time.

### How much survives?

Tables are copied whole where possible. Only if that fails does the tool bisect
the table by row id to find the damage, so one bad row group costs that row
group rather than the table or the database. In practice recovery is typically
well above 99%.

The bisection stops subdividing at 2,048 rows (`MIN_CHUNK` in
`modules/telemetry_rebuild.py`) and writes off the whole span. Every bisection
step costs a failing query, and DuckDB only raises *after* reaching the bad
block, so chasing the last few hundred rows is slow. Some rows inside a
written-off span were probably readable — that is a deliberate trade of a small
amount of data for boot time. Lower the constant, or use the CLI (no time
budget), if you are recovering something that matters more than host metrics.

### What cannot be recovered

- **Rows inside a quarantined WAL.** DuckDB exposes no way to replay a WAL it
  has already rejected. Reading one offline with a matching DuckDB build is the
  only theoretical route, and the odds are poor.
- **Rows inside a damaged block.** The data is not there to read.

Neither the automatic repair nor the CLI can retrieve these. Anything that
claims otherwise is misreading what was recovered.

---

## Upgrade safety

Because a bad build can damage data or fail to stay up, the upgrade path has its
own guardrails.

**Stability soak.** After a new version first reports healthy, it is watched for
a further 180 seconds before the swap is accepted. Passing a health check only
proves the app *booted* — it wants two passes three seconds apart, roughly six
seconds of evidence. A build that boots cleanly and dies a minute later would
pass that and then be committed, with rollback no longer automatic. If the app
stops answering or restarts during the soak, the upgrade rolls back by itself.

**Crash-loop detection.** A crash after the app has been up a while is treated
as a one-off and simply restarted. Three of them inside 15 minutes is a loop
that restarting will not fix, so the launcher stops and enters recovery standby
on `:8000`, pointing at the ZMM Manager on `:8001`.

**Automatic rollback.** If that loop began within 6 hours of an upgrade, the new
version is the prime suspect and the previous image is requested automatically.
It is attempted **once per version**, so it can never ping-pong between two
images, and it is skipped entirely for a version that has been running for days
— a box that was fine for a week and starts looping has a different problem, and
reverting the code would hide it.

---

## Tuning

| Variable | Default | Effect |
|---|---|---|
| `ZMM_TELEMETRY_REBUILD_BUDGET` | `90` | Seconds the startup rebuild may take before postponing itself. It runs before the app serves, so it must finish well inside the health and watchdog deadlines. |
| `ZMM_QUARANTINE_RETENTION_DAYS` | `7` | How long quarantined WALs and pre-rebuild copies are kept. |
| `ZMM_STABILITY_SOAK` | `180` | Seconds a new version must stay healthy before a swap is accepted. |
| `ZMM_HEALTH_TIMEOUT` | `300` | Seconds to wait for a new version to become healthy at all. |
| `ZMM_TELEMETRY_BACKEND` | *(unset)* | Set to `python` to force the executemany path instead of the Rust appender. Expect a WAL quarantine on the first boot after changing this. |

---

## Files you may see in `data/`

| Pattern | What it is |
|---|---|
| `*.duckdb`, `*.duckdb.wal` | Live database and its write-ahead log. **Never delete these.** |
| `*.wal.unreplayable-<ts>` | A quarantined WAL. Swept after the retention window. |
| `*.wal.corrupt-<ts>` | The same thing under an older name. |
| `*.damaged-<ts>`, `*.damaged-<ts>.wal` | A pre-rebuild copy of the database. Swept after the retention window. |
| `.telemetry_rebuild_needed` | Sentinel: rebuild on the next start. Removed once done. |
| `.telemetry_backend` | Which write backend last wrote, used to detect a switch. |

A large `.wal` next to a much smaller `.duckdb` is the signature worth knowing:
it means checkpointing has been failing for some time, and it is the state that
precedes losing data. If you see it and no alert has fired, run the CLI in
inspect mode.

---

## Health endpoint

`GET /api/system/health` reports the write buffer under `telemetry_buffer`:

```json
{
  "buffered": 0, "capacity": 50000, "high_water": 12,
  "dropped": 0, "write_failures": 0, "last_batch": 4,
  "last_drain_age_s": 1.2, "db_fatal": false
}
```

`buffered` climbing, or a non-zero `dropped`, means the database is not keeping
up with writes. `db_fatal: true` means it has gone unusable and will be repaired
on the next restart.

Device telemetry is buffered in memory and written by a background worker, never
on the event loop — a slow or wedged database delays history by a couple of
seconds instead of stalling the whole application.

## Implementation notes

Extracted from `modules/telemetry_db.py` so the module itself stays terse.

### Why three database files

Octopus data, host system metrics and device telemetry each live in their own
DuckDB file.

Octopus write-collides with the high-frequency telemetry writers (the Rust
appender) if they share a file, and a corrupted `telemetry.duckdb` must not
take a year of energy history with it. The data is tiny (≤ ~50 rows/day/fuel)
and useful year-on-year, so default retention is long and user-configurable via
`octopus.retention_days` → `prune_octopus()`.

Host metrics are separated for a sharper reason. A single damaged block
anywhere in a DuckDB file makes the **whole file uncheckpointable**: the WAL
then grows without bound, and a WAL that cannot be replayed loses every table's
recent writes, not just the damaged one. Host CPU and memory samples are the
least valuable rows in the system, so they must not be able to take device
history down with them.

Different write patterns, different value, different lifetimes: different files.

### Backend selection

Precedence, highest first:

1. `ZMM_TELEMETRY_BACKEND=python` → force the Python `executemany` fallback
2. `zmm_telemetry` wheel installed → use the Rust appender
3. Otherwise → Python `executemany` fallback

This allows reverting from Rust to Python without rebuilding the image: set
`ZMM_TELEMETRY_BACKEND=python` in the systemd unit or container env and
restart. The schema is identical between backends, so the existing
`telemetry.duckdb` continues to work either way.

### Write-backend reconciliation

The write backend can change under a database already on disk — installing or
removing the wheel, or setting the env var, switches paths. The engine that
wrote the existing WAL may not be the one now trying to replay it, which
surfaces as the "checkpoint WAL" open failure `_is_unreplayable_wal_error()`
catches.

Reconciliation runs on its own dedicated thread because every step can be slow
(open, migration, checkpoint) and none of it may ever land on the event loop —
that is precisely the stall that trips `loop_monitor`'s exit-70.

The connection is opened *before* the backend is read: which engine writes is
only settled once the connection is up, since `_finish_db_init` decides whether
the appender is opened.

### Do not add an explicit CHECKPOINT to the unreplayable-WAL path

It is tempting — an unreplayable WAL is usually one that never got folded into
the main file, so "just checkpoint it" looks like the cure. DuckDB checkpoints
on its own when it can. If the WAL is growing without bound, that is a signal
the database needs rebuilding, not a signal to force the operation that is
failing on it.

### Quarantine, not deletion

An unreplayable WAL is labelled "unreplayable", not "corrupt": the usual cause
is a backend switch leaving a WAL the incoming engine will not accept, and the
file itself is generally intact. It is renamed aside rather than deleted — it
holds the only copy of any rows that never reached the main file, and the
engine that wrote it may still be able to read it.

Quarantined files are kept on purpose for the same reason, including pre-rebuild
database copies. They are also large, and once the live database has been
checkpointing cleanly for a while nobody is going to mine them, so
`QUARANTINE_RETENTION_DAYS` eventually reclaims the space.

### Fatal-state latch

Some DuckDB failures are terminal for the process: once it reports "database
has been invalidated ... must be restarted", every subsequent statement raises.
Without a latch each caller logs its own failure and the log fills with
thousands of identical lines that bury the one that mattered. The latch records
the first, reports it once with an actionable message, and lets callers skip
work that cannot possibly succeed.

Cursor creation sits **inside** the `try`: on an invalidated database it is
`.cursor()` itself that raises, not the `execute`. With it outside, every write
after the first fatal escaped the latch, so the sentinel was never written and
the next boot never self-repaired.

`REBUILD_SENTINEL` is dropped when the DB goes terminal and read on the next
boot by `modules.telemetry_rebuild.auto_rebuild_if_needed()`. A corrupt DuckDB
cannot be rebuilt from inside the process already stuck on it — the first fatal
invalidates every connection until restart — so recovery is handed to the next
startup, when nothing holds the file open.

### Locking

`_db_lock` is an `RLock` because `_get_db()` → `_finish_db_init()` may re-enter
`_get_db()`. One shared connection per file serves both reads and writes
(per-DB singleton + RLock).

### Other constraints

- **No migration on the write/query path.** Those run on the event loop and
  anything slow there stalls the whole app. Migration is a startup step — see
  `migrate_system_metrics()`.
- **`DROP TABLE system_metrics` must precede any CHECKPOINT.** Freeing those
  blocks is what lets the main database checkpoint again, and a failed
  checkpoint invalidates the connection, making the DROP impossible until the
  process restarts.
- **The appender is tried first whenever open**, matching `write_device_state()`.
  This is the hot path for all device telemetry (the collector buffers and
  drains through here), so skipping it would quietly disable the appender for
  the highest-volume table in the app. Timestamps are unaffected: the appender
  stamps explicitly, the Python path via the `ts` column DEFAULT.

## Salvage and rebuild

A DuckDB file with a corrupt block **cannot be repaired in place**: there is no
repair tool, no "skip the bad block" option, and forcing a CHECKPOINT over the
damage escalates it into a FATAL that invalidates the whole database. The only
way back is to copy what is still readable into a new file.

`modules/telemetry_rebuild.py` is the engine for that, with two entry points:

- `auto_rebuild_if_needed()` — called during startup, before anything opens the
  database, so a corrupt file self-heals without anyone having to notice.
- `scripts/rebuild_telemetry_db.py` — the manual CLI, for running it
  deliberately or inspecting what would be recovered.

### Design rules, and why each matters

- **The damaged file is opened READ_ONLY and never written.** A failed rebuild
  leaves it exactly as it was.
- **Tables are copied whole where possible**, bisecting by rowid only on
  failure, so one damaged row group costs that row group rather than the table.
- **The copy runs inside DuckDB** (`ATTACH` + `INSERT…SELECT`), so rows never
  cross into Python. It is the fastest option available and it preserves the
  original timestamps. The Rust appender is **not** usable here: it stamps
  `ts = now()` on every row it takes, which would silently rewrite the whole
  history to today.
- **Nothing is swapped in until the rebuild has been verified** by reopening it
  and reading every table back.

### Startup budget

The automatic rebuild runs inside the app's lifespan, before uvicorn serves, so
every second is a second the app is not answering `/api/system/health`. The
budget has to clear three deadlines:

| Deadline | Fires at |
| --- | --- |
| container HEALTHCHECK | unhealthy at ~120 s after start |
| manager watchdog | `STARTUP_GRACE` 180 s, then restarts at ~240 s |
| `upgrade.sh do_swap` | `HEALTH_TIMEOUT` 300 s, then **rolls back** the upgrade |

A rebuild that overruns is abandoned with the original untouched and the
sentinel kept, so a damaged database can never turn an upgrade into a rollback
loop. The manual CLI has no budget — run it there if a rebuild genuinely needs
longer.

### Manual CLI

`scripts/rebuild_telemetry_db.py` is the manual front-end to the same engine.
Use it to inspect what would be recovered, or to rebuild deliberately.

```bash
# Inspect what is recoverable; writes ./data/telemetry.rebuilt.duckdb
python3 scripts/rebuild_telemetry_db.py

# Rebuild and swap it in (original kept as telemetry.duckdb.damaged-<ts>)
python3 scripts/rebuild_telemetry_db.py --install
```

The original is opened READ-ONLY and never written to, so a dry run cannot make
anything worse. Nothing is swapped into place unless you pass `--install`.

**Stop the application before using `--install`.** Swapping the file under a
running process leaves it holding a deleted inode and writing into nowhere. In
normal operation you should not need this at all — the app repairs itself on
restart.

## Tables and retention

| Table | Contents | Cadence |
| --- | --- | --- |
| `system_metrics` | CPU, memory, temperature, disk | sampled every 30 s |
| `packet_stats` | per-device RX/TX/error counters | flushed every 60 s |
| `device_states` | device attribute changes | on state change only |
| `spectrum_scans` | channel energy levels | per background scan |

Retention is configurable per table, defaulting to 7 days. Location:
`./data/telemetry.duckdb`.

### Why DuckDB over SQLite

- Columnar storage is 5–10× more efficient for time-series aggregation.
- Automatic ZSTD compression keeps disk usage low.
- Concurrent reads do not block writes.
- Built-in time-bucket aggregation functions.
