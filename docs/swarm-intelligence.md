# Swarm Intelligence

## Overview

The automation engine speaks in raw attribute names, operators and literals.
Nothing in it knows that `occupancy` is a thing that can *become true*, that a
lux reading is what "dark" means, or that a radar and a bulb in the same room
are an obvious pair. Swarm Intelligence is the layer that does.

Every device — Zigbee, Matter, Nuki, a presence user — is reduced to the same
shape: a list of **offers**. An offer is one thing a device can contribute to a
rule, in one of three roles:

| Role          | What it is                     | Example                              |
|---------------|--------------------------------|--------------------------------------|
| **trigger**   | an edge worth waking a rule on | "someone is detected in the Hallway" |
| **condition** | a state worth testing          | "the Hallway is dark"                |
| **action**    | a command worth sending        | "turn on Light - Hallway"            |

Because every device is described in one vocabulary, **any trigger composes with
any action anywhere on the network**. The wiring between two devices is derived,
not enumerated: there is no list of supported combinations to maintain, and a
device nobody anticipated wires to everything the moment its capabilities
resolve.

Offers compile down to the rule dict `AutomationEngine.add_rule()` already
accepts. This layer adds vocabulary, not a second execution path.

---

## Layout

```
modules/swarm/
  capabilities.py   the vocabulary — what each capability can contribute
  resolver.py       one device, of any protocol, reduced to its offers
  network.py        the whole swarm, and the ranked wiring between devices
  stigmergy.py      patterns: named shapes over the vocabulary
  matcher.py        fills a pattern's slots, and traces why each did or did not
  compiler.py       a filled pattern -> the rule dict the engine accepts
  dedupe.py         matching suggestions against rules that already exist
  suggestions.py    match -> compile -> validate -> dedupe, in one call
  diagnostics.py    triage: what is broken, what is blind, and why
  doctor.py         the same report from the command line
  api.py            HTTP surface
```

Everything except applying a suggestion is read-only. `POST .../apply` is the
single mutating path, and it creates the rule through the engine's own
`add_rule()` — there is no second way into the rule store.

---

## The vocabulary

`capabilities.py` holds one entry per semantic capability. Each declares the
attributes that back it and the offers it contributes:

```python
"presence": {
    "label": "Presence",
    "kind": "sensor",
    "tags": ["occupancy", "security"],
    "attrs": ["occupancy", "presence", "motion", "occupied", "presence_state"],
    "triggers": [
        {"id": "detected", "label": "someone is detected in {room}",
         "operator": "eq", "value": True, "weight": 2, "polarity": 1},
        ...
    ],
    ...
}
```

Offer fields:

| Field                    | Meaning                                                               |
|--------------------------|-----------------------------------------------------------------------|
| `id`                     | stable within the capability; `"<cap>:<id>"` is globally stable       |
| `label`                  | sentence fragment; `{device}`, `{room}` and `{value}` are substituted |
| `operator` / `value`     | the comparison it compiles to                                         |
| `command` / `value_from` | the command it compiles to, and the parameter supplying its argument  |
| `step`                   | a non-command step type, for the message action                       |
| `sustain`                | suggested hold before the edge counts                                 |
| `weight`                 | tiebreak only — orders equally-scored pairs, never changes confidence |
| `polarity`               | `+1` activating, `-1` deactivating, absent where ambiguous            |

Two capability-level relations exist:

- **`implies`** — a capability entailing another that nothing in the device's
  state or commands would reveal. A person is messageable by being a person.
- **`excludes`** — a capability ruling one out. A presence user's `presence`
  attribute names a place, so without `person` excluding `presence` the person
  themselves resolves as a motion sensor.

### Parameters

Thresholds are not baked into offers. `PARAMS` holds the tunable ones —
`dark_lux`, `cold_c`, `battery_pct`, `clear_hold_s` and the rest — with a
default, a unit and bounds, so a suggestion card can expose the number as a
field. `dark_lux` shares its default with the NL parser, so "dark" means the
same value however a rule is authored.

---

## Resolution

`resolver.describe_device()` folds four sources into one canonical capability
list:

