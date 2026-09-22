# Heating

## Overview

ZigBee Matter Manager has two complementary heating modules:

- **Heating Advisor** — a read-only analytical engine. It correlates outdoor weather with indoor temperatures and heating demand to produce an EPC-style rating for the property, pre-heat timing recommendations, cost estimates, and efficiency tips. It never sends commands to devices.
- **Heating Controller** ([docs/heating-controller.md](heating-controller.md)) — the active control layer. It watches room temperatures, decides which rooms are calling for heat, turns the boiler/zone valve on and off via the receiver, and coordinates TRV setpoints so a hot room can't steal heat from a cold one on the same circuit.

Everything the UI shows in the **Heating** tab is derived from these two modules plus the per-room **thermal profile** (heat loss physics) and **radiator sizing** (BTU/Watt capacity check). Both are purely calculated from the room's dimensions and insulation — you don't need sensors to get a baseline, but the more telemetry you give the system the more the numbers improve.

### What powers what

```
Weather (Open-Meteo)  ─┐
                       ├─► HeatingAdvisor   ──► EPC band, tips, preheat, costs
Devices (HVAC/TRVs)  ──┤
Telemetry DB (history) ┤
                       └─► ThermalProfile   ──► W/K per room, tau, anomaly baseline
                           RadiatorSizing   ──► required W vs installed W

Config (circuits/rooms) ──► HeatingController ──► boiler calls, TRV setpoints
                                             └──► AnomalyWatcher (fast-cool alerts)
```

---

## The Physics, Briefly

Heat loss through a building obeys **Newton's law of cooling**:

> A room loses heat to the outside at a rate proportional to the temperature difference between inside and outside.

The proportionality constant is called the **heat loss coefficient**, written *UA* or *W/K* — it's the power in watts needed to keep the room 1 °C warmer than outside. The whole heating model rests on four equations.

### 1. Steady-state heat loss

```
Q_loss  =  UA × (T_indoor − T_outdoor)     [watts]
```

A room with UA = 80 W/K on a 0 °C day with a 20 °C target needs `80 × 20 = 1600 W` of continuous heat input to hold that temperature.

### 2. Newton's law of cooling (with the heating off)

```
T(t)  =  T_outdoor  +  (T_0 − T_outdoor) × exp(−t / τ)
```

`τ` (tau) is the **thermal time constant** in seconds — roughly how long it takes the room to cool 63% of the way to outdoor temperature. A well-insulated heavy-mass room has τ ≈ 8–12 h; a cold-conservatory-style room can be under 1 h.

`τ` is related to UA by:

```
τ  =  (thermal mass, J/K)  /  UA
```

### 3. Heat-up time

Combining the two above, the time to heat from `T_from` to `T_to` with a radiator delivering `Q_rad` watts is:

```
T_steady  =  T_outdoor + (Q_rad / UA)
t  =  −τ × ln( (T_steady − T_to) / (T_steady − T_from) )
```

`T_steady` is where the room would plateau if you ran the radiator forever. If `T_steady ≤ T_to`, the radiator is undersized for the conditions and the target is **unreachable** — the controller flags this explicitly.

### 4. Radiator derating at lower flow temperatures

Radiators are rated at **ΔT50** (mean water temperature 70 °C, room 20 °C). A condensing boiler typically runs flow ~55 °C / return ~45 °C, so mean water temp is about 50 °C and ΔT is 30, not 50. Actual output follows an empirical exponent:

```
Q_actual / Q_rated  =  (ΔT_actual / 50) ^ 1.3
```

At ΔT30 that's `(30/50)^1.3 ≈ 0.52` — a radiator rated 2000 W at ΔT50 gives only about 1040 W in a condensing system. This is why correctly sizing radiators matters and why the BTU check uses your configured flow temperature.

---

## Thermal Profile (per-room)

Every room in the circuits config can have a `dimensions` block:

