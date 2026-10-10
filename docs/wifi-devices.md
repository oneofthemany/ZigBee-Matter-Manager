# Wi-Fi devices: Shelly and ESPHome

Shelly and ESPHome devices on their **local** APIs — no vendor cloud. Add them
under **Settings → APIs → Shelly / ESPHome** (admin); they then appear in the
device list and behave like any Zigbee or Matter device: rule triggers,
conditions and commands, notification rules, alarm zones, Frames, the swarm.

| Path | Role |
|---|---|
| `modules/lan_devices.py` | the shared device object, registry, credentials, publishing, mDNS discovery |
| `modules/shelly.py` | Shelly Gen1 and Gen2+ |
| `modules/esphome.py` | ESPHome native API |
| `routes/lan_device_routes.py` | setup API |
| `static/js/lan-devices-settings.js`, `static/js/modal/lan-device-modal.js` | setup, the device modal |
| `data/shelly_devices.json`, `data/esphome_devices.json` | the devices — no credentials |
| `config/secrets.yaml` → `shelly`, `esphome` | passwords and API keys (0600) |

## How they fit in

Each device is an engine device, `shelly::<id>` or `esphome::<id>`, and reports
the same keys as everything else:

| | Keys |
|---|---|
| switch / relay / light | `state` (ON/OFF), `on`, `brightness` (0-254), `level` (%), `color_temp` (mireds) |
| cover | `position` (%), `cover_state` |
| metering | `power` (W), `energy` (kWh), `voltage`, `current` |
| sensors | `temperature`, `humidity`, `illuminance`, `battery`, `co2`, `pm25` |
| binary sensors | `occupancy`, `contact` (True = closed, as ZCL), `water_leak`, `smoke`, `vibration`, `tamper` |
| buttons | `action` (`single`, `double`, `triple`, `hold`) with `action_endpoint` — an event, not a state |

A **multi-channel** device (Shelly 2PM, Pro 4PM, an ESPHome board with two
relays) suffixes each channel's keys — `state_1`, `power_2` — and its commands
carry `endpoint_id`, as multi-gang Zigbee devices do; the swarm reads the suffix
as an endpoint, so a dual-gang device stays two outlets. Commands are the usual
`on`, `off`, `toggle`, `brightness` (0-100), `color_temp` (Kelvin), `open`,
`close`, `stop`, `position`.

Only what changed reaches the rule engine, so a poll that finds nothing new
triggers nothing; a button press is delivered every time, even the same press
twice. A device that drops off becomes unavailable (`available: false`), which
notification rules' *went offline* picks up.

Device type in the list is Switch, Light, Cover or Sensor — never *Router*,
which the UI offers as a Zigbee pairing parent.

## Shelly

Hand-written client (`aioshelly` would bring a Bluetooth stack ZMM doesn't use).

- **Gen2+** (Plus, Pro, Gen3, Gen4) — JSON-RPC. ZMM holds the device's `/rpc`
  websocket open and gets changes as they happen (`NotifyStatus`) plus button
  presses (`NotifyEvent`); a full status every five minutes catches anything
  missed, and if the websocket can't be held it polls every 30 s. Channel
  names set on the device become the control labels.
- **Gen1** — REST, polled every 5 s. A Shelly 2.5 in roller mode is a cover.
  Button presses aren't seen on Gen1 (they need CoIoT); its inputs' on/off is.
- **Passwords** — Gen2+ uses HTTP digest (SHA-256), and the websocket the RPC
  `auth` object, both for user `admin`; Gen1 uses basic auth. A wrong or missing
  password is caught when adding, not later as "offline".
- **Not supported:** battery Shellys (H&T, Door/Window, Plus H&T) — they sleep
  and only wake to push to a configured server.

Discovery lists anything announcing `_shelly._tcp`, or `_http._tcp` with a
Shelly name (Gen1, if mDNS is on in its settings).

## ESPHome

The native API through `aioesphomeapi` (pinned `29.0.0`, the newest that fits the
Matter server's protobuf/zeroconf/cryptography pins) — the same encrypted,
push-based connection Home Assistant uses.

- **Encryption key**: the `api: encryption: key:` from the device's YAML.
  Checked for shape before connecting, and a key the device rejects is reported
  as such. Old firmware with an API *password* instead works too.
- **Entities**: switches, lights and covers become channels (in that order);
  sensors and binary sensors map by `device_class` onto the keys above, the
  first of each kind taking the common key (`temperature`) and every one also
  kept under its own `object_id`; buttons are offered as *Press*. Config and
  diagnostic entities (restart switches) aren't channels.
- **Connection**: pushed; reconnects with backoff (5 s → 5 min) and is offline
  meanwhile.

Discovery lists `_esphomelib._tcp`, marking devices that need a key.

## Setup API (admin)

`/api/shelly` and `/api/esphome`, the same shape:

| | |
|---|---|
| `GET` | devices, with online and last error; never credentials |
| `POST` | add `{host, port?, name?, password? / encryption_key?}` — the device is contacted first |
| `PUT /{id}` | edit; a blank secret keeps the stored one, `clear_credentials` removes them |
| `DELETE /{id}` | remove, with its credentials |
| `POST /discover` | mDNS on the hub's network (3 s) |

Using a device is the ordinary `POST /api/device/command` (`device:write`) and
`/api/devices` (`device:read`).

Discovery is multicast: it finds devices on the hub's own network, and should
be tried from the hub, not a laptop on Wi-Fi.

## Not yet

Tasmota, BTHome / Xiaomi BLE (2.2's next steps), RGB colour control, Shelly
Gen1 button events, battery Shellys, MQTT/Home Assistant republishing of these
devices, and their history in the telemetry database.
