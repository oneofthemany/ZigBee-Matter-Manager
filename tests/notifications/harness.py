"""
Shared scaffolding for the notification-rule tests.

modules.notification_rules is standard library only, so the real store and
engine run here unstubbed. Devices, the clock and delivery are stand-ins.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# App modules default their data dir to ./data; a test must never touch it.
if "ZMM_DATA_DIR" not in os.environ:
    os.environ["ZMM_DATA_DIR"] = tempfile.mkdtemp(prefix="zmm_test_data_")
    atexit.register(shutil.rmtree, os.environ["ZMM_DATA_DIR"], True)

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modules.notification_rules import NotificationRuleEngine, NotificationRuleStore  # noqa: E402


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
            print(f"    FAIL {label}  <- {detail!r}"[:400])
        return bool(ok)


class FakeDevice:
    def __init__(self, name: str, state: Optional[Dict[str, Any]] = None, available: bool = True):
        self.friendly_name = name
        self.state = dict(state or {})
        self.available = available

    def is_available(self) -> bool:
        return self.available


class Rig:
    """An engine over fake devices, a settable clock and a recording deliverer."""

    def __init__(self, tmp: Path, devices: Dict[str, FakeDevice],
                 tabs: Optional[Dict[str, List[str]]] = None, local_time: str = "12:00",
                 state_path: Optional[Path] = None, store: Optional[NotificationRuleStore] = None):
        self.store = store or NotificationRuleStore(tmp / "rules.json")
        self.devices = devices
        self.tabs = tabs or {}
        self.now = 1_000_000.0
        self.local_time = local_time
        self.sent: List[tuple] = []

        async def deliver(owner, payload):
            self.sent.append((owner, payload))

        self.engine = NotificationRuleEngine(
            self.store,
            get_devices=lambda: self.devices,
            get_names=lambda: {},
            get_tabs=lambda: self.tabs,
            deliver=deliver,
            clock=lambda: self.now,
            local_now=lambda: datetime.strptime(f"2026-10-02 {self.local_time}", "%Y-%m-%d %H:%M"),
            state_path=state_path,
        )

    def rule(self, owner: str = "alice", **fields) -> Dict[str, Any]:
        return self.store.create(owner, {"trigger": "motion_detected", "cooldownMinutes": 0, **fields})

    def change(self, ieee: str, **changed) -> List[tuple]:
        """Apply a state change as the device layer would, then notify the engine."""
        self.devices[ieee].state.update(changed)
        return self._run(lambda: self.engine.observe(ieee, changed))

    def sweep(self) -> List[tuple]:
        return self._run(self.engine.sweep_availability)

    def _run(self, fn) -> List[tuple]:
        before = len(self.sent)

        async def go():
            fn()
            await asyncio.sleep(0)       # let the delivery tasks run
            await asyncio.sleep(0)
            await asyncio.sleep(0.05)    # …and any state save handed to a thread

        asyncio.run(go())
        return self.sent[before:]
