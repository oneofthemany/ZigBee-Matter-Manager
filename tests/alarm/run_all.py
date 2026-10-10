#!/usr/bin/env python3
"""
Run every house-mode and alarm test.

    python3 tests/alarm/run_all.py

No network and no hardware. The route tests run only where FastAPI
imports; elsewhere they report themselves skipped.
Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

PY_MODULES = ["test_alarm", "test_house_mode", "test_house_routes"]


def main() -> int:
    passed, failures = 0, []
    for name in PY_MODULES:
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
