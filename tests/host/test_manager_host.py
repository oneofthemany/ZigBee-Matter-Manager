"""manager/host.py: what the dashboard is told, and how a reboot is requested."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

from harness import REPO, Checker


def run() -> Checker:
    c = Checker("manager_host")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ZMM_DATA_DIR"] = tmp
        sys.path.insert(0, str(REPO))
        import manager.host as host
        host = importlib.reload(host)            # DATA_DIR is read at import
        data = Path(tmp) / "data"
        (data / "os_updates").mkdir(parents=True)
        (data / "os_updates.json").write_text(json.dumps({
            "os": "Fedora Linux 44 (Silverblue)", "pkg_manager": "rpm-ostree",
            "update_count": 0, "security_count": 0, "reboot_required": True,
            "staged_version": "44.20261004.0", "live_applicable": None, "reboot_packages": [],
            "packages": [], "checked_at": "2026-10-04T10:00:00Z"}))

        c.section("what the dashboard sees")
        d = host.detail()
        c.check("a staged update is reported with its version", d["staged_version"] == "44.20261004.0" and d["reboot_required"], d)
        c.check("…and flagged for attention", d["status"] == "attention", d["status"])
        c.check("live-apply fields pass through", "live_applicable" in d and d["reboot_packages"] == [], d)

        c.section("reboot request")
        ok, msg = host.request_reboot()
        c.check("writes the reboot trigger the host's path unit watches",
                ok and (data / "os_updates" / "reboot").exists(), msg)
        c.check("the card shows an OS action pending", host.detail()["apply_pending"] is True)
        ok2, msg2 = host.request_reboot()
        c.check("a second request while one is pending is refused", not ok2 and "in progress" in msg2, msg2)
        ok3, _ = host.request_apply()
        c.check("…and so is an apply", not ok3)
        (data / "os_updates" / "reboot").unlink()
        (data / "os_updates" / "apply_status.json").write_text(json.dumps({"state": "rebooting", "action": "reboot"}))
        ok4, _ = host.request_reboot()
        c.check("refused while the host reports it's already rebooting", not ok4)
    return c
