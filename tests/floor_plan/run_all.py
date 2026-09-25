#!/usr/bin/env python3
"""
Run every floor-plan test.

    python3 tests/floor_plan/run_all.py

test_routes needs FastAPI, so on the dev box run it from the lockfile venv
(AGENTS.md); without it that module is skipped, not failed.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_store", "test_model", "test_mesh", "test_radio", "test_coverage_store",
           "test_routes", "test_scopes"]
# The editor's own geometry maths, sliced out of the shipped .js and run.
JS_TESTS = ["test_calibrate.js", "test_bg_placement.js", "test_daylight_field.js",
            "test_room_corners.js", "test_mapgeo.js"]


def run_node() -> tuple[list[str], list[str]]:
    if not shutil.which("node"):
        return [], ["the JS tests (node not installed)"]
    failures = []
    for name in JS_TESTS:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        result = subprocess.run(["node", str(HERE / "js" / name)], capture_output=True, text=True)
        print(result.stdout.rstrip() or result.stderr.rstrip())
        if result.returncode != 0:
            failures.append(name)
    return failures, []


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
    js_failures, js_skipped = run_node()
    failures.extend(js_failures)
    skipped.extend(js_skipped)

    print(f"\n{'=' * 62}")
    print(f"{passed} passed, {len(failures)} failed")
    for s in skipped:
        print(f"  skipped {s}")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
