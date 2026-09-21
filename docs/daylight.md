# Daylight — outdoor light without a lux sensor

`modules/daylight.py` estimates the light level outdoors — what a lux sensor on
the roof would read — from the sun's position and the weather. The result is
published on the Weather virtual device (`virtual::weather`), so any rule can
trigger on it without a sensor. The user-facing part, and the ready-made rule
that uses it, are in `docs/automations.md` § *Daylight: lights without a lux
sensor*.

## 1. Inputs

| Input | Where from | Needed? |
|---|---|---|
| Home location | `weather.latitude/longitude`, else `location:` (`location.home_coords`) | For the sun. Without one, only a measured irradiance can answer |
| Sun elevation | `sun_position()`, computed locally every refresh | Derived from location |
| Global horizontal irradiance | Open-Meteo `shortwave_radiation`, current | Optional |
| Cloud cover | Open-Meteo `cloud_cover`, current | Optional |

Weather is polled every 30–60 minutes, and dusk changes much faster than that.
So **the weather sets how clear the sky is, and the sun sets the timing**.
Everything here is recomputed every 60 s from in-memory state. No I/O, no database.

## 2. The clear-sky curve

`CLEAR_SKY_LUX` is horizontal illuminance (sun plus sky) under a clear sky,
against solar elevation. It runs from astronomical twilight (−18°) to the
zenith and is interpolated in log space, since illuminance spans eight orders
of magnitude across dusk.

| Elevation | Lux | |
|---|---|---|
| −18° | 0.001 | astronomical dusk |
| −6° | 3.4 | civil dusk — too dark to read outdoors |
| 0° | 600 | sunset, clear sky |
| 5° | 5 000 | |
| 30° | 48 000 | |
| 60° | 95 000 | a midsummer noon in southern England |

The values are rounded approximations of published sun-plus-sky illuminance
tables. That's accurate enough for switching a light, but not for photometry.
Real skies are almost never brighter than a clear one, so treat the curve as an
upper bound.

## 3. Clearness: the weather's contribution

Three sources, tried in order. The one used is published as `daylight_source`.

1. **`measured`** — the last irradiance reading, as a fraction of clear sky:

       kt = GHI × K(cloud) / clear_sky_lux(elevation when measured)
       lux = clear_sky_lux(elevation now) × kt

   `K` is luminous efficacy: 105 lm/W under a clear sky, rising to 125 lm/W
   overcast, because diffuse skylight is richer in visible light. The reading is
   used only while it is under 90 minutes old (`MEASURED_MAX_AGE_S`), and only
   if it was taken with the sun at least 5° up (`MEASURED_MIN_ELEVATION_DEG`).
   Nearer the horizon, the ratio mostly reflects the model's own error.
   `kt` is clamped to 0.03–1.3.
2. **`cloud`** — clear sky × the Kasten–Czeplak cloud attenuation
   (`solar_gain._beam_attenuation`, the same factor the solar-gain model uses),
   from cloud cover up to 3 hours old.
3. **`clear_sky`** — the curve alone. It is always the brightest of the three,
   so a house with no weather goes dark at clear-sky dusk: late on a grey day,
   but never early.

With no location at all, lux is `GHI × K` when a measured irradiance is
available, and otherwise nothing is published.

## 4. Bands and hysteresis

A threshold on raw lux flickers when a cloud passes at dusk. So the published
flags come from a small set of named bands, entered on falling below a
threshold and left only once the light is `LEVEL_HYSTERESIS` (1.5×) above it:

| `daylight_level` | Entered below | Left above | Meaning |
|---|---|---|---|
| `dark` | 10 lx | 15 lx | night |
| `dusk` | 400 lx | 600 lx | around sunset on a clear day; earlier under cloud |
| `dull` | 5 000 lx | 7 500 lx | an overcast day — rooms feel gloomy |
| `bright` | — | — | |

It is `daylight_level`, not `light_level`, on purpose: `light_level` is one of the
names a lux sensor reports, and the swarm would then read the Weather device as a
lux sensor in every room.