```yaml
rooms:
  - id: living
    name: Living room
    target_temp: 21
    dimensions:
      width_m: 4.2
      depth_m: 3.8
      ceiling_height_m: 2.4
      floor_type: carpet_over_concrete
      ceiling_type: insulated
      walls:
        front: { type: external }
        back:  { type: internal }
        left:  { type: party }
        right: { type: external }
      windows:
        - { wall: front, area_m2: 2.1, glazing: double }
      doors:
        - { wall: back, area_m2: 1.9, type: internal }
```

From that, `compute_static()` in `thermal_profile.py` produces a heat loss breakdown:

| Element              | Formula                                              |
|:---------------------|:-----------------------------------------------------|
| External walls       | `(wall_area − openings) × U_wall_ext`                |
| Party walls          | `area × U_party` (usually 0 — neighbour is heated)   |
| Internal walls       | 0 — loss-free to adjacent heated rooms               |
| Windows              | `area × U_glazing(single/double/triple)`             |
| External doors       | `area × U_door_ext`                                  |
| Floor                | `floor_area × U_floor(type)`                         |
| Ceiling              | `floor_area × U_ceiling(type)`                       |
| Ventilation          | `ρ_air × C_p × volume × ACH / 3600`                  |

Sum these and you get the room's **static W/K**. U-values come from SAP Appendix S and CIBSE Guide A, selected by the dwelling-wide `insulation` level (`none`, `partial`, `full`, `cavity_wall`).

### Measured W/K

`compute_measured()` does the same job from telemetry. It looks through the last N hours of temperature history, finds intervals where:

- The temperature is monotonically falling (with a small noise tolerance)
- The interval is at least 30 min and at most 6 h
- The total drop is at least 0.5 °C

For each window it fits Newton's cooling model by linear regression on `ln((T − T_out) / (T_0 − T_out)) = −t/τ` and keeps fits with R² ≥ 0.5. The median τ across all fits, combined with an estimated thermal mass of `3 × ρ_air × V × C_p` (the "3×" factor is CIBSE TM41 for furnishings/fabric), gives measured UA:

```
UA_measured  =  (3 × ρ × V × C_p) / τ_median
```

### Blending

If measured confidence is ≥ 0.3, the UI shows a **blended** W/K:

```
w  =  min(1.0, 0.7 × confidence / 0.7)
blended  =  w × measured + (1 − w) × static
```

Otherwise it falls back to static only. Confidence itself is `sample_factor × mean_R²` where `sample_factor` saturates at 10 good fits.

---

## Radiator Sizing

Given the blended W/K and a design outdoor temperature (−3 °C for UK MCS), the required radiator output is:

```
required_watts          =  W/K × (target_temp − design_outdoor)
required_with_margin    =  required_watts × 1.15       (15% headroom)
required_btu_hr         =  required_with_margin / 0.2931
```

If you've entered `installed_watts_at_dt50` for the room's radiator, the sizing module derates it for your actual flow temperature using the derate formula from section 4 above, then compares:

| Difference                         | Status        |
|:-----------------------------------|:--------------|
| installed < required − 50 W        | `undersized`  |
| installed > required × 1.5         | `oversized`   |
| otherwise                          | `adequate`    |

The UI shows this along with a deficit or surplus figure in watts.

---

## Efficiency Tips

The **per-room tips panel** in the Heating Controller modal runs two rule sets: a client-side mirror for instant feedback, and the backend `_generate_room_tips` for authoritative data. Both check roughly the same conditions:

| Trigger                                              | Severity | Notes                                                    |
|:-----------------------------------------------------|:---------|:---------------------------------------------------------|
| Dimensions missing                                   | info     | Cannot compute heat loss without them                    |
| No radiator capacity configured                      | info     | Required for sizing check                                |
| Radiator placement not set                           | info     | Affects efficiency flagging                              |
| Radiator under window                                | warning  | ~10% efficiency loss from warm/cold air mixing           |
| No reflective panel, radiator on external wall       | info     | Panel returns 3–8% more heat into the room               |
| No reflective panel, radiator on internal wall       | info     | Smaller gain (~1–3%) but helps TRV responsiveness        |
| Reflective panel status unknown                      | info     | Prompt to mark it either way                             |
| Single-panel radiator                                | info     | K2/P+ upgrade in same footprint roughly doubles output   |
| Single-glazed windows                                | warning  | ~4.8 W/m²/K vs ~1.6 for a good double                    |
| External door in room                                | info     | Check seals; heavy door curtain helps                    |
| Suspended or wooden floor                            | info     | Under-floor insulation is a fast retrofit payback        |

