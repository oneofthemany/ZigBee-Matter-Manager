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
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_resolver", "test_network", "test_virtual", "test_stigmergy",
           "test_suggestions", "test_offers", "test_diagnostics", "test_api"]
# The browser code is exercised by slicing the real functions out of the shipped
# .js and running them, which is the only way to catch an undefined identifier
# inside a function body.
JS_TESTS = ["test_swarm_suggest.js"]


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
