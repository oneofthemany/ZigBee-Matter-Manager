#!/usr/bin/env python3
"""
Run every object-detection test.

    python3 tests/vision/run_all.py

No camera, model or accelerator: a scripted detector stands in for the real
one. Needs numpy (the dev-box venv has it). Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_pipeline", "test_sidecar_api", "test_bridge", "test_metrics"]


def main() -> int:
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("SKIPPED: numpy not installed")
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