The **dashboard-level tips** in `heating_advisor._generate_tips` cover whole-dwelling patterns:

- Room is over-heating (indoor > target + 1 °C): each 1 °C cut saves ~3% on bills
- Mild weather (outdoor > 15 °C) while heating is active → consider turning off
- Cold snap forecast (next 6h min < 2 °C) → pre-heat now to avoid demand spike
- EPC band E/F/G → insulation upgrade would save 20–40%
- Single glazing
- Late-night heating → night setback could save ~10%
- Economy 7 / Agile tariffs → use the off-peak window
- Boiler < 90% efficient and gas → modern condensing saves ~£150/year
- Good insulation + fossil boiler → heat pump candidate (BUS grant £7,500)

---

## EPC Estimation

The Heating Advisor produces an EPC-style band from:

```
annual_kwh          =  UA_total × HDD × 24 / 1000        (HDD ~ 2200 UK average)
annual_fuel_kwh     =  annual_kwh / boiler_efficiency
kwh_per_m2_per_yr   =  annual_fuel_kwh / floor_area_m2
```

Where `UA_total` is the whole-dwelling coefficient `U × glazing_factor × floor_area`. `kwh_per_m2_per_yr` is the standard SAP metric and maps to letter bands:

| Band | kWh/m²/year |
|:-----|:------------|
| A    | 0–25        |
| B    | 25–50       |
| C    | 50–75       |
| D    | 75–100      |
| E    | 100–125     |
| F    | 125–150     |
| G    | 150+        |

The headline score is `max(1, min(100, 100 − kWh/m² × 0.6))` which keeps A ≈ 92–100, B ≈ 81–91, etc. The annual cost figure is `annual_fuel_kwh × unit_rate + 365 × standing_charge`.

These are **estimates, not formal EPCs**. They use SAP-style defaults, not the full BREDEM model an accredited assessor would run. Treat them as directional.

---

## Pre-heat Recommendation

The preheat calculator has two implementations. The quick one, on the dashboard, uses bulk thermal mass:

```
boiler_watts      =  boiler_kw × 1000 × efficiency
heat_loss_watts   =  UA × max(0, mean_indoor − outdoor)
net_watts         =  boiler_watts − heat_loss_watts
energy_needed_kJ  =  thermal_mass × (target − start)
minutes           =  ceil( energy_kJ × 1000 / net_watts / 60 )
```

(Thermal mass defaults to `80 kJ/m² × floor_area` for pre-1960 buildings, `60 kJ/m² × floor_area` otherwise, reflecting solid-wall heavyweight vs modern lightweight construction.)

If `net_watts ≤ 0` the boiler can't keep pace with losses at the current outdoor temp — preheat is clamped to the configured max (default 90 min) and the UI flags it.

The **per-room** version in `compute_preheat()` uses the more accurate Newton-of-heating formula from section 3 above, driven by the room's measured `τ` and derated radiator output. It also tells you the *steady-state* temperature the room would plateau at — if that's below your target, heating can't reach it at this outdoor temp regardless of how long you wait.

Confidence drops to **low** when there's no measured τ (fallback default 3 h) or no radiator capacity configured (assumes perfect sizing).

---

## Heating Controller

The active layer — circuits, the 60 s tick, hysteresis, force-close, TRV
settings, its config block and its API — is
**[docs/heating-controller.md](heating-controller.md)**. It commands hardware,
so it is gated behind its own `heating.controller.enabled` flag and a `dry_run`
mode; the Advisor on this page is read-only and cannot cause harm. Both read the
same thermal profile, so the numbers the Advisor shows are the ones the
controller will act on.

