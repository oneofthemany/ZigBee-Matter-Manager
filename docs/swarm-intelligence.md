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

### What a device is, versus what it claims

Three rules, each earned from a device that broke without it. Together they are
why a door sensor is a door sensor.

1. **A device that describes itself is believed about actuation.** The command
   list is built from clusters without regard to direction, so a sensor
   advertising OnOff as an *output* cluster for binding gets an `on`/`off`
   command it will never honour — a Hue SML and an Aqara magnet both do. Zigbee
   capability detection already applies quirks that discard exactly those
   claims; inferring actuation back from the command list walked straight past
   that work, and made **door sensors switches and motion sensors lights**.
   Command-based inference is now a fallback for devices nothing has described
   (an unprofiled Tuya socket), never a second opinion against a stack that has
   already decided.

2. **A capability whose readings are not exclusive to it is never sniffed.**
   `weather` reads a temperature, and so does every thermostat and half the
   motion sensors. Inferring it made a bathroom sensor *the weather* — and
   since weather is house-scoped, that let it reach into every other room and
   offer to switch on a pendant two floors away. The three service-backed
   virtual devices are always constructed with their capability declared, so
   they are marked `sniffable: False`. `person` and `household` stay sniffable:
   presence users declare nothing, and `place` and `anyone_home` belong to
   nothing else.

3. **A declaration the device contradicts is dropped** — see
   `capabilities_unproven` under Debugging.

Because actuation now comes from declarations, **every actuator capability must
be reachable from one** — a legacy name that folds to it, or another capability
that implies it. `color_temp` was not: no vocabulary names it separately, and it
had only ever resolved through command sniffing, so colour bulbs quietly lost
colour temperature. `color` now implies it, which is safe because the action is
still gated on the device having the command. `test_resolver.py` asserts the
reachability of every actuator so this cannot recur.

`DEVICE_CLASS_RULES` is ordered on the same principle: identity capabilities
first (only a person has `person`, and the weather must outrank the temperature
it reports), then unambiguous hardware, then sensing that cannot be faked,
*then* the switchable classes. A plug outranks a light, because a metering
socket often advertises a level cluster it does not use while a dimmable bulb
almost never reports power.

`tests/swarm/test_real_house.py` pins all of it to a live household's device
shapes — including the awkward ones — so this class of nonsense cannot come
back.

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
- Two shapes of the same reading are both accepted where they exist in the
  wild, and an offer may pin itself to one of them with its own `attrs`:

  - A **battery** is a percentage from the power-configuration cluster *or* a
    bare low flag from the IAS Zone status bitfield. A percentage-only
    capability left the sensors most in need of a low-battery warning without
    one.
  - A **tariff** is cheap by *window* on an agile plan — relative, and it moves
    daily — or simply by *price* on a fixed one. Modelling only the window left
    a household with a working unit rate and no agile window unable to say
    anything about electricity at all.

  A pattern slot may therefore name **several alternative offers**
  (`["battery:low", "battery:low_flag"]`, `["tariff:off_peak_started",
  "tariff:got_cheap"]`) — the same intent reached two ways, where a device has
  one or the other rather than both.
- An attribute is matched by exact name first, then by **every** endpoint-suffixed
  form of it. A multi-endpoint device spells its readings `power_1`, `state_2`,
  `power_demand_1`; matching only the bare name excluded a whole class of
  devices, and did so silently — an Aqara socket resolved the `power` capability
  from its cluster and then offered nothing.

  Per-endpoint keys beat a bare one. A real dual-gang socket reports `state`
  *and* `state_1` *and* `state_2` — the bare key is an aggregate or an alias for
  the first outlet, and the numbered pair is what can actually be addressed. A
  *single* suffixed endpoint is not a fan-out: `brightness` beside
  `brightness_1` is one control named twice, so the bare form is preferred and
  the offer key stays stable.

  A dual-gang socket gets **one offer per outlet**, because its two gangs switch
  and draw power independently. Outlet 1 keeps the bare offer key so a key stays
  stable whether or not a device turns out to have a second gang, and only
  outlet 2+ is annotated — outlet 1 is the default one, and naming it is noise.
  The action side had always fanned out per endpoint; before this the trigger
  side collapsed to outlet 1, so a socket could be *told* to switch outlet 2 but
  could not *trigger* on it. The vocabulary must therefore not list an
  endpoint-suffixed candidate explicitly: `state_1` in the `on_off` attrs
  short-circuited the scan as an exact match and collapsed the fan-out.

  Suggestion variants are keyed on device *and* endpoint for the same reason: if
  outlet 1 is the washing machine and outlet 2 the dryer, "tell me when it
  finishes" is two rules. A button's four press types share an endpoint and stay
  one suggestion.
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

