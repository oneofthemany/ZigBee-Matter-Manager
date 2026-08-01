# ZMM Presence — changelog

Every entry is a `versionName (versionCode)` pair matching `app/build.gradle.kts`.
The app shows both under the wordmark, so you can confirm from the phone which
build is running rather than inferring it from what you last typed.

Bump `versionCode` on **any** build you install anywhere. Android uses it, not
`versionName`, to decide whether an install is an upgrade — two different builds
sharing a code silently install over each other with no way to tell them apart
afterwards.

`build_release.py` reads the version out of the built APK (not out of the source
tree) and prints the transition on install, e.g. `1.0 (1) -> 1.1.0 (2)`.

---

## 1.4.0 (6)

Activity recognition, so a journey is the drive and nothing else.

- `ActivityMonitor` subscribes to Play Services activity transitions and
  `ActivityReceiver` records the current one. Drive mode is triggered by the
  car's Bluetooth, which answers "near the car", not "the car is moving" —
  so sitting in a parked car recorded a journey of GPS drift, and the walk
  from the space to the door was inside the recorded distance.
- Every drive fix now carries the activity. The hub keeps the fix but excludes
  walking / running / on-foot / cycling from distance, speed, behaviour and the
  drawn track. `still` is deliberately counted: a car at a red light reports
  it, and dropping those would delete the idling being measured.
- Drive mode stops on leaving the vehicle rather than waiting for Bluetooth to
  drop, which head units hold while the car sits parked.
- Needs ACTIVITY_RECOGNITION, requested after arming. Refusing it changes
  nothing except that journeys may again include time parked or on foot.
- It cannot tell a driver from a passenger — both are IN_VEHICLE.

## 1.3.1 (5)

- Fixed the fuel screen never appearing in Android Auto. `PlaceListMapTemplate`
  requires `androidx.car.app.MAP_TEMPLATES`, which the manifest never declared;
  the host refuses the template and drops the app, with nothing shown on the
  phone. Enabling Unknown sources could not work around it.
- `MotionSampler` now reports `horiz_peak` — peak horizontal acceleration,
  which needs no forward axis and so covers a whole drive. The hub's route map
  banded on `long_peak`/`lat_peak` alone, which the phone only sends once
  calibration converges, so drives were drawn part green and the rest grey.

## 1.3.0 (4)

Drive mode now requires a hub reachable from outside the home network.

- `Prefs.isPublicUrl()` classifies the stored hub address. RFC1918, loopback,
  link-local, CGNAT (`100.64/10`), `*.local`, single-label hosts and IPv6
  ULA/link-local all count as home-network only.
- Gated in three places, not just the UI: the Drive mode card (button disabled,
  pill reads `Unavailable`, card explains why), `DriveService.onStartCommand`
  (so a pairing made before Remote Access existed cannot start a GPS-holding
  foreground service that posts nowhere), and the Android Auto fuel screen.
- Unit tests for the classifier — its negative cases can't be reached from a
  phone without re-pairing against each address in turn. Adds `junit` as a
  `testImplementation` dependency; run with `./gradlew :app:testDebugUnitTest`.
- `build_release.py` now runs the unit tests as part of a build and fails the
  run if any fail (`--skip-tests` opts out). It reports the case count from the
  JUnit XML rather than trusting Gradle's exit status, since a test task with
  nothing to run succeeds identically to one that ran and passed.

## 1.2.0 (3)

- Theme toggle in the hero strip, cycling system → light → dark. The choice is
  stored in `Prefs.themeMode` as an `AppCompatDelegate.MODE_NIGHT_*` constant
  and survives "Forget this hub".
- Fixed content scrolling under the status bar: `clipToPadding` is back to its
  default, which clips scrolled content at the inset while leaving the
  honeycomb running to the screen edge.

## 1.1.0 (2)

Hive theme — the app now looks like the manager dashboard rather than stock
Material.

- Hive palette from `static/css/hive-tokens.css`, transcribed to
  `res/values/colors.xml` with a night variant. Honey accents, navy/wax ink,
  warm-paper light mode.
- Honeycomb backdrop (`HoneycombDrawable.kt`) — tiled hex outlines with a honey
  glow, drawn as a path so it stays crisp at any size and recolours per theme.
- Content grouped into outlined cards with the dashboard's `⬡` mono micro-label
  headers.
- Status pills (paired / armed / permissions / drive mode) so the screen reads
  at a glance down the right-hand edge. Foreground-only location now shows as
  `Partial` rather than passing as granted.
- Custom hero strip replaces the system ActionBar; version and build shown under
  the wordmark.
- Filled buttons pinned to raw honey with dark ink in both themes, matching the
  dashboard's button treatment.

## 1.0 (1)

Initial release. Pairing, certificate pinning, geofence, heartbeat, drive mode,
Android Auto fuel screen.