---

## Anomaly Detection

The **Heating Anomaly Watcher** scans every 5 minutes. For each room with a known baseline τ, it pulls the last ~3 h of temperature history and fits Newton's cooling over recent cool-down windows. It then compares observed vs baseline τ:

| Ratio (observed / baseline) | Severity   | Interpretation                                           |
|:----------------------------|:-----------|:---------------------------------------------------------|
| < 0.3                       | `critical` | Cooling 3× faster than baseline — window open, broken seal, boiler off |
| < 0.5                       | `warning`  | Cooling 2× faster than baseline                          |
| ≥ 0.5                       | none       | Within normal variation                                  |

Active anomalies surface on the dashboard as "Room X cooling faster than expected" cards. Once the condition resolves (subsequent scans show normal τ), the anomaly moves into a 6-hour history buffer so users can see the "was" card briefly before it drops off.

This is how the system catches problems like: a window left open overnight, a TRV that's stopped responding, a broken door seal, or a room whose insulation has degraded — all without the user needing to watch temperature graphs.

---

## Configuration Reference

### Dwelling-level config (`heating:` block)

The controller's own block, `heating.controller:` and `heating.circuits`, is in
[heating-controller.md](heating-controller.md) § Configuration.

```yaml
heating:
  enabled: true
  property:
    type: semi-detached        # detached | semi-detached | mid-terrace | flat
    age: 1960                  # build year — affects thermal mass estimate
    insulation: partial        # none | partial | full | cavity_wall
    glazing: double            # single | double | triple
    floor_area_m2: 85
    floors: 2
  tariff:
    type: fixed                # fixed | economy7 | agile | variable
    unit_rate_p: 24.5
    standing_charge_p: 46.36
    off_peak_start: "00:00"
    off_peak_end: "07:00"
    off_peak_rate_p: 7.5
  boiler:
    type: gas                  # gas | oil | electric | heat_pump
    efficiency_percent: 89
    output_kw: 24
  comfort:
    min_temp: 18.0
    target_temp: 21.0
    night_setback: 16.0
    preheat_max_minutes: 90
```

### Key constants (non-configurable defaults)

| Constant                       | Value      | Module                       |
|:-------------------------------|:-----------|:-----------------------------|
| `DEFAULT_OVERSIZE_FACTOR`      | 1.15       | `radiator_sizing.py`         |
| `DEFAULT_DESIGN_OUTDOOR_C`     | −3.0 °C    | `radiator_sizing.py`         |
| `RADIATOR_DERATE_EXPONENT`     | 1.3        | `radiator_sizing.py`         |
| `ROOM_THERMAL_MASS_FACTOR`     | 3.0        | `thermal_profile.py`         |
| `SCAN_INTERVAL_SEC` (anomaly)  | 300 s      | `heating_anomaly_watcher.py` |

---

## Troubleshooting

**"No tips are showing for my room"** — check that `dimensions` is populated and that at least one wall is typed `external`. Most tips are gated on data completeness.

**"Preheat says 90 minutes every morning"** — that's the clamp hitting `preheat_max_minutes`. Either your boiler is undersized for the conditions (check the net watts calculation), your outdoor temp sensor is wrong, or your room has no measured τ yet. Check the preheat warnings — they'll say which.

**"Radiator sizing status is unknown"** — you need both `dimensions` (to compute W/K) and `radiator.watts_at_dt50` (to compare against). Missing either one returns `unknown`.

**"Anomaly watcher never fires"** — it needs a baseline τ. The thermal profile has to produce a measured W/K first, which needs ~10 h of temperature history with at least one clean cool-down window. Give it a day of data after you configure dimensions.

**"The EPC number looks wrong"** — remember it's a SAP-style estimate using UK average 2200 heating degree-days, not the full BREDEM methodology. It's a relative indicator for seeing whether changes help, not a formal certification.

