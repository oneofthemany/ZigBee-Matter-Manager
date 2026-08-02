# Air conditioning

Local-LAN air-conditioner control (`modules/ac_controller.py`) for
Gree-protocol units (EcoAir and other Gree clones) and Midea-protocol units
(Comfee and other Midea clones). No Home Assistant bridge — both protocols are
spoken directly on the LAN using the same libraries the popular HA components
wrap.

| Brand | Library | Transport | Keying |
| --- | --- | --- | --- |
| gree | `greeclimate` | async UDP, port 7000 | per-device AES key derived once by `bind()` and persisted to config |
| midea | `midea-local` | TCP, port 6444 | V3 devices need a token/key pair fetched once from the Midea cloud; the library ships a preset anonymous account, so no personal credentials are required. Everything after that is local. |

Both libraries are optional dependencies: the module degrades to reporting
"library not installed" rather than breaking the app.

## Config

```yaml
ac:
  units:
    - id: ac_living          # stable id, generated on add
      name: Living Room AC
      brand: gree | midea
      host: 192.168.1.60
      port: 7000             # gree default 7000, midea default 6444
      mac: "ab12cd34ef56"    # gree only
      key: "..."             # gree bind key / midea key
      device_id: 12345       # midea only (appliance id)
      token: "..."           # midea only
      protocol: 3            # midea only (1/2/3)
      model: ""              # midea only, optional
      subtype: 0             # midea only, optional
      room_id: room_abc      # optional heating/floor-plan room binding
```

## Normalised state

Every adapter reports and accepts the same shape:

```
{ power: bool, mode: auto|cool|dry|fan|heat,
  target_c: float, current_c: float|None,
  fan: auto|low|medium|high|turbo,
  swing_v: bool, swing_h: bool,
  extras: {toggle_name: bool, ...} }        # only supported toggles
```

plus a `capabilities` block describing what the unit supports:

```
{ modes: [...], fan: [...], swing_v: bool, swing_h: bool,
  extras: [toggle names accepted by control()],
  min_c: float|None, max_c: float|None,
  source: b5|probed|assumed }
```

Midea capabilities come from the protocol's B5 capability frames, decoded by
`midea-local` on every refresh. Gree has no capability query, so support is
inferred from which props the unit echoes back in its status response
(`raw_properties`); an empty echo falls back to the standard Gree set.

Normalised toggle names accepted by `control()`, mapped per brand: `turbo`,
`quiet`, `xfan`, `light`, `sleep`, `anion`, `eco`, `display`, `indirect_wind`.
Vendor-specific keys (Gree `horizontal_swing` ints, Midea `swing_vertical`, …)
still pass straight through for callers that want raw control.

## Device list integration

`/api/devices` appends AC units so they appear in the main device list
alongside Zigbee and Matter devices. The pseudo-IEEE is the unit id and the
protocol is `wifi`; the frontend routes **Manage** to the AC modal via
`ac_unit_id`.

## Protocol quirks

- **Midea connect backoff.** Midea dongles refuse TCP for a while after rapid
  connect churn — verified on a Comfee 00000Q1D, where roughly three reconnects
  in a few seconds kills port 6444. A failed connect backs off rather than
  hammering the unit back into lockup.
- **Gree keyed bind.** `greeclimate` raises "cipher must be provided when key is
  provided" unless the cipher negotiated on first contact is passed back in
  (persisted as `cfg["cipher"]`), and it skips opening the UDP transport
  entirely — so the endpoint setup its negotiation path performs has to be
  replicated.
- **`check_protocol=True` is load-bearing** on the Midea refresh. Without it
  `midea-local` only *sends* the queries; responses are consumed by the
  library's background `run()` loop, which is not running here, so attributes
  would stay at their defaults forever.

## API

`routes/ac_routes.py` is a thin HTTP layer over `modules/ac_controller.py`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/ac/units` | configured units with live status |
| `POST /api/ac/discover` | LAN scan for both protocols |
| `POST /api/ac/units` | add or update a unit |
| `DELETE /api/ac/units/{unit_id}` | remove a unit |
| `GET /api/ac/units/{unit_id}/status` | live status |
| `POST /api/ac/units/{unit_id}/control` | `{power, mode, target_c, fan, ...}` |
| `POST /api/ac/units/{unit_id}/bind` | midea: fetch token/key (preset cloud account by default); gree: force a fresh key bind |
| `GET /api/ac/units/{unit_id}/timers` | list timers |
| `POST /api/ac/units/{unit_id}/timer` | `{in_minutes, changes}` |
| `DELETE /api/ac/timers/{timer_id}` | cancel a timer |

Config is read per request so Settings edits apply without a restart.

### Timers are app-side

Neither vendor protocol exposes its onboard timer usefully, so a timer is a
stored intention: apply `changes` after the delay. They persist in
`data/ac_timers.json` and are rescheduled on startup — the lifespan in `main.py`
calls `app.state.ac_timers_start` once the loop runs.

Timers missed by more than 15 minutes while the app was down are **dropped**.
Firing an hours-stale "turn on" after a long outage is worse than skipping it.
