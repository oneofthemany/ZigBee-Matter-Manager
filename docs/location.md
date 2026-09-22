# The home location

One position for the home, kept in one place and read by everything that needs
to know where the house is.

**Where it's kept.** `location.latitude` / `location.longitude` in config.yaml,
and nowhere else. `modules/location.py` holds it live: `home()` reads it,
`set_home(lat, lon)` changes it.

**Who reads it.**

- the weather service (`WeatherService.latitude/longitude` are properties over
  it), and through it the heating advisor
- sun times (`set_location_provider`)
- daylight, the sun path and the floor plan's map
- the swarm's virtual devices
- presence, where each user's `home_lat` / `home_lon` is a read-only view of it
- journeys and fuel prices.

**Who changes it.** All three go through `set_home`:

- **Settings → Weather.** The *Home latitude/longitude* fields are the home's.
- **The floor plan's map.** Line the map up under the plan, then *Set as the
  home location* makes the plan's middle the home. That's the most exact way to
  set it ([floor-plan.md § The map backdrop](floor-plan.md#the-map-backdrop)).
- **Presence settings.** *Set the home to where I am* uses the phone's position.

`GET/POST /api/location/home` (`system:read` / `system:write`) is the route.
A change is written to `location:` with the file's comments intact, and applied
at once: the weather refetches for the new place (starting its poll loop if it
never could for want of one), and sun times drop their cached day. No restart
is needed.

**From before.** The home used to be kept three times: `weather.latitude/longitude`
(which the weather, sun times and virtual devices read), `location:` (which fell
back to the weather's), and a `home_lat/home_lon` per presence user. They
drifted: on one hub the weather said 51.380509, the presence users 51.380537
and 51.38054. On the first boot after this change, `location.init` moves the
first one set into `location:`. That's the weather's, since it drove the most,
or else a presence user's. It then removes `weather.latitude/longitude` from the
file, leaving `weather: {}` rather than an empty block if nothing else was
there. Presence users' copies are ignored, and dropped from
`presence_users.yaml` on its next save.

One home for everyone reverses presence's earlier per-user homes (a lodger's
home was meant to be their own). A household with someone who lives elsewhere
would now need a **place** for that person's other home
([presence_detection.md](presence_detection.md)).
