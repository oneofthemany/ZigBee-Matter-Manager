# Journeys — drive tracking

Drive tracking fed by the companion app's drive mode. While the phone is
connected to the car's Bluetooth, `DriveService` streams fixes tagged with a
`trip_id` (plus GPS speed and bearing). `modules/journeys.py` stores those
fixes, segments them into trips, and computes per-trip statistics: distance,
duration, and average / max / min / standard deviation of speed.

## Driving behaviour

Each fix may also carry an inertial summary of the interval that ended at it,
and any discrete events the phone detected within it — see the companion app's
`MotionSampler` for how those are produced. GPS answers where and how fast; the
accelerometer answers *how*, which is the part of a drive that position alone
cannot show. From it a closed trip gains harsh braking / acceleration /
cornering counts, peak longitudinal and lateral acceleration, road roughness,
and a smoothness score.

Everything inertial is optional at every level. A phone with no gyroscope
reports no cornering; one with no barometer falls back to GNSS altitude for
climb; one with no motion sensing at all still records a perfectly good
journey, and the behaviour columns stay NULL rather than zero — the distinction
between "smooth" and "not measured" has to survive into the UI, or a trip with
no data reads as a perfect drive.

## Storage

`data/journeys.duckdb` — a database dedicated to this module. DuckDB is
single-writer per file, so journeys must never share a `.duckdb` with any other
subsystem; all access goes through one dedicated worker thread that owns the
connection (one DB, one thread — the project convention). Async callers reach
it via `run_in_executor`.

`_SCHEMA` is split on semicolons by `_open`, so **no SQL comment in that string
may contain a semicolon.**

`_MIGRATIONS` exists because `CREATE TABLE IF NOT EXISTS` is a no-op on a
database that already has the table. A hub that recorded journeys before motion
sensing existed would otherwise keep the old, narrower tables forever and every
insert would fail. The migrations run on every open; `ADD COLUMN IF NOT EXISTS`
makes that idempotent and costs nothing on an already-current database.

## Trip lifecycle

- First fix with an unseen `trip_id` opens a trip.
- The phone stops streaming when the car's Bluetooth disconnects; there is no
  explicit "trip ended" call (a network call in `onDestroy` is not reliable). A
  closer loop finalises any open trip whose last fix is older than
  `TRIP_CLOSE_GAP_S`.
- Finalisation computes distance (haversine over consecutive fixes) and speed
  statistics in SQL, resolves start/end places, then marks the trip closed.
  Trips with fewer than `MIN_TRIP_FIXES` fixes are discarded as noise (a
  Bluetooth blip, a parked reconnect).

Speeds are stored and aggregated in m/s (the phone reports GPS doppler speed in
m/s); the UI converts for display. Where the phone sent no speed, a speed is
derived from consecutive fixes as a fallback.

### Reopening a prematurely closed trip

A fix arriving for a trip already marked closed means the closer called it
early: the phone went quiet for longer than `TRIP_CLOSE_GAP_S` — a tunnel, a
dead spot, a spell with no mobile data — and then came back mid-drive. Without
the reopen the trip stays closed forever, every later fix lands in `trip_fixes`
where nothing will ever aggregate it, and the summary is frozen on the handful
of fixes that beat the gap. That is a drive reported as three fixes and zero
miles while the rest of it sits in the table unread.

Reopening is enough to repair it. Finalisation recomputes distance, duration,
speed, behaviour and endpoints from every fix the trip has, so the next closer
pass produces the same answer it would have if the gap had never happened.
This is why the finalising `UPDATE` uses `COALESCE` on the attribution columns
rather than plain assignment — refinalising must not discard an attribution
someone made by hand in between.

A crash mid-drive leaves trips open with no more fixes coming; they are swept
by the first closer pass rather than special-cased, since "no fix for
`TRIP_CLOSE_GAP_S`" already covers it.

## Tuning constants

