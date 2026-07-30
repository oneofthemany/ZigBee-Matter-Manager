#!/usr/bin/env python3
"""
Build and verify a signed release APK of ZMM Presence.

Wraps what BUILDING.md describes by hand: locate a usable JDK, create a signing
key if there isn't one, build, then prove the result is actually signed and that
the security config survived into the release variant.

The verification half is the point. A release build that quietly produces an
unsigned APK, or one that inherited the debug network config, still "succeeds"
as far as Gradle is concerned — you find out when the phone refuses to install
it, or worse, you don't find out at all.

Passwords are read with getpass and passed to keytool over stdin, so they stay
out of your shell history and out of the process list (`ps` shows every
argument of every running command, including other users' commands).

Usage:
    python3 build_release.py                 # build, signing if configured
    python3 build_release.py --setup         # create keystore + properties first
    python3 build_release.py --verify-only   # re-check an existing APK
    python3 build_release.py --debug         # debug APK instead
    python3 build_release.py --install       # ...then install: one device
                                              # installs straight away, several
                                              # prompt for which
    python3 build_release.py --install SERIAL  # ...then install to that
                                                # device by name, no prompt
    python3 build_release.py --install --reinstall
                                             # ...replacing a copy signed with
                                             # a different key (e.g. the debug
                                             # build). Discards the pairing.

Exit status is 0 only if every check passed (and, with --install, the install
itself succeeded), so this is safe to use in a script.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYSTORE = HERE / "zmm-release.jks"
KEY_PROPS = HERE / "keystore.properties"
KEY_ALIAS = "zmm"
PACKAGE_ID = "com.zmm.presence"      # must match applicationId in build.gradle.kts

# Gradle 8.13 refuses to run on JDK 22+. Anything older than 17 can't build
# this project. That window is why we hunt for a JDK instead of trusting
# whatever `java` happens to be on PATH.
JDK_MIN, JDK_MAX = 17, 21


# ---------------------------------------------------------------- output

class C:
    """ANSI colours, disabled when not writing to a terminal or NO_COLOR is set."""
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    G = "\033[32m" if on else ""
    R = "\033[31m" if on else ""
    Y = "\033[33m" if on else ""
    B = "\033[1m" if on else ""
    X = "\033[0m" if on else ""


def head(msg): print(f"\n{C.B}== {msg} =={C.X}")
def ok(msg):   print(f"  {C.G}PASS{C.X}  {msg}")
def bad(msg):  print(f"  {C.R}FAIL{C.X}  {msg}")
def warn(msg): print(f"  {C.Y}WARN{C.X}  {msg}")
def info(msg): print(f"        {msg}")


class Failed(Exception):
    """A check failed, or a prerequisite is missing. Message is user-facing."""


def run(cmd, **kw):
    """Run a command, capturing output. Never raises on non-zero."""
    return subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=kw.pop("cwd", HERE), **kw,
    )


# ---------------------------------------------------------------- toolchain

def java_version(java_home: Path) -> int | None:
    """Major version of a JDK, or None if it isn't one."""
    exe = java_home / "bin" / "java"
    if not exe.exists():
        return None
    r = run([str(exe), "-version"])
    # Written to stderr, in one of two shapes:
    #   openjdk version "17.0.9" ...   -> 17
    #   java version "1.8.0_202" ...   -> 8
    m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr or r.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


_jdk_cache: Path | None = None


def find_jdk() -> Path:
    """A JDK in [JDK_MIN, JDK_MAX]. Studio bundles one; prefer it."""
    global _jdk_cache
    if _jdk_cache is not None:
        return _jdk_cache

    candidates: list[Path] = []

    env = os.environ.get("JAVA_HOME")
    if env:
        candidates.append(Path(env))

    candidates += [
        Path.home() / ".local/share/JetBrains/Toolbox/apps/android-studio/jbr",
        Path("/opt/android-studio/jbr"),
        Path("/usr/lib/jvm/java-21-openjdk"),
        Path("/usr/lib/jvm/java-17-openjdk"),
        Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
        Path.home() / "Applications/Android Studio.app/Contents/jbr/Contents/Home",
    ]
    candidates += sorted(Path("/usr/lib/jvm").glob("*"), reverse=True)

    rejected = []
    for c in candidates:
        v = java_version(c)
        if v is None:
            continue
        if JDK_MIN <= v <= JDK_MAX:
            _jdk_cache = c
            return c
        rejected.append((c, v))

    detail = ""
    if rejected:
        c, v = rejected[0]
        detail = (f"\n  Found JDK {v} at {c}, which is outside the supported "
                  f"range.\n  Gradle 8.13 rejects JDK 22+.")
    raise Failed(
        f"No JDK between {JDK_MIN} and {JDK_MAX} found.{detail}\n"
        "  Install Android Studio (it bundles one) or set JAVA_HOME yourself."
    )


