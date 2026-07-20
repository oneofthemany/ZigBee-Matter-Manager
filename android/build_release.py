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
    python3 build_release.py              # build, signing if configured
    python3 build_release.py --setup      # create keystore + properties first
    python3 build_release.py --verify-only  # re-check an existing APK
    python3 build_release.py --debug      # debug APK instead

Exit status is 0 only if every check passed, so this is safe to use in a script.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYSTORE = HERE / "zmm-release.jks"
KEY_PROPS = HERE / "keystore.properties"
KEY_ALIAS = "zmm"

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


def find_jdk() -> Path:
    """A JDK in [JDK_MIN, JDK_MAX]. Studio bundles one; prefer it."""
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


def find_gradlew() -> Path:
    g = HERE / "gradlew"
    if g.exists():
        return g
    raise Failed(
        "./gradlew not found — the wrapper jar is a binary and isn't checked in.\n"
        "  Open android/ in Android Studio once (it generates the wrapper), or:\n"
        "    gradle wrapper --gradle-version 8.13"
    )


# ---------------------------------------------------------------- signing setup

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
        pw = getpass.getpass("  Keystore password: ")
        if not pw:
            raise Failed("No password given.")
        # Confirm it actually opens the keystore, rather than writing a wrong
        # password and failing confusingly during the build.
        r = run([str(keytool), "-list", "-keystore", str(KEYSTORE), "-storepass:env", "KSPW"],
                env={**os.environ, "KSPW": pw})
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
        pw = getpass.getpass("  Choose a keystore password (min 6 chars): ")
        if len(pw) < 6:
            raise Failed("keytool requires at least 6 characters.")
        if pw != getpass.getpass("  Confirm: "):
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
        ], env={**os.environ, "KSPW": pw})
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

def gradle_build(jdk: Path, task: str) -> None:
    head(f"Gradle :{task}")
    info(f"JAVA_HOME={jdk}")
    env = {**os.environ, "JAVA_HOME": str(jdk)}

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
    apksigner = build_tool(sdk, "apksigner")
    jdk = find_jdk()
    r = run([str(apksigner), "verify", "--print-certs", str(apk)],
            env={**os.environ, "JAVA_HOME": str(jdk)})

    if r.returncode != 0:
        bad("apksigner rejected the APK")
        info((r.stderr or r.stdout).strip()[:400])
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
    needles = {
        "pin mismatch rejection": b"Certificate pin mismatch",
        "unpinned-connection guard": b"No certificate pin stored",
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
    args = ap.parse_args()

    release = not args.debug

    try:
        if args.setup:
            setup_signing()

        head("Toolchain")
        jdk = find_jdk()
        ok(f"JDK {java_version(jdk)} at {jdk}")
        sdk = find_sdk()
        ok(f"Android SDK at {sdk}")

        if release and not KEY_PROPS.exists():
            warn(f"{KEY_PROPS.name} not found — the APK will be unsigned")
            info("run with --setup to create a signing key")

        if not args.verify_only:
            gradle_build(jdk, "assembleRelease" if release else "assembleDebug")

        apk = find_apk(release)
        head("Artifact")
        ok(f"{apk.relative_to(HERE)}  ({apk.stat().st_size / 1e6:.1f} MB)")

        results = [
            verify_signature(sdk, apk),
            verify_security_config(sdk, apk, release),
            verify_pinning_code(apk),
        ]
        if release:
            check_gitignored()

        head("Result")
        if all(results):
            ok("all checks passed")
            print(f"\n  Install with:\n    adb install -r {apk.relative_to(HERE)}\n")
            return 0
        bad("one or more checks failed — see above")
        return 1

    except Failed as e:
        print(f"\n{C.R}Error:{C.X} {e}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
