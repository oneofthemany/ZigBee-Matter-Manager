# ZMM Presence — Android companion

A minimal, self-hosted replacement for OwnTracks. It talks to **your hub only**.

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

Open `android/` in Android Studio and Run. First sync downloads Gradle and the
Play Services dependency.

There is no `gradle-wrapper.jar` checked in (it's a binary). Android Studio will
offer to create the wrapper on first open, or run `gradle wrapper` if you have
Gradle on PATH.

## Cleartext HTTP

`usesCleartextTraffic` is **off**. If your hub is plain `http://` on the LAN, add
your host to `res/xml/network_security_config.xml` — the file has a commented
example. Don't enable cleartext globally.

## Known limits

- Geofence transitions can lag **1–2 minutes**; the OS batches them to save power.
- After a reboot, geofences are re-armed by `BootReceiver` — but Android only
  delivers `BOOT_COMPLETED` if the app has been opened at least once since install.
- Aggressive OEM battery managers (Xiaomi, Samsung, Huawei) can kill geofences.
  If transitions stop, exempt the app from battery optimisation.
- If the hub is unreachable when a transition fires, that fix is **lost** — there
  is no retry queue. The next transition corrects it.
