"""
Shared scaffolding for the host OS-update tests.

scripts/os_updates.sh and scripts/os_apply.sh run unmodified against stand-in
commands on PATH: rpm-ostree serves fixture JSON and records what it was asked
to do; curl serves a fixture Bodhi release list; sudo and systemctl record.
Nothing touches the real system — a reboot or rebase is a line in calls.log.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# App modules default their data dir to ./data; a test must never touch it.
if "ZMM_DATA_DIR" not in os.environ:
    os.environ["ZMM_DATA_DIR"] = tempfile.mkdtemp(prefix="zmm_test_data_")
    atexit.register(shutil.rmtree, os.environ["ZMM_DATA_DIR"], True)

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


class Checker:
    """Collects pass/fail lines so a module can report as a group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, label: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failures.append(f"{self.name}: {label}")
            print(f"    FAIL {label}  <- {detail!r}"[:600])
        return bool(ok)


RPM_OSTREE_STUB = r"""#!/bin/bash
F="$STUB_DIR"
echo "rpm-ostree $*" >> "$F/calls.log"
case "$1" in
  status)   cat "$F/status.json" ;;
  upgrade)
    if [[ "${2:-}" == "--check" ]]; then exit "$(cat "$F/check_rc" 2>/dev/null || echo 0)"; fi
    [[ -f "$F/status_after_upgrade.json" ]] && cp "$F/status_after_upgrade.json" "$F/status.json"
    exit 0 ;;
  db)       cat "$F/dbdiff.json" ;;
  apply-live|ex)
    [[ "$*" == *--help* ]] && exit 0
    exit "$(cat "$F/live_rc" 2>/dev/null || echo 0)" ;;
  rebase)   exit 0 ;;
esac
exit 0
"""

SIMPLE_STUBS = {
    "curl": '#!/bin/bash\necho "curl $*" >> "$STUB_DIR/calls.log"\ncat "$STUB_DIR/bodhi.json"\n',
    "sudo": '#!/bin/bash\n[[ "$1" == "-n" ]] && shift\n[[ "$1" == "true" ]] && exit 0\nexec "$@"\n',
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$STUB_DIR/calls.log"\n',
}


def bodhi(current: List[int], pending: List[int]) -> Dict[str, Any]:
    rel = [{"name": f"F{v}", "version": str(v), "state": "current", "id_prefix": "FEDORA"} for v in current]
    rel += [{"name": f"F{v}", "version": str(v), "state": "pending", "id_prefix": "FEDORA"} for v in pending]
    # Noise Bodhi really returns alongside the plain releases.
    rel += [{"name": "F99C", "version": "99", "state": "current", "id_prefix": "FEDORA-CONTAINER"},
            {"name": "EPEL-10", "version": "10", "state": "current", "id_prefix": "FEDORA-EPEL"}]
    return {"releases": rel}


def deployment(version: str, checksum: str, booted: bool, staged: bool = False,
               origin: str = "fedora:fedora/44/x86_64/silverblue", **extra) -> Dict[str, Any]:
    return {"version": version, "checksum": checksum, "booted": booted, "staged": staged,
            "origin": origin, "container-image-reference": None, **extra}


def cached_update(version: str, changed: List[str], advisories: Optional[List[list]] = None) -> Dict[str, Any]:
    return {"version": version,
            "advisories": advisories or [],
            "rpm-diff": {"upgraded": [[0, n, ["1", "x86_64"], ["2", "x86_64"]] for n in changed],
                         "downgraded": [], "removed": [], "added": []}}


def dbdiff(changed: List[str]) -> Dict[str, Any]:
    return {"pkgdiff": [[n, 2, {}] for n in changed]}


class Host:
    """A temp data dir plus stub commands; run either script against it."""

    def __init__(self, release: int = 44) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.data = root / "data_dir"
        self.stubs = root / "stubs"
        self.bin = root / "bin"
        for d in (self.data, self.stubs, self.bin):
            d.mkdir()
        (self.bin / "rpm-ostree").write_text(RPM_OSTREE_STUB)
        for name, body in SIMPLE_STUBS.items():
            (self.bin / name).write_text(body)
        for f in self.bin.iterdir():
            f.chmod(0o755)
        (root / "ostree-booted").write_text("")
        self.os_release = root / "os-release"
        self.os_release.write_text(f'ID=fedora\nVERSION_ID={release}\nPRETTY_NAME="Fedora Linux {release} (Silverblue)"\n')
        self.ostree_booted = root / "ostree-booted"
        self.set(bodhi=bodhi([release - 1, release], [release + 1, release + 2]))

    def set(self, **files: Any) -> "Host":
        for name, value in files.items():
            text = value if isinstance(value, str) else json.dumps(value)
            (self.stubs / f"{name}.json" if not name.endswith("_rc") else self.stubs / name).write_text(text)
        return self

    def trigger(self, name: str, content: str = "1") -> None:
        d = self.data / "data" / "os_updates"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content)

    def _run(self, script: str) -> subprocess.CompletedProcess:
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ['PATH']}",
               "STUB_DIR": str(self.stubs),
               "ZMM_DATA_DIR": str(self.data),
               "ZMM_OSTREE_BOOTED": str(self.ostree_booted),
               "ZMM_OS_RELEASE": str(self.os_release),
               "ZMM_BODHI_URL": "https://bodhi.example/releases"}
        return subprocess.run(["bash", str(SCRIPTS / script)], env=env, capture_output=True, text=True, timeout=60)

    def collect(self) -> Dict[str, Any]:
        self._run("os_updates.sh")
        return json.loads((self.data / "data" / "os_updates.json").read_text())

    def apply(self) -> Dict[str, Any]:
        self._run("os_apply.sh")
        status = self.data / "data" / "os_updates" / "apply_status.json"
        return json.loads(status.read_text()) if status.exists() else {}

    def calls(self) -> List[str]:
        log = self.stubs / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def close(self) -> None:
        self._tmp.cleanup()
