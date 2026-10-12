# Heating controller

The active control layer: it watches room temperatures, decides which rooms are
calling for heat, turns the boiler or zone valve on and off through the
receiver, and coordinates TRV setpoints so a hot room can't steal heat from a
cold one on the same circuit. `modules/heating_controller.py`, with its API in
`routes/heating_controller_routes.py`.

The physics it acts on — heat loss, thermal profile, radiator sizing — and the
read-only Advisor beside it are in **[docs/heating.md](heating.md)**. The plan
its rooms are drawn on is [docs/floor-plan.md](floor-plan.md).

## Why it is a separate module

The Advisor is **read-only and cannot cause harm**. It runs analysis, surfaces recommendations, and nothing it does changes the state of a radiator valve or a boiler. You can run the Advisor with the Controller disabled and still get the dashboard, EPC, tips and preheat advice.

The Controller **actively commands hardware**, so it's gated behind a separate `heating.controller.enabled` flag and has a `dry_run` mode. In dry-run the full tick cycle runs, decisions are logged, and the UI shows what *would* happen, but no commands are sent. This is the recommended way to validate a new circuit/room config before letting it touch real TRVs.

**Disabled means stopped.** The tick refuses to run while `enabled` is false, and a config save starts or stops the loop, so enabling or disabling takes effect without a restart. Leaving live control — disabling, or turning dry run on — first sends `off` to any receiver the controller left calling: a Hive receiver holds a call indefinitely (hold duration `0xFFFF`), so walking away from one would leave the boiler on. The receiver is otherwise left as it is: dry run does **not** hand control back to the receiver's own schedule.

Crossing the dry-run boundary in either direction clears the command cache. Dry run records the commands it *would* have sent, and without the reset the first live tick would believe they were already in force.

`dry_run` is persisted by `POST /api/heating/controller/dry-run`, so a restart keeps it.

Both modules share the same underlying thermal profile and telemetry, so the numbers in the Advisor's preheat estimate are the same ones the Controller will see once you turn it on.

## Data model

```
Circuit (receiver + zone valve → calls the boiler)
  └── Room (target temp, schedule, optional external sensor)
        └── TRV(s) (regulate flow into that room's radiators)
```

A circuit represents one call-for-heat signal to the boiler. Most UK homes have two (upstairs/downstairs) or one open zone. Rooms on the same circuit share the boiler call — the controller decides per-TRV how much of that heat each room gets.

## Tick cycle (every 60 s)

1. **Snapshot** every device's current state.
2. **Pick a room temperature source**, in priority order:
    - External sensor (`temperature_sensor_ieee`), if present and reading
    - Mean of the room's TRV `local_temperature`s
3. **Classify each room** against its effective target (§ Targets):
    - `cold`       while the room is calling (§ Hysteresis)
    - `hot`        if `current > target + 0.3 °C`
    - `ontarget`   otherwise
    - `unknown`    no trustworthy temperature — the room never calls
4. **Circuit call-for-heat**: if any room in the circuit is `cold`, the circuit calls; all-`hot`-or-`ontarget` stands down.
5. **Receiver action**: send `system_mode=heat`/`off` (or relay on/off) to the receiver, only if different from the last command sent. In thermostat mode it also pushes a high setpoint (30 °C default) when calling and a low one (7 °C) when idle, so the receiver's internal comparator fires the boiler reliably.
6. **TRV setpoints**:
    - Room `cold` with an `advisory` external sensor → setpoint = target + `ADVISORY_OPEN_BOOST_C` (§ External temperature modes)
    - Room `cold` otherwise, or `ontarget` → setpoint = target
    - Room `hot` or window open → setpoint = `max(min_setpoint, min(room_temp − 1, target − 1))` — forces the valve shut even when the circuit is about to fire for a colder room
7. **Cooldowns and deltas** — skip the command if the TRV setpoint is already within 0.5 °C of the intended value, or if the same command was sent less than 5 min ago. Saves TRV battery airtime.

## Hysteresis

```python
COLD_BAND = 0.5   # a call starts when temp < target − 0.5
HOT_BAND  = 0.3   # room is HOT  if temp > target + 0.3
```

A call is **latched**: it starts below `target − COLD_BAND` and holds until the room reaches `target` (less any learned early stop, below). Inside the band a room keeps doing what it was doing — a room warming up keeps calling, a room cooling down doesn't restart until it drops below the band. A single threshold would stop the boiler at `target − 0.5` and leave every room hovering half a degree short.

