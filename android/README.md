# ZMM Presence — Android companion

A minimal, self-hosted presence app. It talks to **your hub only** — no
accounts, no third-party service, no analytics.

## What it does

Arms a **geofence** at your home (fetched from the hub's presence config) and
POSTs a fix to `/api/presence/users/<user_id>/fix` on ENTER/EXIT. The OS delivers
those transitions even when the app is killed, so there is no persistent
notification and effectively no battery cost.

It does *not* stream your location. It reports two events: you arrived, you left.

## Security model

The phone holds a **scoped bearer token**, not your password:

| Scope | Why |
|---|---|
| `presence:read:<user>` | fetch **its own** home lat/lon/radius to arm the geofence |
| `presence:write:<user>` | report **its own** fixes |

That token can do nothing else — it cannot read other users' locations, list the
household, or touch a single device. Revoke it per-phone in
**Settings → Auth → Tokens** (issue it with a label like "Sean's Pixel" and the
device id the app shows you).

Do NOT give the phone the unscoped `presence:read` — that means "read *every*
user's location", which is exactly what you don't want on a device you can lose.

### Transport: HTTPS with certificate pinning

The hub must be `https://`. A URL typed without a scheme is assumed to be
https, and an explicit `http://` is **refused at pairing** — the token rides on
every request, and putting it on the wire in clear text is not a tradeoff worth
offering.

How the hub is authenticated depends on how it is reached, decided once at
pairing (`CertPin.kt`):

| Your hub | Mode | What happens |
|---|---|---|
| Behind a tunnel with a real certificate | **System** | Ordinary CA validation. No fingerprint prompt, no pin stored. |
| Direct on the LAN, self-signed | **Pinned** | You confirm a fingerprint; only that key is accepted thereafter. |

Neither mode trusts the phone's **user CA store**, which is what would let any
CA installed on the device intercept this app's traffic. Publicly-issued
certificates are deliberately not pinned — they rotate on renewal, and a pin
would break every cycle while reporting it as interception.

In pinned mode:

1. At pairing the app shows the certificate's SHA-256 fingerprint.
   **Compare it against your hub before accepting.** This is the one step
   nothing can verify for you.
2. That key is stored, and every later connection must present it.
3. The pin is checked during the TLS handshake, before the `Authorization`
   header is written — a server that fails the check never receives the token.

Print the fingerprint on the hub to compare against:

```bash
openssl x509 -in cert.pem -noout -pubkey \
  | openssl pkey -pubin -outform der \
  | openssl dgst -sha256 -c
```

We hash the public key (SPKI), not the certificate, so **renewing** the cert
with the same key keeps working. Regenerating with a **new key** breaks the pin
on purpose: use **Forget**, then pair again and re-check the fingerprint. A pin
mismatch and a real interception are indistinguishable from the phone, which is
why it fails loudly instead of recovering silently.

## Requirements

- **Google Play Services** — the Geofencing API needs `play-services-location`.
  On a de-Googled phone this app will not work; there is no OS geofencing without it.
- Android 8+ (`minSdk 26`).
- The hub reachable from the phone (LAN, VPN or your remote-access tunnel).
  Use HTTPS if it's exposed; see "Cleartext" below.

## Setup

1. **Hub:** create the presence user — Settings → Auth → Users → edit your user →
   tick **Mobile presence**. Then set home lat/lon and radius in the Presence tab.
2. **Hub:** Settings → Auth → Tokens → new token for that user, scopes
   `presence:read:<user>` and `presence:write:<user>`. Copy it once — it is shown
   once.
3. **Phone:** open the app, enter hub URL + user id + token, press **Pair**.
4. Grant location — Android asks in two steps by design:
   - "While using the app" first,
   - then **Allow all the time**, which the OS only offers on a second, separate
     prompt. A geofence does not survive without it.
5. Press **Arm geofence**.

## Building

> **Building this yourself? See [BUILDING.md](BUILDING.md)** — full walkthrough
> including signing keys, hub prerequisites, verifying the certificate
> fingerprint, and troubleshooting. The summary below assumes you already know
> Android tooling.

Open `android/` in Android Studio and Run. First sync downloads Gradle and the
Play Services dependency.

There is no `gradle-wrapper.jar` checked in (it's a binary). Android Studio will
offer to create the wrapper on first open, or run `gradle wrapper` if you have
Gradle on PATH.

From the command line, `JAVA_HOME` must point at a JDK 17–21; Gradle 8.13
rejects newer ones. Studio's bundled runtime works:

```bash
export JAVA_HOME=~/.local/share/JetBrains/Toolbox/apps/android-studio/jbr
./gradlew assembleDebug      # app/build/outputs/apk/debug/app-debug.apk
```

### Release builds

Release APKs need a signing key. Generate your own — it is never checked in,
and no key ships with this repo:

```bash
cd android
keytool -genkeypair -v -keystore zmm-release.jks \
  -alias zmm -keyalg RSA -keysize 4096 -validity 10000
cp keystore.properties.example keystore.properties
# then edit keystore.properties with the passwords you just chose
./gradlew assembleRelease    # app/build/outputs/apk/release/app-release.apk
```

`keystore.properties`, `*.jks` and `*.keystore` are gitignored. Back the
keystore up somewhere outside the repo: **losing it means you can never update
an installed app again**, only uninstall and reinstall. Anyone who has it plus
its password can ship an update Android accepts as genuinely yours.

Without `keystore.properties` the release build still succeeds but emits an
unsigned APK (and warns) — the phone will refuse to install it.

## Cleartext HTTP

Not supported, by design — see "Transport" above. `usesCleartextTraffic` is off
and pairing refuses `http://` URLs, because the bearer token would be readable
by anyone on the network. Debug builds permit cleartext to a few fixed LAN and
emulator addresses (`src/debug/res/xml/network_security_config.xml`) for
development only; that file never ships in a release APK.

## Known limits

- Geofence transitions can lag **1–2 minutes**; the OS batches them to save power.
- After a reboot, geofences are re-armed by `BootReceiver` — but Android only
  delivers `BOOT_COMPLETED` if the app has been opened at least once since install.
- Aggressive OEM battery managers (Xiaomi, Samsung, Huawei) can kill geofences.
  If transitions stop, exempt the app from battery optimisation.
- If the hub is unreachable when a transition fires, that fix is **lost** — there
  is no retry queue. The next transition corrects it.
