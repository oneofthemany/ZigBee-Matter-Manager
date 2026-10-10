#!/usr/bin/env python3
"""
Run every manager test.

    python3 tests/manager/run_all.py

The manager modules against stand-ins (a fake container runtime on a unix
socket); needs httpx. Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_images", "test_beekeeper_sync", "test_beekeeper_autostart", "test_go2rtc_sidecar",
           "test_accelerators", "test_routes"]


def main() -> int:
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("SKIPPED: httpx not installed")
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
