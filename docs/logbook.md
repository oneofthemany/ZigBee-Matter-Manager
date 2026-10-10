# Logbook — the live log's history, and the trace behind a line

**Debug ▸ Logs** is the house's timeline: every device update, join and leave,
rule firing and command result, in order. The hub keeps those lines, so the
view survives a reload (**Load earlier** pages back), and **clicking a line
traces it**: what caused the change, which rules looked at it and why each did
or didn't fire, what they did, and what that changed in turn.

| Path | Role |
|---|---|
| `modules/logbook.py` | stores lines, chains and trace entries; builds a trace |
| `modules/automation.py` | `current_chain()`, `add_trace_listener()` — the engine's side |
| `routes/logbook_routes.py` | API |
| `static/js/logging.js` | the log view, history, and the trace modal |
| `data/logbook.duckdb` | the store — 7 days |

## What is kept

- **Lines.** Every `log` event broadcast to the browser passes through
  `broadcast_event`, where the logbook gives it an `event_id` and stores it.
  Zigbee attribute updates, joins and leaves were already log lines; the
  logbook adds lines for rule firings and command results, and for changes on
  devices the Zigbee path doesn't cover — workers (so house mode), the alarm,
  presence, cameras, Matter, Nuki, Shelly and ESPHome.
- **Chains.** Every rule evaluation runs in a *chain* — one id for a device
  change and everything the engine does about it. It is a ContextVar, so the
  sequence a rule starts inherits it, and an evaluation started from inside one
  (a rule sets a worker, another rule watches that worker) records its parent.
  Clock, webhook and "run now" evaluations open a chain of their own.
- **Trace entries.** Each engine trace entry is stored against its chain: the
  verdict per rule (fired, conditions not met, prerequisites, cooldown), with
  the per-condition results — attribute, operator, threshold, the value it
  actually had, pass or fail — and each step it ran.
- **Causes.** When a rule sends a command, or a person does through
  `POST /api/device/command`, the target is remembered for 15 s. The device's
  next reported change is attributed to that rule's chain, or to that person.

A chain is only stored once it has something to say — a rule looked at it, or
it has a known cause. Most attribute updates trigger no rule and store nothing
but their line.

## The trace

`GET /api/logbook/trace/{event_id}` answers, for one line:

- **Why it happened** — the person who commanded it; or the rule, and the
  device change that fired that rule; or neither, when the device reported it
  unprompted.
- **Rules that looked at it** — each with its verdict, its conditions with the
  actual values, and its steps.
- **What changed as a result** — device lines attributed to this chain.
- **…which set off** — the chains those changes started.

A line is tied to its chain by id where the logbook wrote the line itself, and
otherwise by device, attribute and time (within 3 s): the Zigbee path emits its
log line and calls the engine separately, a moment apart.

Attribution by time has a limit worth knowing: if a rule switches a light and
someone presses its wall switch within the same 15 s, that press is credited to
the rule.

## Hardening that came with it

- **The log view escapes what it shows.** Messages carry device names and the
  values devices report; they were inserted as HTML. Search is matched as text,
  where a `(` used to throw and stop the log rendering.
- **Its own file, its own thread.** `data/logbook.duckdb` has one worker thread
  holding the only connection; the event loop only queues rows (flushed every
  second). It is not the telemetry database, so neither can stall or damage the
  other. If the disk stalls, rows past 20 000 queued are dropped and counted
  (`dropped` in the API), rather than held in memory.

## API

| | |
|---|---|
| `GET /api/logbook/events?before=&limit=&ieee=&q=` | lines, newest first; `before` is a `ts` to page back from |
| `GET /api/logbook/trace/{event_id}` | the trace; 404 once the line has aged out |

Both need `system:read`.
