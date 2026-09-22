#!/usr/bin/env python3
"""
Run every upgrade test.

    python3 tests/upgrade/run_all.py

No network: GitHub's release list is a fixture, and version state lives in a
temporary directory rather than data/state.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_ordering"]


def main() -> int:
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
