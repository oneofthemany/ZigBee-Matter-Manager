# Device Profiles

Unified Zigbee + Matter device modelling framework (`modules/device_profiles.py`).

Replaces — and stays backwards-compatible with — the older split system of
`modules/device_overrides.py` (Zigbee attribute renaming) and
`modules/matter_definitions.py` (Matter endpoint mapping).

A *profile* is a JSON document describing a device model in protocol-agnostic
terms. The same schema covers Zigbee and Matter; only the `protocol` field and
the contents of `endpoints[*].clusters` change.

## Schema (canonical, v1)

```jsonc
{
  "schema_version": 1,
  "id": "lumi.sensor_magnet.aq2",          // stable identifier
  "protocol": "zigbee",                     // "zigbee" | "matter"
  "match": {                                // how a device gets matched
    "model":        "lumi.sensor_magnet.aq2",
    "manufacturer": "LUMI",
    "vendor_id":    null,                   // matter only
    "product_id":   null                    // matter only
  },
  "device_type": "contact_sensor",          // see DEVICE_TYPES
  "capabilities": ["contact", "battery"],
  "endpoints": {
    "1": {
      "role": "primary",                    // primary | controller | sensor | ...
      "kind":  null,                        // On/Off EPs: "light" | "switch" pins classification (endpoint-classification.md)
      "label": "Sensor",
      "group": "",                          // used for button grouping
      "clusters": {
        "0x0500": {
          "attributes": {
            "0x0000": {
              "name":         "contact",
              "scale":        1,
              "unit":         "",
              "device_class": "door",
              "invert":       true,
              "value_map":    {"0": "closed", "1": "open"}
            }
          },
          "commands": {}
        }
      }
    }
  },
  "actions": [
    {
      "id":       "toggle",
      "label":    "Toggle",
      "ep":       1,
      "cluster":  "0x0006",
      "command":  "0x02",
      "args":     [],
      "writes":   []   // alternative to command: [{ep, cluster, attr, value, type}]
    }
  ],
  "reporting": [
    { "ep": 1, "cluster": "0x0402", "attr": "0x0000",
      "min": 60, "max": 300, "delta": 10 }
  ],
  "ieee_overrides": {                       // legacy per-device mappings
    "00:11:22:...": {
      "cluster_mappings": {
        "cluster_0500_attr_0000": {"name": "contact"}
      }
    }
  },
  "meta": {
    "author":     "user@local",
    "source":     "user",                   // user | bundled | imported
    "created_at": 1700000000,
    "updated_at": 1700000000
  }
}
```

A second file, `ieee_overrides.json`, holds per-IEEE pinning to a profile
(`{ieee: profile_id}`) plus device-specific attribute mappings that have not
been promoted to a profile yet.

## Storage

One JSON file per profile under `data/device_profiles/`, keyed by the `id`
field. Bundled profiles shipped with the app live under
`data/community_profiles/` and are read-only. User profiles override bundled
ones with the same id.

## ZMM entries

A profile carrying the `zmm` block is a ZMM quirk entry (what it may hold
and how one is written: §4 below). Shipped entries live in `zmm_quirks/` beside
the code and load as the `zmm` tier: they override community profiles, and a
user's own profile overrides them. Anything saved through the API is a user
profile, whatever its `meta.source` says. A `zmm` entry that names its
manufacturer never matches through the model-only fallback.

Additions to the schema:

```jsonc
"endpoints": {
  "3": { "kind": "switch",          // light | switch
         "label": "USB",            // names the EP's HA entities
         "metering": "none",        // self | device_total | none
         "actions": "multistate",   // the EP's Multistate Input carries presses
         "role": "controller" }     // outputs are a remote's, not a load
},
"zmm": {
  "corrections":  { "device_type": "ignore" },   // the model misdeclares it
  "measurements": { "active_power": {"cluster": "0x0B04", "attr": "0x050B", "divisor": 10},
                    "rms_voltage": null },       // null: absent, never configure
  "settings":     [ { "id": "button_leds", "label": "Button LEDs", "type": "bool",
                      "ep": 1 /* or "each" */, "cluster": "0xFCC0", "attr": "0x0203",
                      "mfr": "0x115F", "values": {"0": "Off", "1": "On"} } ],
  "struct_tags":  { "0x97": {"name": "voltage", "scale": 1},   // Aqara 0xF7/0xDF blob tags
                    "0x9A": null },                            // null: not what the map says
  "battery":      "voltage",       // the reported % is useless: derive it from volts
  "evidence":     { "probes": ["<probe file>"], "verified_fw": ["<sw_version>"] }
}
```