| Constant | Rationale |
| --- | --- |
| `DRIVE_FIX_INTERVAL_S = 10` | Cadence the hub asks journey-enabled phones to use. At 60 s a bendy road loses real distance and the speed distribution is undersampled; at 10 s both are honest. Served to the phone via `GET /api/presence/users/{id}` so retuning is a hub-side edit. |
| `TRIP_CLOSE_GAP_S = 300` | Comfortably larger than the fix interval so a tunnel or signal gap doesn't split one drive into two. |
| `MIN_TRIP_FIXES = 3` | One or two fixes is a Bluetooth blip or a parked engine-start, not a journey. |
| `TRACK_RETENTION_DAYS = 90` | Raw track points are deleted after this; summary rows are kept. |
| `MAX_PLAUSIBLE_SPEED_MPS = 90.0` | ~200 mph. Above this is a GPS glitch. Such fixes stay in the stored track but are excluded from statistics rather than allowed to poison max/stddev. |
| `STOPPED_SPEED_MPS = 0.5` | ~1.1 mph. GPS speed does not settle to exactly zero, so "stationary" needs a floor rather than a comparison with 0. |
| `MAX_IDLE_SEGMENT_S = 30.0` | Idle time only accrues across gaps no longer than this. A longer gap is lost signal — a tunnel, a multi-storey car park — and counting it as idling would invent minutes of stationary time that never happened. |
| `METRES_PER_HPA = 8.3` | The barometric relation is exponential, but over the few hundred metres a road covers the linear term is accurate to well under a metre, and the phone's sensor noise is larger than the error this discards. |
| `CLIMB_DEADBAND_HPA / _M` | Both sensors are noisy in a way that summing only positive deltas turns into phantom altitude gain — a flat motorway would otherwise "climb" tens of metres an hour. A real gradient clears both comfortably: 2% at 20 m/s is ~4 m (0.5 hPa) per fix interval. |
| `MIN_GRADIENT_SPEED_MPS = 5.0` | ~11 mph. Gradient is a vertical rate divided by a horizontal one, so the divisor shrinking towards zero turns barometer noise into a cliff face. Crawling traffic gets NULL, which the UI can show as unknown; it cannot show a number as wrong. |
| `MAX_GRADIENT_GAP_S = 30.0` | Wider than a fix interval to survive a dropped fix, short enough that the two pressures still describe one stretch of road. |
| `MAX_PLAUSIBLE_GRADIENT_PCT = 25.0` | Steeper is not a public road — the steepest in the country are around a third, and a motorway is under 4. Catches gross artefacts only. The barometer sits in the cabin, so a window or the HVAC opening steps the pressure in a way no hill does, and a step small enough to land inside this range still reads as a hill for one fix interval. A sustained gradient is the trustworthy signal; a single steep sample is not. |
| `MIN_SCORE_DISTANCE_M = 2000.0` | One firm brake on a 500 m drive is not a driving style, but per-distance normalisation would score it as if it were. |
| `SCORE_DECAY_EVENTS_PER_100KM = 100.0` | See [Smoothness score](#smoothness-score). |
| `MIN_LEADERBOARD_DISTANCE_M = 40000.0` | ~25 miles. See [Leaderboard](#leaderboard). |
| `COPRESENCE_*` | See [Co-presence](#co-presence). |

### `NON_VEHICLE_ACTIVITIES`

Activities that are certainly not the car moving. Fixes carrying one are kept
in the table but excluded from every aggregate and from the drawn track — the
walk from a parking space is otherwise distance the driver is credited with,
and the drift while sat in a parked car is a journey that never happened.

`"still"` is deliberately absent: a car at a red light reports it, and dropping
those fixes would delete the idling and stops being measured. Anything
unrecognised, and a NULL from a phone that cannot report at all, counts — the
fix is trusted unless the phone actively says otherwise.

## The SQL

`_FINALIZE_SQL` — distance + speed statistics for one trip, all in the engine.
Consecutive fixes are paired with `LAG`; each segment contributes haversine
metres; the speed sample is the GPS-reported speed, falling back to segment
distance over segment time when the phone sent none. Implausible samples are
excluded from the aggregates (not the table).

`_MOTION_SQL` — driving behaviour from the per-fix inertial summaries. Kept
separate from `_FINALIZE_SQL` rather than bolted onto it: that query is about
the track and this one about the sensors, they fail independently (a phone with
no accelerometer produces all-NULL here and a perfectly good trip there), and
one window function over ten columns is harder to read than two over five.

Roughness is the mean of the per-window RMS values. Averaging RMS values is not
the RMS of the whole trip, but the windows are equal-length by construction
(one fix interval each), which makes the two equal to within the rounding the
phone already applied.

Throughout the altitude maths: **pressure falls as the car climbs**, hence the
reversed subtractions. Descent is summed as a positive magnitude and kept
separate from climb rather than netted into it — a round trip nets to zero,
which says nothing about the road.

`_TRACK_SQL` — the track, with a signed road gradient per fix. Gradient is a
rate over a rate — metres climbed per metre travelled — so it needs no phone
orientation at all. That is the whole reason it is derived this way: gravity
cannot separate a cradle tilted 20 degrees from a hill of 20 degrees, and
asking the driver to mount the phone a particular way would trade away the one
property that makes the rest of this work anywhere.

Gradient is barometric only. GNSS altitude is good to tens of metres, which
over the few hundred metres between two fixes is larger than the height change
being measured; a gradient from it would be noise with a plausible unit
attached. Phones without a barometer get NULL, and the UI says unknown.

### Climb source selection

`_behaviour` prefers the barometer: over the few hundred metres a road covers
it resolves a metre where GNSS altitude is good to tens of them. GNSS is the
fallback for phones without one, and is why climb is reported at all rather
than only for the subset of devices with a barometer. Both directions take the
same source, so a trip cannot report barometric climb against GNSS descent and
appear to gain height it never lost.

## Smoothness score

Harsh events per 100 km, mapped to 0-100 by exponential decay. Exponential
rather than a linear penalty because the interesting difference is at the
smooth end: linear scoring compresses "one event" and "no events" into the same
couple of points while letting a bad enough drive go negative and need
clamping. Decay has neither problem — it is steepest exactly where the drives
being compared usually sit, and approaches zero without ever reaching it.

`SCORE_DECAY_EVENTS_PER_100KM` is calibrated against what the phone's 3.5 m/s²
threshold actually fires on: a motorway run produces almost nothing, an
ordinary urban drive one to four events in ten kilometres, and hard city
driving several times that. At 100 that maps to 90 / 75 / 55 / 25 across the
range, which keeps the resolution where the drives being compared sit. A
smaller constant looks sharper but pushes everyday driving into the fifties, at
which point the number stops distinguishing anything.

The score is a relative indicator of how smoothly a car was driven —
deliberately *not* an insurance-style risk rating, which would need speed
limits, road class and time of day this hub has no access to.

The `"harsh"` event kind is one the phone detected before it had learned the
car's forward axis. It counts toward the total — it was a real excursion — but
cannot be attributed to braking or cornering.

`max_brake` comes out of `MIN(long_peak)`, so it is negative or NULL. It is
reported as the magnitude it is spoken about as ("braked at 4 m/s²").

## Drivers and attribution

A trip is recorded by a phone, not by a person: `user_id` says whose phone was
in the car, which is only the same thing as who was driving when the owner
drove. Pooling every recording user's trips into one score, or crediting a
passenger's phone with the driving, both produce a number that describes
nobody. So attribution is a separate concept — a roster of drivers, one of whom
owns each trip.

A driver may be linked to a presence user, in which case that user's trips are
attributed to them automatically at close; the link is optional so a household
member who carries no tracked phone can still be scored on trips reassigned to
them. `driver_id` stays NULL until someone claims the trip, and unattributed
trips are counted separately rather than folded into whoever happened to be
carrying the phone.

Attribution is automatic where the evidence allows and records how it was
decided, because a guess presented as a fact is worse than no attribution:
`attribution` says which rule fired and `confidence` how far to trust it, so
the UI can mark a trip as needing confirmation rather than silently crediting
the wrong person.

| `attribution` | `confidence` | Set by |
| --- | --- | --- |
| `sole_phone` | `high` | Trip close, when exactly one phone recorded it. High until the co-presence pass finds another phone in the car, which is the only thing that can undermine it: one phone recording means one person known to have been there. |
| `sole_phone` | `medium` | Backfill when a driver is linked to a user. An inference over history rather than an observation of it: trips older than `COPRESENCE_LOOKBACK_S` were never checked for a second phone, so `high` would be claiming more than is known. |
| `copresence` | `low` | The co-presence pass. Several people were in the car and nothing the hub can see says which one drove. |
| `manual` | `high` | Someone who was there said so, which outranks every inference here — and is why the co-presence pass leaves manual attributions alone. |

`_claim_history` attributes a newly linked user's unclaimed trips to the new
driver. Without it a driver created today starts with an empty leaderboard row
while months of their own trips sit unattributed. Only NULL `driver_id` rows
are touched: linking a phone must never take a trip away from whoever is
already credited with it. The claimed count is over journeys only — a collapsed
duplicate is claimed too (it is still that phone's record) but it reaches no
aggregate, so reporting it would promise history that never appears in the
driver's totals.

Deleting a driver unattributes rather than cascades: the trips happened, and
deleting a driver is a statement about the roster, not about the history.

`car_bt_address` identifies the vehicle and never the driver. A household with
one car has every driver sharing that address, so the two facts are independent
by construction: the same car appears under every name on the leaderboard, and
a trip is attributed by who recorded it, not by what they drove. The column is
here for per-vehicle reporting and to key a future learned prior — which must
be conditioned on time and occupancy as well, never on the car alone.

## Co-presence

`trip_id` is minted on the phone, so two journey-enabled phones in one car
record the same physical drive as two unrelated trips. Left alone that
double-counts distance and averages one drive into the aggregate twice. A pass
over recently-closed trips pairs them up by time overlap and endpoints, keeps
one as the journey, and points the other at it through `primary_trip_id` —
duplicates stay in the table (they are that phone's own record) but are
excluded from every aggregate and from the trip list.

The pass runs *after* closing, not before: a pair is only detectable once both
phones' trips have distance and endpoints, and the second one may not have
closed until this same pass.

| Constant | Rationale |
| --- | --- |
| `COPRESENCE_LOOKBACK_S = 6h` | Both phones disconnect from the car within seconds of each other, so their trips close on the same pass or the next one. Six hours is slack for a hub that was down for the afternoon, while keeping the pairwise comparison over a handful of rows. |
| `COPRESENCE_MIN_OVERLAP = 0.6` | One car cannot carry two people along different roads, so a genuine pair overlaps almost entirely; the slack is for the phones starting and stopping their fixes at slightly different moments. |
| `COPRESENCE_ENDPOINT_M = 500.0` | Wide enough for two phones acquiring GPS at different moments as the car pulls away, tight enough that two cars leaving the same house for different places do not pair. |
| `COPRESENCE_DISTANCE_TOL = 0.25` | The same road measured by two phones differs by a few percent through fix timing alone; a quarter is generous and only rules out gross mismatches. Endpoints and timing agreeing while the distances do not means the two phones did not travel the same roads between them. |

Two trips from the *same* user that overlap are a recording fault, not two
occupants — the same phone cannot be a passenger in its own car — and
collapsing them would hide it.

Grouping (`_cluster_drives`) is by similarity to anything already in the group
rather than to a fixed representative, so three phones in one car land in one
group even where the first and last of them pair only through the middle one.
One journey is chosen per group, once: pairwise marking would let three phones
chain A→B→C, and A would resolve to a trip that is not itself a journey.

`_rank` decides which trip survives as the journey — the richer recording wins
(motion data first, then fix count), so the trip that is kept is the one that
can be scored at all. `trip_id` breaks a tie, only so that repeated passes
reach the same answer. A settled group keeps the primary it has: if a third
phone's trip closes later and outranks the current primary, promoting it would
leave the old primary's duplicates pointing at a trip that is no longer a
journey. Only trips still acting as journeys in their own right are candidates,
which is what keeps repeated passes from re-pairing a settled group.

## Leaderboard

`_LEADERBOARD_SQL` is deliberately a `LEFT JOIN` from `drivers`: a driver with
no trips yet is a row of nulls, not a missing name, because "registered but
hasn't driven" is a state worth showing. Duplicates are excluded here as
everywhere — the whole point of collapsing them is that one drive counts once,
for one driver.

Smoothness is distance-weighted (matching the single-driver figure in
`_user_stats`): a two-mile trip's score must not weigh as much as a fifty-mile
one when ranking how someone drives. Overall average speed is total distance
over total time for the same reason — a mean of per-trip means would overweight
short trips.

`MIN_LEADERBOARD_DISTANCE_M` gates ranking. The per-trip score is already
distance-normalised, so a single clean three-mile run scores as well as a
careful month of commuting and would take first place on a leaderboard that
ranked everyone. Drivers below the threshold are listed but unranked — held
back rather than hidden, because "not enough data yet" is the honest reading
and a missing name looks like a bug. Unranked drivers are ordered by how close
they are to qualifying rather than by an unearned score.

`events_per_100km` is shown alongside the score because "3 events in 60 miles"
is checkable in a way that a 0-100 number is not.

Unattributed totals are surfaced so the UI can say how much history is sitting
outside the table; a leaderboard that quietly omits half the driving invites
more trust than it has earned.

## Privacy

Recording is opt-in per presence user (`UserConfig.journeys_enabled`) — this
module persists movement history, which `presence_users.py` deliberately does
not. Raw track points and individual events are purged after
`TRACK_RETENTION_DAYS`; the per-trip summary rows (no coordinates) are kept
indefinitely.

Individual events go with the track rather than with the summary: each one is a
timestamped record of a moment, and the counts that make them worth keeping are
already denormalised onto the trip row.

Events carry no coordinates, so unlike the track they are not behind the admin
gate: "braked hard four minutes in" says how someone drove, which
`presence:read` already sees in aggregate, not where they were.

`_resolve_places` runs outside the DB thread because it consults the presence
and place managers; coordinates leave the database only long enough to be
turned into names, and only the names are written back.

## Drive tab (frontend)

`static/js/drive.js` renders three pieces on one page: journeys recorded by the
companion app's drive mode, the cheapest fuel stations near home or a typed
postcode, and a price-history chart drawn from the snapshots `fuel_history.py`
records at every search (daily median line over a min–max band).

It is an ES module — unlike `presence-settings.js` — because the chart goes
through the shared `chart-utils`/ECharts layer. It still exposes
`window.initDriveTab` for `main.js`'s tab listener.

### Acceleration RAG thresholds

Red is the phone's own event threshold (`MotionSampler.EVENT_ENTER_MPS2`, 3.5
m/s²): above it the sampler logged a discrete event, so the map agrees with the
event list by construction rather than by coincidence. Amber is the approach to
it — firm but not logged — which is the band worth showing a driver, because it
is where a habit is visible before it becomes an event.

**Change these together with the phone's constant, or the two stories stop
matching.**

### Interaction details

- The **driver picker** is present on every trip, not only low-confidence ones.
  Correcting a confident wrong guess is exactly the case where the score is most
  misleading, and hiding the control behind the hub's own certainty would make
  that the hardest one to fix.
- The **coaching note** gives one sentence on what to work on, from whichever
  event kind dominates. The counts alone say what happened; this says what to do
  about it, which is the point of showing them. It is withheld below three
  events — two hard stops on one trip is traffic, not a habit, and advice given
  on that evidence teaches drivers to distrust the rest of it.
- **Trip detail** is fetched once per trip per page load; the markup and Leaflet
  instance stay in place so collapsing and re-expanding a row costs nothing.
  Every trip map must be torn down before the host's `innerHTML` is replaced —
  Leaflet attaches listeners to `window` and `document`, not only to its
  container, so dropping the markup alone leaves those live.

## Fuel price history

`modules/fuel_history.py` persists what the fuel-price feeds said, and when we
asked.

The `uk-fuel-prices-api` package holds retailer data only in memory, and most
retailers publish just a daily number with no archive: once tomorrow's price
replaces today's, today's is gone. This module snapshots every station returned
by a Drive-tab query into DuckDB, so price trends — "is this station creeping
up?", "cheapest E10 seen this month" — become answerable.

**Storage.** `data/fuel_prices.duckdb`, a database dedicated to this module,
with all access through one dedicated worker thread that owns the connection.
DuckDB is single-writer per file, so this must never share journeys' or
telemetry's database.

**Dedupe.** Retailer feeds update roughly daily, but users may search many times
a day. One row per `(site_id, fuel, feed day)` — re-recording the same feed value
is a no-op, so history growth is bounded by stations × fuels × days regardless of
how often anyone searches.

**Privacy.** Rows describe petrol stations, not people. Where the user searched
from is deliberately **not** stored — only which stations came back, and their
prices.

## API and scopes

`routes/journey_routes.py` mirrors the presence scope model:

| Scope | Grants |
| --- | --- |
| `presence:read` | trip summaries, driving events, and aggregate stats — no coordinates |
| `admin` | additionally the raw track points, and deletion |

Track coordinates cross the same privacy boundary as the live presence map, so
they are gated the same way: `presence:read` tells you someone drove 12 miles at
an average of 31 mph; pinning the route to streets is an administrator's
capability.

**Driving events sit on the `presence:read` side of that line deliberately.**
They carry a time, a kind and a magnitude but no position, so they say how the
car was driven and not where — the same class of fact as the average speed and
harsh-event counts already in the summary, at finer resolution.

The driver roster and the leaderboard read at `presence:read` for the same
reason. *Editing* them is `admin`: attributing a drive to someone decides whose
record it lands on, which is a claim about a person rather than a view of one.

## Fuel prices

`modules/fuel_prices.py` finds the cheapest fuel near a location from the
[UK retailer open-data feeds](https://www.gov.uk/guidance/access-fuel-price-data)
via the `uk-fuel-prices-api` package.

That package fetches ~15 retailer JSON feeds (Asda, Tesco, BP, Shell, …) and
holds them in memory with an hour's cache; most retailers only refresh daily, so
that cadence loses nothing. This module wraps it with a refresh guard (one
refresh at a time, with callers sharing the result), postcode → coordinates via
postcodes.io (free, no key, and no logging of who asked), and "best nearby"
queries: stations within a radius selling the wanted fuel, sorted cheapest
first, each with a Google Maps link built from its postcode so a phone can
navigate to the winner in one tap.

Prices are re-fetched on demand, but each query's results are also snapshotted
into [fuel price history](#fuel-price-history) — the feeds publish only today's
number with no archive, so anything not recorded at query time is gone tomorrow.

### Fuel API

Centre resolution, in order of preference:

1. an explicit `?postcode=`, resolved via postcodes.io
2. an explicit `?lat=` and `?lon=`
3. the requesting household's home — the first presence user with one set

Prices themselves are public open data; the location the query centres on is
not. That is why the fallback is the home location every household member
already knows, and why any authenticated user may query while nothing about who
asked is stored.