**Learned early stop.** After a call ends, the controller watches for the peak (until the temperature drops `OVERSHOOT_PEAK_DROP_C` or `OVERSHOOT_PEAK_TIMEOUT_SEC` passes) and records `peak − target` as that cycle's overshoot. The per-room offset is an EWMA (`OVERSHOOT_LEARN_ALPHA`) starting from zero, so no single cycle sets it; a rise larger than `2 × OVERSHOOT_MAX_OFFSET_C` is a shower or the sun, not radiator carry-over, and is discarded. The latch then releases at `target − offset`, capped at `MAX_EARLY_STOP_C` so it always releases above the start threshold. Offsets persist in `data/heating_overshoot.json`.

## External temperature modes

Many wall-mounted thermostats read hot-pipe temperature, not air temperature, and report 2–4 °C higher than the room actually is. Three modes handle this:

| Mode       | Controller classifies using | TRV regulates using        | Use when                                                                |
|:-----------|:----------------------------|:---------------------------|:------------------------------------------------------------------------|
| `off`      | TRV local temp              | TRV local temp             | No external sensor configured                                           |
| `advisory` | External sensor             | TRV local temp, opened by a setpoint boost while the room is cold | Safe default when an external sensor exists — fixes controller-side bias |
| `push`     | External sensor             | External value written to TRV (Aqara 0xFCC0 attr 0x0280) | Aqara TRVs with external sensor mode enabled        |

In `advisory` mode the TRV still judges by its own reading beside the radiator, which runs warmer than the room. Given just the target, it closes before the room gets there — and the boiler fires into shut valves. While the external sensor says the room is cold, its TRVs get `target + ADVISORY_OPEN_BOOST_C` (capped at `TRV_MAX_SETPOINT`); once the room is on target they drop back to the target itself.

## Targets

`_effective_target`, most specific first:

1. An active schedule slot → its temperature.
2. A room that has a schedule but no slot active → `night_setback` (floored at `min_temp`). The schedule says when the room is wanted warm.
3. Operating hours on: in hours → `target_temp`; out of hours by `out_of_hours_action` — `setback` (target + offset, floored at `min_temp`), `min_only` (`min_temp`), `off` (`OUT_OF_HOURS_OFF_C`, the lowest TRV setpoint: neither calling nor holding valves open).
4. Otherwise the fixed 22:00–06:00 `night_setback`.

Preheat may raise the target ahead of an upcoming slot; it never lowers the one in force.

## Solar suppression

A cold room's call is held off while the sun is expected to carry it to target:
solar gain through its windows must cover `suppress_fraction` of the room's
loss to outdoors at target, `W/K × (target − outdoor)`. With no outdoor reading
it never fires.

The estimate is on probation. Unless the room has warmed `SOLAR_PROOF_RISE_C`
within `SOLAR_PROOF_SEC` of the suppression starting, it is dropped until that
call for heat is satisfied — the room not warming is better evidence than the
model.

## Force-close logic

A common multi-room problem: circuit A has a cold room and a hot room. When the boiler fires, hot water flows through both TRVs. If the hot room's TRV isn't explicitly closed, it'll over-shoot further. The controller handles this by writing a setpoint *below* the hot room's current temperature, so the TRV's own thermostat clamps the valve shut. The setpoint is clamped to the TRV's `min_setpoint` (5 °C default for Aqara E1) to stay within its valid range.

This is done **pre-emptively** — even on a tick where the circuit isn't currently calling, a hot-room TRV gets force-closed so it's already shut the next time the circuit fires for a different room.

## Per-TRV persistent config

On controller start, each configured TRV has its Aqara-cluster settings applied:

| Setting            | Cluster        | Attribute | Effect                                                   |
|:-------------------|:---------------|:----------|:---------------------------------------------------------|
| `window_detection` | 0xFCC0         | 0x0273    | Close valve when a rapid temp drop suggests open window  |
| `child_lock`       | 0xFCC0         | 0x0277    | Disable manual TRV adjustment                            |
| `valve_detection`  | 0xFCC0         | 0x0274    | Detect stuck/unresponsive valves                         |

These are applied when the loop starts and after any config change that adds a TRV (deduplicated via `_trv_config_applied`; a TRV whose write failed is retried next time), and re-applied via `POST /api/heating/controller/trv/apply-config` if the user changes them.

The TRV's **sensor mode** follows the room: external only while the room's
`external_temp_mode` is `push`, internal otherwise. A TRV left external with
nothing feeding it regulates on the last value it was pushed. A TRV that reports
no sensor mode is never switched to internal.

`min_setpoint` is a fourth per-TRV setting: the floor for every setpoint the controller writes to it (default 5 °C). It is not sent to the device.

## Configuration (`heating.controller:`)

