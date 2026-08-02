# Frames

Dynamically-generated dashboards, laid out from device chamber + type
(`modules/frames.py`). A pure module: no I/O, no FastAPI, no global state,
wired in by `routes/frame_routes.py`.

## Terminology

- **Hive** — the install.
- **Frame** — a dashboard.
- **Chamber** — a room (see `modules/chambers.py`).
- **Cell** — one device tile. A honeycomb cell is the individual hexagon.

## Cell kind is precedence-ordered

A device carries many capabilities at once — a smart bulb reporting power and
battery is still a light — so a cell's identity is its most specific *control*
surface, resolved in this order:

```
climate > cover > light > switch > lock > sensor > unknown
```

`battery` / `power_monitoring` / `lqi` never decide cell kind; they render as
badges. `unknown` still renders (name + last seen) rather than being silently
dropped: a device you can't see is worse than a device you can't use.

`lock` is declared but nothing emits it yet — `DeviceCapabilities` has no lock
detection, since locks are Nuki, via `modules/nuki_controller.py`, outside the
Zigbee capability path. It stays in the list so the precedence order is the
whole story rather than a half-truth, and so it lands with the non-Zigbee
adapters.

## Why this module does not use `CAPABILITY_TO_HA`

There are two capability vocabularies in this codebase and they do not match.

| Source | Emits |
| --- | --- |
| `DeviceCapabilities.get_capabilities()` (`modules/device_capabilities.py`) | `contact_sensor`, `motion_sensor`, `level_control`, `temperature_sensor` |
| `CAPABILITY_TO_HA` (`modules/device_profiles.py`) | `contact`, `motion`, `brightness`, `temperature` |

Only `on_off`, `cover`, `thermostat` and `battery` coincide. `CAPABILITY_TO_HA`
belongs to the *profiles* layer and is not usable against `capability_list`.

## Two deliberate subtleties

1. **The switch cell matches `switch`, never `on_off`.** The quirk system adds
   `on_off` whenever the cluster exists but *discards* `switch` to mean "this is
   not a controllable actuator". A Philips SML motion sensor has the OnOff
   cluster on a controller endpoint, and `lumi.sensor_magnet` likewise. Matching
   `on_off` would render motion and contact sensors as toggles.

2. **Contact vs motion is disambiguated by state, not capability alone.**
   `device_capabilities` maps IAS_ZONE to `motion_sensor` for every model except
   `lumi.sensor_magnet`, so non-Aqara door sensors (Sonoff, Tuya) arrive typed
   as motion. A `contact` / `is_open` state key is stronger evidence and wins —
   the same reasoning as `overview.js:hasContactSensing()`.

## Chambers

`modules/chambers.py` is the chamber registry — the Frames-side notion of "a
room in the home". Pure module, wired in by `routes/chamber_routes.py`.

`chamber` is **Frames-side vocabulary only**. The heating subsystem and the
floor-plan editor both say `room` internally and deliberately keep doing so:
renaming them would mean migrating the config schema underneath
`heating_controller.py` / `thermal_profile.py` / the floor-plan projection for
no functional gain. Translation happens at this boundary and nowhere else.

### The registry is a union, not a migration

Rooms are already described in two places, and this module adopts both rather
than replacing either:

| Source | `source` |
| --- | --- |
| `chambers:` | `config` (this module) |
| `heating.circuits[].rooms[]` | `heating` (adopted) |
| `heating.floor_plan.levels[].rooms[]` | `floor_plan` (adopted) |

A chamber's `id` is the **same string** heating uses as its `room.id` (e.g.
`living`). That is what keeps "Living Room" a single chamber instead of two.

When a configured chamber shadows an adopted room id, the configured entry wins
and is flagged `adopted: True` — meaning heating also defines this room, so its
id must not be renamed or the two drift apart.

Nothing here ever writes to the heating config.

> `zones` / `config/zones.yaml` are RSSI presence-detection zones, **not** rooms.
> They are unrelated to chambers.

## Frontend

`static/js/frames.js` gets structure — which cell kind, which quick actions,
which readouts — from `/api/frames/auto`. Live **values** come from
`state.deviceCache`, which the existing websocket already keeps current, so a
state change re-renders one cell and never refetches the layout.

Zigbee-only for now; the backend excludes everything else.

### Quick actions

Device cells go through `window.sendCommand` (`actions.js`), which already does
the optimistic update. Group cells go through `frameGroupCommand`
(`POST /api/groups/{id}/control`) instead — a group has no single device to
send an optimistic update for, so its tile relies on member devices' own
websocket updates via `framesHandleDeviceUpdate`.

That group endpoint has its own unit conventions, which differ from the
single-device path:

| Field | Convention |
| --- | --- |
| `brightness` | The slider is 0–100 as for a device, but `control_group` calls the ZCL level-control cluster directly with no conversion, so it wants a raw 0–254 level. `handlers/general.py:set_brightness_pct` is what converts on the single-device path. |
| `position` | The UI convention is 100 = open. `handlers/blinds.py` inverts this for a single device just before the cluster call; `control_group` has no such inversion, so `frameGroupCommand` does it instead. |

## API

`routes/frame_routes.py`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/frames/auto` | grouped cells, laid out on the fly |
| `GET /api/frames/cells` | flat cell list, no grouping, for the picker |
| `GET /api/frames/kinds` | cell kinds + labels |
| `GET /api/frames` | saved frames |
| `POST /api/frames` | create or update a saved frame |
| `DELETE /api/frames/{frame_id}` | delete a saved frame |
| `GET /api/frames/{frame_id}` | render a saved frame |

Query params on `auto`: `split=chamber|type` to group by room or device type,
`chambers=a,b` to restrict to chambers, `kinds=light,switch` to restrict to cell
kinds.

A Zigbee group assigned a chamber becomes its own controllable cell in that
chamber's section on a chamber-split frame. No query param is needed — it is
driven by the group's own `chamber` field.

Saved frames live in `data/frames.json`. **A frame is only filters over the live
hive** — it never stores device state, so it cannot go stale.

Zigbee-only: AC units, media players and heating are not cells yet.

## Chamber API

`routes/chamber_routes.py`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/chambers` | registry (config + adopted rooms) |
| `POST /api/chambers` | create or update a chamber |
| `DELETE /api/chambers/{chamber_id}` | remove a Frames-owned chamber |
| `GET /api/chambers/assignments` | `{ieee: chamber_id}` |
| `POST /api/chambers/assign` | assign one device |
| `POST /api/chambers/assign/bulk` | assign many at once |

Chamber definitions live under `chambers:` in `config/config.yaml`; device
assignment lives in `device_settings[ieee]["chamber"]`
(`data/device_settings.json`).

### Why `device_settings` rather than a new file

It merges rather than clobbers on write (`existing.update(...)` in
`configure_device`), it is deleted with the device on removal, it is already in
the backup set, and it already ships to the frontend as `settings` on every
device from `get_device_list()`. So assignment needs no read API and no new
backup wiring.

Phase 1 is Zigbee-only: assignment validates against `zigbee_service.devices`,
and Matter/AC/media devices are not assignable yet by design.