1. **Declared** — whatever the device's own stack claims, in that stack's
   vocabulary. Four shapes exist and all are handled: a Zigbee
   `DeviceCapabilities` object, the Matter `_get_capabilities()` accessor, a
   duck-typed capabilities object (Nuki), and a plain list.
2. **Profile** — a matched `device_profiles` entry, which is the user's explicit
   answer about what a device is, so it is additive rather than filtered.
3. **Sniffed sensing** — capabilities evidenced by the attributes the device
   actually reports.
4. **Sniffed actuation** — capabilities evidenced by the commands it accepts.

The split at 3/4 matters. A `state` attribute is claimed by locks, switches and
covers alike, so reading one proves nothing about what a device can be *told* to
do — a dispatchable `lock` command does. Inferring actuation from state instead
of commands is what previously made every bulb resolve as a lock.

Three legacy vocabularies are folded here rather than at each call site:
`DeviceCapabilities` (Zigbee clusters), `device_profiles.DEVICE_TYPES`, and the
Matter definition scan. They disagree on names for the same idea —
`motion_sensor` / `motion`, `power_monitoring` / `power` — and
`LEGACY_CAPABILITY_ALIASES` is the single place that reconciles them.

Either detection source alone is sufficient: a profile claiming `presence` on a
device reporting no matching attribute yields nothing, and an unprofiled radar
reporting `presence` yields the full presence offer set without anyone having
described it first.

### Guards

- An action is only offered for a command in `AutomationEngine.VALID_COMMANDS`
  *and* present on the device. The Nuki's `unlatch` and `lock_n_go` are real
  commands the engine cannot dispatch, so they never become offers — a rule that
  validates and then does nothing is worse than no rule.
- Boolean offers are rewritten into the device's own vocabulary: a rule
  comparing `state == True` against a device reporting `"ON"` never matches.
- Contact polarity is resolved per device, because it is genuinely
  device-specific rather than derivable — the Zigbee `contact` attribute is
  `True` when the door is *shut*, while `is_open` means what it says.
- An offer the device cannot express is dropped rather than emitted broken.

---

## Pairing

`network.pairings()` returns every wiring a device participates in, in both
directions. `outbound` is this device's triggers driving others' actions;
`inbound` is others' triggers driving this one. A device appears in both when it
is a sensor and an actuator — an actuator *being switched on* is itself an edge
worth triggering on, which is why "if this light comes on, also do X" needs no
special case.

The cross-product is complete, so ranking is what makes it usable. Score:

| Term                             | Weight   | Why                                                                                   |
|----------------------------------|----------|---------------------------------------------------------------------------------------|
| capability → action-tag affinity | ×2       | The dominant term: a leak sensor's best pairing is telling someone, wherever they are |
| same room                        | +3       | Proximity, for room-scoped devices                                                    |
| source is house-scope            | +3       | A person applies everywhere, so proximity cannot be scored against them               |
| polarity match / mismatch        | +2 / −3  | Keeps "someone is detected → turn the light **off**" out of the lead                  |
| same device                      | −5       | Legitimate but never the pairing being looked for                                     |

Confidence bands at ≥7 (high) and ≥4 (medium).

Two details carry most of the quality:

- **Affinity outweighs proximity.** Weighting them equally let "battery low →
  message Charlie" tie with "presence → hallway light".
- **Device class contributes tags.** `on_off` is generic switching on a plug and
  lighting on a bulb; the capability alone cannot know which. Without
  `DEVICE_CLASS_TAGS`, "set brightness" outranked "turn on" on a light, because
  only the former was tagged `lighting`.

Nothing is a whitelist. A low-scoring pair is still returned, just further down.

---

## Reading the network

The engine's merged registry is the only complete view of the network — Zigbee,
Matter, Nuki and presence users all reach automations through it — so describing
that view describes everything an automation can address.

Room assignment rides on `device_settings[ieee]["chamber"]`, the same key the
Frames UI already writes; the chamber registry is cached against `config.yaml`'s
mtime.

---

## What this produces

Given a hallway holding a radar (presence + lux), a dimmable light and a door
contact, plus a Nuki lock and two presence users, the top-ranked wirings are:

```
Radar Sensor - Hallway — 60 possible wirings
  [high] When someone is detected in Hallway, turn on Light - Hallway
  [high] When Hallway gets dark, turn on Light - Hallway
  [high] When Hallway becomes empty, turn off Light - Hallway

Sean — 36 possible wirings
  [high] When Sean arrives home, unlock Front Door Lock
  [high] When Sean leaves home, lock Front Door Lock
  [high] When Sean arrives home, turn on Light - Hallway
```

None of those combinations is written down anywhere. They fall out of the
vocabulary, the room index and the ranking.

---

## Testing

    python3 tests/swarm/run_all.py

475 checks across six modules. See `tests/swarm/README.md`. The fake network exists so the ranking assertions
mean something: the tests check that the pairing a person would choose by hand
is the one that comes out on top, without that pairing appearing in the source.

---

---

## Stigmergy

Stigmergy is how a swarm coordinates without a coordinator: each member leaves a
trace in the shared environment, and those traces tell the next member what to
do. Here the environment is the device registry, and a **pattern** is a trace
laid over it.

A pattern names an intent and describes a shape more complex than a single
trigger-to-action pair — a condition drawn from a second device, an else-branch
that turns the light back off, a delay before a message. Patterns do not decide
what is *possible*; `pairings()` already returns every wiring the swarm
supports. They decide what is worth *offering*.

Patterns are JSON, in `data/stigmergy/` (shipped) and `data/stigmergy_user/`
(yours, overriding by id — so a local edit survives an upgrade). 22 ship by
default across lighting, climate, security, safety, energy, presence,
maintenance and convenience.

```json
{
  "id": "presence_light_when_dark",
  "title": "Light on when the room is occupied after dark",
  "scope": "room",
  "slots": {
    "trig":  {"role": "trigger",   "offer": "presence:detected"},
    "dark":  {"role": "condition", "offer": "illuminance:is_dark",
              "optional": true, "prefer": "same_device", "prefer_slot": "trig"},
    "light": {"role": "action",    "offer": "on_off:turn_on",
              "device_class": ["light", "color_light", "switch", "plug"]}
  },
  "emits": {
    "name": "{room} light on presence",
    "source": "trig",
    "conditions": ["trig", "dark"],
    "then": ["light"],
    "else": ["off"],
    "cooldown": 5
  },
  "params": {"dark_lux": 11, "clear_hold_s": 120}
}
```

`scope` decides the partition. A **room**-scoped pattern is tried once per room,
drawing on that room's devices plus every house-scope device (a person applies
everywhere, so excluding them would make "lights on when someone gets home"
inexpressible per room). A **house**-scoped pattern is tried once against the
whole network.

`optional` slots may go unfilled; every reference to an absent slot is dropped
rather than compiled to nothing. How many optional slots landed is what sets a
suggestion's confidence — the pattern reduced to its mandatory slots still
works, but the whole shape landing is a stronger suggestion.

`prefer: same_device` is not cosmetic — see the next section.

### Condition or prerequisite

The engine evaluates `conditions` against the **source** device's state and
`prerequisites` against any other device. So the same check belongs in different
places depending on which device supplies it, and the compiler decides from the
resolved devices rather than making each pattern say.

That is exactly the hallway case. A radar reporting both presence and lux fills
both slots from one device, so "is dark" compiles to a *condition* and the rule
re-evaluates when the lux changes. Where the lux comes from a separate sensor,
the identical check compiles to a *prerequisite*. Writing it into the pattern by
hand would get it wrong for one of the two layouts, and the wrong choice
produces a rule that validates and never fires.

### What varies

A pattern matched in one scope can still yield several suggestions, and which
slot varies is a judgement about what a person would want twice:

- The **action** slot varies. Two lamps in a room are two real automations.
- In a **house**-scoped pattern the **trigger** varies too. Nothing else
  partitions it: "tell me when a battery runs low" must produce one per battery
  device, and "unlock when someone gets home" one per person.
- **notify** slots never vary. Messaging a different person is the same
  automation with a different recipient, so the alternatives are offered as a
  choice rather than as extra cards.