```yaml
heating:
  controller:
    enabled: true
    dry_run: false             # true logs what it would do without sending
  circuits:
    - id: downstairs
      name: Downstairs
      receiver_ieee: "00:15:8d:00:00:aa:bb:cc"
      receiver_command: thermostat    # 'thermostat' or 'switch'
      receiver_endpoint: 1
      receiver_call_setpoint: 30.0    # pushed when calling
      receiver_idle_setpoint: 7.0     # pushed when idle
      rooms:
        - id: living
          name: Living
          target_temp: 20.5
          night_setback: 17.0
          min_temp: 16.0
          temperature_sensor_ieee: "00:1e:5e:09:02:a3:e4:c1"
          external_temp_mode: advisory
          external_temp_push_interval_sec: 300
          dimensions: { ... }          # see heating.md § Thermal Profile
          radiator:
            watts_at_dt50: 1800
            flow_temperature_c: 55
            type: double_panel_double_conv
            wall: front
            placement: external_wall   # under_window | external_wall | internal_wall
            reflective_panel: true
          trvs:
            - ieee: "54:ef:44:10:00:67:3e:a6"
              window_detection: true
              child_lock: false
              valve_detection: true
              min_setpoint: 5.0
          schedule:
            - days: [mon, tue, wed, thu, fri]
              start: "07:00"
              end:   "22:00"
              temp:  20.5
```

The dwelling-level `heating:` block the Advisor reads — property, tariff,
boiler, comfort — is in [heating.md](heating.md) § Configuration Reference.
With a floor plan saved, the `rooms:` block is kept in step with it in both
directions (§ Config modes), so most of it is normally written by an editor
rather than by hand.

Per circuit, `operating_hours` and `weather_suppression` override the
controller-wide setting when present.

## Config modes

`heating.controller.config_mode` says which editor owns device bindings:
`floor_plan` (the plan editor) or `manual` (the controller modal). It does not
say which data is live — both modes read the same rooms.

**The rule.** When a floor plan exists, every write goes through it, and
`heating.circuits` and `heating.controller.circuits` are written as one
(`sync_heating_config` in `routes/heating_controller_routes.py`):

1. A save from the controller modal — config, a room target, TRV settings — is
   written into the plan (`apply_circuits_to_plan`): circuits and their
   overrides, room membership and settings, TRV bindings with their flags and
   `min_setpoint`, temperature sensors with height and primary, contacts and
   their tuning, radiator specs. Geometry is never written this way.
2. The plan is projected back out (`project_floor_plan_to_circuits`). The plan
   is authoritative for geometry and bindings: a device removed in the editor
   leaves the controller. A plan room's settings win; the previous circuits
   only fill what the plan lacks.
3. The result is written to both lists, so switching mode copies and strips
   nothing.