## Lookup precedence

Highest first:

1. IEEE-pinned profile id (explicit user assignment)
2. User profile matching (protocol, model, manufacturer)
3. User profile matching (protocol, vendor_id, product_id) — Matter
4. Bundled profile matching (same priorities)
5. None — the device runs on built-in handlers / generic fallback

A user profile with the same `id` as a shipped one (ZMM entry or community
profile) is an edit of it, not a replacement: it is laid over the shipped one
(`overlay_profile`). What the user set wins; what they left unset (an empty
label, the default role, `generic` type) keeps the entry's value; settings and
actions merge by id; capabilities are the union. A tool that saves only part of
an entry therefore cannot drop the rest. A user profile under a new id still
stands alone.

## Application order

Profiles are applied after handler configure, so per-handler reporting wins on
conflicts; before the MQTT announce, so the friendly capability set reaches
discovery; and before `poll()`, so friendly keys are present in the first state
snapshot. Application is idempotent and a no-op when no profile matches.

## `state_mappings`

Friendly names for arbitrary device state keys (Tuya datapoints, derived keys),
keyed by the literal state key. These mirror the per-device `state:<key>` learn
mappings, so a mapping learned on one device can be promoted to the model level
and shared across every device of that model.

## Applying a profile

`modules/device_profiles_apply.py` is intentionally **read-only on the profile**
— it never mutates it. Four things happen on apply:

1. **Capabilities** — the device's capability set is augmented with whatever the
   profile declares, so the Control tab, automation triggers and MQTT discovery
   all see the device as the right type.
2. **Actions** — actions defined in the profile are registered under
   `device.profile_actions`. The Control tab reads that list and presents
   buttons; the action runner executes them.
3. **Reporting** — the profile's reporting rows are pushed to the device
   immediately on apply, and re-pushed on every interview-complete. The apply
   runner is idempotent, so battery devices that miss the first attempt
   eventually pick it up.
4. **State transforms** — for every raw `cluster_XXXX_attr_XXXX` key in device
   state that has a mapping in the profile or the IEEE overrides, a friendly key
   is added to the state dict.

## Battery

No entry is needed to say where a device's battery level comes from
(`modules/battery.py`). Each source offers what it reads, and the strongest
to have given a real value owns `battery` and `battery_voltage`:

1. the level Power Configuration reports;
2. the level in an Aqara status blob (a tag an entry names `battery`, or tag
   `0x18` beside a plausible `0x17` on a battery-powered device);
3. a level derived from Power Configuration's voltage;
4. a level derived from the blob's voltage (an entry's `battery_voltage`, or
   tag `0x01` / `0x17` in a cell's range on a battery-powered device).

0 V is never a reading, and 0 % from a cluster whose voltage reads 0 V is
dropped with it. A level is derived from a voltage only up to 3.6 V, where the
coin-cell curve applies. `zmm.battery: "voltage"` remains for a device whose
own percentage is flat until it dies.

## Every writable attribute

A device is configurable the day it joins, before anything names its
attributes (`zmm_settings.raw_options`). Every maker's attribute the device
has shown accepts writes (listed writable by discovery, or accepted by the
learning write probe), of a whole-number or on/off type, and not offered by an
entry setting or a handler option, is a control under its id
(`raw_<ep>_<cluster>_<attr>`): a number bounded by its type, or Off/On. The
settings form shows them under their own *Unnamed* tab and sends only the
controls a person changed. Naming one in the entry turns it into an ordinary
setting, and it leaves the Unnamed tab.

---

# Device recognition

How ZMM decides what a device is and what it can do: from evidence it has
gathered and weighed itself, with per-model knowledge (ZMM entries) authored
from its own probes. zigpy stays the Zigbee stack and zhaquirks stay applied;
both are sources of evidence, not the authority on what a device is. Code
cites these sections as `device-profiles.md §N`.

## 1. Principles

1. **Evidence, not identity.** A model match adds knowledge; it never replaces
   what the device itself showed. A device with no entry still gets sound
   decisions from its own evidence.
2. **Every fact carries its source**, ranked (§2). A weak source never
   overrides a strong one.
3. **Function from function-specific evidence**: metering, colour, dimming,
   reports seen. Never a vendor cluster, never a self-declared device type on
   its own.
4. **Ask the device.** A capability that depends on an attribute exists only
   if the attribute answered. UNSUPPORTED is a fact, not a default.
