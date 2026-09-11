# ZigBee Manager — Automation Engine

## Overview

The automation engine provides state-machine-based triggers with recursive action sequences, executing directly at the ZigBee gateway level with zero MQTT delay. Rules evaluate device attribute changes in real time and fire ordered sequences of commands, delays, waits, gates, branching logic, and parallel execution.

![Automation tab overview showing rule list with state badges and action buttons](./images/automation-tab-overview.png)


> **Swarm Intelligence** describes every device in one vocabulary of triggers,
> conditions and actions, so any device can be wired to any other without a rule
> written by hand for each combination. It compiles to the rules documented
> here — see `docs/swarm-intelligence.md`.

> **Suggested** is the third sub-tab: every rule the swarm reckons your devices
> could support, grouped by room, each one built with a single click. See
> `docs/swarm-intelligence.md`.

> **Workers** are the second sub-tab of the Automations tab. A rule can only
> react to something a device did; a worker is state *you* set — holiday mode,
> the mode the house is in, a countdown, a tally — that any rule can then test
> and any rule can set. See `docs/workers.md`.

---

## Core Concepts

### State Machine

Rules track **matched/unmatched** state and only fire on transitions — not on every matching update.

| Previous State    | New State | Action                  |
|-------------------|-----------|-------------------------|
| unmatched         | matched   | Run **THEN** sequence   |
| matched           | unmatched | Run **ELSE** sequence   |
| matched           | matched   | Nothing (still matched) |
| unmatched         | unmatched | Nothing                 |
| init (first eval) | matched   | Run **THEN**            |
| init (first eval) | unmatched | Nothing                 |

![State machine diagram showing transitions between init, matched, and unmatched states](./images/state-machine-diagram.png)

### Rule Structure

Every automation rule consists of four parts:

1. **Trigger Conditions** — attribute checks on the source device, or on any other device a condition names (AND or OR logic, up to 5)
2. **Prerequisites** — optional state checks on other devices before firing (supports NOT)
3. **THEN Sequence** — action steps when conditions become true
4. **ELSE Sequence** — action steps when conditions become false

---

## Creating a Rule

The Automations tab has three sub-tabs: **Rules** (this document), **Workers**
(`docs/workers.md`) and **Suggested** (`docs/swarm-intelligence.md`). Rules is
the one selected on arrival.

Click **Add Rule** on the Rules sub-tab to open the rule builder.

![Add Rule form showing empty condition, prerequisite, and sequence builders](./images/add-rule-form.png)

### Step 1: Trigger Conditions

Conditions evaluate attributes on the source device. Each condition specifies an attribute, operator, and threshold value.

**Match ALL (AND) / Match ANY (OR)** — the selector beside the **+** button controls how multiple conditions combine. It appears once a second condition is added; with one condition there is nothing to combine.

- **Match ALL (AND)** — every condition must hold. The default, and how all rules saved before this option existed continue to behave.
- **Match ANY (OR)** — one condition being true is enough. Use it for "either / or" triggers, e.g. a presence user whose `place` is `sky_slough` **or** `sky_osterley` — a single rule covering both sites instead of two near-identical rules.

The joiner badge on each row (`AND` amber / `OR` purple) reflects the current choice, so a glance at the rule tells you which way it reads.

![Condition builder with IF/AND badges, attribute dropdown, operator, and value fields](./images/condition-builder.png)

**Supported Operators:**

| Symbol            | Meaning                   |
|-------------------|---------------------------|
| `=`               | equals                    |
| `≠`               | not equal                 |
| `>` `<` `>=` `<=` | numeric comparisons       |
| `∈`               | in list (comma-separated) |
| `∉`               | not in list               |
| `Δ` changes       | takes any new value — see *Change triggers* |
| `→` changes to    | the moment it becomes a value |
| `←` changes from  | the moment it stops being a value |
| `↑` rises by      | has moved up by at least N within a window |
| `↓` falls by      | has moved down by at least N within a window |

**Sustain** — optional hold timer (seconds). The condition must remain true for the specified duration before triggering.
When the time is up the engine re-checks the rule by itself, so it fires even if the device reports nothing further — a door
sensor that reports "open" once still fires "open for 10 minutes". The clock starts when *that* condition became true,
whatever the other conditions were doing: "dark **and** door open for 10 minutes" times the door from when it opened,
not from when it got dark. Changing or disabling the rule resets its clocks.

