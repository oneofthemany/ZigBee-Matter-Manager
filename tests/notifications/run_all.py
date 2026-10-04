#!/usr/bin/env python3
"""
Run every notification-rule test.

    python3 tests/notifications/run_all.py

The engine and store tests are standard library only. test_rule_routes needs
FastAPI (use the lockfile venv from AGENTS.md); without it, it is reported as
skipped rather than passed. Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

PY_MODULES = ["test_rule_engine", "test_rule_store", "test_rule_state", "test_catalogue_sync", "test_rule_routes"]
NEEDS_FASTAPI = {"test_rule_routes"}


def main() -> int:
    passed, failures = 0, []
    try:
        import fastapi  # noqa: F401
        have_fastapi = True
    except ImportError:
        have_fastapi = False
    for name in PY_MODULES:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        if name in NEEDS_FASTAPI and not have_fastapi:
            print("  SKIPPED: FastAPI not installed (see AGENTS.md, The dev box)")
            continue
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