The previous band is the Weather device's own state, so this carries across
refreshes with nothing extra stored. After a restart the first reading starts
without history, which is the plain band.

## 5. What is published

On `virtual::weather`, alongside the existing weather readings:

| Attribute | Value |
|---|---|
| `outdoor_lux` | estimate, rounded to two significant figures so it doesn't re-evaluate rules on every refresh |
| `daylight_level` | `dark` / `dusk` / `dull` / `bright` |
| `is_daylight` | 1 in `dull` or `bright`, else 0. Swarm offer `weather:got_dark_out` ("daylight fades") |
| `is_gloomy` | 1 in anything but `bright`. Swarm offer `weather:got_gloomy_out` ("the day turns gloomy") |
| `daylight_source` | `measured` / `cloud` / `clear_sky` |
| `sun_elevation` | whole degrees |

Previously `is_daylight` was "irradiance above 1 W/m²". That only changed when
the weather refreshed and ignored cloud. It now comes from the model, so
existing rules on it fire at the same point on a clear day, earlier under cloud,
and on time between weather polls.

## 6. Limits

- The Weather device reports outdoor light. How dark a room is depends on its
  windows; see §7 for the per-room estimate.
- Terrain, tall buildings and trees to the west bring real dusk forward. With
  the ready-made rule, raise the hold or use the *gloomy* trigger instead.
- Snow, fog and heavy rain are only represented through irradiance and cloud
  cover. A `measured` source captures them; `cloud` only partly.

## 7. Each room

With a floor plan, every room that has a window to the outside gets its own
estimate: roughly what a lux sensor in the middle of the room would read,
from daylight alone.

**Geometry** (`floor_plan.daylight_geometry`). For each room this gives the inner
surface area `A` (floor + ceiling + walls) and its windows: area, glazing, and
true bearing after the compass. A window counts only if it sits on this room's
own edge, on an external wall. One long outside wall running past several rooms
gives its window to the room it opens into, not to every room the wall touches.
Rooms with no outside window get no estimate.

**Sky light** — the BRE average daylight factor:

    DF% = Σ (T · W · θ) / (A · (1 − R²))
    E_sky = E_diffuse_outdoor × DF / 100

`T` is the glazing's visible transmittance (single 0.85, double 0.75, triple 0.65),
`W` the window area, `θ = 70°` the visible sky angle (a little obstruction), and
`R = 0.5` the mean reflectance of the room's surfaces.

**Sunlight.** The outdoor estimate is split into diffuse and beam using the same
diffuse fraction the solar-gain model uses (`0.15 + 0.85·cloud`). Beam light
through each window is `E_beam_normal · T · W · cos(incidence)`. The incidence
comes from `solar_gain._cos_incidence` against the window's bearing, and is zero
when the sun is behind the wall or below 2°. The resulting light is spread over
the room the same way, `/ (A · (1 − R²))`. That makes it an average, not the
bright patch on the floor.

For a 5 × 4 m room with a 1.7 m² double-glazed window, at a clear midwinter noon
in London: facing south about 1,300 lux with sun in; facing north about 40 lux.
Under full cloud, about 70 lux either way.

**Published** on `virtual::daylight::<room id>`, named "<Room> daylight", which
sits in its room:

| Attribute | Value |
|---|---|
| `illuminance_lux` | the estimate, two significant figures |
| `direct_sun` | 1 while sunlight is coming through a window |

The device declares the `illuminance` capability, so every room-scoped "when it
gets dark" suggestion works with it (see `docs/automations.md` § Daylight). It is
marked as estimated, and a real lux sensor in the same room is always preferred
over it. It also declares `daylight_estimate`, which means it is never offered
as a device that can go offline.

**The editor's Daylight layer** (floor plan → View → *Daylight in each room*)
colours each room by the same estimate for any time today, via
`GET /api/floor-plan/daylight?step_minutes=30`. The current weather is used near
now, the hourly cloud forecast further away, and a clear sky where neither is
available. It reads the saved plan, so a window you've just drawn counts once
you save.