#### Condition types

| Type          | Triggers on                                                        |
|---------------|--------------------------------------------------------------------|
| **Attr**      | An attribute on the source device meeting a comparison             |
| **Alarm**     | A clock time on chosen days                                        |
| **Time/Day**  | Being inside a time window on chosen days                          |
| **Sun**       | Being between two sun/clock boundaries (tracks the seasons)        |
| **Zone**      | A person entering or leaving a place — offered for presence users  |
| **Offline**   | A device that has stopped reporting                                |

#### Zone: arriving and leaving

Pick **Enters** or **Leaves**, then tick the places the crossing is about.

- **Any place** — every arrival at, or departure from, any named place (or home).
- **One place** — just that one.
- **Several places** — they form a *single* zone. "Work" spanning two offices fires
  once on arriving at either and once on leaving both; driving between the two is
  movement *inside* the zone, so it triggers nothing. This is what you want for a
  person with more than one site, and it's why ticking two places is not the same
  as two OR'd conditions (those would fire on the hop between them).

A zone condition is **edge-triggered**: the crossing is the trigger. That has two
consequences worth knowing:

- The rule only runs its **THEN** sequence. "Not arriving right now" is not the
  same as leaving, so the ELSE sequence is never run — build the opposite crossing
  as a second rule with **Leaves**.
- The trigger re-arms after each crossing, so arriving tomorrow fires it again.

Leaving somewhere for "away" counts as a departure from that place; "away" and
"unknown" are the *absence* of a place, so they can't themselves be entered or left.

After a hub restart the engine restores where each person was, so the first
crossing after a restart is still reported correctly.

#### Change triggers: changes, rises, falls

The operator picker on an **Attr** row has a *when it changes* section. These
compare the value with an earlier one, so they only exist on trigger conditions
— not prerequisites, gates or If/Else steps.

- **changes** — any new value. A device reporting the same value again is not a
  change.
- **changes to** / **changes from** — the moment it becomes, or stops being, a
  value. For *from X to Y*, add both to one rule with **Match ALL**: they are
  judged on the same update.
- **rises by** / **falls by** — has moved by at least N within a window you set
  in minutes (default 60), measured from the lowest (or highest) reading in that
  window. Offered for numeric attributes. "Humidity rises by 15 within 10 min"
  is a shower starting, whatever the humidity was before.

