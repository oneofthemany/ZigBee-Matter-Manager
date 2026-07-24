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