## Thermal profile internals

`ROOM_THERMAL_MASS_FACTOR = 3.0` — rooms have roughly 3× the thermal mass of
their air alone once furnishings, plasterboard and screed are accounted for.
Standard in the thermal-model literature (CIBSE TM41).

`U_VALUES` are keyed by insulation level, from SAP Appendix S + CIBSE Guide A.
`party_wall_u` is 0 for heated-neighbour party walls (the normal assumption for
terraces and flats); an isolated unheated void would be ~0.5.

### Sensor stratification correction

Warm air stratifies upward in heated rooms: a sensor mounted high reads warmer
than the comfort zone, one near the floor cooler. The correction is the
standard CIBSE Guide A rule of thumb — ~0.5 °C per metre above the reference
height, the vertical gradient of a heated room under typical convective
heating.

Reference height is 1.5 m: the standing breathing zone, the default mounting
height for residential thermostats, and what target temperatures implicitly
refer to. A sensor exactly at 1.5 m receives no correction.

The correction is additive on the delta from reference:

```
correction_c = -GRADIENT * (sensor_height_m - REFERENCE_HEIGHT_M)
```

Subtract from the raw reading to get the comfort-zone temperature.

It is deliberately *not* gated on "is heating active", because (a) the average
gradient over a heating season is dominated by heated time, (b) with heating
off the gradient self-decays and the correction is small in absolute terms —
well under sensor noise, and (c) gating would require coupling temperature
reads to controller state.

### Cool-down window thresholds

Two profiles:

- `LEARN_*` — fitting baseline τ from the long telemetry window. Deliberately
  loose, so more candidate windows are accepted and the R² filter in
  `_fit_newton_cooling` culls the noisy ones. Rooms held near setpoint most of
  the time still produce enough usable drifts.
- `ALERT_*` — the anomaly detector (`detect_fast_cooling`), comparing a single
  recent window against the baseline. Stricter, to avoid false "window open"
  alarms from small natural drifts.

A heating-state gate rejects windows where too many samples overlap a period
when heating was active. The tolerance is above zero to cover transient TRV
cycling at the window boundaries.

### Windows in the plan-aware path

Only windows whose host wall is external contribute. The plan-aware path
classifies this per opening; the bbox path would have folded the wall first and
then asked whether the *bin* was external, which is wrong for L-shaped rooms
where two edges fall in the same bin.

### Solar gain in pre-heat

Solar gain reduces the net load on the radiator, lowering time-to-target. It is
modelled by boosting effective radiator output by the average solar watts over
the pre-heat window:

```
T_steady = T_outdoor + (Q_rad + Q_solar) / W_per_K
```

Radiator and sun together push the room to a higher steady state, so it reaches
target sooner. τ does not change — it is a property of the room fabric, not the
heat source.

`minutes_saved = minutes_without_solar − minutes_with_solar` is also computed,
so the UI can say "pre-heat: 45 min (solar saving ~10 min)".

When there is no measured τ, one is synthesised from the static model alone:
`tau = (m·c) / UA`, with `m·c ≈ 3 × (ρ·V·Cp)` per room — the same factor
`compute_measured` uses. Without floor area, V cannot be estimated directly, so
it falls back to a typical indoor τ of 3 h. Less accurate, but it keeps a value
available from day one.

## Solar Gain

`modules/solar_gain.py` estimates instantaneous and time-averaged solar heat
gain into individual rooms from window geometry and real-time sun position. It
is the first layer of solar-aware preheat and cooldown logic, answering two
questions the controller needs:

1. How many watts of free heat is the sun putting into this room right now?
2. How many watts will it average over the next N minutes (the preheat window)?

All functions are pure — no I/O — and thread-safe.

### Physics model

The ASHRAE simplified solar heat gain approach:

```
Q_window = A × SHGC × I_incident        [W]
```

- `A` — window area [m²]
- `SHGC` — Solar Heat Gain Coefficient (glazing-type dependent)
- `I_incident` — irradiance falling perpendicularly on the glass [W/m²]