`chambers.build_registry()` unions three sources by **id**: configured chambers,
rooms adopted from heating, and rooms adopted from the floor plan. Two
subsystems naming one physical room differently therefore produce two
chambers — one of which is then always empty. That is by design, since the ids
are load-bearing, but it has a real failure mode: if devices end up assigned to
*both* ids the swarm treats them as separate rooms and room-scoped patterns
cannot pair across them. `load_room_meta()` carries each chamber's `source` so
the empty-rooms finding can say which subsystem defined it, rather than leaving
an empty room looking stale.

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

602 checks across eight modules. See `tests/swarm/README.md`. The fake network exists so the ranking assertions
mean something: the tests check that the pairing a person would choose by hand
is the one that comes out on top, without that pairing appearing in the source.

---

---

## Virtual devices

Some inputs are not devices: the weather, the thermal state of the house, the
electricity tariff. `modules/swarm/virtual.py` presents them as ordinary
device-likes, registered through the engine's `add_device_getter()` hook, so a
pattern fills a slot from one without knowing it is different and a rule
triggers on one with no new condition type.

| Device | Attributes |
|---|---|
| `virtual::weather` | `temperature`, `humidity`, `wind_speed`, `solar_wm2`, `is_daylight` |
| `virtual::house` | `indoor_avg_temp`, `outdoor_temp`, `preheat_minutes`, `outdoor_cooler_than_indoor`, `preheat_now_for_arrival` |
| `virtual::tariff` | `unit_rate`, `is_off_peak` (only on an agile tariff, where a cheapest window exists) |

### Colour as a notification

A light that can hold a colour says something a light that only switches
cannot — a notification nobody has to read. The device layer had always
dispatched `hs_color`, but `get_control_commands()` never offered it and the
engine's `VALID_COMMANDS` rejected it, so no rule could carry one. Both are
fixed, and the `color` capability now has a `set_color` action.

A colour travels as `[hue 0-360, saturation 0-100]`, which is what the device
takes. The `alert_colour` parameter names its choices — red, amber, green,
blue, purple, warm white — so a card can offer swatches and `param_display()`
can render the sentence as "turn Pendant - Living red" rather than "to
[0, 100]".

Five patterns use it: a door left open turning a light amber and back, leak and
smoke turning lights red, a warmer colour temperature on arrival after dark, and
a green tint while electricity is cheap.

### Computed flags

The engine compares an attribute to a **literal**, never to another live
reading. "Is it cooler outside than in?" and "is somebody due home within the
time it takes to warm the house?" are comparisons between two moving values, so
they are decided by the module that owns the model and published as a plain
flag. That keeps the rule engine simple and puts the arithmetic where the
knowledge is — it is the reason the two hardest examples compile to ordinary
rules:

```
When somebody is due home within the warm-up time and the house is below 18.0,
    set Lounge TRV to 21.0

When the house rises above 24.0 and it is cooler outside than in,
    message Sean
```

`preheat_now_for_arrival` compares each away person's travel time against the
advisor's live warm-up estimate. Travel time is derived from the `distance_m`
the presence users already hold, at a deliberately conservative average speed —
**not** from journey history, because reading that means a DuckDB query and
nothing here may touch a database. An arrival predicted late is a cold house; one
predicted early wastes a little gas.

### Rules this module keeps

- **Nothing on the event loop.** Every value comes from a service's already
  cached state, and the arithmetic is a handful of floating-point operations. No
  query, no file, no DuckDB. A timer refreshes and publishes a dict; the engine
  only ever reads that dict.
