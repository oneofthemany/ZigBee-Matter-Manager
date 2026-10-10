"""scripts/sidecar_service.sh: the sidecars' boot-time services on systemd, OpenRC and
neither. Beekeeper runs through the beekeeper_service.sh wrapper, as installed hosts call it."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from harness import SCRIPTS, Checker

NAME = "zigbee-matter-manager-beekeeper"

STUBS = {
    # systemctl keeps enabled/active state as marker files so is-enabled/is-active answer truthfully.
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$STUB_DIR/calls.log"
case "$1" in
  is-enabled) [[ -f "$STUB_DIR/sd_enabled" ]] ;;
  is-active)  [[ -f "$STUB_DIR/sd_active" ]] ;;
  enable)     touch "$STUB_DIR/sd_enabled" ;;
  disable)    rm -f "$STUB_DIR/sd_enabled"; [[ "$*" == *--now* ]] && rm -f "$STUB_DIR/sd_active"; true ;;
  restart|start) touch "$STUB_DIR/sd_active" ;;
  *) true ;;
esac''',
    "rc-update": r'''#!/bin/bash
echo "rc-update $*" >> "$STUB_DIR/calls.log"
case "$1" in
  add)  touch "$STUB_DIR/rc_enabled" ;;
  del)  rm -f "$STUB_DIR/rc_enabled" ;;
  show) [[ -f "$STUB_DIR/rc_enabled" ]] && echo "         zmm-beekeeper | default" ;;
esac; true''',
    "rc-service": r'''#!/bin/bash
echo "rc-service $*" >> "$STUB_DIR/calls.log"
case "$2" in
  status) [[ -f "$STUB_DIR/rc_active" ]] ;;
  restart|start) touch "$STUB_DIR/rc_active" ;;
  stop) rm -f "$STUB_DIR/rc_active" ;;
  *) true ;;
esac''',
    "podman": '#!/bin/bash\necho "podman $*" >> "$STUB_DIR/calls.log"\n',
    "docker": '#!/bin/bash\necho "docker $*" >> "$STUB_DIR/calls.log"\n',
    "sudo": '#!/bin/bash\n[[ "$1" == "-n" ]] && shift\n[[ "$1" == "true" ]] && exit 0\nexec "$@"\n',
}


class Box:
    def __init__(self, backend: str, runtimes: str = "podman docker"):
        self._t = tempfile.TemporaryDirectory()
        r = Path(self._t.name)
        self.data, self.stubs, self.bin = r / "data", r / "stubs", r / "bin"
        self.systemd, self.initd = r / "systemd", r / "initd"
        for d in (self.data, self.stubs, self.bin, self.systemd, self.initd):
            d.mkdir()
        for n, body in STUBS.items():
            (self.bin / n).write_text(body)
            (self.bin / n).chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "STUB_DIR": str(self.stubs),
                    "ZMM_DATA_DIR": str(self.data), "ZMM_SYSTEMD_DIR": str(self.systemd),
                    "ZMM_INITD_DIR": str(self.initd), "ZMM_RUNTIMES": runtimes,
                    "ZMM_SYSTEMD_RUN": str(r / ("sdrun" if backend == "systemd" else "absent")),
                    "ZMM_OPENRC_RUN": str(r / ("rcrun" if backend == "openrc" else "absent"))}
        (r / "sdrun").mkdir(); (r / "rcrun").mkdir()

    def run(self, action: str, sidecar: str = "beekeeper") -> dict:
        d = self.data / "data" / sidecar
        d.mkdir(parents=True, exist_ok=True)
        (d / "service_action").write_text(action)
        (self.stubs / "calls.log").write_text("")
        cmd = (["bash", str(SCRIPTS / "beekeeper_service.sh")] if sidecar == "beekeeper"
               else ["bash", str(SCRIPTS / "sidecar_service.sh"), sidecar])
        subprocess.run(cmd, env=self.env, capture_output=True, timeout=30)
        self.trigger_left = (d / "service_action").exists()
        return json.loads((d / "service_status.json").read_text())

    def calls(self):
        return (self.stubs / "calls.log").read_text().splitlines()

    def close(self):
        self._t.cleanup()


def run() -> Checker:
    c = Checker("beekeeper_service")

    c.section("systemd")
    b = Box("systemd")
    st = b.run("install")
    unit = b.systemd / "zmm-beekeeper.service"
    text = unit.read_text() if unit.exists() else ""
    c.check("writes a unit that supervises the container by name",
            f"start -a {NAME}" in text and "Restart=always" in text and "StartLimitIntervalSec=0" in text, text[:300])
    c.check("uses the host's own runtime binary", str(b.bin / "podman") in text)
    c.check("doesn't wait for the app — DNS shouldn't depend on Zigbee starting",
            "zigbee-matter-manager.service" not in text)
    c.check("enables it for boot and starts it",
            "systemctl enable zmm-beekeeper.service" in b.calls() and "systemctl restart zmm-beekeeper.service" in b.calls(), b.calls())
    c.check("reports installed, enabled and active",
            (st["backend"], st["installed"], st["enabled"], st["active"]) == ("systemd", True, True, True), st)
    c.check("consumes the trigger", not b.trigger_left)
    st = b.run("install")
    c.check("installing again with nothing changed doesn't bounce DNS",
            not any("restart" in x for x in b.calls()), b.calls())
    st = b.run("remove")
    c.check("remove disables, stops and deletes the unit",
            "systemctl disable --now zmm-beekeeper.service" in b.calls() and not unit.exists()
            and st["installed"] is False and st["enabled"] is False, (b.calls(), st))
    b.close()

    b = Box("systemd")
    (b.systemd / "beekeeper-dns.service").write_text(
        f"[Service]\nExecStart=/usr/bin/podman start -a {NAME}\nRestart=always\n")
    st = b.run("install")
    c.check("a hand-written unit already managing it is respected: no second unit",
            not (b.systemd / "zmm-beekeeper.service").exists() and st["conflict"] == "beekeeper-dns.service", st)
    c.check("…and the reason is reported", "already manages" in st["detail"], st["detail"])
    b.close()

    b = Box("systemd", runtimes="docker")
    b.run("install")
    c.check("a docker host gets a docker unit", f"docker start -a {NAME}" in (b.systemd / "zmm-beekeeper.service").read_text())
    b.close()

    b = Box("systemd")
    b.run("install", sidecar="vision")
    text = (b.systemd / "zmm-vision.service").read_text()
    c.check("the detection sidecar's unit waits for the Coral driver, whose device node it needs",
            "After=network-online.target zmm-coral-driver.service" in text
            and "start -a zigbee-matter-manager-vision" in text, text[:300])
    b.run("install", sidecar="go2rtc")
    c.check("…and the others don't", "zmm-coral-driver" not in (b.systemd / "zmm-go2rtc.service").read_text())
    b.close()

    c.section("OpenRC (Alpine, Gentoo)")
    b = Box("openrc")
    st = b.run("install")
    script = b.initd / "zmm-beekeeper"
    text = script.read_text() if script.exists() else ""
    c.check("writes a supervised init script", text.startswith("#!/sbin/openrc-run") and "supervise-daemon" in text
            and f"start -a {NAME}" in text and "need net" in text, text[:300])
    c.check("…executable", script.exists() and os.access(script, os.X_OK))
    c.check("adds it to the default runlevel and starts it",
            "rc-update add zmm-beekeeper default" in b.calls() and "rc-service zmm-beekeeper restart" in b.calls(), b.calls())
    c.check("reports installed, enabled and active",
            (st["backend"], st["installed"], st["enabled"], st["active"]) == ("openrc", True, True, True), st)
    st = b.run("remove")
    c.check("remove stops it, drops it from the runlevel and deletes the script",
            "rc-update del zmm-beekeeper default" in b.calls() and not script.exists() and st["installed"] is False, b.calls())
    b.close()

    c.section("no service manager")
    b = Box("none")
    st = b.run("install")
    c.check("says it's relying on the restart policy instead of pretending",
            st["backend"] == "none" and st["installed"] is False and "restart policy" in st["detail"], st)
    c.check("writes nothing", not any(b.systemd.iterdir()) and not any(b.initd.iterdir()))
    b.close()

    c.section("go2rtc")
    b = Box("systemd")
    st = b.run("install", "go2rtc")
    unit = b.systemd / "zmm-go2rtc.service"
    text = unit.read_text() if unit.exists() else ""
    c.check("go2rtc gets its own unit supervising its own container",
            "start -a zigbee-matter-manager-go2rtc" in text and "camera streaming" in text
            and "Restart=always" in text, text[:300])
    c.check("…enabled for boot and started",
            "systemctl enable zmm-go2rtc.service" in b.calls() and st["installed"] and st["active"], (b.calls(), st))
    c.check("…with its status beside go2rtc's config, not Beekeeper's",
            (b.data / "data" / "go2rtc" / "service_status.json").exists()
            and not (b.data / "data" / "beekeeper" / "service_status.json").exists())
    c.check("Beekeeper's unit is untouched", not (b.systemd / "zmm-beekeeper.service").exists())
    st = b.run("remove", "go2rtc")
    c.check("remove takes go2rtc's unit away", not unit.exists() and st["installed"] is False, st)
    b.close()
    b = Box("openrc")
    b.run("install", "go2rtc")
    c.check("OpenRC gets a go2rtc init script", (b.initd / "zmm-go2rtc").exists()
            and "rc-update add zmm-go2rtc default" in b.calls(), b.calls())
    b.close()

    c.section("input")
    b = Box("systemd")
    st = b.run("rm -rf /")
    c.check("an unknown action only checks", st["action"] == "check" and not (b.systemd / "zmm-beekeeper.service").exists(), st)
    b.close()
    r = subprocess.run(["bash", str(SCRIPTS / "sidecar_service.sh"), "../../etc"], capture_output=True, timeout=10)
    c.check("an unknown sidecar is refused before touching anything", r.returncode == 2)
    return c