5. **Watch the device.** Reports seen after join confirm or correct
   structure: which EP carries power, which gang sends presses.
6. **Decide once, store it, everyone reads it**: discovery, the Control tab,
   capabilities, groups, automations.
7. **Decisions are visible and correctable.** Each has a reason. A correction
   is stored as a fact and can become an entry.
8. **Change is a diff.** Re-deriving never silently replaces: old → new is
   recorded and logged.
9. **Facts per endpoint and per capability**, so knowledge composes: a
   firmware variant with one extra EP keeps everything it shares with an entry.

## 2. Evidence

A fact is `(ieee, endpoint, subject, source, value)` with when it was first
and last seen, in `device_facts` (`modules/device_facts.py`), stored in the
zigbee cache DB and written through its write queue, never from the event
loop, and only when the value changed. Subjects name what is known:
`cluster_in:0x0B04`, `attr:0xFCC0/0x0142@0x115F` (an attribute that answered,
with its type, access and value), `attr_unsupported:...`,
`reports:0xFCC0/0x0142@0x115F` (an attribute seen in a report, with its last
value and whether it was ever non-zero), `kind`, `label`, `metering`. The
`@0x115F` suffix marks an attribute read with a manufacturer code, since the
same id can mean different things with and without one.

Sources, strongest first:

| Source | Learned by | Example |
|---|---|---|
| `user` | confirmed or corrected in the Identity panel | "EP3 is the USB port" |
| `learned` | a result demonstrated on this device (learning-a-device.md §6) | presence is `0xFCC0/0x0142` |
| `zmm` | a ZMM entry for the model | aeu002 EP3 is a switch |
| `observed` | a report seen from the device | EP3 reported active power 8.2 W |
| `answered` | a discovery or read the device answered | `0xFCC0/0x0201` is readable |
| `declared_quirk` | the zhaquirks replacement applied | `OppleClusterLight` on EP1 |
| `declared_device` | the device's own descriptors | device type `0x0000` |

Rank is per subject where it matters: a declared *cluster* is solid evidence
the cluster exists; a declared *device type* is the weakest fact held.

Where facts come from:

- **Declared**: at join, and again at every start (`record_declared`), so
  quirk or firmware changes are picked up.
- **Answered**: probe-lite (`modules/probe_lite.py`) runs discovery only at
  every join and once for devices never probed: mains devices, one at a time,
  a start-up backfill that waits two minutes and spaces devices five seconds
  apart. Battery devices are asked on demand by the learning step *Find
  everything*, which also reads attributes a device reports but does not
  list. Any cluster discovery a person runs also records answered facts, and
  a one-off backfill turned older discoveries into facts.
- **Observed**: `modules/device_observer.py` is fed every received frame and
  records an attribute's first report on an EP each run, and its first
  non-zero value. At start-up, `modules/vendor_cache.py` records every
  maker's attribute zigpy already holds.
- **Learned** and **user**: the learning wizard and the Identity panel.

*Vendor-scoped* means a manufacturer code **or** a cluster id of 0xFC00 and
up (`device_facts.vendor_scoped`): some makers' clusters answer only without
a code.

## 3. Rules and decisions

A rule reads facts and gives `(subject, value, reason)`. Every decision is
recorded in `device_decisions` (`modules/device_decisions.py`) with its rule,
reason and previous value, written only when the conclusion changes. The
order is always: the user's correction, then a ZMM entry or profile, then the
rules. Consumers read the live classification; the table is its record.
Decisions are re-derived on announce, after a correction, when an entry
changes and at start-up, never on a timer.

| Decides | From | Where |
|---|---|---|
| primary function | what the EP's clusters do (thermostat, cover, lock come before light or switch) | `modules/endpoint_kind.py` |
| light or switch, per EP | load clusters, a dimmable type with Level (endpoint-classification.md) | `modules/endpoint_kind.py` |
| device class | one decision for the whole device: functions, IAS zone type, sensing clusters, handler capabilities | `modules/device_class.py` |
| metering scope, per EP | which EPs *report* power under load, not which declare 0x0B04 (§7) | `handlers/power.py` |
| scaling | the device's answered scaling attributes; never a silent 1 (§7) | `handlers/power.py` |
| actions | Multistate Input declared and presses seen | entry or learning |
| settings | maker's attributes that answered or were reported, named by an entry (§4) | `modules/zmm_settings.py` |
| power source | the node descriptor | `device_class.is_mains_powered` |

Nothing downstream turns "neither light nor switch" into a switch.

## 4. ZMM entries