- **A missing reading is dropped, never published as zero.** An absent value
  read as `0` would fire every "below" rule in the house the moment a service
  restarts.
- **Only changes are reported.** The refresh returns what actually moved, so a
  rule does not re-evaluate every minute for no reason.
- **Every service is optional.** A house with no tariff integration has no
  tariff device rather than a broken one, and a throwing advisor does not take
  the weather down with it.
- **Availability is not offered.** These devices are always available, so an
  "goes offline" trigger on one would compile and never fire. The `excludes`
  mechanism suppresses it — as it does for presence users and the household,
  which report availability unconditionally for the same reason.

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

Patterns are JSON, in two places:

| | |
|---|---|
| `modules/swarm/patterns/` | **Bundled.** Ships with the release, alongside the code. |
| `data/stigmergy_user/` | **Yours.** Overrides a bundled pattern by id, so a local edit survives an upgrade. |

The split is a deployment constraint, not tidiness: the container mounts the
host's persistent directory over `/app/data`, so anything the image ships under
`data/` is masked at runtime — present in the image and unreadable. Bundled
patterns are shipped content and belong with the code; user patterns are runtime
data and belong in the volume. `BUNDLED_DIR` resolves from the module's own
location, so it works whatever the working directory is.

28 patterns ship by default across lighting, climate, security, safety, energy,
presence, maintenance and convenience.

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

A slot may be **anchored** to another with `prefer`:

| | |
|---|---|
| `same_device` | The reading belongs with its own trigger — a radar reporting both presence and lux answers "is it dark here" about the room it is watching. |
| `same_room` | A house-scoped pattern whose condition should still be local. "Is it dark", asked of a bathroom sensor while switching a living-room lamp, is technically an answer and reads as a mistake. |

When a varying slot has something anchored to it, candidates that let the
anchored slot fill are offered first — otherwise the variant cap can spend
itself on rooms that cannot answer, and every suggestion silently loses its
condition. Rooms that cannot answer are still offered, just later.

A slot may carry its own `params`, overriding what the rest of the pattern
shares. Some thresholds mean different things in different places: `cold_c` is a
cold snap at 5 degrees outdoors and an unheated room at 18 indoors, and a
pattern comparing the two would otherwise have to pick one and be wrong about
the other. Pattern-level parameters are what a suggestion card exposes as
editable fields; a slot override is deliberately not, since it exists precisely
because that slot needs a different value.

Sentences are re-rendered at the pattern's own thresholds rather than the
vocabulary defaults the offers were built with — a card advertising "drops below
18.0" while compiling a rule that fires at 5.0 is worse than no card.

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

## In the UI

The swarm does not get a tab of its own. It enhances the one creation flow that
already exists, because two ways to build a rule is one too many.

**Add Rule** no longer opens a blank form. It opens the chooser:

```
Add Rule  (device already chosen)
  ├── Ready-made — stigmergy suggestions for this device, one tap
  ├── Or wire it to something — ranked pairings, plain English
  └── Start from scratch — the blank form, as before
```

Every path ends at `window._aShowFormWith(rule)` — the hook the AI generator
already used — so the user lands in **the existing builder, pre-filled**. The
whole step palette is untouched and one click away: delay, gate, wait-for,
if/then/else, parallel, media, message. The simple choice fills the form in; the
advanced work happens where it always did.

That is the point of the layering. Offers describe a trigger, a condition and an
action — they cannot express a delay chain or a parallel branch, and they are not
meant to. They get you to a working rule in one tap; the builder takes it from
there.

| File | Role |
|---|---|
| `static/js/swarm-suggest.js` | The chooser. Fetches suggestions and pairings, renders the options, hands the chosen shape to the builder |
| `static/js/modal/automation.js` | Unchanged except that `_aShowForm` opens the chooser first, and a save invalidates its cache |
| `static/js/automations-page.js` | Coverage strip in the header, and the click-through list of devices no rule touches |
| `static/js/automation-sentence.js` | The one plain-English voice. The rules list and the editor both import it, so a rule reads the same wherever it appears |
| `static/css/swarm.css` | Only the hover affordance and sentence wrapping — everything else is shared Bootstrap |