`I_incident` comes from either:

- a measured `shortwave_radiation` value from Open-Meteo — preferred, because
  it is the real, cloud-attenuated value — with a cosine projection applied for
  the angle between sun and window face; or
- a clear-sky beam model (`1000 × sin(elevation)`) attenuated by a cloud
  fraction term, when `shortwave_radiation` is unavailable.

For a vertical window on a wall with outward normal `N_deg` (bearing, clockwise
from true north) and sun azimuth `S_deg`:

```
cos_inc = cos(elevation) × cos(S_deg − N_deg)
```

This is zero when the sun is behind or parallel to the wall, and 1.0 when the
sun shines perpendicularly at zero elevation — which never happens in practice,
but the geometry is correct.

### Diffuse component

On overcast days the beam is negligible but diffuse sky radiation is
significant:

```
Q_diffuse = A × SHGC × diffuse_fraction × shortwave_radiation
diffuse_fraction = 0.15 + 0.85 × cloud_fraction
```

On a clear day most radiation is direct beam; on a fully overcast day
essentially all of it is diffuse, though the total is lower.

### API

| Function | Returns |
| --- | --- |
| `solar_gain_now(room_config, lat, lon, dt_utc, shortwave_wm2, cloud_fraction)` | `float` [W] — instantaneous gain for one room |
| `solar_gain_window(room_config, lat, lon, start_utc, duration_minutes, shortwave_wm2, cloud_fraction)` | `SolarGainWindow` — average watts + breakdown, used by preheat |
| `solar_gain_forecast(room_config, lat, lon, start_utc, hourly_shortwave, hourly_cloud_cover)` | `List[SolarGainSample]` — per-hour profile, for scheduling |

### Room config

Uses the existing `dimensions.windows[]` and `dimensions.walls{}`, plus one
optional field per wall:

```yaml
dimensions:
  walls:
    front: { type: external, facing_deg: 180 }   # south-facing outward normal
    left:  { type: external, facing_deg: 270 }   # west-facing
  windows:
    - { wall: front, area_m2: 2.1, glazing: double }
```

`facing_deg` is the compass bearing of the wall's outward normal (0 = N,
90 = E, 180 = S, 270 = W). A wall with no `facing_deg` contributes zero solar
gain — the function degrades gracefully rather than crashing.

### Measured solar impact

`modules/solar_impact.py` is the empirical counterpart to `solar_gain.py`: that
module predicts what the sun *should* contribute from window geometry, this one
reads what it *actually* contributed from the temperature record.

**Method**

1. Pull the room's temperature history and the controller's per-tick heating
   state, and find heating-off cool-down windows using the same machinery
   `thermal_profile.py` uses for τ learning.
2. Classify every window with the clear-sky solar model:
   - **baseline** — the model says the room's windows receive ~no direct sun
     during the interval (night, or the facade is in shade throughout);
   - **sunlit** — the model expects meaningful gain (≥ `SUNLIT_MIN_MODELLED_W`);
   - **ambiguous** — in between; excluded from both sides.
3. Fit Newton cooling on the baseline windows only → the room's no-solar time
   constant τ_night. This is the room's own control group.
4. For each sunlit window, predict the end temperature from τ_night and the
   outdoor record, and read the residual:

   ```
   residual_c = observed_end − predicted_end        (> 0 ⇒ un-modelled heat)
   C [J/K]    = UA [W/K] × τ_night [s]              (UA from the thermal profile)
   measured_w = C × residual_c / duration_s
   ```

5. Compare with the clear-sky model's average for the same window. The median
   measured/modelled ratio is the room's solar calibration factor: below 1 means
   shading, cloud or film is already attenuating the sun; above 1 means the room
   heats up more than its glazing suggests (check loft and fabric).

Everything degrades gracefully and reports *why* it stopped: no sensor, no
telemetry, no cool-down windows, no location, no window geometry, or not enough
baseline windows yet.