def jdk_env(jdk: Path, **extra: str) -> dict:
    """
    Environment for anything that needs a JVM.

    JAVA_HOME alone is not enough. The SDK's apksigner is a shell wrapper whose
    last line is `exec java -jar apksigner.jar`, so it resolves the JVM from
    PATH and ignores JAVA_HOME entirely. On a machine whose only JDK is the one
    bundled inside Android Studio, nothing puts `java` on PATH and the wrapper
    dies with "exec: java: not found" — which reads as a broken APK rather than
    a missing interpreter. Setting both covers either convention.
    """
    return {
        **os.environ,
        "JAVA_HOME": str(jdk),
        "PATH": f"{jdk / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        **extra,
    }


def find_sdk() -> Path:
    """Android SDK, from local.properties or the usual env vars."""
    lp = HERE / "local.properties"
    if lp.exists():
        for line in lp.read_text().splitlines():
            if line.strip().startswith("sdk.dir="):
                # Gradle escapes colons and backslashes in this file.
                p = Path(line.split("=", 1)[1].strip().replace("\\:", ":").replace("\\\\", "\\"))
                if p.exists():
                    return p

    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var) and Path(os.environ[var]).exists():
            return Path(os.environ[var])

    for p in (Path.home() / "Android/Sdk",
              Path.home() / "Library/Android/sdk",
              Path("/usr/local/lib/android/sdk")):
        if p.exists():
            return p

    raise Failed(
        "Android SDK not found.\n"
        "  Create android/local.properties containing:\n"
        "    sdk.dir=/path/to/Android/Sdk\n"
        "  or set ANDROID_HOME."
    )


def build_tool(sdk: Path, name: str) -> Path:
    """Newest build-tools copy of apksigner/aapt2/etc."""
    versions = sorted(
        (d for d in (sdk / "build-tools").glob("*") if (d / name).exists()),
        key=lambda d: [int(x) if x.isdigit() else 0 for x in d.name.split(".")],
        reverse=True,
    )
    if not versions:
        raise Failed(
            f"'{name}' not found under {sdk / 'build-tools'}.\n"
            "  Install build-tools via Android Studio's SDK Manager."
        )
    return versions[0] / name


def apksigner_cmd(sdk: Path, jdk: Path) -> list[str]:
    """
    Command prefix that runs apksigner.

    Invokes the jar with the JDK we located rather than going through the
    shell wrapper, so the JVM is chosen here instead of by whatever PATH
    happens to hold. The wrapper is kept as a fallback for build-tools layouts
    that don't ship lib/apksigner.jar where we expect it; jdk_env() makes it
    work there too.
    """
    exe = build_tool(sdk, "apksigner")
    jar = exe.parent / "lib" / "apksigner.jar"
    if jar.exists():
        return [str(jdk / "bin" / "java"), "-jar", str(jar)]
    return [str(exe)]


def find_adb(sdk: Path) -> Path:
    """PATH first — build-tools has no adb, it lives under platform-tools."""
    which = shutil.which("adb")
    if which:
        return Path(which)
    candidate = sdk / "platform-tools" / "adb"
    if candidate.exists():
        return candidate
    raise Failed(
        "adb not found on PATH or under the SDK's platform-tools/.\n"
        "  Install platform-tools via Android Studio's SDK Manager, or add it "
        "to PATH."
    )


def list_devices(adb: Path) -> list[dict]:
    """
    Parse `adb devices -l`.

    Includes devices adb can see but cannot use (unauthorized, offline) rather
    than silently dropping them — a phone sitting there unauthorized looks
    identical to "not connected" unless something says otherwise, and that is
    the exact confusion a first-time USB debugging setup runs into.
    """
    r = run([str(adb), "devices", "-l"])
    if r.returncode != 0:
        raise Failed(f"adb devices failed:\n{r.stderr or r.stdout}")

    out = []
    for line in r.stdout.splitlines()[1:]:          # skip "List of devices attached"
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        model = next((p.split(":", 1)[1] for p in parts[2:]
                     if p.startswith("model:")), "")
        out.append({"serial": serial, "state": state, "model": model})
    return out