The swarm is an **enhancement, never a gate**. Every fetch is best-effort: if
`/api/swarm/*` is unavailable, slow, or returns nothing worth offering, the
chooser falls straight through to the blank form and the builder behaves exactly
as it did before. Clock-triggered rules (`__time__`) skip the chooser entirely —
there is no device to suggest for.

Two filters apply, and the second is a correctness guard rather than a
preference:

- Suggestions **already built** are dropped rather than greyed out. This is a
  creation step, and an option that cannot be picked is clutter.
- Suggestions **not triggered by this device** are dropped. The builder saves
  with `source_ieee: currentSourceIeee` and fetches its attribute list for that
  device, so offering one sourced elsewhere would put a rule saved against the
  wrong device one click away. The API's `device=` filter is deliberately
  broader — it answers "what does this device take part in" — so the narrowing
  happens in the chooser, where the constraint actually lives. Pairings use
  `outbound` only for the same reason.

The Rules header carries the counterpart — `18/34 devices automated`, with the
gaps one click away.

---

## Offers — a message that can act

Some automations should not act on their own. "It is cooler outside than in" is
a reason to *ask*: opening a window may be the better answer, and only somebody
in the room knows. The `offer` step exists for that.

```json
{
  "type": "offer",
  "to_user": "sean",
  "message": "It is warmer inside than out. Cool the house down?",
  "expires_in": 3600,
  "accept_steps": [
    {"type": "command", "target_ieee": "0xplug", "command": "on"}
  ]
}
```

It sends a message like the `request` step, tagged `automation_offer` so a
client renders it with Accept and Decline rather than as text. **The action is
held in the engine, never in the message** — what runs on acceptance is what the
rule said, not what came back over the wire. Accepting runs `accept_steps`
through the same `_run_sequence()` every other sequence uses.

| Property | Behaviour |
|---|---|
| Not persisted | An offer is a question about right now. One that survived a restart to fire hours later would be worse than one that quietly lapsed. |
| Removed before running | Removed from the registry *before* the sequence starts, so a double tap cannot run the action twice. |
| Lapses | Expires after `expires_in` (default an hour, cap a day), swept lazily on read rather than by a timer. |
| One per rule and recipient | A rule re-firing replaces its own question rather than queueing a second copy. Different rules asking the same person stand separately. |
| Capped | `MAX_PENDING_OFFERS`, oldest evicted — a stale question is the one least worth keeping. |
| Addressed | Answering an offer addressed to somebody else is refused. |
| Not sent, not pending | If the message fails to send, the offer is dropped: nobody was told, so nothing can be accepted. |

```
GET  /api/automations/offers?to_user=       offers awaiting an answer
POST /api/automations/offers/{token}/accept run the stored sequence
POST /api/automations/offers/{token}/decline nothing runs; it goes away
```

The pending list appears at the top of the Automations page — an offer is a rule
that stopped to ask, so it belongs with the rules rather than in a notification
tray.

### In a pattern

A pattern splices its own slots into the accept branch with `{"slot": "id"}`:

```json
"then": [{
  "type": "offer",
  "to_user": "$who",
  "message": "It is warmer inside than out. Cool the house down?",
  "accept_steps": [{"slot": "cool_it"}]
}]
```

Two markers, deliberately distinct: `$slot` inside a string is that slot's
**address** (a message naming its recipient), while `{"slot": id}` is that
slot's **step**. Conflating "the address of the thing" with "the action on the
thing" reads ambiguously in exactly the place it matters.

The shipped `ac_offer_when_cooler_outside` produces:

```
When the house rises above 24.0 and it is cooler outside than in,
    ask Sean first, and only then turn on the Lounge Plug
```

### In the builder

`Ask` sits alongside `Msg` in the step palette. Its accept branch is a nested
sequence like an `if_then_else` arm, so anything the builder can express can run
on acceptance — including a delay, a gate, or another branch.

### Editing a rule

The editor was built for authoring and is now mostly used for *reading and
adjusting* something the swarm already filled in — a different job. Three
changes, all reuse rather than replacement:

