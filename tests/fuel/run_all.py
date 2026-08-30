#!/usr/bin/env python3
"""
Run every fuel test.

    python3 tests/fuel/run_all.py

No test framework and no network. The Python modules exercise the parsers,
the service and the history schema; the Node ones exercise the browser code by
slicing the real functions out of the shipped .js files and running them, which
is the only way to catch an undefined identifier inside a function body.

Exits non-zero if anything failed. A module whose dependency is missing is
skipped with a note rather than counted as a failure — duckdb is the only one,
and only test_history.py needs it.
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

PY_MODULES = ["test_base", "test_service", "test_providers", "test_history"]
ANDROID_CHECK = "check_android.py"
JS_TESTS = ["test_formatters.js", "test_settings.js", "test_drive_render.js"]


def run_python() -> tuple[int, list[str], list[str]]:
    passed, failures, skipped = 0, [], []
    for name in PY_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as e:
            skipped.append(f"{name} ({e.name} not installed)")
            continue
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        checker = module.run()
        passed += checker.passed
        failures.extend(checker.failures)
    return passed, failures, skipped


def run_node() -> tuple[list[str], list[str]]:
    failures, skipped = [], []
    if not shutil.which("node"):
        return failures, ["the JS tests (node not installed)"]
    for name in JS_TESTS:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        result = subprocess.run(["node", str(HERE / "js" / name)],
                                capture_output=True, text=True)
        print(result.stdout.rstrip() or result.stderr.rstrip())
        if result.returncode != 0:
            failures.append(name)
    return failures, skipped


def run_android() -> list[str]:
    """Static checks on the Kotlin. Not a compile — see the script's docstring."""
    print(f"\n{'=' * 62}\n{ANDROID_CHECK}\n{'=' * 62}")
    result = subprocess.run([sys.executable, str(HERE / ANDROID_CHECK)],
                            capture_output=True, text=True)
    print(result.stdout.rstrip() or result.stderr.rstrip())
    return [ANDROID_CHECK] if result.returncode else []


def main() -> int:
    passed, failures, skipped = run_python()
    js_failures, js_skipped = run_node()
    failures.extend(js_failures)
    failures.extend(run_android())
    skipped.extend(js_skipped)

    print(f"\n{'=' * 62}")
    print(f"{passed} python checks passed")
    for note in skipped:
        print(f"SKIPPED: {note}")
    if failures:
        print(f"{len(failures)} FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