*changes*, *changes to* and *changes from* are **moments**, like a zone crossing:
the rule runs THEN when the change happens, never runs ELSE ("no change right
now" is not the opposite of a change), and re-arms for the next change. *Rises
by* / *falls by* are **states**: true while the movement holds, so THEN runs
when it starts and ELSE when the window slides past it. None of them take a
sustain.

A change on another device (see *Several trigger devices*) counts only on that
device's own update, and the first change after a rule is added is caught — the
rule starts from each device's current value.

#### Offline: a device that stops reporting

A device whose battery dies or that drops off the mesh sends nothing, so no
attribute can trigger on it. The **Offline** condition type reads its silence:

- **silent for N min** — the device has not reported for N minutes. Right for
  most devices; pick N comfortably above how often it normally reports (a
  temperature sensor every few minutes, a door contact maybe only when used plus
  a periodic check-in).
- **blank** — when the hub itself marks it unavailable, which for Zigbee is after
  25 hours (72 for sensors that only report events).

Offline is checked once a minute, and any report from the device clears it, so
**THEN** is "it went offline" and **ELSE** is "it came back". A device that
reports no last-seen time can only use the hub's verdict, and the trace says so
when neither is available.

Battery level needs no special type: `battery` (a percentage) and `battery_low`
are ordinary attributes wherever a device reports them — **Battery is below 15**.

#### Several trigger devices (AND / OR across devices)

A condition does not have to read the source device. Each **Attr** and **Zone**
row has a device picker: **This device** is the rule's source, and picking any
other device makes that row read it instead. The **Match ALL / Match ANY**
selector then combines the rows across devices exactly as it does on one:

- **Match ALL (AND)** — fires when every condition holds. An update on *any* of
  the named devices re-evaluates the rule, reading the others as they currently
  stand, so the order the devices change in does not matter.
- **Match ANY (OR)** — fires when any one condition holds, whichever device it
  is on. "Front door **or** back door opens" is one rule, not two.

A condition on another device is different from a prerequisite: a prerequisite
is only *checked* when the trigger fires, while a trigger condition on another
device can fire the rule itself.

Some things to know:

- **Momentary attributes** — `action`, `click`, `button_action`, `event`,
  `scene`, `command` — count only on the update that carries them. A button
  pressed this morning does not still read "pressed" when another device
  updates this evening, or when a clock boundary re-evaluates the rule.
- A **Zone** condition on another person passes only on that person's own
  crossing, for the same reason.
- A **group** can't be a trigger device: it never reports a change of its own.
  Check a group's state with a prerequisite.
- The per-device rule limit (`MAX_RULES_PER_DEVICE`) counts every trigger
  device a rule names, not only its source.
- Removing a device that is one of several trigger devices **disables** the rule
  (with an alert) instead of deleting it; removing the source device still
  deletes it.
- A **Time / Alarm** rule can carry device conditions too; each must pick a device.

On the wire a condition names its device with `ieee`; without one it reads
`source_ieee`, so rules saved before this existed are unchanged:

```json
{
  "source_ieee": "0x00158d0001a2b3c4",
  "condition_logic": "and",
  "conditions": [
    { "type": "attribute", "attribute": "occupancy", "operator": "eq", "value": true },
    { "type": "attribute", "ieee": "0x00124b0022d4e5f6",
      "attribute": "illuminance_lux", "operator": "lt", "value": 20 }
  ]
}
```

#### Condition groups: (A and B) or C

**Group** adds a box of conditions with its own **All of these / Any of these**.
The rest of the rule sees the box as one condition, joined by the rule's
**Match ALL / Match ANY**:

- Match ALL, group set to *Any of these* — **(front door opens or back door opens) and it's dark**
- Match ANY, group set to *All of these* — **(motion and it's dark) or the button is pressed**

A new group starts with two rows and the opposite logic to the rule's, since
that is usually why you are grouping. Rows inside a group work like any other:
their own device, type and sustain. Groups go one level deep (a group can't hold
a group), with up to 5 conditions each. A **Zone** anywhere in the rule, grouped
or not, makes it a crossing rule that only runs THEN.

```json
"condition_logic": "and",
"conditions": [
  { "type": "group", "condition_logic": "or", "conditions": [
      { "type": "attribute", "ieee": "0x00124b0011aa0001", "attribute": "contact", "operator": "eq", "value": false },
      { "type": "attribute", "ieee": "0x00124b0011aa0002", "attribute": "contact", "operator": "eq", "value": false }
  ]},
  { "type": "attribute", "attribute": "illuminance_lux", "operator": "lt", "value": 20 }
]
```

The trace log shows a group as one line with its members indented beneath it.

### Step 2: Prerequisites (Optional)

Prerequisites check the current state of **other devices** before the rule fires. These support a **NOT** flag to negate the check.

![Prerequisite builder with CHECK badge, NOT checkbox, device picker, and attribute fields](./images/prerequisite-builder.png)

Example: Only fire if the hallway light is currently OFF.

### Step 3: THEN Sequence

Action steps that execute when conditions transition from unmatched → matched.

![THEN sequence builder with Command, Delay, Wait, Gate, If/Then/Else, and Parallel buttons](./images/then-sequence-builder.png)

### Step 4: ELSE Sequence

Action steps that execute when conditions transition from matched → unmatched.

![ELSE sequence builder with a delay step followed by a command step](./images/else-sequence-builder.png)

### Run mode: firing again while still running

A sequence can still be running when the rule fires again — the ELSE of a
motion light whose "wait 60 s, then off" is under way when motion returns, or a
doorbell announcement when the door opens again. **Fires again while running**
(beside Cooldown) decides what happens:

| Mode | Label | What happens |
|------|-------|--------------|
| `restart` | Restart it | The running sequence is cancelled and the new one starts. **The default**, and what every rule did before run modes existed — right for a motion light, where new motion should cancel the pending "off". |
| `queued` | Queue it | The running sequence finishes, then the new one runs. Right for announcements and anything that must not be cut off halfway. |
| `single` | Ignore it | The running sequence finishes and the new one is dropped. Right for a routine that should not stack, like a wake-up fade. |
| `parallel` | Run both | The new sequence starts alongside the running one. |

The run mode governs only whether a sequence *runs*: the rule's matched /
unmatched state still follows its conditions. Under **Ignore it** a THEN dropped
because the ELSE was still running is not replayed later, so pick it only for
rules where that is what you want. Queued and parallel rules hold at most 10
live runs (`MAX_RULE_RUNS`); a trigger beyond that is dropped and logged as
`QUEUE_FULL`. Disabling or deleting a rule cancels its running and queued runs.

On the wire it is `"run_mode": "queued"`; a rule without the key restarts.

---

## Step Types

### Command

Sends a ZigBee command to a target device. Select the target, command, and optional value. Endpoint is auto-detected.

![Command step showing target device dropdown, command dropdown, and value input](./images/step-command.png)

**Workers** are valid targets too, so a rule can set household state as well as
act on hardware — increment a counter, mark that something happened, put the
house into night mode. See `docs/workers.md`.

**Value references.** Instead of a literal, a command's value (and any condition
threshold) may point at a live attribute:

```json
{ "command": "temperature", "value": { "worker": "comfort_temp" } }
{ "command": "temperature", "value": { "ref": "virtual::weather", "attribute": "temperature" } }
```

This is how one shared number drives many rules: change the worker, not the
rules. An unresolvable reference fails the comparison and skips the command
rather than acting on a stale or invented number.

### Delay

Pauses the sequence for a specified number of seconds.

![Delay step with seconds input field](./images/step-delay.png)

### Wait For

Pauses until a device attribute matches a condition, with a configurable timeout. If the timeout expires, the sequence stops.

![Wait For step with device picker, attribute, operator, value, and timeout fields](./images/step-wait-for.png)

### Gate

An inline condition check that stops the sequence if the condition is false. Supports NOT for negation.

![Gate step with NOT checkbox, device picker, attribute, operator, and value](./images/step-gate.png)

### If / Then / Else (Branching)

Evaluates one or more inline conditions and branches into nested THEN or ELSE paths. When a single condition is used, the AND/OR selector is hidden for a clean simple IF. Adding a second condition reveals the AND/OR logic toggle.

![If/Then/Else step with single inline condition, nested THEN and ELSE sequences](./images/step-if-then-else-single.png)

![If/Then/Else step with multiple inline conditions and AND/OR toggle visible](./images/step-if-then-else-multi.png)

Each inline condition supports NOT negation, device selection, attribute, operator, and value — identical to prerequisites but evaluated inline during sequence execution.

### Parallel

Executes two or more branches concurrently. All branches run simultaneously and the step completes when all branches finish.

![Parallel step with Branch 1 and Branch 2 containers, each with their own step builders](./images/step-parallel.png)

Additional branches can be added with the **+ Branch** button.

### Repeat

Runs its steps (**EACH TIME**) again and again:

| Mode | Repeats | Condition checked |
|------|---------|-------------------|
| **Times** | a fixed number of times (up to 500) | — |
| **While…** | while its conditions hold | before each pass — false at the start means no passes |
| **Until…** | until its conditions hold | after each pass — the steps always run at least once |

While and Until stop at **at most N times** (default 20, up to 500) even if the
condition never changes, and log a warning when that is why they stopped. Their
conditions work like If / Else ones: device, attribute, operator, value, NOT,
AND/OR.

Put a **Delay** inside to pace it. *Until Front Door is closed: message "front
door still open", wait 600 s* is a reminder every ten minutes, at most 20 times.
A **Gate** that fails, or a **Wait For** that times out, ends that pass only — as
it would inside an If / Else — and the next pass starts.

Disabling, deleting or restarting the rule stops a repeat mid-pass. (Cancelling
a rule used not to reach steps nested inside If / Else or Together at all: the
sequence carried on with its next step. It now stops everything.)

### Live values in messages

**Message**, **Ask First** and **Announce** text can include placeholders, filled
in when the step runs. The **＋ value** picker beside the text inserts them:

| Placeholder | Becomes |
|---|---|
| `{time}` / `{date}` | `18:42` / `Fri 11 Sep` |
| `{trigger}` | the name of the device whose update fired the rule |
| `{trigger.temperature}` | that device's `temperature`, read as the step runs |
| `{<device id>.humidity}` | any device's value — `{0x00158d0001a2b3c4.humidity}`, `{group:3.state}`, `{worker::comfort_temp.value}` |

`{trigger}` is what makes one rule with several trigger devices speak precisely:
*{trigger} was left open* names whichever door it was. A clock-fired rule, or a
sustain re-check, uses the rule's first device; an **Ask First** remembers the
device that fired it, so its yes-steps can use `{trigger}` too.

Values are read when the step runs, not when the rule fired. A value that can't
be read becomes `?`; braces that don't name a device (`{like this}`) are left as
written. Yes/no values read `yes` / `no`, and decimals are rounded to two places.
The editor preview shows each placeholder as ‹what will fill it›.

---

## Rule Card Display

Each saved rule displays as a card showing conditions, prerequisites, sequence summaries, and state.

![Rule card showing IF/AND conditions, CHECK prerequisites, THEN/ELSE summaries, and action buttons](./images/rule-card.png)

**State Badges:**

| Badge               | Meaning                     |
|---------------------|-----------------------------|
| `matched` (green)   | Conditions currently true   |
| `unmatched` (grey)  | Conditions currently false  |
| `init` (dark)       | Not yet evaluated           |
| `⏳` (yellow)        | Sequence currently running  |

**Action Buttons:**

| Button | Action                               |
|--------|--------------------------------------|
| 🔍     | Open trace log filtered to this rule |
| ✏️     | Edit the rule                        |
| ⏻      | Enable / disable                     |
| 🗑️    | Delete the rule                      |
| ⬇️     | Download rule as JSON                |

---

## JSON Export

Each rule can be downloaded as a JSON file via the download button on the rule card. The exported file contains the complete rule definition including conditions, prerequisites, and both sequences — useful for backup, sharing, or importing into another instance.

![Download button on rule card and example JSON file](./images/json-download.png)

---

## Trace Log

The trace log shows real-time evaluation history for debugging automation behaviour. Open it via the **Trace** button.

![Trace log panel with timestamp, phase badges, result badges, and condition evaluation details](./images/trace-log.png)

**Result Colours:**

| Colour  | Results                                                                  |
|---------|--------------------------------------------------------------------------|
| Green   | SUCCESS, FIRING, COMPLETE, WAIT_MET, GATE_PASS, IF_TRUE, PARALLEL_DONE, REPEAT_DONE |
| Red     | FAIL, ERROR, EXCEPTION, MISSING, CMD_FAIL                                |
| Yellow  | BLOCKED, SUSTAIN_WAIT, DELAY, WAITING, RUN_SKIPPED, QUEUE_FULL           |
| Blue    | CANCELLED, WAIT_TIMEOUT, IF_FALSE, QUEUED, DEQUEUED                      |

Filter by a specific rule using the dropdown, or select **System** to see engine-level events.

---

## Example: Door Contact Light

A practical example — turn on a light when a door opens in low light, turn it off 5 seconds after the door closes.

**Conditions:**
- IF `contact` = `open`
- AND `illuminance` < `11`

**THEN:**
- ⚡ Command → Hall Light → ON

**ELSE:**
- ⏱ Delay → 5 seconds
- ⚡ Command → Hall Light → OFF

---

## Example: One Rule for Two Work Sites (OR)

A presence user who works at either of two offices. With **Match ANY (OR)** a single
rule covers both, rather than one rule per site.

**Conditions** (Match ANY):
- IF `place` = `sky_slough`
- OR `place` = `sky_osterley`

**THEN:**
- 💬 Message → set "at work"

**ELSE:** fires when the user is at neither site — i.e. on leaving work.

---

## Example: Arriving At and Leaving Work

The same two offices as a **zone**, which is usually the better shape: it separates
arriving from leaving into two rules that each do one thing, and it ignores the
drive between the two sites.

**Rule 1 — arriving**

- Source: the presence user
- IF **Zone** → **Enters** → ☑ Slough ☑ Osterley
- THEN: 💬 Message "at work" → ⚡ Turn off Hall Light

**Rule 2 — leaving**

- Source: the presence user
- IF **Zone** → **Leaves** → ☑ Slough ☑ Osterley
- THEN: ⚡ Heating → on

Neither rule needs an ELSE. Driving from Slough to Osterley fires nothing, because
both sites are the same zone.

---

## Example: Branching with If/Then/Else

A more advanced example using inline branching — when motion is detected, check time of day and set appropriate brightness.

**Conditions:**
- IF `occupancy` = `true`

**THEN:**
- If/Then/Else:
    - IF Kitchen Light `brightness` < `50`
        - THEN: ⚡ Kitchen Light → brightness = 255
        - ELSE: ⚡ Kitchen Light → brightness = 128

---

## Example: Holiday Mode

A boolean worker (`worker::holiday_mode`) gates the morning alarm, so going away
is one switch rather than an edit to every alarm rule.

**Worker**

| Field | Value |
|-------|-------|
| Name | Holiday Mode |
| Type | Boolean |
| Survives a restart | yes |

**Rule**

| Field | Value |
|-------|-------|
| Source | (time) 07:00, weekdays |
| Prerequisite | Holiday Mode · `value` = `off` |
| THEN | Bedroom Speaker → announce "Good morning" |

Flip the worker on from the Workers sub-tab and the alarm stops firing; flip it
off and everything resumes. A rule can flip it too — an arrival at the airport
place, say — so the switch does not have to be thrown by hand.

---

## Example: Ask Before Repeating Yourself

A marker worker gives an edge-triggered engine a memory, which is what "have I
already mentioned this today?" needs.

| Field | Value |
|-------|-------|
| Source | Front Door · `contact` = `false` |
| Prerequisite | Door Reminder (marker) · `age_minutes` > `360` |
| THEN | 1. Message → "Front door is open" |
| | 2. Door Reminder → `mark` |

The marker's own step is what stops the second message: until six hours have
passed, the prerequisite fails and the rule does nothing.

---

## Rule chains and the chain limit

Because a worker is both a target and a trigger source, setting one from a rule
re-enters evaluation. That is the point — it is what lets a door contact
increment a counter and a second rule act on the count. It also means two rules
that set each other's workers would recurse without end, and cooldowns cannot
catch that: each hop is a *different* rule firing once.

The engine counts how many rules have fired in one causal chain and stops at
**4** (`MAX_CHAIN_DEPTH`). The stop appears in the trace log as `CHAIN_LIMIT` and
in the engine stats as `chain_stops`, so a loop surfaces as a warning rather than
as a hang.

---

## Tips

- **Cooldown** prevents rapid re-firing. Set it based on how quickly your sensor re-triggers (motion sensors: 5-10s, contact sensors: 1-2s).
- **Prerequisites** let you create context-aware rules without duplicating conditions across multiple rules.
- **Zone** beats a `place` equality check whenever you care about the *moment* someone arrives or leaves rather than where they currently are — and it's the only way to act on a departure without an ELSE.
- **Match ANY (OR)** collapses "one rule per value" duplicates into a single rule — and the ELSE sequence then means "none of them are true", which is usually what you want for a leaving/away action.
- **Gates** are useful mid-sequence to bail out if conditions have changed since the sequence started.
- **Wait For** is ideal for confirming a command took effect before proceeding.
- **Parallel** lets you command multiple devices simultaneously rather than sequentially.
- **Workers** turn "one rule per case" into "one rule that reads the case" — a
  mode worker replaces a stack of near-identical rules, and a number worker
  replaces the same figure copied into several of them (`docs/workers.md`).
- **JSON export** is your backup safety net — download rules before making major changes.
## Local natural-language parser

`modules/nl_automations.py` is a deterministic, dependency-free compiler that
turns a constrained-English sentence into the same rule dict the
`AutomationEngine` consumes — **without an LLM**. Designed for resource-limited
SBCs: a parse is pure-Python string work, taking microseconds and never making a
network call.

It is the **first** path tried by `POST /api/ai/automation`. Only if it cannot
fully resolve the sentence does the caller fall back to the LLM, where one is
configured. Either way the produced rule is identical in shape.

**Grounding**: every device, attribute, value and command is resolved against the
live registry via the engine's existing metadata methods
(`get_all_devices_summary`, `get_device_state`, `get_actuator_devices`), so
values are never guessed. "50%" becomes the right 0–254 brightness, "motion"
maps to whichever boolean attribute that specific device actually exposes, and
so on.

Supported shapes, case-insensitive and order-flexible:

- "turn on the hall light when the hallway sensor detects motion"
- "when the front door opens turn on the ensuite lights"
- "turn off the media socket after 30 minutes"
- "set the bedroom lights to 50% when motion is detected only if it is dark"
- "turn on the hallway lights between 08:00 and 23:30"
- "when kitchen temperature goes above 25 turn on the fan otherwise turn it off"

"for N minutes" without a trigger compiles to the classic auto-revert timer,
keyed on the device reaching the acted state.