def choose_device(devices: list[dict], requested: str | None) -> str:
    """
    Resolve which device to install to.

    `requested` is either a literal serial (from `--install SERIAL`), the
    `__prompt__` sentinel (bare `--install`), or None.
    """
    usable = [d for d in devices if d["state"] == "device"]

    if requested and requested != "__prompt__":
        if not any(d["serial"] == requested for d in devices):
            raise Failed(f"No device with serial '{requested}' in `adb devices`.")
        if not any(d["serial"] == requested and d["state"] == "device" for d in devices):
            raise Failed(f"Device '{requested}' is not in a usable state "
                        f"(check for an 'allow USB debugging' prompt on the phone).")
        return requested

    if not devices:
        raise Failed(
            "No devices found by adb.\n"
            "  Check the cable, that USB debugging is enabled, and that you "
            "accepted the 'allow USB debugging' prompt on the phone."
        )
    if not usable:
        detail = "\n".join(f"    {d['serial']}  {d['state']}" for d in devices)
        raise Failed(f"adb sees device(s) but none are usable:\n{detail}\n"
                    f"  'unauthorized' means accept the prompt on the phone's screen.")
    if len(usable) == 1:
        d = usable[0]
        ok(f"one device connected: {d['serial']} ({d['model'] or 'unknown model'})")
        return d["serial"]

    # Multiple devices: this must be interactive. A non-interactive caller
    # (CI, a script) cannot answer a picker, and installing to a guessed
    # device would be worse than refusing outright.
    if not sys.stdin.isatty():
        detail = "\n".join(f"    {d['serial']}  {d['model']}" for d in usable)
        raise Failed(f"Multiple devices connected and no terminal to ask which:\n"
                    f"{detail}\n"
                    f"  Run again with --install <serial>.")

    print(f"\n  {C.B}Multiple devices connected:{C.X}")
    for i, d in enumerate(usable, 1):
        print(f"    {i}) {d['serial']}  {d['model'] or 'unknown model'}")
    while True:
        choice = input(f"  Install to which? [1-{len(usable)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(usable):
            return usable[int(choice) - 1]["serial"]
        print("  Not a valid choice.")


def installed_package(adb: Path, serial: str) -> dict | None:
    """
    What's already installed, or None if the package isn't present.

    Used to explain a signature clash before acting on it. The signature field
    dumpsys prints is a truncated hash, not the certificate digest, so it can't
    be compared against apksigner's output — but the DEBUGGABLE flag identifies
    the overwhelmingly common cause directly: an Android Studio debug build,
    signed with the debug key, sitting where the release build wants to go.
    """
    r = run([str(adb), "-s", serial, "shell", "dumpsys", "package", PACKAGE_ID])
    out = r.stdout or ""
    if "versionName=" not in out:
        return None

    def grab(pattern: str) -> str:
        m = re.search(pattern, out)
        return m.group(1).strip() if m else ""

    return {
        "versionName": grab(r"versionName=(\S+)"),
        "versionCode": grab(r"versionCode=(\S+)"),
        "firstInstall": grab(r"firstInstallTime=(.+)"),
        "debuggable": "DEBUGGABLE" in grab(r"pkgFlags=\[(.*?)\]"),
    }


def wait_until_gone(adb: Path, serial: str, timeout: float = 15.0) -> bool:
    """
    Block until PackageManager has actually finished removing the package.

    `adb uninstall` reports Success when the removal is accepted, not when it
    has settled. Installing inside that window loses a race, and the installer
    reports it as INSTALL_FAILED_PACKAGE_CHANGED — "Package was removed before
    install could complete", which reads as a broken APK rather than as two
    commands issued too close together. `pm path` is the authority on whether
    the package is really gone.
    """
    deadline = time.monotonic() + timeout
    while True:
        r = run([str(adb), "-s", serial, "shell", "pm", "path", PACKAGE_ID])
        if not (r.stdout or "").strip():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def uninstall(adb: Path, serial: str) -> bool:
    r = run([str(adb), "-s", serial, "uninstall", PACKAGE_ID])
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 and "Success" in out:
        return True
    bad("uninstall failed")
    info(out.strip()[:300])
    return False


def _confirm_wipe(existing: dict | None) -> bool:
    """
    Ask before discarding the phone's pairing.

    Uninstalling is the only way past a signature clash — allowBackup is false,
    so there is nothing to save and restore around it. What goes with the app is
    the hub URL, the bearer token, the captured certificate pin and the cached
    geofence, and re-pairing is a manual trip through the hub UI. That is not a
    cost to absorb silently on someone's behalf, so it needs either a terminal
    answer or --reinstall stating the intent up front.
    """
    if existing and existing["debuggable"]:
        info("the installed copy is a DEBUG build (signed with the debug key)")
    info("uninstalling discards the hub pairing: URL, token, certificate pin")
    info("and cached geofence. You will need to pair again from the app.")

    if not sys.stdin.isatty():
        info("no terminal to confirm — re-run with --reinstall to allow this")
        return False
    try:
        return input("  Uninstall and reinstall? [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def install_apk(adb: Path, serial: str, apk: Path, allow_reinstall: bool = False,
                version: tuple[str, int] | None = None) -> bool:
    head("Install")

    existing = installed_package(adb, serial)
    if existing:
        kind = "debug" if existing["debuggable"] else "release"
        info(f"on device: {existing['versionName']} "
             f"(code {existing['versionCode']}, {kind} build)")
    if version:
        info(f"installing: {version[0]} (code {version[1]})")

    # Two builds sharing a versionCode install over each other leaving nothing
    # on the phone to tell them apart — the app's own version strip will read
    # identical. Say so before the install, while bumping it is still cheap.
    if existing and version and str(existing["versionCode"]) == str(version[1]):
        warn(f"versionCode {version[1]} is already what's installed — bump it in "
             f"app/build.gradle.kts")
        info("otherwise you cannot confirm from the phone which build is running")

    r = run([str(adb), "-s", serial, "install", "-r", str(apk)])
    out = (r.stdout or "") + (r.stderr or "")

    if r.returncode == 0 and "Success" in out:
        ok(f"installed to {serial}")
        return True

    # A signature clash is the one install failure with a known, safe recovery,
    # so it is handled rather than merely reported. Everything else (no space,
    # downgrade, bad ABI) needs a human deciding what to do.
    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
        warn("signature mismatch with the installed copy")
        if not (allow_reinstall or _confirm_wipe(existing)):
            bad("install failed — left the existing copy alone")
            return False

        info("uninstalling...")
        if not uninstall(adb, serial):
            return False
        if not wait_until_gone(adb, serial):
            bad("package still present after uninstall reported success")
            return False
        ok("removed the previous copy")

        # One retry: PACKAGE_CHANGED is transient by definition, and the point
        # of this branch is to not hand back a failure that a second attempt
        # would have cleared.
        for attempt in (1, 2):
            r = run([str(adb), "-s", serial, "install", str(apk)])
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and "Success" in out:
                ok(f"installed to {serial}")
                warn("the app is unpaired — pair it again from the hub")
                return True
            if "INSTALL_FAILED_PACKAGE_CHANGED" not in out or attempt == 2:
                break
            info("package manager was still settling; retrying")
            time.sleep(1.0)

    bad("install failed")
    info(out.strip()[:500])
    return False


def find_gradlew() -> Path:
    g = HERE / "gradlew"
    if g.exists():
        return g
    raise Failed(
        "./gradlew not found — the wrapper jar is a binary and isn't checked in.\n"
        "  Open android/ in Android Studio once (it generates the wrapper), or:\n"
        "    gradle wrapper --gradle-version 8.13"
    )


def preflight_tools(jdk: Path, sdk: Path) -> None:
    """
    Prove the verification tools actually execute before anything long runs.

    Existing on disk is not the same as being runnable: apksigner needs a JVM
    it finds for itself, and aapt2 needs its shared libraries. Both fail late
    otherwise — after a full Gradle build — and the failure lands in the
    verification output, where it reads as a problem with the APK.
    """
    r = run(apksigner_cmd(sdk, jdk) + ["version"], env=jdk_env(jdk))
    if r.returncode != 0:
        raise Failed(
            "apksigner is present but will not run:\n"
            f"    {(r.stderr or r.stdout).strip()[:300]}\n"
            f"  Tried the JVM at {jdk / 'bin' / 'java'}."
        )
    ok(f"apksigner runnable ({build_tool(sdk, 'apksigner').parent.name})")

    r = run([str(build_tool(sdk, "aapt2")), "version"])
    if r.returncode != 0:
        raise Failed(
            "aapt2 is present but will not run:\n"
            f"    {(r.stderr or r.stdout).strip()[:300]}"
        )
    ok("aapt2 runnable")


# ---------------------------------------------------------------- signing setup

def ask_password(prompt: str) -> str:
    """
    Read a password without echoing it.

    getpass needs a terminal. Run under a harness that gives the process no tty
    (an IDE run window, CI, a tool that pipes stdin) it either warns and echoes
    the password in the clear, or raises EOFError with no useful context. Both
    are bad enough to refuse outright: silently echoing a signing password into
    a scrollback buffer is exactly the leak this function exists to prevent.
    """
    if not sys.stdin.isatty():
        raise Failed(
            "No terminal available, so the password cannot be read without\n"
            "  echoing it. Run this in a real terminal window:\n\n"
            f"    cd {HERE}\n"
            "    python3 build_release.py --setup\n\n"
            "  Or skip the script and write keystore.properties by hand — see\n"
            "  section 5.2 of BUILDING.md."
        )
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Failed("Password entry was interrupted.")


def props_escape(v: str) -> str:
    """
    Escape a value for a Java .properties file.

    Backslashes are escape characters there, so a password containing one is
    silently corrupted otherwise — and the failure surfaces much later as an
    unexplained 'keystore password was incorrect'.
    """
    return v.replace("\\", "\\\\")


def setup_signing() -> None:
    """Create the keystore and keystore.properties, prompting for the password."""
    head("Signing setup")

    if KEY_PROPS.exists():
        ok(f"{KEY_PROPS.name} already exists — leaving it alone")
        return

    jdk = find_jdk()
    keytool = jdk / "bin" / "keytool"

    if KEYSTORE.exists():
        info(f"{KEYSTORE.name} exists; recording its password in {KEY_PROPS.name}.")
        pw = ask_password("  Keystore password: ")
        if not pw:
            raise Failed("No password given.")
        # Confirm it actually opens the keystore, rather than writing a wrong
        # password and failing confusingly during the build.
        r = run([str(keytool), "-list", "-keystore", str(KEYSTORE), "-storepass:env", "KSPW"],
                env=jdk_env(jdk, KSPW=pw))
        if r.returncode != 0:
            raise Failed("That password did not open the keystore.")
        ok("password verified against the existing keystore")
    else:
        print("""
  A signing key identifies this app to Android. Losing it means you can never
  update an installed copy again, only uninstall and reinstall. Anyone with the
  file AND its password can publish updates Android accepts as yours.

  Back up zmm-release.jks somewhere outside this repo.
""")
        pw = ask_password("  Choose a keystore password (min 6 chars): ")
        if len(pw) < 6:
            raise Failed("keytool requires at least 6 characters.")
        if pw != ask_password("  Confirm: "):
            raise Failed("Passwords did not match.")

        cn = input("  Common name [ZMM Presence]: ").strip() or "ZMM Presence"

        # -storepass:env keeps the password out of argv; ps(1) would otherwise
        # expose it to every user on the machine for the life of the process.
        r = run([
            str(keytool), "-genkeypair", "-v",
            "-keystore", str(KEYSTORE),
            "-alias", KEY_ALIAS,
            "-keyalg", "RSA", "-keysize", "4096",
            "-validity", "10000",          # ~27 years; an expired key can't ship updates
            "-storetype", "PKCS12",
            "-dname", f"CN={cn}",
            "-storepass:env", "KSPW",
            "-keypass:env", "KSPW",
        ], env=jdk_env(jdk, KSPW=pw))
        if r.returncode != 0:
            raise Failed(f"keytool failed:\n{r.stderr}")
        ok(f"created {KEYSTORE.name} (RSA 4096, valid ~27 years)")

    KEY_PROPS.write_text(
        "# Generated by build_release.py. Gitignored — do not commit.\n"
        f"storeFile={KEYSTORE.name}\n"
        f"storePassword={props_escape(pw)}\n"
        f"keyAlias={KEY_ALIAS}\n"
        f"keyPassword={props_escape(pw)}\n"
    )
    KEY_PROPS.chmod(0o600)   # plaintext password; don't leave it world-readable
    ok(f"wrote {KEY_PROPS.name} (mode 600)")

    check_gitignored()


def check_gitignored() -> None:
    """Confirm git will not commit the signing material."""
    if not shutil.which("git"):
        return
    leaked = []
    for f in (KEYSTORE, KEY_PROPS):
        if not f.exists():
            continue
        r = run(["git", "check-ignore", "-q", str(f)])
        if r.returncode != 0:
            leaked.append(f.name)
    if leaked:
        bad(f"NOT gitignored: {', '.join(leaked)} — add them to .gitignore before committing")
    else:
        ok("signing material is gitignored")


# ---------------------------------------------------------------- build

def gradle_build(jdk: Path, sdk: Path, task: str) -> None:
    head(f"Gradle :{task}")
    info(f"JAVA_HOME={jdk}")
    info(f"ANDROID_HOME={sdk}")
    # ANDROID_HOME is passed explicitly, not inherited: find_sdk() also accepts
    # the SDK at its conventional path, so preflight can succeed on a machine
    # where the variable is unset and no local.properties exists. Gradle has no
    # such fallback and fails with "SDK location not found" — which reads as a
    # missing SDK rather than an unexported variable.
    env = jdk_env(jdk, ANDROID_HOME=str(sdk))

    proc = subprocess.Popen(
        [str(find_gradlew()), task],
        cwd=HERE, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    tail: list[str] = []
    for line in proc.stdout:                       # type: ignore[union-attr]
        tail.append(line.rstrip())
        tail[:] = tail[-40:]
        s = line.strip()
        if s.startswith(("> Task", "FAILURE", "BUILD")) or "warning" in s.lower():
            info(s)
    if proc.wait() != 0:
        raise Failed("Gradle build failed:\n" + "\n".join(tail))
    ok("build succeeded")


def run_unit_tests(jdk: Path, sdk: Path) -> bool:
    """
    Run the JVM unit tests and report what actually ran.

    Gradle's own "BUILD SUCCESSFUL" is not enough on its own: a test task with
    no tests to run succeeds identically to one that ran and passed, so a
    source set that silently stopped being compiled would look like a clean
    bill of health. The JUnit XML says how many cases there were.

    Only the debug variant has a unit test task in this project, and the code
    under test is variant-independent, so debug is the whole suite.
    """
    head("Unit tests")
    results = HERE / "app/build/test-results/testDebugUnitTest"
    if results.exists():
        shutil.rmtree(results)          # never report a previous run's XML

    proc = run([str(find_gradlew()), ":app:testDebugUnitTest"],
               env=jdk_env(jdk, ANDROID_HOME=str(sdk)))

    total = failed = 0
    cases: list[str] = []
    for xml in sorted(results.glob("*.xml")) if results.exists() else []:
        text = xml.read_text()
        m = re.search(r'tests="(\d+)"[^>]*failures="(\d+)"[^>]*errors="(\d+)"', text)
        if m:
            total += int(m.group(1))
            failed += int(m.group(2)) + int(m.group(3))
        cases += re.findall(r'<testcase name="([^"]+)"', text)

    if proc.returncode != 0 or failed:
        bad(f"{failed} of {total} unit tests failed")
        for line in (proc.stdout or "").splitlines():
            if "FAILED" in line or "expected" in line.lower():
                info(line.strip()[:160])
        return False

    if total == 0:
        warn("no unit tests were found — the test source set may not be compiled")
        return True

    ok(f"{total} unit tests passed")
    for c in sorted(cases):
        info(c)
    return True


def find_apk(release: bool) -> Path:
    d = HERE / "app/build/outputs/apk" / ("release" if release else "debug")
    apks = sorted(d.glob("*.apk"))
    if not apks:
        raise Failed(f"No APK in {d}")

    # A build with no signing config emits *-unsigned.apk and only warns. Say
    # so loudly here rather than letting it reach a phone that will reject it.
    unsigned = [a for a in apks if "unsigned" in a.name]
    signed = [a for a in apks if "unsigned" not in a.name]
    if signed:
        return signed[0]
    raise Failed(
        f"Only an UNSIGNED APK was produced: {unsigned[0].name}\n"
        f"  Gradle could not find {KEY_PROPS.name}, so it skipped signing.\n"
        "  Run:  python3 build_release.py --setup"
    )


# ---------------------------------------------------------------- verification

def verify_signature(sdk: Path, apk: Path) -> bool:
    head("Signature")
    jdk = find_jdk()
    r = run(apksigner_cmd(sdk, jdk) + ["verify", "--print-certs", str(apk)],
            env=jdk_env(jdk))

    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip()
        # Distinguish "the APK is bad" from "apksigner could not run at all".
        # Both surface as a non-zero exit, but only one of them is about the
        # artifact, and reporting a toolchain fault as a signature failure
        # sends you looking in entirely the wrong place.
        if "DOES NOT VERIFY" in detail or "not signed" in detail.lower():
            bad("apksigner rejected the APK")
        else:
            bad("apksigner could not run — this is a toolchain fault, not a bad APK")
        info(detail[:400])
        return False

    out = r.stdout
    ok("APK is signed and the signature verifies")

    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Signer") and ("DN:" in s or "digest:" in s):
            info(s)

    # A debug-key APK is a real signature, so apksigner is happy — but it is
    # not what you want to hand to anyone else. Debug builds are debuggable,
    # which means ADB can read the app's memory, token included.
    if "CN=Android Debug" in out:
        warn("signed with the ANDROID DEBUG KEY — fine for yourself, not for distribution")

    return True


def apk_version(sdk: Path, apk: Path) -> tuple[str, int] | None:
    """
    versionName and versionCode as built into the APK.

    Read from the artifact rather than from build.gradle.kts on purpose: the
    point is to confirm what you are about to install, and the source tree can
    have moved on since the APK was assembled.
    """
    r = run([str(build_tool(sdk, "aapt2")), "dump", "badging", str(apk)])
    name = re.search(r"versionName='([^']*)'", r.stdout or "")
    code = re.search(r"versionCode='(\d+)'", r.stdout or "")
    if not name or not code:
        return None
    return name.group(1), int(code.group(1))


def _release_res_path(aapt2: Path, apk: Path, name: str) -> str | None:
    """
    Map a resource name to its path inside the APK.

    Release builds rename resources (res/xml/foo.xml -> res/8G.xml), so the
    path cannot be hardcoded. `aapt2 dump resources` prints, for each entry:

        resource 0x7f120000 xml/network_security_config
          () (file) res/8G.xml type=XML
    """
    r = run([str(aapt2), "dump", "resources", str(apk)])
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip().endswith(f"xml/{name}"):
            for nxt in lines[i + 1:i + 4]:
                m = re.search(r"(res/[^\s]+\.xml)", nxt)
                if m:
                    return m.group(1)
    return None


def verify_security_config(sdk: Path, apk: Path, release: bool) -> bool:
    """
    Confirm the APK's network security config matches its variant.

    The specific hazard: src/debug/ contains an override that trusts user CAs
    and permits cleartext to some LAN addresses. It must not reach a release
    APK. Gradle's variant merging is correct, but this is cheap to check and
    catastrophic to get wrong, and it silently affects only shipped builds.

    In a DEBUG apk those same settings are the intended behaviour, so they are
    reported as expected rather than as failures. A check that cries wolf on
    every debug build is a check people learn to ignore on release ones.
    """
    head(f"Network security config ({'release' if release else 'debug'} variant)")
    aapt2 = build_tool(sdk, "aapt2")

    path = _release_res_path(aapt2, apk, "network_security_config")
    if not path:
        bad("no network_security_config in the APK")
        return False
    info(f"resource: {path}")

    tree = run([str(aapt2), "dump", "xmltree", "--file", path, str(apk)]).stdout
    good = True

    if 'src="user"' in tree:
        if release:
            bad("trusts USER CAs — any CA installed on the phone could intercept traffic")
            good = False
        else:
            warn("trusts user CAs — expected in debug, must not appear in release")
    else:
        ok("user CA store not trusted (pinning is the sole hub authentication)")

    if 'src="system"' in tree:
        ok("system CA trust anchor present")

    if "cleartextTrafficPermitted=true" in tree:
        if release:
            bad("permits cleartext — the debug config leaked into the release build")
            good = False
        else:
            warn("permits cleartext to the configured dev hosts — expected in debug")
    else:
        ok("cleartext traffic disabled")

    return good


def verify_pinning_code(apk: Path) -> bool:
    """
    Confirm the pinning guards are compiled into the shipped dex.

    Checks for the literal strings of each refusal path, which is a proxy for
    the code being present and not stripped. It proves the guards shipped; it
    does not prove they are reachable. The runtime check is pairing against a
    hub with a deliberately wrong pin.
    """
    head("Certificate pinning (compiled in)")
    # Keep in step with the literals in HubClient.kt / CertPin.kt. These are a
    # proxy for the guards being compiled in, so a reworded message shows up
    # here as a failure — annoying, but the alternative is a check that quietly
    # stops testing anything the day someone edits a string.
    needles = {
        "pin mismatch rejection": b"Certificate pin mismatch",
        "untrusted-hub guard": b"Hub trust not established",
        "pinned-without-pin guard": b"Pinned mode with no stored pin",
        "plaintext refusal": b"Refusing to send credentials over plain http",
    }
    blob = b""
    with zipfile.ZipFile(apk) as z:
        for n in z.namelist():
            if n.startswith("classes") and n.endswith(".dex"):
                blob += z.read(n)

    good = True
    for label, needle in needles.items():
        if needle in blob:
            ok(label)
        else:
            bad(f"{label} MISSING from the APK")
            good = False
    return good


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build and verify a signed release APK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit status is 0 only if every check passes.",
    )
    ap.add_argument("--setup", action="store_true",
                    help="create the signing keystore and keystore.properties first")
    ap.add_argument("--verify-only", action="store_true",
                    help="verify the existing APK without rebuilding")
    ap.add_argument("--debug", action="store_true",
                    help="build the debug variant instead of release")
    ap.add_argument("--install", nargs="?", const="__prompt__", default=None,
                    metavar="SERIAL",
                    help="install to a device after a successful build. With "
                         "no value: use the one connected device, or prompt if "
                         "there are several. With a value: install straight to "
                         "that adb serial, no prompt.")
    ap.add_argument("--skip-tests", action="store_true",
                    help="don't run the JVM unit tests before verifying")
    ap.add_argument("--reinstall", action="store_true",
                    help="if the installed copy was signed with a different key "
                         "(typically a debug build), uninstall and install "
                         "fresh without asking. This DISCARDS the hub pairing.")
    args = ap.parse_args()

    if args.reinstall and args.install is None:
        ap.error("--reinstall only means anything alongside --install")

    release = not args.debug

    try:
        if args.setup:
            setup_signing()

        head("Toolchain")
        jdk = find_jdk()
        ok(f"JDK {java_version(jdk)} at {jdk}")
        sdk = find_sdk()
        ok(f"Android SDK at {sdk}")
        preflight_tools(jdk, sdk)

        if release and not KEY_PROPS.exists():
            warn(f"{KEY_PROPS.name} not found — the APK will be unsigned")
            info("run with --setup to create a signing key")

        tests_ok = True
        if not args.verify_only:
            gradle_build(jdk, sdk, "assembleRelease" if release else "assembleDebug")
            if not args.skip_tests:
                tests_ok = run_unit_tests(jdk, sdk)

        apk = find_apk(release)
        head("Artifact")
        ok(f"{apk.relative_to(HERE)}  ({apk.stat().st_size / 1e6:.1f} MB)")
        version = apk_version(sdk, apk)
        if version:
            ok(f"version {version[0]} (code {version[1]})")

        results = [
            tests_ok,
            verify_signature(sdk, apk),
            verify_security_config(sdk, apk, release),
            verify_pinning_code(apk),
        ]
        if release:
            check_gitignored()

        checks_ok = all(results)
        head("Result")
        if checks_ok:
            ok("all checks passed")
        else:
            bad("one or more checks failed — see above")

        if not checks_ok:
            return 1

        if args.install is not None:
            adb = find_adb(sdk)
            devices = list_devices(adb)
            serial = choose_device(devices, args.install)
            if not install_apk(adb, serial, apk, allow_reinstall=args.reinstall,
                               version=version):
                return 1
        else:
            print(f"\n  Install with:\n"
                 f"    python3 {Path(__file__).name} --install\n"
                 f"  or:\n"
                 f"    adb install -r {apk.relative_to(HERE)}\n")

        return 0

    except Failed as e:
        print(f"\n{C.R}Error:{C.X} {e}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
