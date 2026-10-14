# Teaching ZMM a new device

When a device joins that ZMM has never seen, it still works, but some of
what it can do may show up as plain numbers with codes for names, or not
show up at all. *Learn this device* fixes that: ZMM asks the device what it
has, you show ZMM what a few things mean, and the result is saved so every
device of that model works the same way from then on.

You do not need to know anything about Zigbee to do this.

---

## Before you start

- Pair the device and give it a minute to settle.
- Have the device in front of you. Some steps ask you to press its button,
  walk past it, or plug something into it.
- If it runs on batteries, it is asleep most of the time. Whenever a step
  says *wake it*, press its button once and start the step straight away.

## The simple way

Open the device, go to the **Identity** tab and press **Learn this device**.
The **Simple** view lists only the steps you need, in order. Work down it:

1. **Find everything.** Press the device's button if it runs on batteries,
   then **Start**. ZMM asks the device what it has, reads every value, and
   checks which ones can be changed. It takes up to a couple of minutes; a
   spinner shows while it works. Nothing about how the device behaves is
   changed. When it finishes it tells you how many values it found and how
   many can be changed.
2. **The steps for this kind of device.** These depend on what the device
   is, and each tells you exactly what to do. For example:
   - a presence or motion sensor: *Walk in*, then *Walk out*;
   - a button or remote: *Press the button* (once per kind of press);
   - a smart plug: *Load test* (plug in something you know the wattage of).

   Press **Start**, do what it says, and wait for the step to finish. ZMM
   shows what it found. Proposals it is sure of are already ticked; press
   **Accept selected**. If it found nothing, it says why: do it again with
   **Redo** (or **Run again**).
3. **Values I know** (optional). If the device's own app, screen or manual
   shows you some of its settings, type them in as `name: number` pairs,
   for example `absence delay: 30, sensitivity: 2`. ZMM finds where each is
   kept and offers it as a setting. Where two of the device's values match
   the same number, it lists both instead of guessing.
4. **Review & save.** ZMM shows what saving will change. Press **Save**. It
   applies to every device of this model on your system. **Roll back my
   profile**, in the same place, undoes the last save.

That is all most people need.

## What you get

- **Readings by name** in the device card and in Home Assistant (for
  example *Occupancy* instead of `opple 0x0142`).
- **Settings** in the device's **Overview → Configuration**, under **Device
  Settings**. Ones that have a name are under **Named**. Everything else the
  device lets you change is under **Unnamed**, labelled by its code until
  something names it: you can still change it there.
- The battery level, wherever the device keeps it.

Saving in Configuration only sends the settings you changed.

## If something goes wrong

| What you see | What to do |
|---|---|
| *the device did not answer: wake it* | Press the device's button and press **Start** again within a second or two. |
| *no two-state reading changed* (presence) | Make sure nobody was in view before you started, then try again. |
| *nothing switched* or *no button press arrived* | Use the device itself, not its app, while the step is running. |
| *more than one fit, so not proposed* | Two of the device's values hold the same number. Change the setting on the device if you can, then run *Values I know* again with the new number. |
| A reading stays at its old value | Make the device report again (trigger it once). Values a device has not sent since it last joined appear when it next sends them. |

## What ZMM cannot do

A device tells ZMM *that* a value exists, what kind of number it is, and
whether it can be changed. It never says what the value is *for*. Names come
from you: from a step you demonstrate, or from values you type in. A value no
one has named still works under **Unnamed**; it just keeps its code as a name.

---

## Advanced

Switch to **Advanced** at the top of the learning panel to see every step,
grouped under a tab per kind of step.

### Find everything, in parts

*Find everything* is two steps you can also run on their own:

- **Find its attributes** asks the device which of its maker's attributes it
  has (with their types and whether each is listed as writable) and reads
  them. Some devices report attributes they leave out of that list, often
  their real functions; those are read too and reported as *not listed*.
- **Check which can be set** settles, for each of those unlisted attributes,
  whether it can be written, the only way the device allows: it reads the
  value, writes the same value back, and notes whether the device accepts.
  Only number and on/off attributes are tried, never ones known to start an
  action. Silence records nothing, so a sleeping device cannot produce a
  wrong answer.

### Device settings steps