- **A live plain-English preview at the top**, in the same words the rules list
  uses. `_collectRule()` was extracted from the save path so the preview and the
  save build the same object from the same DOM — a preview assembled a second
  way would eventually disagree with what saving produces, which is worse than
  no preview. It refreshes on a delegated `input`/`change` listener rather than
  per-widget handlers, because the builder creates its controls dynamically and
  anything bound per-widget would miss the ones added later.
- **Optional sections collapse.** Prerequisites and the ELSE branch are empty on
  most rules, so they are hidden until they hold something — revealed
  automatically for a rule that uses them, and on demand behind "More options".
- **Save moved to its own bar** at the foot, beside Cancel. It was a grid cell
  next to Cooldown, which read as one more field.

The phrasing itself moved to `automation-sentence.js`, bound to injected name
lookups rather than module globals: the rules page caches names per page load,
the editor holds them per open device, and neither has to reshape its state to
borrow the other's voice. Saying it two ways would eventually mean saying it two
different ways.

---

## Debugging

Failures in this layer are quiet by nature. A pattern that failed to load, one
that loaded but matched nothing, a match that failed to compile, a compile that
failed validation, and a suggestion correctly withheld because the rule already
exists **all present identically to a user — as an absence**. Each has a
different fix, so each is reported separately and by name.

### Readiness — read this before believing a finding

A report taken shortly after start describes a half-filled network. Nothing is
wrong: a battery sensor may not have woken, the weather may not have been
fetched, a tariff may not have polled. But **a service that has not reported yet
looks identical to one that is misconfigured**, and only the clock tells them
apart.

So the report carries a `readiness` block and marks the affected findings
`provisional: true`:

```json
"settled": false,
"readiness": {"uptime_s": 12.0, "warmup_seconds": 150, "provider_refreshes": 0}
```

Two conditions must both hold to be settled: past `WARMUP_SECONDS` (150s — the
virtual provider waits 5s and then refreshes on a 60s cycle, so anything earlier
is guaranteed premature about it), and the virtual provider having refreshed at
least once. `provider.status()` reports the second, per device, counting only
attributes beyond `available` as "reported".

Provisional findings are the ones that read what a device *reports*:
`capabilities_unproven`, `capabilities_silent`, `capabilities_absent`,
`devices_without_capabilities`, `patterns_unmatched`. Everything else — patterns
loading, rooms, rules, unplaced devices — is true immediately and is never
marked.

Unknown uptime counts as settled, because the CLI has no idea how long the app
has been running and withholding every finding there would make the offline
report useless.

This matters in practice. Two runs minutes apart showed the tariff reporting
`["available"]` and then `["available", "unit_rate"]` — the first read as a
broken service and was simply an early one.

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
| `capabilities_unproven` | warning | A device declared a capability and reports nothing backing it. Dropped so it cannot mis-describe the device, but reported with both lists side by side. |
| `capabilities_silent` | warning | A capability resolved but produced no offer, so it counts as present yet blocks every pattern needing it |
| `rules_orphaned` | warning | A rule names a source the swarm cannot see |
| `patterns_unmatched` | info | Matched nothing, with the blocking slot named |
| `rooms_empty` | info | A chamber holds no devices, with the subsystem that defined it named |
| `capabilities_absent` | info | Not present on any device, so patterns needing them cannot match |

Three findings sound alike and mean different things; confusing them wastes an
afternoon.

- **Absent** — nothing on the network has the capability. Buy a sensor.
- **Unproven** — a device *declared* it and reports nothing backing it. The
  declaration is dropped, because a capability a device contradicts would
  otherwise mis-describe it: Zigbee detection guesses that any IAS Zone device
  which is not a known magnet contact is a motion sensor, so a contact sensor
  arrives declaring presence and would classify as a presence sensor — the wrong
  kind of device for a pattern to pick. It is still reported, with the expected
  attributes beside the ones the device actually sends, because the cause is
  sometimes a bad guess upstream and sometimes a name this vocabulary does not
  know yet. Reading *"declares power, reports power_1"* is how the
  endpoint-suffix gap was found.
- **Silent** — the capability resolved but produced no offer. In practice a
  virtual device whose service is configured and has no data yet, which is a
  real condition rather than a bad guess, so it is kept and reported instead of
  dropped.

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
