#!/usr/bin/env python3
"""
Run every floor-plan test.

    python3 tests/floor_plan/run_all.py

test_routes needs FastAPI, so on the dev box run it from the lockfile venv
(AGENTS.md); without it that module is skipped, not failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_store", "test_model", "test_mesh", "test_routes", "test_scopes"]


def main() -> int:
    passed, failures, skipped = 0, [], []
    for name in MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as e:
            skipped.append(f"{name} ({e.name} not installed)")
            continue
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        checker = module.run()
        passed += checker.passed
        failures.extend(checker.failures)
    print(f"\n{'=' * 62}")
    print(f"{passed} passed, {len(failures)} failed")
    for s in skipped:
        print(f"  skipped {s}")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