- **Look back over its history**: proposes attributes that changed every
  time a known value did (a target temperature, say). Nothing to do; it gets
  better the longer the device has been in use.
- **Change an on/off setting on the device** / **Set a number on the
  device**: change one setting on the device itself and ZMM finds which
  attribute moved, with its scale.
- **Let ZMM flip one (visible / behaviour)**: for settings you cannot change
  on the device, ZMM flips one candidate at a time while you watch, and puts
  it back when you answer or after a timeout.

### Editing the entry directly

**Review & save** (or **Edit its entry** outside a session) opens the entry
editor: per endpoint, its type, name, what its power reading measures and
whether it sends button presses; measurement scaling; its **attributes**;
and **blob tags**.

**Attributes** lists the device's own values (the `opple 0x…` and similar
rows in its state): each with its last value and whether it can be set. Type
a name to publish it under that name: as a setting if it can be set
(it then moves from *Unnamed* to *Named* in Configuration), otherwise as a
reading. **Divide by** turns the raw number into its unit (`1000` for a time
held in milliseconds). Only the values the device sends by itself are listed
at first; tick the box to see the ones it only lists, usually the maker's
internals. To find which is which, change one in Configuration → Unnamed and
watch what the device does, or use *Values I know*.

Blob tags are values some makers (Aqara) pack into one status report. The
table lists every tag the device has sent with its last value, so you can
match one against something you know (a battery at 3.2 V reads `3211` in
millivolts) and name it. Give it a scale to turn the raw number into its
unit (`0.001` for mV to V). An empty name drops a tag.

The JSON box below the form holds the whole entry, including settings the
form does not cover.

### Your edits and the shipped entry

If ZMM already ships an entry for the model, what you save is laid over it:
what you set wins, what you left alone keeps the shipped value. Deleting your
version (rolling back past your first save) returns to the shipped entry.

### Sharing

**Export** writes the entry with a summary of what settled each part, and no
addresses or file names. **Import** previews someone else's export for your
model and saves it if you choose. A well-tested export can become a shipped
entry in `zmm_quirks/`.

---

## How it works (for developers)

Code cites these sections as `learning-a-device.md §N`. Entries, overlays,
battery sources and the unnamed-attribute controls are in
[`device-profiles.md`](device-profiles.md).

Learning lets anyone teach ZMM a device end to end (what each endpoint is,
what it measures and how it is scaled, what its buttons and settings do) and
turn the result into their own entry for the model. It is guided, evidence
based, and driven by data: covering a new kind of device means writing a
recipe, not code.

### 1. The pieces

| Piece | Where | Role |
|---|---|---|
| Sessions | `modules/device_learning.py` | the steps a device gets, the raw frames it sends while learned, each step's proposals |
| Inference operations | `modules/learning_ops.py` | pure functions from what a step captured to proposals (§5) |
| Recipes and loader | `learning_recipes/*.json`, `modules/learning_recipes.py` | which steps a kind of device gets, as data (§3) |
| Wizard | `static/js/modal/learning.js` (Identity tab) | Simple and Advanced views, review, save, export, import |
| Entry editor | `static/js/modal/quirk_editor.js` | a form over the entry, previewed through the normaliser |
| Evidence store | `modules/device_facts.py`, `modules/device_decisions.py` | facts by source; decisions with reasons |
| Cached attributes | `modules/vendor_cache.py` | what zigpy already holds, surfaced as evidence and state |
| Discovery | `modules/probe_lite.py` | probe-lite at join (mains), `discover_vendor` on demand |

Frames are captured in `ZigbeeService.handle_message` (reports and read
responses, before any handler scales them). A step also reads the attributes
its operations need at its start and end, since a device may not report
within the window.

### 2. The flow

1. **Find everything** (`discover.find_all`): discovery of the maker's
   clusters, a read of every attribute (unlisted ones first, since nothing
   else reads them), then the write probe (§4) for the attributes the device
   reports but does not list. Recorded as `answered` facts.
2. **Experiments**, one at a time, chosen by the device's recipes. Each
   captures frames while the user acts (or a window passes), then runs its
   operations. The user sees what was inferred and why, and accepts, redoes
   or skips it.
3. **Review**: the entry saving would store, built from the current entry,
   gaps filled from the evidence draft (`modules/quirk_draft.py`), learned
   results on top; with a preview of what changes (EP kinds and names,
   readings, device type, settings, blob tags, power scaling).
