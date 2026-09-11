# ZigBee Manager — Workers

## Overview

An automation can only react to something a device did. A **worker** is the other
half: a value nobody's hardware reports — holiday mode, the mode the house is in,
a countdown, a tally, when something last happened — that a person or a rule sets
deliberately, and that any rule can then test.

Workers live on their own sub-tab of the **Automations** tab, alongside **Rules**.

The canonical example:

> **Holiday mode** is a boolean worker. The morning-alarm rule has a prerequisite
> "holiday mode is off". Turn the worker on before you leave and the alarm rules
> stop firing — without editing a single rule, and without a rule per person who
> might be away.

Each worker is an ordinary device-like under `worker::<id>`, merged into the
automation engine through the same `add_device_getter` hook the swarm's virtual
devices use (`modules/swarm/virtual.py`). That is the whole integration: a rule
triggers on a worker, tests one as a prerequisite, and commands one through an
ordinary **Command** step. There is no new condition type and no new step type.

---

## The six types

Each type exists because it expresses something the others cannot.

| Type | Attributes | What it's for |
|---|---|---|
| **Boolean** | `value` = `on` / `off` | A flag. Holiday mode, guest staying, bins out. |
| **Mode** | `value` = one of N labels | Mutually exclusive states. `home` / `away` / `night` / `holiday`. |
| **Timer** | `value` = `on` / `off`, `remaining_s` | A flag that clears itself. "Do not disturb for two hours." |
| **Counter** | `value` = integer | A tally. "Times the door opened today." |
| **Marker** | `age_minutes`, `marked`, `last_at` | When something last happened. Gives rules a memory. |
| **Number** | `value` = float | A shared setpoint. Comfort temperature, alarm delay. |

### Boolean

The baseline. `on` or `off`, and nothing else.

Because the value is the string `on`/`off`, the rule builder offers it as a
dropdown and rules read naturally: *when Holiday Mode's value is `on`*.

### Mode

The one to reach for when you find yourself creating a second boolean. Four
booleans can be on at once, and three of those sixteen combinations are states
your house is not in. A mode cannot contradict itself — one option is true, the
rest are false, always.

Options are set on the worker (2–12 of them). Setting an option the worker does
not have is rejected rather than silently accepted, so a typo in a rule surfaces
in the trace log instead of quietly never matching. Matching is
case-insensitive, so a rule saved with `Away` still fires against `away`.

### Timer

A boolean with a deadline. Start it for N seconds and it clears itself, so
nobody has to remember to turn it off — the standard failure mode of
flag-based automation.

- `start` — begins a countdown (the worker's default duration if none is given)
- `extend` — adds to a running countdown, or starts one if it has finished
- `cancel` — clears it immediately

`remaining_s` counts down in whole seconds on a 30-second tick, so a rule can
test "less than ten minutes left" as well as "running at all".

A timer is restored against its **deadline**, not its duration: one that expired
while the hub was down is simply over, and one that has not is still counting.
Restoring the duration instead would silently extend every timer by however long
the restart took.

### Counter

A tally with bounds. `increment` / `decrement` move it by the worker's step (or
by an explicit amount), `set` puts it somewhere, `reset` returns it to its
minimum. Values are clamped to `min`/`max` rather than wrapping.

Set **resets daily at** to a time and the counter clears itself once a day. Leave
it blank and the tally runs until something resets it.

This is what lets a rule say something the engine cannot otherwise express:
*if the door has opened more than six times this evening, remind me to lock it.*

### Marker

Records **when** something happened; rules ask **how long since**.

The automation engine is edge-triggered — it knows what just changed, not what
happened this morning. A marker is its memory. `mark` stamps now; `age_minutes`
then climbs, updated to whole minutes on the tick.

A marker that has never been marked reads as `age_minutes` = **525600** (a year),
which is the truthful answer to "how long since?" when the thing has never
happened. It also keeps every comparison well-defined: *more than six hours
since* is true on a fresh install, *less than six hours* is false. A missing
attribute would leave both undefined.

`marked` (`on`/`off`) distinguishes "never" from "a long time ago" when you need
to.

### Number

A float with `min`, `max`, `step` and an optional unit. Useful two ways:

1. **As a trigger or a prerequisite** — *when Alarm Delay is above 0*.
2. **As a value another step reads** — see below. This is the payoff: one number
   drives fifteen rules, and you change the number instead of the fifteen rules.

---

## Using a worker in a rule

### As a trigger

Pick the worker as the rule's **source device**. Its attributes appear like any
other device's, with a dropdown of valid values for booleans, timers and modes.

```
WHEN   Holiday Mode · value = on
THEN   Hallway Light → off
```

### As a prerequisite

Add a prerequisite pointing at the worker. This is the holiday-alarm pattern:

```
WHEN   (time) 07:00
IF     Holiday Mode · value = off
THEN   Bedroom Speaker → announce "Good morning"
ELSE   —
```

Or, with the ELSE path doing the delayed version:

```
WHEN   (time) 07:00
IF     House Mode · value ≠ holiday
THEN   Bedroom Speaker → announce "Good morning"
```

### As a target

A worker is a valid **Command** step target, so a rule can set one:

```
WHEN   Front Door · contact = false
THEN   Door Opens (counter) → increment
       Last Entry (marker)  → mark
```

Commands by type:

| Type | Commands |
|---|---|
| Boolean | `on`, `off`, `toggle` |
| Mode | `set` (value must be one of its options) |
| Timer | `start`, `extend` (seconds), `cancel` |
| Counter | `increment`, `decrement`, `set`, `reset` |
| Marker | `mark`, `reset` |
| Number | `set`, `increment`, `decrement` |

