# Endpoint classification: what an endpoint is for

`modules/endpoint_kind.py` decides what an endpoint is for. It is the only place
that decision is made, and it is made in two steps:

1. **Primary function** (`primary_function`). A server cluster that says what
   the endpoint does: Thermostat (0x0201) → thermostat, Window Covering
   (0x0102) → cover, Door Lock (0x0101) → lock. On/Off beside one of these is
   that function's own power control, not a second device.
2. **Light or switch** (`classify`), asked only of an On/Off endpoint with no
   primary function.

`endpoint_function` gives the combined answer for any endpoint; the device list
carries it per endpoint as `function`, beside `component_type` (the
light/switch half).

Nothing downstream may turn "neither" back into a switch. Three places once
did — the On/Off handler built a switch entity, kept writing `on`/`state`, and
the Control tab read `component_type || 'switch'` — which is how a SONOFF
TRVZB, declaring itself a thermostat by cluster and by device type, showed a
switch beside its thermostat controls. In the zhaquirks corpus the same shape
is 27 thermostat endpoints across 5 models and 9 cover endpoints.

Consumers:

| Consumer | Uses it for |
|---|---|
| `OnOffHandler.get_discovery_configs` | HA entity component (`light` / `switch`) |
| `OnOffHandler.get_component_type` | Control tab card type (`component_type`) |
| `OnOffHandler._is_light_endpoint` | retained state publish for lights |
| `DeviceCapabilities` | device-level `light` / `switch` capability |
| `GroupManager.get_device_type` | which group types a device may join |

A new rule goes in `classify()`, with a test in
`tests/devices/test_endpoint_kind.py`. Nothing else may test clusters to guess
a light.

## Rules, first match wins

An elimination: each rule fires only on positive evidence, and an EP that
offers none of it falls through to switch.

1. No On/Off input cluster → not classified (controller, sensor).
2. Override, strongest first: the user's correction (Identity tab), then a
   ZMM entry's or profile's `endpoints[ep].kind`. An entry's
   `zmm.corrections.device_type: ignore` drops the declared type from rule 4.
   An override is also how a thermostat whose On/Off is worth exposing (a
   wall thermostat's power button) gets its switch back.
2a. A primary function (above) → not classified as light or switch.
3. Colour Control (0x0300) → light. No outlet carries it.
4. Outlet device type → switch: HA Mains Power Outlet (0x0009), Smart Plug
   (0x0051), On/Off Plug-in Unit (0x010A), On/Off Output (0x0002); ZLL On/Off
   Plug-in Unit (0x0010). Before Touchlink, because Hue and Innr plugs carry
   0x1000.
5. Level Control (0x0008), not on a cover:
   - no load cluster → light;
   - with a load cluster, a dimmable/colour light type (HA 0x0101, 0x0102,
     0x010C, 0x010D; ZLL 0x0100, 0x0200, 0x0210, 0x0220) → light (metered wall
     dimmer: Sinopé DM2500ZB, Inovelli VZM31-SN);
   - with a load cluster and any other type → switch (the Level dims the
     socket's LED: Aurora, Innr SP 240).
6. A load cluster → switch: Electrical Measurement (0x0B04), Metering (0x0702),
   Multistate Input (0x0012), Sonoff (0xFC11).
7. Touchlink (0x1000) → light.
8. Otherwise → switch. On/Off with no light evidence drives a relay; HA's
   switch and on/off light behave the same, so this is the safe miss.

## Device class

`modules/device_class.py` answers the question one level up — what the whole
device is — once, for every consumer that used to guess it from a model name.
It is a key of `device_profiles.DEVICE_TYPES`, carried in the device list as
`device_class` (`type`, `label`, `source`, `reason`, `power`) and shown in the
Identity tab, where it can be confirmed or corrected (a `user` fact on
endpoint 0, subject `device_type`).

Order: the user's correction, then a ZMM entry's or profile's `device_type`,
then rules over the device's own evidence, first match wins:

1. A thermostat endpoint → radiator valve if the device takes a valve
   setting (`valve_detection`, `motor_calibration`, a valve opening or closing
   limit), else thermostat. A cover endpoint, or the cover
   capability its handler gives a datapoint device → blind. A lock endpoint →
   lock.
2. A light endpoint → colour light if that endpoint has Colour Control, else
   light. A switch endpoint → plug if the device meters a load, else switch.
3. IAS Zone `zone_type`, the device's own statement: 0x000D motion, 0x0015
   contact, 0x0028 smoke, 0x002A leak, 0x002D vibration. A zone that never
   gave its type is not guessed at.
4. A handler's contact capability; Occupancy Sensing or a handler's motion
   capability; Temperature; Humidity.
5. On/Off, Level or Colour as client clusters only, or Multistate Input →
   button.
6. Otherwise generic.

An actuator outranks a sensor on the same device, and a sensor outranks a
remote's outputs (a motion sensor with a scene-controller endpoint is a motion
sensor).

The thermostat cluster alone does not tell a radiator valve from a wall
thermostat or a boiler receiver: a receiver lists PIHeatingDemand and a valve
may not. What does is a setting only a valve has — detecting or calibrating
the pin it drives — known from a handler or from an entry's (or a learned)
setting of that name, via `device.supports_setting`. A valve whose settings
are still unnamed is a thermostat until one is learned or an entry says `trv`.

Related evidence that replaced a name test:

| Was | Now |
|---|---|
| battery-or-mains from "plug", "bulb", "switch" in the model | the node descriptor (`is_mains_powered`) |
| every IAS Zone device a motion sensor unless `lumi.sensor_magnet` | `zone_type` |
| heat-only from "TRV" / "AGL001" in the model | ControlSequenceOfOperation (0x001B) |
| boiler receiver vs valve from "SLR" / "SLT" / "TRV" | a thermostat endpoint, and mains or battery |
| which valve settings to offer, from the maker's name | `device.supports_setting(name)` |
| a Hue sensor's scene endpoint, from "SML" in the model | any sensor's endpoint that sends On/Off, Level or Colour and serves none |
| Hue and Lumi sensitivity and LED options, from the maker's name | the manufacturer code in the node descriptor |
| a contact sensor, from `lumi.sensor_magnet` in the model | the entry's `device_type` (or the user's): its clusters match a button's |
| battery % from voltage, from "SML" in the model | the entry's `zmm.battery: "voltage"` |