---

## Suggestions

`suggestions.build()` runs the whole chain — match, compile, validate, dedupe —
and returns suggestions grouped by room with the trace behind them.

**Every suggestion is validated through the engine's own validators before it is
returned.** Reusing `_validate_conditions`, `_validate_prerequisites`,
`_validate_sequence` and `_validate_zone_source` rather than re-implementing
them is the point: a suggestion is only trustworthy if it passes the same checks
the save path applies. Anything rejected is withheld and reported to
diagnostics — a suggestion that fails at Create is a defect in a pattern, and it
should be found here, where the trace says which pattern and which slot produced
it.

### Deduplication makes it a coverage report

A candidate whose wiring is already live comes back marked `active`, pointing at
the rule, instead of being offered again. Matching is by **wiring**, not text:

```
signature = (source_ieee, watched attributes, {(target, command)})
```

Names, thresholds and cooldowns are deliberately excluded. A rule firing at 11
lux and a suggestion at 10 are the same automation, and offering the second is
not useful. That one property is what turns the list into a to-do: what the
swarm could do, minus what it already does.

`coverage` then reports which devices take part in at least one rule — as a
source *or* a target, since a bulb nobody has automated is a gap even though it
triggers nothing itself.

### Applying

`POST /api/swarm/suggestions/{id}/apply` re-matches and recompiles from the live
network rather than trusting a rule posted back by the client — the network may
have moved on since the suggestion was offered, and a client-supplied rule is a
client-supplied rule. A suggestion that no longer matches is refused with a
reason rather than compiled from stale data.

---

## Debugging

Failures in this layer are quiet by nature. A pattern that failed to load, one
that loaded but matched nothing, a match that failed to compile, a compile that
failed validation, and a suggestion correctly withheld because the rule already
exists **all present identically to a user — as an absence**. Each has a
different fix, so each is reported separately and by name.

### The report

```bash
curl localhost:8000/api/swarm/diagnostics      # live, definitive
python3 -m modules.swarm.doctor                # offline, from disk
```

Findings are graded: `error` means something is broken, `warning` means the
swarm works but is blind to part of the house, `info` is context.

| Code | Level | Means |
|---|---|---|
| `pattern_load_failed` | error | A pattern is invalid and is not being offered at all |
| `no_patterns` | error | Nothing loaded — check `data/stigmergy/` |
| `no_devices` | error | The engine has not started, or its registry is empty |
| `suggestions_rejected` | error | Candidates withheld at compile or validate — a defect in a pattern, not the network |
| `rules_unsignable` | error | An existing rule could not be read for dedupe, so suggestions may be re-offered |
| `no_rooms` | warning | No chambers defined, so every room-scoped pattern is unmatchable |
| `devices_unplaced` | warning | In no room, so room-scoped patterns cannot pair them |
| `devices_without_capabilities` | warning | Resolved to nothing — usually an unfinished interview or missing profile |
| `rules_orphaned` | warning | A rule names a source the swarm cannot see |
| `patterns_unmatched` | info | Matched nothing, with the blocking slot named |
| `capabilities_absent` | info | Not present on any device, so patterns needing them cannot match |

A report never raises an alarm about a limitation of its own input. The CLI
reads the state cache, which holds state but not command lists — and actuation
is detected from commands — so it passes `commands_available=False` and
`devices_without_capabilities` is downgraded to info with an explanation. Use
the endpoint for a definitive read on a running system.

### Explaining one pattern

When a specific suggestion is missing, ask why:

```bash
python3 -m modules.swarm.doctor --explain presence_light_when_dark
curl localhost:8000/api/swarm/explain/presence_light_when_dark
```

```
presence_light_when_dark — Light on when the room is occupied after dark
  scope: room   outcome: matched   candidates: 1

  Hallway  match
    ✓ trig     Radar Sensor - Hallway [presence:detected]
    ✓ dark     Radar Sensor - Hallway [illuminance:is_dark]  (same device as trig)
    ✓ light    Light - Hallway [on_off:turn_on]  +1 alternative(s)
    ✓ off      Light - Hallway [on_off:turn_off]  (same device as light)

  Lounge   no match
    no device in Lounge provides what slot 'trig' needs
    ✗ trig     unfilled — no_offer_in_scope (REQUIRED)
```

