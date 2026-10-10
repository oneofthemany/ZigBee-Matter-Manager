"""
Shared scaffolding for the house-mode and alarm tests — plain scripts, matching tests/media.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# App modules default their data dir to ./data; a test must never touch it.
if "ZMM_DATA_DIR" not in os.environ:
    os.environ["ZMM_DATA_DIR"] = tempfile.mkdtemp(prefix="zmm_test_data_")
    atexit.register(shutil.rmtree, os.environ["ZMM_DATA_DIR"], True)

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


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