Still decided by name, for want of evidence: which datapoint map a Tuya
0xEF00 device gets when it has no standard cluster and no entry, and the Hive
receiver's write protocol and its heating endpoint.

## What is never evidence

- **Vendor clusters** (0xFCC0 Aqara, 0xEF00 Tuya). They name the maker, not the
  load. 0xFCC0 as "light" made every Aqara relay, socket and USB port a light
  unless another rule caught it.
- **A light device type on an EP without Level.** 100 Tuya relays declare On/Off
  Light (0x0100); the Aqara dual relay lumi.relay.c2acn01 declares Dimmable
  Light (0x0101). A light type only settles rule 5.
- **The quirk's cluster class** (e.g. `OppleClusterLight`, which the Clusters
  modal shows as the 0xFCC0 name). It is a real light signal, but only
  `lumi.light.acn003`/`acn014` use it and both carry Colour, so rule 3 already
  decides them. Its absence says nothing: most Aqara devices have no quirk.

The figures come from the zhaquirks registry; `tests/devices/test_quirk_corpus.py`
re-runs the classifier over every quirk endpoint on each test run.

## Profile override

In the device's profile (`docs/device-profiles.md`):

```json
"endpoints": { "3": { "kind": "switch" } }
```

It takes effect on the next announce. Re-announcing also retracts the entity
the endpoint had under the other kind, so HA keeps no ghost.

## Multi-gang buttons

Multistate Input writes `action_<ep>` per gang plus bare `action` ("last press
on any gang"). A device with Multistate on more than one EP publishes one HA
Action sensor per gang and retracts the old shared `action` entity.

## Tuya (0xEF00) devices

A quirked TS0601 exposes standard clusters (On/Off, Level, Window Covering), so
its On/Off EPs go through this classifier like any other device; the corpus
test includes them.

`TuyaDeviceTypeDetector` (`handlers/tuya.py`) answers a different question:
which datapoints to publish as sensors. It must not reuse this classifier:
Tuya power meters, valves, breakers and sirens carry On/Off, and typing them
"switch" would cut their datapoints to 1–4. For an un-quirked, EF00-only device
it types from the model string; pin those with a device profile.
