# Getting the fuel screen into a real car

This document exists for one reason: **Android Auto will not load a sideloaded
car app.** The fuel screen (`car/FuelCarAppService.kt`) works perfectly over the
Desktop Head Unit and is invisible in an actual vehicle, and no manifest change
or developer toggle alters that. The gate is on *where the app was installed
from*, not on anything in the app.

If you only want to develop against the car UI, you do not need any of this —
see `run_dhu.sh` and BUILDING.md. This is only for a persistent entry in a real
head unit's launcher.

---

## 1. Why the developer toggle doesn't work

Android Auto has an **Unknown sources** developer setting, and it does not apply
here. From [Google's testing documentation](https://developer.android.com/training/cars/testing),
verbatim:

> This setting applies to media, messaging notifications, and parked apps but
> doesn't apply to apps built using the Android for Cars App Library.

Our fuel screen is a `CarAppService` — an Android for Cars App Library app, the
excluded case. The manifest is correct, the service is registered, the
`MAP_TEMPLATES` permission is granted, and the car still ignores it. Nothing is
broken. [Google's own guidance](https://developer.android.com/training/cars/testing)
names Internal App Sharing and the Internal Test Track as the routes.

That is also why the launcher entry vanishes when the DHU disconnects. The app
is not being *removed*; a developer projection session is the only context in
which it was ever permitted, and persisting means being trusted, which comes
from the install source.

---

## 2. What this costs you, stated up front

This project deliberately ships as source rather than a binary (BUILDING.md §1).
Routing builds through Google is in tension with that, so be clear-eyed:

| | |
|---|---|
| **Money** | Play Console registration is a **one-time US$25**. There is no free tier. |
| **A second signing identity** | Neither route serves the APK you signed with `zmm-release.jks`. Both re-sign. |
| **Your pairing** | Because the signature changes, the Play copy **cannot install over** your sideloaded one. Android refuses it as a different app. You uninstall first — and `allowBackup="false"` means hub URL, token, cert pin, geofence places and car Bluetooth pairing are all lost. Have a new token ready before you start. |

Nothing here forces the app public. Internal testing is not a store listing, is
not searchable, and does not require the 12-testers-for-14-days rule that gates
production for individual accounts.

---

## 3. Which route

| | Internal App Sharing | Internal Test Track |
|---|---|---|
| **Setup** | Upload page, nothing else | App must exist in Console with basic details |
| **Artifact** | APK or AAB | AAB (`bundleRelease`) |
| **Version code** | Reusable — upload `7` forever | Must strictly increase every upload |
| **Signing** | Re-signed with a per-app *Internal App Sharing* key. **Not** Play App Signing. | Play App Signing. Google holds the app signing key; `zmm-release.jks` becomes your *upload* key. |
| **Distribution** | A link. Tester opens it, installs. | Testers by email list, updates arrive through Play |
| **Good for** | Iterating on the car screen | Living with it day to day |

**Read §8 before choosing.** Until 31 August 2026 this was a free choice, and
the advice was to start with Internal App Sharing: lower commitment, and the
reusable version code matters more than it sounds when you are rebuilding the
screen repeatedly. The target API deadline changed the calculus.

**Go straight to the Internal Test Track if you have not uploaded yet.** It is
the route that establishes the app record the Android Auto form factor needs
(§7), it is the one you want for living with the screen day to day, and it is
the one the deadline applies to. Internal App Sharing does not create an app in
the Console, so time spent there is not progress toward a car launcher entry.

Whether Internal App Sharing enforces the target API requirement is not
documented either way. Do not find out at the deadline.

Internal App Sharing remains the better tool once the app exists and you are
iterating on the screen — reusing version code `7` forever is worth having.

---

## 4. Prerequisites (both routes)

1. **A Play Console account** — <https://play.google.com/console>, US$25 once.
2. **A signing key.** You already have one: `zmm-release.jks` with
   `keystore.properties` (BUILDING.md §5.1). Both routes accept it; Internal App
   Sharing accepts literally any key, including the debug one.
3. **The car metadata in the manifest.** Already present:

   ```xml
   <meta-data
       android:name="com.google.android.gms.car.application"
       android:resource="@xml/automotive_app_desc" />
   ```

   This is one of the two things Play checks when you opt into Android Auto.

**Do not declare `android.hardware.type.automotive` at all** — not even with
`required="false"`. Play rejects the bundle outright:

> The app cannot declare 'android.hardware.type.automotive' device feature and
> 'com.google.android.gms.car.application' metadata at the same time.

The two declarations describe different products. The feature marks an app as
built for **Android Automotive OS**, the embedded platform — a different track,
its own screenshots, a much heavier review. The metadata marks an app as
projecting to **Android Auto** from a phone, which is this one. Play permits
either, never both, and the check is on the declaration being *present*, so
`required="false"` is not the safe middle ground it looks like.

The manifest no longer declares it, and carries a comment saying why so it does
not get helpfully re-added.

---

## 5. Route A — Internal App Sharing

Build. Version code does not matter, so no bump needed:

```bash
cd android
export JAVA_HOME=~/.local/share/JetBrains/Toolbox/apps/android-studio/jbr
./gradlew assembleRelease
```

Or `python3 build_release.py`, which additionally proves the APK is genuinely
signed and that the debug network config did not leak into the release variant
— worth having for something you are about to hand to Google.

Then:

1. Open the [internal app sharing upload page](https://play.google.com/console/internal-app-sharing).
2. **Upload** → `app/build/outputs/apk/release/app-release.apk`.
3. Give it a version name you will recognise (`1.4.1 fuel screen`).
4. Copy the generated link.
5. Open that link **on the phone**, signed into the same Google account. Install.

Uninstall the sideloaded copy first — the signatures differ and the install will
be rejected otherwise. See §2 about the pairing.

The tester account must be allowlisted for internal app sharing under Console →
Settings → License testing.

---

## 6. Route B — Internal Test Track

The track needs an **App Bundle**, not an APK:

```bash
python3 build_release.py --aab
# app/build/outputs/bundle/release/app-release.aab
```

That wraps `./gradlew bundleRelease` (which works standalone if you prefer) and
adds the checks that matter for an upload: that the bundle is signed with your
key and not the debug one, that the debug network config did not leak into it,
and that the pinning guards compiled in. It prints the `versionCode` it built,
which is the one thing Play refuses an upload over without warning you first.

`versionCode` must strictly increase on every upload — Play rejects a reused
one. It is at **7** in `app/build.gradle.kts`; bump it and `versionName`
together, per the CHANGELOG's rule.

1. Console → **Create app**. Name it, pick *App*, *Free*, accept declarations.

   **The package name binds here and is permanent.** It comes from the first
   bundle the app record accepts, and it can never be changed or reused
   afterwards — not by editing the record, not by deleting it. If the Console
   is asking for a package name that is not `com.zmm.presence`, the record is
   bound to something else and this bundle will never upload to it. Fix the
   record, not the bundle: delete the app in the Console (possible only while
   nothing has been rolled out to any track) and create it again. Changing
   `applicationId` to match instead makes a different app — new identity on the
   phone, and the sideloaded copy's pairing is not inherited.

2. **Testing → Internal testing → Create new release**.
3. Upload the `.aab`. Accept Play App Signing when prompted — this is the point
   at which `zmm-release.jks` becomes your *upload* key and Google generates the
   app signing key.
4. Add testers by email under the **Testers** tab, save, copy the opt-in URL.
5. Fill the blocking items Play lists: app access (note that the app needs a
   hub and a token, and supply test credentials or explain that it cannot be
   exercised without one), content rating, data safety, target audience.
6. Roll out to internal testing.
7. On the phone, open the opt-in URL, accept, then install from Play.

**Data safety is not a formality here.** This app collects background location.
Declare it: location, collected and transmitted, to the user's own server.
Getting this wrong is a rejection, and honestly declaring it is easy — the app
sends latitude, longitude, accuracy and a timestamp to one endpoint you typed in
yourself, and nothing else. There is no analytics or crash reporting to declare.

---

## 7. Turning on Android Auto in the Console

Required for the car to see the app at all, on either route:

1. Console → **Advanced settings → Form factors**.
2. **Add form factor → Android Auto**.
3. It checks two things: the `com.google.android.gms.car.application` metadata
   (already present), and that you have released an Android Auto artifact to a
   testing track.

Android Auto artifacts get a **detailed review** against the
[car app quality guidelines](https://developer.android.com/docs/quality-guidelines/car-app-quality).
Review is blocking for production and open testing, and not for internal
testing — so the internal track is also the fastest way to find out whether the
fuel screen passes. Expect the result by email.

The relevant guidelines for a POI app are about not being distracting: the
screen must be a template, must not animate, and must not require more than a
glance. `PlaceListMapTemplate` is already the sanctioned shape for this, which
is most of the work done.

---

## 8. The target API deadline, and the order it forces

This project is on `targetSdk = 35` / `compileSdk = 35`, and **31 August 2026**
is the date that matters ([target API requirements](https://developer.android.com/google/play/requirements/target-sdk)).

Two separate rules apply, and confusing them is the trap:

| Rule | Requires | Effect on this app at 35 |
|---|---|---|
| New apps and app **updates**, from 31 Aug 2026 | API **36** | An upload *after* the date is rejected |
| Existing apps, to stay available to **new users** | API **35** | Already satisfied, indefinitely |

### The sequencing

**Get the bundle uploaded at 35 before 31 August 2026.** An upload accepted
before the deadline stays accepted, and 35 already satisfies the availability
rule — so the app keeps working and keeps installing after the date passes.
Only the *next update* needs 36.

Miss the date and the position is worse than it sounds: the very first upload
now needs 36, so the fuel screen does not reach the car until the SDK bump is
done and tested. Uploading first turns the bump into unhurried work; not
uploading makes it a blocker.

Bumping to 36 is not a one-line change to make blind — do it as its own piece of
work with a device to test on, not in the middle of a release. That is precisely
why it is worth getting an accepted upload behind you first.

An extension to 1 November 2026 can be requested from the Console if the bump
cannot be done in time.

### One thing not to misread

The requirements table lists **Android Automotive OS** apps as needing only API
35. That exemption does **not** apply here. This is a phone app that projects to
Android Auto, which is the general case at 36 — see §4 on why
`android.hardware.type.automotive` is not declared at all. Reading the AAOS row
as cover for staying on 35 is the same confusion §4 warns about, arriving by a
different door.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| Install from the share link fails, "app not installed" | The sideloaded copy is still there and is signed with a different key. Uninstall it first. |
| App still absent from the car launcher | The Android Auto form factor is not enabled, or no artifact has been released to a track yet. §7. |
| Play rejects the AAB: version code already used | Internal Test Track requires a strictly increasing `versionCode`. Internal App Sharing does not — if you are iterating, use that instead. |
| "App bundle expected, APK found" | The track needs `bundleRelease`, not `assembleRelease`. §6. |
| Play rejects the upload: cannot declare `android.hardware.type.automotive` and `com.google.android.gms.car.application` together | The AAOS feature declaration must be absent entirely, `required="false"` included. §4. |
| Play rejects the upload: "needs to have the package name ..." | The Console app record is bound to a different `applicationId` than the bundle's, permanently. §6. |
| Play rejects the upload over target API level | Uploading after 31 Aug 2026 on `targetSdk = 35`. Needs the bump to 36, or a Console extension. §8. |
| Fuel screen appears, then the app is dropped by the host | A template the POI category does not permit, or the missing `androidx.car.app.MAP_TEMPLATES` permission. Both are already correct in this repo — suspect a manifest edit. |
| Review rejected on driver distraction | Car app quality guidelines. The screen must stay a template; anything custom-drawn fails. |

---

## 10. What this does not get you

A public listing. None of the above puts the app on the store, and nothing here
obliges you to. If you ever do want that, production release is a separate
decision with a real review, a store listing, and — for an individual developer
account — a 12-tester, 14-day closed test first.

For the stated goal, a persistent fuel screen in your own car, internal testing
is the end of the road and not a step towards publishing.
