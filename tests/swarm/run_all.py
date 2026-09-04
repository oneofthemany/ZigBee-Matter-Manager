#!/usr/bin/env python3
"""
Run every Swarm Intelligence test.

    python3 tests/swarm/run_all.py

No test framework and no network: the resolver and the pairing logic are pure
functions over fake devices, and the API tests drive real routes through
Starlette's TestClient.

Exits non-zero if anything failed. A module whose dependency is missing is
skipped with a note rather than counted as a failure — only test_api.py needs
fastapi installed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_resolver", "test_network", "test_stigmergy",
           "test_suggestions", "test_diagnostics", "test_api"]


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
