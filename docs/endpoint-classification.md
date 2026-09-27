# Endpoint classification: light or switch

`modules/endpoint_kind.py` decides whether an On/Off endpoint is a **light** or
a **switch**. It is the only place that decision is made. Its consumers:

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

1. No On/Off input cluster → not classified (controller, sensor, cover).
2. Override, strongest first: the user's correction (Identity tab), then a
   ZMM entry's or profile's `endpoints[ep].kind`. An entry's
   `zmm.corrections.device_type: ignore` drops the declared type from rule 4.
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