4. **Save** as the user's profile for the model, laid over a shipped entry of
   the same id. Every save archives the previous version; rollback restores
   it, or with none returns to the shipped entry.
5. **Share** (optional): export, import (§8).

Examples for a double socket:

| Instruction | Inference | Settles |
|---|---|---|
| "Press socket 1's button once, then twice" | which EP sent the press; the value per press type | `actions`, press names |
| "Plug a kettle into socket 1, enter its rating" | which EPs' power moved; raw ÷ rating → a power of ten | `metering` per EP, `measurements.active_power` |
| "Repeat for socket 2 and USB" | as above, then the metering map across all tests | whole-device vs per-socket readings |
| "Change the LED setting on the device" | which writable maker's attribute changed, old → new | a named `setting` with its values |
| *(during the kettle step)* | blob tags whose value tracks a known reading | `struct_tags` |

### 3. Recipes

Shipped in `learning_recipes/`, plus the user's own in
`<data>/learning_recipes/` (the same `id` replaces the shipped one). A device
gets every recipe whose `applies_when` it matches.

```jsonc
{
  "id": "metered_outlet",                  // [a-z][a-z0-9_]*
  "title": "Power metering",               // the Advanced view's tab
  "applies_when": {                        // every condition given must hold
    "in_clusters_any": ["0x0B04"],         // an EP serves one of these
    "not_in_clusters_any": [],             // no EP serves any of these
    "kind": "switch",                      // an EP classified light or switch
    "device_types_any": [],                // an EP declares one of these types
    "vendor_cluster": false,               // an EP has a maker's cluster (0xFC00+)
    "battery_powered": false               // the node descriptor says battery
  },
  "steps": [{
    "id": "switch_test",
    "name": "Load test",                   // shown on the step's card
    "level": "simple",                     // simple (default) | advanced
    "for_each": "switch_endpoint",         // or light_endpoint, endpoint_with:0xXXXX
    "instruction": "Plug a known load into {label} ...",   // {label}: the EP's name
    "inputs": [{"name": "rating_w", "label": "Its rating (W)", "type": "number", "min": 1}],
    "mode": "observe",                     // §4
    "window_s": 90,                        // 5–900
    "watch": {"cluster": "0x0006", "attr": "0x0000"},      // optional, for which_endpoint_changed
    "expect_kind": "switch",               // which_endpoint_changed: what the EP drives
    "expect": null,                        // occupancy_attribute: occupied | clear
    "infer": [{"op": "power_follows_switch", "yields": "endpoints.*.metering"}]
  }]
}
```

A recipe only orders instructions and names operations; it cannot run code.
Loading rejects unknown operations, malformed fields and windows out of range,
and each proposal must fall inside its step's `yields`.

### 4. Step modes

| Mode | What happens |
|---|---|
| `observe` (default) | frames are captured while the user acts; the operations run on Done or when the window ends |
| `history` | concluded from the device's recorded history; nobody acts |
| `lookup` | concluded from evidence already held and the user's inputs |
| `try_write` | ZMM flips one writable toggle while the user watches, and puts it back on the answer, after a timeout, or when the session ends; each put-back is read to confirm it. Only `confirmed_write` may run here |
| `discover` | the device is asked for its maker's attributes (types, access) and they are read |
| `write_probe` | each reported-but-unlisted whole-number or on/off attribute is read, written back as read, and read again: accepted is recorded `RW`, refused `R`, silence nothing |
| `find_all` | `discover` then `write_probe`, led by a plain summary |
| `summary` | built by the session from other steps (the metering map) |

`discover`, `write_probe` and `find_all` run in the background, since the
device answers over many seconds: the step shows as running, and its result
appears when the device has answered. They have no operations.

`simple` steps make up the wizard's default view, in the order Find
everything, the device kind's demonstrations, Values I know. `advanced` steps
(the parts of Find everything, history, change, set or flip a setting) appear
in the Advanced view.

### 5. Inference operations

Pure functions over the frames a step captured (and its inputs), each
returning proposals `(path, value, confidence, evidence)`. A proposal with no
path is a note.