Unfilled slots carry one of four distinct reasons, because each has a different
fix:

| Reason | Fix |
|---|---|
| `no_offer_in_scope` | Nothing in this room makes the offer — add a device, or move one in |
| `class_mismatch` | Something offers it, but is the wrong kind of device — check its profile |
| `preference_unmet` | The slot must sit on the same device as its anchor, and none does |
| `no_offer` | Nothing on the network makes this offer at all |

When a slot will not fill, the first question is whether *anything* offers it,
ignoring room and class:

```bash
curl localhost:8000/api/swarm/explain/presence_light_when_dark/slot/light
```

### Authoring a pattern

`POST /api/swarm/validate` checks a pattern without saving it, and
`POST /api/swarm/stigmergy/reload` re-reads the directories without a restart.
Validation is deliberately strict at load — a pattern that cannot compile is
worse than one that does not exist, because it reaches a user as a suggestion
and then fails at Create. It catches unknown capabilities and offer ids, a
source slot that is not a trigger, an optional source, slots referenced by
`emits` that do not exist, `$slot` references in literal steps that do not
resolve, unknown parameters, and slots that nothing in `emits` ever uses.

### Other suspects

```bash
curl localhost:8000/api/swarm/suggestions?include_trace=true   # every scope tried
curl localhost:8000/api/swarm/capabilities/<ieee>              # what one device offers
curl localhost:8000/api/swarm/pairings/<ieee>                  # what it could wire to
python3 -m modules.swarm.doctor --suggestions                  # what would be offered
python3 -m modules.swarm.doctor --json                         # machine-readable
```

If a device offers nothing, the fault is in Phase 1 resolution rather than in a
pattern — check `/api/swarm/capabilities/<ieee>` before reaching for `explain`.

---

## API

All read-only except `apply`. Rule creation goes through the engine's
`add_rule()`; there is no second way into the rule store.

| Route | Returns |
|---|---|
| `GET /api/swarm/vocabulary` | The capability table and parameters. Static for the process life. |
| `GET /api/swarm/capabilities` | Every device's offers, plus a coverage summary |
| `GET /api/swarm/capabilities/{ieee}` | One device's offers |
| `GET /api/swarm/pairings/{ieee}` | Ranked wiring, both directions. `min_confidence`, `limit` |
| `GET /api/swarm/rooms` | Chamber registry plus which devices sit in each |
| `GET /api/swarm/stigmergy` | Patterns loaded, and any that failed |
| `GET /api/swarm/stigmergy/{id}` | One pattern |
| `POST /api/swarm/stigmergy/reload` | Re-read the pattern directories |
| `POST /api/swarm/validate` | Check a pattern without saving it |
| `GET /api/swarm/suggestions` | Suggestions. `room`, `category`, `status`, `include_trace` |
| `GET /api/swarm/suggestions/{id}` | One suggestion, with the rule it would create |
| `POST /api/swarm/suggestions/{id}/apply` | Create it. Body: `{params, name}` |
| `GET /api/swarm/coverage` | Which devices take part in a rule |
| `GET /api/swarm/diagnostics` | Triage report |
| `GET /api/swarm/explain/{pattern_id}` | Why a pattern matched, per scope |
| `GET /api/swarm/explain/{pattern_id}/slot/{slot}` | Everything that could fill one slot |

---

## Next

Phases 1 and 2 have shipped. There is still no UI — everything is API and CLI.
`docs/plans/swarm-intelligence.md` carries the plan for:

- **Phase 3** — virtual devices (`weather::`, `house::thermal`, `tariff::`) and
  computed attributes like `minutes_to_home`, which unlock the pre-arrival
  heating and the AC-offer patterns
- **Phase 4** — the `offer` step (a message with a one-tap action), and the
  Suggested tab / wire-from-device views
- **Phase 5** — retro-tagging existing rules, and pointing the NL and AI paths at
  the vocabulary
