#!/usr/bin/env python3
"""
Run every host OS-update test.

    python3 tests/host/run_all.py

The real scripts/os_updates.sh and os_apply.sh against stand-in commands; needs
bash and jq, nothing else, and never touches the real system. Exits non-zero if
anything failed.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_rpm_ostree", "test_manager_host", "test_reboot_route", "test_beekeeper_service", "test_coral_driver"]


def main() -> int:
    if not shutil.which("jq"):
        print("SKIPPED: jq not installed")
        return 0
    passed, failures = 0, []
    for name in MODULES:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        checker = importlib.import_module(name).run()
        passed += checker.passed
        failures.extend(checker.failures)
    print(f"\n{'=' * 62}")
    print(f"{passed} passed, {len(failures)} failed")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
