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

Start with **Internal App Sharing**. It is the lower-commitment of the two, and
the reusable version code matters more than it sounds — you will rebuild this
screen more than once, and bumping `versionCode` on every attempt gets old.

Move to the Internal Test Track once the screen is right and you want it to
update itself.

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

Leave `android.hardware.type.automotive` at `required="false"`. That is correct
for a phone app that projects to Android Auto, and the manifest comment already
explains why. `required="true"` targets **Android Automotive OS** — a different
product, with its own track, its own screenshots, and a much heavier review.
Not what you want.

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
./gradlew bundleRelease
# app/build/outputs/bundle/release/app-release.aab
```

`versionCode` must strictly increase on every upload — Play rejects a reused
one. It is at **7** in `app/build.gradle.kts`; bump it and `versionName`
together, per the CHANGELOG's rule.

1. Console → **Create app**. Name it, pick *App*, *Free*, accept declarations.
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

## 8. Deadline you need to know about

**From 31 August 2026, new apps and app updates must target API 36** to be
accepted by Play ([target API requirements](https://developer.android.com/google/play/requirements/target-sdk)).

This project is on `targetSdk = 35` / `compileSdk = 35`. As of this writing that
is **under a month away**. Two consequences:

- Uploading before the deadline works as-is.
- Any upload after it needs `compileSdk = 36` and `targetSdk = 36` in
  `app/build.gradle.kts`, plus whatever Android 16 behaviour changes bite.

An extension to 1 November 2026 can be requested from the Console if you need
the room. Existing apps separately need to target 35+ to stay available to new
users, which this already satisfies.

Bumping to 36 is not a one-line change to make blind — do it as its own piece of
work with a device to test on, not in the middle of a release.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| Install from the share link fails, "app not installed" | The sideloaded copy is still there and is signed with a different key. Uninstall it first. |
| App still absent from the car launcher | The Android Auto form factor is not enabled, or no artifact has been released to a track yet. §7. |
| Play rejects the AAB: version code already used | Internal Test Track requires a strictly increasing `versionCode`. Internal App Sharing does not — if you are iterating, use that instead. |
| "App bundle expected, APK found" | The track needs `bundleRelease`, not `assembleRelease`. §6. |
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
