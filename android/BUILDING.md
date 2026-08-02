# Building ZMM Presence from source

This app is distributed as source rather than a prebuilt APK, on purpose. It
asks for background location and holds a token for your hub — that is exactly
the kind of app you should not install as an opaque binary from a stranger.
Building it yourself means the thing on your phone is the thing you read.

It is a normal Android project. Expect 10–15 minutes the first time, most of it
downloads.

---

## 1. What you need

| | |
|---|---|
| **Android Studio** | Any recent version. Bundles the SDK and a suitable Java runtime. |
| **JDK 17–25** | Only for command-line builds. Studio's bundled runtime already qualifies. |
| **A hub on HTTPS** | The app refuses plaintext. See [Hub prerequisites](#6-hub-prerequisites). |
| **Google Play Services on the phone** | Non-negotiable: OS geofencing lives there. On a de-Googled phone this app cannot work. |

Android 8.0 (API 26) or newer, per `minSdk` in `app/build.gradle.kts`.

---

## 2. The scripted path

`build_release.py` does everything in this document and then checks the result.
It needs only Python 3.9+ and Android Studio installed.

```bash
cd android
python3 build_release.py --setup     # first time: creates the signing key
python3 build_release.py             # thereafter: build + verify
```

It finds a usable JDK (a JDK outside the range Gradle supports is the most
common first failure), locates the SDK, builds, and then verifies:

- the APK is **actually signed** — a release build with no signing config still
  "succeeds" and emits an unsigned APK with only a warning, which you would
  otherwise discover when your phone refuses to install it;
- it was **not** signed with the Android debug key, which would be wrong to
  distribute;
- the shipped network security config does not trust user CAs and does not
  permit cleartext — i.e. the permissive `src/debug/` override did not leak
  into the release variant;
- the pinning guards are compiled into the shipped dex;
- the keystore and its password file are gitignored.

Exit status is 0 only if everything passed, so it can gate a release.

Passwords are read with `getpass` and handed to `keytool` through the
environment, never as command-line arguments — `ps` shows every argument of
every running process, including to other users on the machine.

Other flags: `--verify-only` re-checks an existing APK without rebuilding,
`--debug` builds the debug variant instead.

The rest of this document explains what the script automates, in case you would
rather do it by hand or need to debug it.

## 3. The quick path (Android Studio)

1. **File → Open** → select the `android/` directory (not the repo root).
2. Wait for Gradle sync. First run downloads Gradle and Play Services.
3. **Build → Build Bundle(s) / APK(s) → Build APK(s)**.
4. Click **locate** in the notification.

That produces a **debug** APK at `app/build/outputs/apk/debug/app-debug.apk`.
It installs and works fine — signed with Android's automatic debug key. For a
personal build that's genuinely enough; §5 covers when it isn't.

---

## 4. Command line

`JAVA_HOME` must point at a JDK 17–25. **Gradle rejects a JDK newer than the
wrapper's version supports**, so a bleeding-edge system Java fails with a
version error until you set this. Studio's bundled runtime is the easiest
source, and is what the IDE itself uses:

```bash
# Linux (Toolbox install)
export JAVA_HOME=~/.local/share/JetBrains/Toolbox/apps/android-studio/jbr
# Linux (standalone)      export JAVA_HOME=/opt/android-studio/jbr
# macOS                   export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
```

Then:

```bash
cd android
./gradlew assembleDebug
```

**If `./gradlew` does not exist**, the wrapper isn't checked in (it contains a
binary `.jar`). Open the project in Studio once and it generates it, or if you
have Gradle installed:

```bash
gradle wrapper --gradle-version 8.13
```

**If Gradle can't find the SDK**, create `android/local.properties`:

```properties
sdk.dir=/home/you/Android/Sdk
```

Studio writes this automatically; it's gitignored because it's specific to your
machine. On macOS it's usually `/Users/you/Library/Android/sdk`.

---

## 5. Signed release builds

A debug APK is fine for yourself. Build a release APK when you want:

- to **share** the build with someone (debug builds are `debuggable`, meaning
  anything with ADB access can inspect the app's memory — including the token);
- a smaller, faster binary;
- a signing identity **you** control, so you can ship updates the phone accepts
  as continuous with what's installed.

### 5.1 Generate your signing key

No key ships with this repo, and there is no shared one. Yours is yours.

```bash
cd android
keytool -genkeypair -v -keystore zmm-release.jks \
  -alias zmm -keyalg RSA -keysize 4096 -validity 10000 -storetype PKCS12
```

`keytool` prompts for a password interactively, which keeps it out of your shell
history. The name/organisation questions end up in the certificate but nothing
validates them — `CN=ZMM Presence` and blanks elsewhere is fine. Use the same
password for key and store; PKCS12 effectively requires it.

`-validity 10000` is about 27 years, deliberately. **An expired signing key
means you can no longer ship updates.**

> **Back this file up somewhere outside the repo.**
>
> Lose it and you can never update an installed app again — only uninstall and
> reinstall, losing the pairing. Anyone who has it *and* its password can build
> an update that Android accepts as genuinely yours. It is the app's identity,
> not just a build input.

### 5.2 Point the build at it

```bash
cp keystore.properties.example keystore.properties
```

Edit it:

```properties
storeFile=zmm-release.jks
storePassword=the-password-you-chose
keyAlias=zmm
keyPassword=the-password-you-chose
```

`storeFile` resolves relative to `android/`. Both `keystore.properties` and
`*.jks` are gitignored — confirm with `git status` before committing anything.

### 5.3 Build

```bash
./gradlew assembleRelease
```

Output: `app/build/outputs/apk/release/app-release.apk`.

**If the filename says `app-release-unsigned.apk`**, Gradle did not find
`keystore.properties`. The build prints a warning rather than failing, so it is
easy to miss:

```
WARNING: android/keystore.properties not found — the release APK will be
UNSIGNED and cannot be installed.
```

Check the file is in `android/`, not `android/app/`.

### 5.4 Verify the signature

Worth doing once, so you know it's genuinely signed by your key:

```bash
"$ANDROID_HOME"/build-tools/*/apksigner verify --print-certs \
  app/build/outputs/apk/release/app-release.apk
```

You should see your certificate's subject and a SHA-256 digest. Record that
digest — it is how you or anyone else can later confirm a given APK came from
you.

---

## 6. Hub prerequisites

The app talks to two endpoints and nothing else. Before pairing:

1. **Serve the hub over HTTPS.** The app refuses `http://` — a bearer token on
   plaintext is readable by anyone on the network. A self-signed certificate is
   expected and fine; see §7.
2. **The certificate must cover the address you type.** If you connect by IP,
   the IP must be in the certificate's `subjectAltName`, not just the CN.
   Verify:

   ```bash
   openssl s_client -connect YOUR_HUB:8000 </dev/null 2>/dev/null \
     | openssl x509 -noout -text | grep -A1 "Subject Alternative Name"
   ```

   You want a line containing `IP Address:192.168.1.x` (your hub's address).
   Missing, and every connection fails hostname verification no matter what
   else is correct.
3. **Enable presence for your user** — Settings → Auth → Users → edit user →
   tick **Mobile presence**. Then set home lat/lon and radius in the Presence
   tab.
4. **Issue a scoped token** — Settings → Auth → Tokens. It needs exactly:

   | Scope | Why |
   |---|---|
   | `presence:read:<user>` | read **its own** home location, to arm the geofence |
   | `presence:write:<user>` | report **its own** position |

   Do **not** grant the unscoped `presence:read`. That reads *every* user's
   location — precisely what you don't want on a device you can lose. Label the
   token per phone so you can revoke one without disturbing the others.

---

## 7. What to check before you trust this build

If you're reading the source before installing, these are the parts that matter:

**Where it connects.** `HubClient.kt` — two endpoints, both on the hub URL you
type. There is no analytics, crash reporting or telemetry anywhere in this app.

**What it sends.** `GeofenceReceiver.kt` posts latitude, longitude, accuracy and
a timestamp on geofence crossings only. Not a continuous track.

**How it authenticates the hub.** `CertPin.kt`, and it depends on how your hub
is reached. The app decides once, at pairing, by attempting an ordinary
validated connection:

| Your hub | Mode | What happens |
|---|---|---|
| Behind a tunnel / reverse proxy with a real certificate | **System** | Ordinary CA validation. No fingerprint prompt, no pin stored. |
| Direct on the LAN, self-signed certificate | **Pinned** | You confirm a fingerprint; only that key is accepted thereafter. |

Neither mode ever trusts the phone's **user CA store**, which is what would let
any CA installed on the device intercept the app's traffic.

Pinning is not applied to publicly-issued certificates on purpose. Those rotate
on renewal, and a pin would break at every cycle while reporting it as
interception — training you to click through the one warning that should never
be routine.

A TLS handshake failure means self-signed. A timeout or refused connection is a
network error and is rethrown, so an unreachable hub is never quietly turned
into "self-signed, please accept this key".

In pinned mode you are shown a fingerprint at pairing. **Compare it against
your hub:**

```bash
openssl s_client -connect YOUR_HUB:8000 </dev/null 2>/dev/null \
  | openssl x509 -noout -pubkey \
  | openssl pkey -pubin -outform der \
  | openssl dgst -sha256 -c
```

That must match what the dialog shows.

> **Known limitation, stated plainly.** Pinned mode is trust-on-first-use, the
> SSH model. The first connection is taken on faith — someone intercepting at
> that exact moment would get pinned instead of your hub. Comparing the
> fingerprint closes that window, which is why the dialog is worded bluntly
> rather than as a routine confirmation.

The pin is checked during the TLS handshake, before the `Authorization` header
is written, so a server failing the check never receives your token. If trust
was never established, requests refuse outright rather than falling back to
plain CA validation — a failed pairing must not silently become an
unauthenticated one.

**Which URL to pair with.** Use your **public/tunnel URL**, even at home. A
geofence EXIT fires exactly when you leave the LAN, at which point a private
address like `192.168.1.x` is unreachable and the report cannot be delivered.
One URL everywhere is also one trust mode everywhere.

**Dependencies.** One third party: `play-services-location`, unavoidable
because OS geofencing lives in Play Services. Everything else is AndroidX and
Kotlin. Networking is plain `HttpURLConnection` — two endpoints don't justify a
library, and each dependency is another thing that could talk to someone who
isn't your hub.

---

## 8. Installing

**Over ADB**, with USB debugging enabled on the phone:

```bash
adb devices     # confirm your phone is listed
adb install -r app/build/outputs/apk/release/app-release.apk
```

**Or copy the APK to the phone** and tap it. Android will ask you to permit
installing unknown apps for whichever app you opened it from.

Then pair: hub URL, user id, token → **Pair** → check the fingerprint → grant
location → **Arm geofence**.

Android asks for location in **two separate steps** by design. "While using the
app" comes first; **Allow all the time** is only offered on a second, later
prompt, and a geofence does not survive without it. If you refuse it twice
Android stops asking entirely and the only route left is Settings → Apps → ZMM
Presence → Permissions → Location.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| `Unsupported class file major version` | `JAVA_HOME` points at a JDK the wrapper's Gradle doesn't support. See §4. |
| `Unable to download toolchain matching the requirements` | A `gradle/gradle-daemon-jvm.properties` pinning a JDK you don't have. Delete it and Gradle uses the JVM that launched it — Studio's runtime in the IDE, `JAVA_HOME` on the command line. |
| `SDK location not found` | Missing `local.properties`. See §4. |
| `./gradlew: No such file` | Wrapper not generated. See §4. |
| APK is `-unsigned` | `keystore.properties` missing or misplaced. See §5.3. |
| "Hub URL must be https://" | The app refuses plaintext by design. See §6.1. |
| Pairing: "Could not reach the hub" | Wrong address/port, firewall, or phone on another network. |
| Pairing: hostname verification failed | Certificate lacks the IP in its SAN. See §6.2. |
| "Certificate pin mismatch" | The hub's key changed. If you regenerated the certificate, use **Forget** and pair again. **If you didn't, stop and investigate.** |
| Pairing: 401 | Token wrong, expired or revoked. Reissue it. |
| Pairing: 403 | Token lacks `presence:read:<user>`. See §6.4. |
| Pairing: "No home location set" | Set home lat/lon and radius on the hub first. |
| Geofence never fires | Missing **Allow all the time**, or an OEM battery manager. See §10. |

---

## 10. Known limits

- Geofence transitions can lag **1–2 minutes**. The OS batches them to save
  power; this is not tunable.
- After reboot, `BootReceiver` re-arms the geofence — but Android only delivers
  `BOOT_COMPLETED` if the app has been opened at least once since install.
- Aggressive OEM battery managers (Xiaomi, Samsung, Huawei, OnePlus) kill
  background geofences. Exempt the app from battery optimisation on those.
- No Play Services means no geofencing. There is no fallback.

## `build_release.py`

`android/build_release.py` wraps what this document describes by hand: locate a
usable JDK, create a signing key if there is not one, build, then prove the
result is actually signed and that the security config survived into the release
variant.

**The verification half is the point.** A release build that quietly produces an
unsigned APK, or one that inherited the debug network config, still "succeeds"
as far as Gradle is concerned — you find out when the phone refuses to install
it, or worse, you never find out.

Passwords are read with `getpass` and passed to `keytool` over stdin, so they
stay out of shell history and out of the process list (`ps` shows every argument
of every running command, including other users').

```
python3 build_release.py                    # build, signing if configured
python3 build_release.py --setup            # create keystore + properties first
python3 build_release.py --verify-only      # re-check an existing APK
python3 build_release.py --debug            # debug APK instead
python3 build_release.py --install          # ...then install; one device installs
                                            # straight away, several prompt
python3 build_release.py --install SERIAL   # ...to that device, no prompt
python3 build_release.py --install --reinstall
                                            # ...replacing a copy signed with a
                                            # different key (e.g. the debug
                                            # build). Discards the pairing.
```

Exit status is 0 only if every check passed — and, with `--install`, the install
itself succeeded — so it is safe to use in a script.

`ANDROID_HOME` is passed to Gradle explicitly rather than inherited: `find_sdk()`
also accepts the SDK at its conventional path, so preflight can succeed on a
machine where the variable is unset and no `local.properties` exists. Gradle has
no such fallback and fails with "SDK location not found", which reads as a
missing SDK rather than an unexported variable.