### As a value another step reads

A step's `value` — and any condition threshold — may point at a worker instead of
carrying a literal:

```json
{
  "type": "command",
  "target_ieee": "0x00124b0022a1b2c3",
  "command": "temperature",
  "value": { "worker": "comfort_temp" }
}
```

The general form works against any device in the registry, not just workers:

```json
{ "ref": "virtual::weather", "attribute": "temperature" }
```

Thresholds accept the same shape, so a gate can compare two live values —
something a plain condition cannot do:

```json
{ "type": "condition", "ieee": "0x00124b00...", "attribute": "temperature",
  "operator": "lt", "value": { "worker": "comfort_temp" } }
```

An unresolvable reference fails the comparison and skips the command, rather than
acting on a stale or invented number.

---

## Suggested by the swarm

The Suggested sub-tab (`docs/swarm-intelligence.md`) reads workers like any
device, and can propose the ones a household tends to want:

| Worker | Type | What the suggestions do with it |
|---|---|---|
| House mode | mode: home, away, night, holiday | follows who is home; night at bedtime, home in the morning; night locks up and switches off; heating follows it; alerts while away; lights on holiday |
| Comfort / Setback temperature | number | every heating schedule and mode rule reads them live |
| Heating boost | timer | radiators up while it runs, back to comfort when it runs out |
| Last movement | marker | marked by every motion sensor; an alert if nobody has moved for hours |
| Door opens today | counter | counted per door, reset at midnight; a reminder when the doors have been busy |
| Plants watered | marker | an evening reminder when it has not been marked for days |

A worker you already have is used rather than duplicated: same type, and either
the same id or a matching word in its name ("House Mode", "Comfort temp").
Otherwise the card says **Also creates …**, and Create makes the worker first,
then the rule. A suggestion made only of the hub and proposed workers — with
nothing on the network in it — is never offered.

---

## The chain limit

A worker is both something a rule can set and something another rule triggers on,
so setting one re-enters evaluation. Two rules that set each other's workers would
recurse without end, and cooldowns cannot catch it — each hop is a *different*
rule firing once, which is exactly what a cooldown permits.

The engine therefore counts how many rules have fired in one causal chain and
stops at **4** (`MAX_CHAIN_DEPTH` in `modules/automation.py`). The stop is logged
to the trace log as `CHAIN_LIMIT` and counted in `chain_stops` on the engine
stats, so a loop shows up as a warning rather than as a silent hang.

Four hops is deliberately generous for intentional chains (*door opens → counter
increments → counter over six → send a message*) and short enough that a genuine
loop is cut within a second.

---

## Restarts

Every type has a **survives a restart** setting, on by default.

| Type | What is restored |
|---|---|
| Boolean, Mode, Number | The value it held |
| Counter | The tally, and the day it last reset |
| Marker | When it was last marked |
| Timer | The deadline — see above |

Turn it off and the worker comes back at its configured starting value. That is
the right choice for anything that should not outlive the process, and the wrong
one for holiday mode.

State lives in `data/workers.json` alongside the configuration, written only when
a value actually changes — never on the countdown tick.

---

## API

| Method | Path | Scope |
|---|---|---|
| `GET` | `/api/workers` | `automation:read` |
| `GET` | `/api/workers/types` | `automation:read` |
| `GET` | `/api/workers/usage` | `automation:read` |
| `GET` | `/api/workers/{id}` | `automation:read` |
| `POST` | `/api/workers` | `automation:write` |
| `PUT` | `/api/workers/{id}` | `automation:write` |
| `DELETE` | `/api/workers/{id}` | `automation:write` |
| `POST` | `/api/workers/{id}/command` | `device:write` |

Configuration is `automation:write` because creating a worker creates something
rules depend on. **Setting** a worker's value is `device:write` instead: that is
the same act as pressing a switch, and the whole point of a worker is that the
household can flip it.

```bash
# Create
curl -X POST /api/workers -H 'Content-Type: application/json' -d '{
  "name": "Holiday Mode", "type": "boolean"
}'

# Set
curl -X POST /api/workers/holiday_mode/command \
  -H 'Content-Type: application/json' -d '{"command": "on"}'

# A two-hour quiet period
curl -X POST /api/workers/do_not_disturb/command \
  -H 'Content-Type: application/json' -d '{"command": "start", "value": 7200}'
```

`GET /api/workers/usage` reports which rules trigger on and which command each
worker, so deleting one that three rules depend on is never a silent act.

---

## Tips

- **Reach for a Mode the moment you want a second boolean.** Two flags that must
  not both be on are one mode.
- **Prefer a Timer to a Boolean you promise to turn off.** The promise is the part
  that fails.
- **A Marker plus a time rule replaces a lot of bookkeeping** — "if the plants
  have not been watered in four days, ask" is one marker and one rule.
- **A Number worker is worth it as soon as the same figure appears in two rules.**
  After that the rules read the worker and you change one thing.
- **Disable rather than delete** while you are still deciding: a disabled worker
  leaves the registry and rules using it stop firing, which is easy to undo.
- **Workers are not hardware.** They carry a `worker` capability so they appear in
  rule targets, and nothing else in the app treats them as devices.

---

## Files

| Path | Role |
|---|---|
| `modules/workers.py` | Types, the device-like, the manager, persistence, the tick |
| `routes/worker_routes.py` | API |
| `static/js/workers-page.js` | Automations → Workers sub-tab |
| `data/workers.json` | Configuration and restored state |

See also `docs/automations.md` for the rule engine itself.