| Operation | From | Proposes |
|---|---|---|
| `which_endpoint_changed` | the On/Off (or `watch`) attribute that changed, per EP | the EP the action belongs to, as `expect_kind` |
| `press_signature` | the value each EP's Multistate Input sent for a demonstrated press | `actions: multistate`, a press name per value |
| `power_follows_switch` | power while the step's EP was switched on and off | notes: did its reading rise and fall, and did another EP show the load |
| `which_endpoints_moved` | per-EP power change during a load | one EP moved → `self`; all alike → one `device_total`, the rest `none` |
| `scale_from_known` | raw power ÷ the known rating | the power of ten within tolerance, refused if the result is impossible |
| `metering_map` (summary) | every load test: which EPs moved for which socket | per EP: `self`, `device_total`, `none` or `measures:<eps>` |
| `attribute_that_toggled` | writable maker's attributes, old → new | a setting with its two values named |
| `attribute_that_became` | writable maker's attributes against the number the user set | a numeric setting and its power-of-ten scale |
| `attribute_that_tracked` | recorded history: maker's attributes against known states | a numeric setting whose every change matched a known state; fewer than three changes, or a read-only one, is a note |
| `occupancy_attribute` | the two-state reading that flipped as the user walked in (`expect: occupied`) or out (`clear`) | that attribute as `occupancy`, with the value meaning occupied, and `device_type: motion_sensor`. One first seen in the step is offered at lower confidence; several flipping together are all offered, for the user to pick one |
| `attributes_holding_known` | values the user knows (`name: number` pairs or a JSON object) against each maker's attribute's last value | the attribute holding each, with its scale: a setting if it can be written, else a named reading. Only where one name fits one attribute, that attribute fits no other name, and the raw value is at least 100; the rest are listed with their candidates |
| `correlate_blob_tags` | blob tag values against known readings in the same step | `struct_tags` for tags that match |
| `confirmed_write` | a `try_write` the user said changed the device | the setting the user named, with its two values |

### 6. Evidence, review and save

An accepted proposal is recorded as a `learned` fact, ranked just below a
`user` correction: the user's own device, demonstrated, outranks a curated
entry for the model. Nothing applies to the device until the review is saved.

The editor (*Edit its entry*, or *Review & save*) works on the same entry
the JSON box shows. Its Attributes section lists the device's own attributes
outside the blobs (`device_learning.vendor_attributes`): naming one makes it
a setting when it can be written, else a reading. Its Blob tags section lists
every tag of the device's latest status blobs with its last value
(`device_learning.blob_tags`). Every edit is previewed through the normaliser
(`/profile/preview`), so what is shown is what saving stores.

### 7. Safety

- **Observe first.** Steps watch what the device does while the user
  operates it. Steps that write say so: `try_write` flips one toggle and puts
  it back; `write_probe` writes only the value it has just read.
- **Never an action.** `NEVER_WRITE` lists attributes that start something
  (an Aqara valve's calibration); no step writes them.
- **Nothing applies until Save.** Learned facts show as proposals.
- **Bounds hold.** A learned power scaling that gives more than the outlet
  can carry is refused, with the reason.
- **Scope.** Saving writes a profile for the exact model and maker; profile
  lookup never applies it to another maker's device.
- **Undo.** Every save keeps the previous version; rollback restores it and
  re-announces the device.
- **Sleepy devices.** Steps that ask the device say to wake it first; one
  that gets no answer says so and can be run again.

### 8. Sharing

Export (`zmm-entry/1`): the entry plus which experiments settled which
fields, and the firmware seen, with IEEE addresses and probe file names
stripped. Import: validated by the entry normaliser, previewed like a save,
stored as the user's profile. Sharing is a file; promotion to `zmm_quirks/`
is a reviewed pull request.

### 9. Not covered yet

- Devices that speak in commands or Tuya datapoints rather than attribute
  reports: learning captures reports and read responses only.
- Thermostats and covers: they need an operation that correlates a reading
  with a change the user makes.
- Experiments ZMM runs itself (change a report interval and time the
  reports) to name settings no one can change on the device.
- User-written recipes have no editor in the UI: they are files.
- Importing an export replaces the user's profile for the model (after a
  preview); two exports for the same model are not merged or compared.
- Firmware differences: entries record `verified_fw`, nothing compares it.