**Geometry flows one way, plan → manual.** Room size and floor area (the
polygon's, not its bounding box), ceiling height, wall types, windows with
their exact bearing (`facing_deg`, used by solar gain) and doors. A manual room
the plan resizes is reported in the save's `warnings`.

**Rooms only one side knows.** A manual room the plan never drew is kept, with
its settings, as an unplaced room. A room the plan drew and has since deleted is
dropped. A room removed from every circuit in manual keeps its drawing and
loses its circuit. A TRV bound in manual to a room with no free radiator gets an
unplaced radiator (`DEFAULT_UNPLACED_RADIATOR_W`) to position in the editor; a
contact gets `room_id` and no opening.

**Legacy configs** kept two diverging copies. The first sync takes every device
any of the two copies or the plan knows, keeps settings from the side being
edited (from the plan when nothing is), and names each setting that differed.
After that the copies are equal and this step is a no-op.

Without a plan, both lists are simply written with the saved circuits.

### Key constants

| Constant | Value | Module |
|:---|:---|:---|
| `COLD_BAND`                    | 0.5 °C     | `heating_controller.py`      |
| `HOT_BAND`                     | 0.3 °C     | `heating_controller.py`      |
| `FORCE_CLOSE_OFFSET`           | 1.0 °C     | `heating_controller.py`      |
| `MIN_SETPOINT_DELTA`           | 0.5 °C     | `heating_controller.py`      |
| `TICK_INTERVAL_SEC`            | 60 s       | `heating_controller.py`      |
| `COMMAND_COOLDOWN_SEC`         | 300 s      | `heating_controller.py`      |
| `MAX_EARLY_STOP_C`             | 0.4 °C     | `heating_controller.py`      |
| `ADVISORY_OPEN_BOOST_C`        | 2.0 °C     | `heating_controller.py`      |
| `OUT_OF_HOURS_OFF_C`           | 5.0 °C     | `heating_controller.py`      |
| `FLATLINE_HOURS` / `FLATLINE_MIN_REPORTS` | 12 h / 6 | `heating_controller.py` |
| `SOLAR_PROOF_SEC` / `SOLAR_PROOF_RISE_C` | 30 min / 0.2 °C | `heating_controller.py` |

## API

`routes/heating_controller_routes.py`. Scopes come from the middleware table
(`modules/auth_scopes.py`): every `GET` under `/api/heating` needs
`heating:read`, everything else `heating:write`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/heating/controller/state` | the last tick's decisions — per circuit and per room |
| `GET /api/heating/controller/managed` | the receivers and TRVs the controller is driving — the device modal disables its direct heating controls for these |
| `POST /api/heating/controller/tick` | force a tick now instead of waiting 60 s |
| `POST /api/heating/controller/dry-run` | turn dry run on or off, persisted (§ Why it is a separate module) |
| `GET`/`POST /api/heating/controller/config` | read and write `heating.controller` + `heating.circuits` |
| `POST /api/heating/controller/config-mode` | choose which editor owns device bindings (§ Config modes) |
| `POST /api/heating/controller/room/target` | set one room's target temperature |
| `GET /api/heating/controller/devices` | receivers and TRVs that could be configured |
| `GET /api/heating/controller/sensors` | temperature sensors that could be a room's source |
| `GET /api/heating/controller/contact-sensors` | contacts that could be bound to a window or door |
| `POST /api/heating/controller/trv/settings` | per-TRV window detection, child lock, valve detection, `min_setpoint` |
| `POST /api/heating/controller/trv/calibrate` | run the TRV's own calibration |
| `POST /api/heating/controller/trv/apply-config` | re-apply the persistent settings above |

The three candidate-list endpoints are also what the floor-plan editor's heating
view filters on, so its device palette and this page can never disagree.

## Implementation notes

Extracted from the code so the modules themselves stay terse.

### Hive SLT → SLR temperature binding

`core/service.py` binds these itself. Hive's SLT thermostat holds the room
temperature sensor; the SLR receiver needs that data to display the room
temperature and, on some firmware, to refine its heating decisions.

Paired to a third-party coordinator rather than the official Hive hub, the SLT
does **not** auto-bind to the SLR and does **not** auto-configure reporting on
its `0x0402` cluster. Two operations, both aimed at the SLT — a sleepy
end-device, so timeouts are generous and the calls are retried:

1. **ZDO `Bind_req`**: SLT (EP9, `0x0402`, server) → SLR (EP5, client). Tells
   the SLT where to send Report Attributes for cluster `0x0402`. Without it the
   SLT only reports to the coordinator (the default).
2. **Configure Reporting** on the SLT's `0x0402.measured_value`: min 30 s,
   max 300 s, change 25 centi-degrees (0.25 °C). Without it the SLT may report
   on an arbitrary firmware schedule, or not at all.

`bind_devices()` cannot be used here: it binds output→input, which is correct
for actuator binds (switch→light), but for sensor-style clusters the source is
the cluster *server* — the side that owns the data — which sits in the SLT's
*input* clusters.

### Receiver write protocol (Hive SLR)

The controller mirrors the `"SLR"` / `"RECEIVER"` model check that the HVAC
handler uses internally, so both agree on which write protocol is in play.

- **Turning off**: `set_hvac_mode` atomically writes `system_mode=off` + hold +
  frost setpoint. `set_target_temperature` must *not* also be called — it would
  wrongly include `system_mode=heat` in the same write.
- **Turning on**: the write atomically carries `system_mode=heat`, so it must
  fire even when the setpoint value is unchanged (e.g. still 30 °C from the
  previous call cycle).
- When calling for heat the controller pushes a high setpoint so the receiver's
  internal comparator fires the boiler, and a low one when standing down so the
  receiver does not fight it. Overridable per circuit via
  `receiver_call_setpoint` / `receiver_idle_setpoint`.

### External-temp push backoff

A TRV whose downlink is dead may still send its own reports, so failures are
only visible on the write path. After `EXT_PUSH_FAIL_STREAK_THRESHOLD` (3)
consecutive failures the effective push interval doubles per further failure,
capped at `EXT_PUSH_BACKOFF_MAX_SEC` — one dead TRV should not log an error
every cycle. `write_fail_streak` is maintained by the device command executor
and any successful write resets it.

### Per-attribute freshness

`last_seen` cannot distinguish a healthy device reporting battery from one that
has stopped reporting temperature — the "frozen attribute" failure. The health
check therefore queries DuckDB for the age of the last *temperature* report.

Results are cached per tick (`IEEE -> (checked_at, age)`) so four rooms sharing
a sensor cost one query rather than four. The lookback window reaches well
beyond the threshold so a sensor reporting every 20 minutes is not flagged just
because nothing landed in the last 15.

Freshness is skipped entirely when `temp_source == "external"`: the TRV's own
`local_temperature` is driving no decision, so its staleness is not a health
signal for that room. A TRV reporting *no* temperature at all is still a
genuine failure and is distinguished from "stale". No DuckDB history at all is
treated as a fresh install, not a fault.

### Stale-sensor fallback

The cached state value outlives the sensor that produced it, so a dead sensor
would otherwise steer a room on a reading weeks old. If DuckDB shows no
temperature report inside the room's freshness threshold the external reading
is discarded, and normal source selection falls back to the TRV mean — or
`"none"`, which classifies the room as unknown. An unknown room never calls for
heat; losing a sensor is an alert, not something the controller papers over.

**Frozen values.** A sensor can keep "reporting" one stuck value on a timer,
which passes the age check. An external sensor with at least
`FLATLINE_MIN_REPORTS` reports and a single distinct value across
`FLATLINE_HOURS` is treated exactly as stale, and its room health says so. The
query (`query_distinct_values`) runs off the loop in `prewarm_flatline`, cached
for `_FLATLINE_CACHE_TTL_SEC`. It is not applied to TRVs, whose 0.5 °C
resolution can legitimately hold one value for hours. The same rule blocks forwarding
a stale reading to a TRV in external mode, which would otherwise pin the TRV's
view of the room at that value.

### Miscellaneous

- **Force-close**: when shutting a TRV, write `(current - FORCE_CLOSE_OFFSET)`
  so the TRV's own thermostat holds the valve closed. Applied whether or not
  the circuit is currently calling, so the valve is already shut the next time
  it fires. Floored at the per-TRV `min_setpoint`.
- **Stall recovery**: a receiver commanded to heat and never seen in a heat
  mode (`HEAT_MODES` — Hive reports a held call as `emergency_heat`) after
  `RECEIVER_STALL_RECOVERY_SEC` gets an explicit off, so it re-evaluates on the
  next tick (bounce off→heat). Once confirmed, a later report is not a stall.
- **Send results**: the command sender reports failure as `{"success": False}`
  rather than raising. A failed receiver or TRV write is not recorded as sent,
  so the next tick retries it instead of the idempotent gate suppressing it.
  Every send goes through `_throttled_send` — the radio write lock and the
  resilience gate.
- **Tick log**: `heating_tick_boiler.receiver_command` is set only when a
  command went out (or would have, in dry run); `heating_tick_rooms` carries the
  outdoor temperature and the first TRV's valve opening — or, for a TRV that
  reports none (Aqara E1), 100 / 0 from its `hvac_action`.
- **TRV health**: a TRV that reports itself uncalibrated (`calibrated` false,
  the device's own flag) or is switched off still accepts a setpoint, so room
  health names it; nothing else would show it. A calibrate *command* that
  timed out is not a fault of the valve and leaves the reported status alone;
  after one that lands, the flag is re-read on a widening schedule until true.
- **Stratification** is applied only to the external sensor path. TRVs sit near
  the floor by their nature, but their readings already carry convective bias
  from the radiator, so a separate height correction would mislead.
- **Telemetry writes** must never block: the tick runs on the event loop and
  the write waits on the telemetry lock, so blocking there stalls every other
  loop task, stream generators included.
- **Config hot-reload** swaps `self.circuits` atomically under the config lock,
  so no tick is ever mid-flight. Stale `_last_command` entries for IEEEs no
  longer in config are dropped, or the idempotent gate could suppress a
  legitimate command if the same IEEE were re-added later.

## Troubleshooting

**"The controller says my room is cold but the TRV reports it's at target"** — you probably need `external_temp_mode: advisory` and a separate wall-mounted temperature sensor. TRVs read the hot pipe, not the room.

**"The controller isn't sending anything"** — check `heating.controller.enabled`
and that `dry_run` is off. In dry run the full tick cycle runs and the UI shows
what *would* happen, but nothing is sent.

**"A room says unknown though its sensor reports"** — look at the room health:
a sensor re-reporting one frozen value is rejected (§ Stale-sensor fallback).
Re-pair it or change its battery.

**"A TRV stopped taking external temperatures"** — the push backs off after
three consecutive write failures (§ External-temp push backoff), so look for the
write path failing rather than the TRV going quiet; its own reports carry on
either way.
