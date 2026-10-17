# Observed device actions

A Maintenance button (Poll, Reconfigure, Aggressive LQI, Restore Baseline)
and the setup after a device joins each run as an *observed action*
(`modules/device_ops.py`). The answer is what the device said to each step,
not that the request was sent.

## Steps

Code that talks to the device records one step per answer with
`device_ops.step(what, ok, reason, cluster=, attr=)`, or `step_status` from a
ZCL/ZDO status. Outside an action it is a no-op, so the normal path pays
nothing. The action is found through a context variable, so steps recorded in
tasks the action spawns still land on it.

Recorded today:

| Where | Step |
|---|---|
| `handlers/base.py` `configure` | the bind (`timeout` when the device does not answer); each reporting row, refused with its ZCL status |
| `handlers/base.py` `poll` | each cluster read |
| `device/handlers.py` | a handler that raised; a handler answering `False` now counts as failed in the configure summary |
| `device_profiles_apply.apply_reporting` | the entry's bind (a failure is a warning: without it a maker's cluster sends nothing) and each reporting row |

A reason is `timeout`, a ZCL status name (`INSUFFICIENT_SPACE`), or an
exception's text.

## What the device does outranks what it answers

A refused reporting step names the cluster and attribute it was for. Before
the verdict, the action asks `device_observer.last_report` whether the device
has reported that attribute since start. If it has, the step is **declined**,
not failed: the device would not store the setup, but sends the reading
anyway, and nothing needs fixing.

An Aqara FP300 whose table is full answers `INSUFFICIENT_SPACE` to setup on
0x0142/0x014D, yet reports both once the cluster is bound.

## The entry's reporting goes first

A small device keeps only a few reporting configurations, and takes them
first come, first served. Setup (join and Reconfigure) therefore sends the
entry's rows (`apply_entry_reporting`) before the handlers' generic
reporting. Handler configure still runs after, so it still wins a conflict on
the same attribute.

The FP300 shows why. With five generic configurations sent first (present
value, illuminance, humidity, temperature, battery, all of which it reports
unasked), it refused 0x0142/0x014D with `INSUFFICIENT_SPACE`. Sent to a fresh
device on their own, the same two rows were accepted.

## Verdict

| Verdict | When |
|---|---|
| `ok` | every step confirmed or declined-but-reporting |
| `partial` | some step failed |
| `failed` | the action raised, or no step was confirmed |
| `unchecked` | it ran, but nothing in it reported an answer |

A `success: false` result counts as an error only when it carries an `error`:
a refusal is already a step.

## Advice

Advice explains an answer; it does not prescribe a fix it cannot know. A
status is the device's answer to *our request*, so advice never tells anyone
to re-pair: the request (interval, type, whether the device takes reporting
setup at all) may be what is wrong. For a refused reading with no reports
yet, it says to trigger the reading and watch the log.

## Streaming

While an action runs, a log handler tees every INFO+ line naming `[ieee]` to
the browser as `device_op` events (`phase: start | line | done`), at most 400
lines. On the loop thread a line is emitted in place, so no line lands after
the verdict; from another thread it hops to the loop.

The Settings tab's action panel shows a spinner, the live log, then the
verdict with advice and the steps that need attention. The join card shows
the same log, and on `ready` the setup's verdict. A card with a problem stays
until dismissed. Identification alone no longer marks a join card ready.
