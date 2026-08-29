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

## 1.5.0 (8)

Setup is a sequence now, not a wall of five open cards.

- The four setup sections are an accordion: one body open at a time, and it is
  the first step that isn't finished. Finishing a step collapses it to a summary
  line and opens the next, so the screen only ever asks for one thing.
- A collapsed step shows the one fact you would have opened it to check — the
  hub URL, the granted permissions, the geofence, the paired car — and hides the
  controls behind it.
- Each step header carries a hex chip: its number while pending, a tick once
  done. Progress reads down the left edge the way state already read down the
  right. The ⬡ prefix came off those four labels, since the chip now carries the
  motif; "This device" keeps it and stays open, because it is not a step.
- Any header reopens its step. The screen outlives setup — it is also where you
  disarm, change car or repoint the hub — so nothing is locked behind having
  finished.
- Drive mode is skipped when the hub is LAN-only rather than opened onto a
  disabled button, and the battery exemption does not gate the permissions step:
  Arm needs only foreground and background location, and a declined system
  dialog would otherwise wedge the sequence. It is called out on the collapsed
  summary instead.

## 1.4.1 (7)

The pairing screen told the truth about the wrong thing, and read bottom-up.

- The hub pill said "Paired" whenever the three fields were non-empty. A
  mistyped or revoked token still fills fields, so the pill sat green while
  every request came back 401 and the geofence quietly refused to arm — the
  screen contradicted itself and the pill was the part that was wrong. It now
  has three states, and "Paired" requires the hub to have actually answered:
  `Prefs.verified` is set only by a successful `fetchHome`, cleared when a
  credential is edited or the hub rejects one.
- A 401/403 is now distinguishable from any other failure (`Result.Err.authFailed`),
  so a rejected token drops the paired state but a timeout on a train does not.
- The 401 message names the real trap: the hub shows a token's plaintext once,
  at creation, so the id in its token list is not the token.
- Permissions moved above Geofence. Arm stays disabled until location is
  granted, and the grant buttons were below the button they unlock — arming
  meant scrolling past a dead control, granting, then scrolling back. Cards now
  run in dependency order.
- The status line is pinned below the scroll instead of sitting at the bottom
  of it. Pressing Pair at the top used to write its answer below the fold.
  Hidden until there is something to say, so it costs no height when idle.

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
