"""scripts/coral_driver.sh: build, load and boot-time service for the Coral M.2
driver, against stand-in make/podman/insmod. No module is built or loaded."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from harness import SCRIPTS, Checker

K = "7.2.8-200.fc44.x86_64"
K2 = "7.2.9-200.fc44.x86_64"

STUBS = {
    "curl": r'''#!/bin/bash
echo "curl" >> "$STUB_DIR/calls.log"
[[ -f "$STUB_DIR/offline" ]] && exit 6
while [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && { cp "$STUB_DIR/src.tgz" "$2"; exit 0; }; shift; done''',
    "make": r'''#!/bin/bash
echo "make $*" >> "$STUB_DIR/calls.log"
[[ -f "$STUB_DIR/make_fail" ]] && exit 2
for a in "$@"; do [[ "$a" == M=* ]] && touch "${a#M=}/gasket.ko" "${a#M=}/apex.ko"; done''',
    "gcc": "#!/bin/bash\n",
    "podman": r'''#!/bin/bash
echo "podman $*" | tr '\n' ' ' >> "$STUB_DIR/calls.log"; echo >> "$STUB_DIR/calls.log"
for a in "$@"; do [[ "$a" == *:/build ]] && touch "${a%:/build}/gasket.ko" "${a%:/build}/apex.ko"; done; true''',
    # vermagic is the kernel the build dir is named for, unless told to lie.
    "modinfo": r'''#!/bin/bash
if [[ "$1" == "-F" ]]; then
  [[ -f "$STUB_DIR/bad_vermagic" ]] && { echo "1.0.0 SMP"; exit 0; }
  echo "$(basename "$(dirname "$3")") SMP preempt"; exit 0
fi
[[ -f "$STUB_DIR/distro_apex" ]]''',
    "insmod": r'''#!/bin/bash
echo "insmod $(basename "$1")" >> "$STUB_DIR/calls.log"
[[ -f "$STUB_DIR/insmod_fail" ]] && exit 1
case "$1" in
  *gasket.ko) mkdir -p "$FAKE_SYS/module/gasket" ;;
  *apex.ko) mkdir -p "$FAKE_SYS/module/apex" "$FAKE_SYS/class/apex/apex_0"; touch "$FAKE_DEV/apex_0" ;;
esac''',
    "modprobe": r'''#!/bin/bash
echo "modprobe $*" >> "$STUB_DIR/calls.log"
mkdir -p "$FAKE_SYS/module/apex"; touch "$FAKE_DEV/apex_0"''',
    "rmmod": r'''#!/bin/bash
echo "rmmod $*" >> "$STUB_DIR/calls.log"
[[ -f "$STUB_DIR/in_use" ]] && exit 1
rm -rf "$FAKE_SYS/module/$1"; [[ "$1" == apex ]] && rm -f "$FAKE_DEV/apex_0"; true''',
    "mokutil": '#!/bin/bash\n[[ -f "$STUB_DIR/secure_boot" ]] && echo "SecureBoot enabled" || echo "SecureBoot disabled"\n',
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$STUB_DIR/calls.log"
case "$1" in
  is-enabled) [[ -f "$STUB_DIR/sd_enabled" ]] ;;
  enable)     touch "$STUB_DIR/sd_enabled" ;;
  disable)    rm -f "$STUB_DIR/sd_enabled" ;;
  *) true ;;
esac''',
    "rc-update": r'''#!/bin/bash
echo "rc-update $*" >> "$STUB_DIR/calls.log"
case "$1" in
  add)  touch "$STUB_DIR/rc_enabled" ;;
  del)  rm -f "$STUB_DIR/rc_enabled" ;;
  show) [[ -f "$STUB_DIR/rc_enabled" ]] && echo "     zmm-coral-driver | default" ;;
esac; true''',
    "getent": '#!/bin/bash\n[[ -f "$STUB_DIR/group_apex" ]]\n',
    "groupadd": '#!/bin/bash\necho "groupadd $*" >> "$STUB_DIR/calls.log"\ntouch "$STUB_DIR/group_apex"\n',
    "udevadm": '#!/bin/bash\necho "udevadm $*" >> "$STUB_DIR/calls.log"\n',
    "chcon": "#!/bin/bash\n",
    "sudo": '#!/bin/bash\n[[ "$1" == "-n" ]] && shift\n[[ "$1" == "true" ]] && exit 0\nexec "$@"\n',
}


def source_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name in ("src/apex_driver.c", "src/Makefile"):
            info = tarfile.TarInfo(f"gasket-dkms-abc/{name}")
            info.size = 2
            t.addfile(info, io.BytesIO(b"x\n"))
    return buf.getvalue()


class Box:
    def __init__(self, backend: str = "systemd", card: bool = True, headers: bool = False,
                 os_id: str = "fedora", runtimes: str = "podman"):
        self._t = tempfile.TemporaryDirectory()
        r = Path(self._t.name)
        self.data, self.stubs, self.bin, self.state = r / "data", r / "stubs", r / "bin", r / "state"
        self.sys, self.dev, self.mods, self.ostree = r / "sys", r / "dev", r / "lib_modules", r / "ostree"
        self.systemd, self.initd, self.udev = r / "systemd", r / "initd", r / "udev"
        for d in (self.data, self.stubs, self.bin, self.sys, self.dev, self.mods, self.systemd, self.initd,
                  self.udev, r / "sdrun", r / "rcrun"):
            d.mkdir()
        for n, body in STUBS.items():
            (self.bin / n).write_text(body)
            (self.bin / n).chmod(0o755)
        if card:
            d = self.sys / "bus/pci/devices/0000:02:00.0"
            d.mkdir(parents=True)
            (d / "vendor").write_text("0x1ac1\n")
            (d / "device").write_text("0x089a\n")
        self.add_kernel(K, headers=headers)
        tgz = source_tarball()
        (self.stubs / "src.tgz").write_bytes(tgz)
        (r / "os-release").write_text(f"ID={os_id}\nVERSION_ID=44\nVERSION_CODENAME=trixie\n")
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "STUB_DIR": str(self.stubs),
                    "FAKE_SYS": str(self.sys), "FAKE_DEV": str(self.dev),
                    "ZMM_DATA_DIR": str(self.data), "ZMM_CORAL_STATE": str(self.state),
                    "ZMM_SYSFS_ROOT": str(self.sys), "ZMM_DEV_ROOT": str(self.dev),
                    "ZMM_MODULES_ROOT": str(self.mods), "ZMM_OSTREE_ROOT": str(self.ostree),
                    "ZMM_OS_RELEASE": str(r / "os-release"), "ZMM_UDEV_DIR": str(self.udev),
                    "ZMM_SYSTEMD_DIR": str(self.systemd), "ZMM_INITD_DIR": str(self.initd),
                    "ZMM_SYSTEMD_RUN": str(r / ("sdrun" if backend == "systemd" else "absent")),
                    "ZMM_OPENRC_RUN": str(r / ("rcrun" if backend == "openrc" else "absent")),
                    "ZMM_RUNTIMES": runtimes, "ZMM_KVER": K, "ZMM_CORAL_WAIT": "0",
                    "ZMM_CORAL_SRC_SHA256": hashlib.sha256(tgz).hexdigest()}

    def add_kernel(self, kver: str, headers: bool = False, staged: bool = False) -> None:
        base = (self.ostree / "deploy/fedora/deploy/abc.0/usr/lib/modules" if staged else self.mods) / kver
        (base / "kernel").mkdir(parents=True)
        if headers:
            (base / "build").mkdir()
            (base / "build" / "Makefile").write_text("")

    def flag(self, name: str, on: bool = True) -> None:
        p = self.stubs / name
        p.write_text("") if on else p.unlink(missing_ok=True)

    def run(self, action: str, via_trigger: bool = True, **env: str) -> dict:
        d = self.data / "data" / "coral"
        d.mkdir(parents=True, exist_ok=True)
        (self.stubs / "calls.log").write_text("")
        cmd = ["bash", str(SCRIPTS / "coral_driver.sh")]
        if via_trigger:
            (d / "driver_action").write_text(action)
        else:
            cmd.append(action)
        subprocess.run(cmd, env={**self.env, **env}, capture_output=True, timeout=60)
        self.trigger_left = (d / "driver_action").exists()
        f = d / "driver_status.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def calls(self):
        return (self.stubs / "calls.log").read_text().splitlines()

    def built(self, kver: str = K) -> bool:
        return (self.state / "modules" / kver / "apex.ko").exists()

    def close(self):
        self._t.cleanup()


def run() -> Checker:
    c = Checker("coral_driver")

    c.section("install on an image-based host (no headers: builds in a container)")
    b = Box()
    st = b.run("install")
    calls = b.calls()
    run_line = next((x for x in calls if x.startswith("podman run")), "")
    c.check("builds in a container of the host's own distro release",
            "registry.fedoraproject.org/fedora:44" in run_line and run_line.rstrip().endswith(K), run_line[:300])
    c.check("…never on the host", not any(x.startswith("make") for x in calls), calls)
    c.check("keeps the modules under the writable state dir, per kernel", b.built())
    c.check("loads gasket before apex", [x for x in calls if x.startswith("insmod")] == ["insmod gasket.ko", "insmod apex.ko"], calls)
    unit = b.systemd / "zmm-coral-driver.service"
    text = unit.read_text() if unit.exists() else ""
    c.check("writes a oneshot unit that loads at boot",
            "Type=oneshot" in text and "coral_driver.sh load" in text and "systemctl enable zmm-coral-driver.service" in calls, text[:300])
    c.check("…after the network, since a new kernel may need a build", "After=network-online.target" in text)
    rule = (b.udev / "65-zmm-apex.rules").read_text() if (b.udev / "65-zmm-apex.rules").exists() else ""
    c.check("creates the apex group and a udev rule for it", 'GROUP="apex"' in rule and "groupadd -r apex" in calls, (rule, calls))
    c.check("reports loaded, installed and the device node",
            (st.get("state"), st.get("loaded"), st.get("installed"), st.get("enabled"), st.get("node"), st.get("source"))
            == ("done", True, True, True, "apex_0", "built"), st)
    c.check("lists what it has modules for", st.get("built_for") == [K], st)
    c.check("consumes the trigger", not b.trigger_left)
    st = b.run("install")
    c.check("installing again neither rebuilds nor reloads",
            not any(x.startswith(("podman run", "insmod", "curl")) for x in b.calls()) and st["state"] == "done", b.calls())

    c.section("a driver already loaded by hand")
    b2 = Box()
    (b2.sys / "module" / "apex").mkdir(parents=True)
    (b2.sys / "module" / "gasket").mkdir(parents=True)
    (b2.dev / "apex_0").write_text("")
    st = b2.run("install")
    c.check("is left loaded, but a copy is built so the next boot has one",
            b2.built() and not any(x.startswith("insmod") for x in b2.calls())
            and st["installed"] and st["source"] == "built", (b2.calls(), st))
    b2.close()

    c.section("surviving a kernel update")
    b.add_kernel(K2, staged=True)
    st = b.run("prebuild", via_trigger=False)
    c.check("prebuilds for a kernel staged by rpm-ostree", b.built(K2) and sorted(st["built_for"]) == [K, K2], st)
    st = b.run("load", via_trigger=False, ZMM_KVER=K2)
    c.check("booting into it loads without building",
            not any(x.startswith("podman run") for x in b.calls()) and st["kernel"] == K2 and st["loaded"], b.calls())
    b.close()

    b = Box()
    b.run("install")
    for mod in ("apex", "gasket"):
        (b.sys / "module" / mod).rmdir()
    (b.dev / "apex_0").unlink()
    (b.state / "modules" / K / "apex.ko").unlink()
    (b.state / "modules" / K / "gasket.ko").unlink()
    st = b.run("load", via_trigger=False)
    c.check("a boot with no modules for the kernel builds them then",
            any(x.startswith("podman run") for x in b.calls()) and st["loaded"], (b.calls(), st))
    b.add_kernel("6.0.0-1.fc44.x86_64")
    b.run("prebuild", via_trigger=False)
    (b.mods / "6.0.0-1.fc44.x86_64" / "kernel").rmdir(); (b.mods / "6.0.0-1.fc44.x86_64").rmdir()
    st = b.run("prebuild", via_trigger=False)
    c.check("modules for a kernel that's gone are pruned", st["built_for"] == [K], st)
    b.close()

    b = Box()
    st = b.run("prebuild", via_trigger=False)
    c.check("prebuild does nothing where the driver was never installed",
            st == {} and not any(x.startswith(("podman", "curl")) for x in b.calls()), (st, b.calls()))
    b.close()

    c.section("other hosts")
    b = Box(headers=True)
    st = b.run("install")
    c.check("headers and a compiler on the host: built there, no container",
            any(x.startswith("make -C") for x in b.calls()) and not any(x.startswith("podman") for x in b.calls())
            and st["loaded"], b.calls())
    b.close()

    b = Box(os_id="debian", runtimes="docker")
    b.bin.joinpath("docker").write_text(STUBS["podman"].replace("podman", "docker")); b.bin.joinpath("docker").chmod(0o755)
    b.run("install")
    c.check("Debian on docker builds in a Debian container",
            any(x.startswith("docker run") and "debian:trixie" in x for x in b.calls()), b.calls())
    b.close()

    b = Box(os_id="alpine")
    st = b.run("install")
    c.check("a distro with no builder image says what to install instead",
            st["state"] == "failed" and "kernel headers" in st["detail"] and not st["loaded"], st)
    c.check("…and installs no boot service for a driver it couldn't build", not st["installed"], st)
    b.close()

    b = Box()
    b.flag("distro_apex")
    st = b.run("install")
    c.check("a distro-packaged apex module is used as is",
            "modprobe apex" in b.calls() and not any(x.startswith(("podman run", "insmod", "curl")) for x in b.calls())
            and st["source"] == "distro" and st["loaded"], (b.calls(), st))
    b.close()

    b = Box(backend="openrc")
    st = b.run("install")
    script = b.initd / "zmm-coral-driver"
    c.check("OpenRC gets an init script in the default runlevel",
            script.exists() and os.access(script, os.X_OK) and "coral_driver.sh\" load" in script.read_text()
            and "rc-update add zmm-coral-driver default" in b.calls() and st["enabled"], (b.calls(), st))
    b.close()

    b = Box(backend="none")
    st = b.run("install")
    c.check("no service manager: loaded now, and told it won't survive a reboot unaided",
            st["loaded"] and not st["installed"] and "at boot yourself" in st["detail"], st)
    b.close()

    c.section("refusals")
    b = Box(card=False)
    st = b.run("install")
    c.check("no card on the bus: nothing built or installed",
            st["state"] == "failed" and "no Coral" in st["detail"]
            and not any(x.startswith(("curl", "podman", "insmod")) for x in b.calls()), (st, b.calls()))
    b.close()

    b = Box()
    st = b.run("install", ZMM_CORAL_SRC_SHA256="0" * 64)
    c.check("source that fails its checksum is never built",
            st["state"] == "failed" and "checksum" in st["detail"]
            and not any(x.startswith(("podman", "make", "insmod")) for x in b.calls()), (st, b.calls()))
    c.check("…or kept", not (b.state / "src").exists())
    b.close()

    b = Box()
    b.flag("offline")
    st = b.run("install")
    c.check("no network says so", st["state"] == "failed" and "download" in st["detail"], st)
    b.close()

    b = Box()
    b.flag("secure_boot")
    st = b.run("install")
    c.check("Secure Boot: refuses up front rather than failing in insmod",
            st["state"] == "failed" and "Secure Boot" in st["detail"] and st["secure_boot"] is True
            and not any(x.startswith(("podman", "insmod")) for x in b.calls()), (st, b.calls()))
    b.close()

    b = Box()
    b.flag("bad_vermagic")
    st = b.run("install")
    c.check("a module built for the wrong kernel is not loaded",
            st["state"] == "failed" and "doesn't match" in st["detail"] and not b.built()
            and not any(x.startswith("insmod") for x in b.calls()), st)
    b.close()

    b = Box()
    b.flag("insmod_fail")
    st = b.run("install")
    c.check("the kernel refusing the module is reported, with no boot service left behind",
            st["state"] == "failed" and "refused" in st["detail"] and not st["installed"], st)
    b.close()

    c.section("thermal throttle")
    b = Box()
    b.run("install")
    d = b.sys / "class/apex/apex_0"
    (b.data / "data/coral/thermal").write_text("70\n")
    b.run("check")
    got = [(d / f"trip_point{i}_temp").read_text().strip() for i in range(3)]
    c.check("one number sets the three trip points 5 °C apart", got == ["70000", "75000", "80000"], got)
    (b.data / "data/coral/thermal").write_text("120")
    b.run("check")
    c.check("a value outside 50–85 °C is ignored",
            (d / "trip_point0_temp").read_text().strip() == "70000")

    c.section("remove")
    st = b.run("remove")
    c.check("unloads apex then gasket", [x for x in b.calls() if x.startswith("rmmod")] == ["rmmod apex", "rmmod gasket"], b.calls())
    c.check("removes the unit, the udev rule and the built modules",
            not (b.systemd / "zmm-coral-driver.service").exists() and not (b.udev / "65-zmm-apex.rules").exists()
            and not b.built() and st["installed"] is False and st["loaded"] is False, st)
    b.close()

    b = Box()
    b.run("install")
    b.flag("in_use")
    st = b.run("remove")
    c.check("a driver in use stays loaded until a reboot, and says so",
            st["loaded"] is True and "until a reboot" in st["detail"] and st["installed"] is False, st)
    b.close()
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
