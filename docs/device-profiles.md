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

## Lookup precedence

Highest first:

1. IEEE-pinned profile id (explicit user assignment)
2. User profile matching (protocol, model, manufacturer)
3. User profile matching (protocol, vendor_id, product_id) — Matter
4. Bundled profile matching (same priorities)
5. None — the device runs on built-in handlers / generic fallback

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
