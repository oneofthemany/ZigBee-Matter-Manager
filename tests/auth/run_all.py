#!/usr/bin/env python3
"""
Run every auth test.

    python3 tests/auth/run_all.py

No test framework and no network, matching tests/media and tests/fuel. Nothing
here imports FastAPI: the route surface is read out of the source files, so
this runs on the host python without a venv.

Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

PY_MODULES = ["test_scope_coverage"]

#: Needs FastAPI, so it is skipped rather than failed on a bare host. CI has
#: the lockfile installed and runs it.
NEEDS_FASTAPI = ["test_middleware_enforcement"]


def main() -> int:
    passed, failures = 0, []
    try:
        import fastapi  # noqa: F401
        modules = PY_MODULES + NEEDS_FASTAPI
    except ImportError:
        modules = PY_MODULES
        print("note: FastAPI absent — skipping " + ", ".join(NEEDS_FASTAPI))

    for name in modules:
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