**Attribution caveat.** Daytime residuals also include internal gains (people,
cooking, electronics). Using each room's own night-time baseline and the
facade-lit classification keeps the signal dominated by solar, but treat
single-window numbers as noisy — the medians are the story.

**Scale caveat.** Measured watts are proportional to the room's estimated
thermal capacitance `C = UA × τ`, and UA comes from the thermal profile's mass
model, which carries real uncertainty. Comparisons *between* rooms and trends
over time (before/after fitting window film, say) are trustworthy; absolute
wattage is indicative only.

## Floor plan → heating

The plan itself — the data model, the editor, background images, device
placement, the scopes and the API — is **[docs/floor-plan.md](floor-plan.md)**.
It is one plan for the whole home, shared with daylight, signal coverage,
chambers and automations. This section is only what heating does with it.

The floor plan is an **editor surface**. The source of truth for circuits and
rooms remains `heating.circuits` in `config.yaml`. On save,
`modules/floor_plan.py` projects the plan back into each existing room's
`dimensions` / `radiator` / `trvs` / `contact_sensors` /
`temperature_sensor_ieee`, so `thermal_profile.py` and `heating_controller.py`
keep working unchanged. `GET /api/floor-plan/preview` is that projection as a
dry run.

Where the plan is richer than the legacy schema (multiple radiators per room,
multiple temperature sensors, contacts bound to specific openings), the
projection emits the legacy singular fields **and** the new plural ones:

| Legacy | New |
| --- | --- |
| `room["radiator"]` — largest-watts radiator in the room | `room["radiators"]` — full list with TRV bindings |
| `room["temperature_sensor_ieee"]` — primary sensor | `room["temperature_sensors"]` — full list with heights |
| — | `room["contact_sensors"][i].opening_id` — opening linkage |

Which walls count as external, and which windows belong to which room, are
decided by the plan's own rules — both change a room's heat loss and solar gain,
and both are in [floor-plan.md](floor-plan.md) § Windows and rooms.

### Thermal overlays

`static/js/floor-plan.js` draws three overlays from one shared scalar field, so
the heat map, the isotherm contours and the cold-zone tint agree by
construction.

**Thermal field.** A per-room "heat coverage" field sampled on a coarse grid:

```
C(p) = Σ_radiators exp(−(d/r₀)²) − Σ_drafts amp · exp(−(d/r_D)² · k)
```

where `r₀ = √(watts / (heatFlux · π))` is the radius each radiator can keep
above the comfort threshold at the configured building heat loss. `C ≈ 1` right
next to a radiator and crosses `COLD_THRESH = e⁻¹` at `d ≈ r₀`, so the
cold-zone boundary lands where the old heated-radius circle used to — but now
it bends around draughts and merges between multiple radiators.

**Solar gain** (clear-sky heuristic, for insight rather than engineering).
Average power admitted through a window over daylight hours:

```
W ≈ 500 W/m² × SHGC(glazing) × area × (sun-minutes / daylight)
```

500 W/m² is the effective clear-sky irradiance on vertical glazing when the
facade faces the sun. Sun-minutes come from today's sun curve — the same data
as the sun-path arc.

**Measured override.** Where `/api/heating/solar-impact` has a trustworthy
measurement for a room (see [measured solar impact](#measured-solar-impact)),
the plan prefers it: the `calibration_ratio` (measured ÷ clear-sky-modelled)
scales the solar sources in the field and the per-window badges, and the
insights panel reports measured watts instead of the estimate.

**Radiator plan-view rendering** has two modes. Wall-mounted radiators draw as
a thin strip along the host wall at a fixed 0.1 m plan depth — `height_m` is
the radiator's *physical* height, used for sizing and heat calculations, not
its footprint — offset perpendicular toward the bound room centroid so it sits
on the room-side face. Freestanding radiators draw as a `length × 0.1 m`
axis-aligned strip at `(x, y)`, for towel rails, columns, underfloor zones, or
anywhere placed away from a wall.