An entry is a profile with the `zmm` block (format in *ZMM entries* above).
It holds facts only: behaviour (Aqara `0xF7` structs, Tuya datapoints) stays
in handlers and is referred to by name. Nothing is copied from zhaquirks or
Zigbee2MQTT; every value is traced to one of ZMM's own probes or a person's
confirmation (`zmm.evidence`). A field in an entry is a `zmm` fact: it
outranks the device's own declarations but loses to a person's correction on
that device.

- **Measurements.** An entry's scaling outranks the device's own; `null`
  marks a measurement the device lacks, so it is never configured.
- **Blob tags.** `zmm.struct_tags` relabels or drops the Aqara `0xF7`/`0xDF`
  tags for the model: the tag layout differs per model, so the global map is
  only a fallback. A standard cluster that measures the same thing (energy on
  0x0702; power, voltage, current on 0x0B04 where the EP has them) always
  outranks the blob, and on a battery device the map's mains and relay tags
  are never used.
- **Settings.** An entry's settings are offered on the EPs where the device
  listed the attribute in discovery or reported it, written raw with the
  entry's manufacturer code. A setting may carry `scale`, `min`, `max`,
  `step` and `unit`, and its id is also a command (`zmm_settings.apply_command`):
  a caller can set `child_lock` on any device whose entry names one, and
  `device.supports_setting(name)` says whether it can. A report of the
  attribute is filed under the setting's id, and a write moves the raw value
  with it, so only a later report of that attribute replaces what was written.
- **Zones.** A setting of type `zones` with `bits: N` (1 to 48) is an octet
  string whose last N bits are on/off zones: zone *i* is bit *i* of the
  trailing bytes read big-endian. It is shown and saved as that mask; a write
  keeps the bytes before the zones, and any spare bits, as the device last gave
  them, so it needs the current value from a read or a report first. Which
  zone is which place is the device's: find it by switching one off and
  walking through it. 48 is the most a browser number carries exactly.
- **Readings.** An attribute mapping names a raw value; the name follows the
  raw value on every report, and a binary device class yields true or false.

## 5. Drafting an entry

`modules/quirk_draft.py` drafts an entry from a device's evidence (*Draft
ZMM entry* in the Identity tab): its EPs with their decided kinds, names and
metering, Multistate actions, answered measurements and scaling, and the
probes behind it. Only what the evidence supports goes in; anything it
cannot settle is left out. A device with no On/Off EP is drafted as
`generic`, which pins nothing. Writable maker's attributes are returned
beside the draft as candidates. A person settles the rest by testing the
device (learning-a-device.md) and saves it as their profile; a proven one is
exported and added to `zmm_quirks/`.

## 6. The identity panel

The device modal's *Identity* tab (`static/js/modal/identity.js`,
`GET/POST /api/device/{ieee}/identity`): per EP, each decision with its value,
source and reason, and *confirm*, *correct* and *reset*. A correction writes a
`user` fact and re-derives; when a re-derive changes a decision the panel
shows old → new. EP names set here label the EP's Home Assistant entities. HA
entity changes go through the retract-then-publish path. The learning wizard
and the entry editor live in the same tab.

## 7. Scaling and metering scope

Scaling (`handlers/power.py`): the entry's, else the device's own (a fresh
read, else zigpy's cached value, so a restart keeps it), else the handler's
default, each recorded as a decision. The rules cannot tell a per-socket meter
from a whole-device one, so the metering rule records `unknown` with the EPs
that showed power; the scope (`self`, `device_total`, `none`, or
`measures:<eps>` for a reading that covers other EPs) comes from the user
after a load test, or from an entry, and then decides which EPs publish power
and what the device total is.

## 8. Model-name quirks give way to entries

Older code recognised devices by model string. Each such rule now runs only
where no entry says otherwise: the Philips SML controller quirk only where no
entry marks a controller EP (`role: controller`); the Tuya presence guess and
`TuyaDeviceTypeDetector`'s model strings only where no entry describes the
device. An entry that names its maker never matches another maker's device,
even through the store's model-only fallback.

## 9. Limits and risks

- **Firmware differences** are not handled: entries record `verified_fw`,
  nothing compares it.
- **Sleepy devices** cannot be interviewed at length: probe-lite skips them,
  and they rely on observed traffic, entries, and *Find everything* when a
  person wakes them.
- **Network load**: probe-lite is discovery only, one device at a time.
- **DB writes** go through the zigbee cache's write queue, batched per device
  and only on change; the DB open itself stays off the event loop.

