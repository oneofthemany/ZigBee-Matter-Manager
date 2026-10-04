#!/usr/bin/env python3
"""
Run every frontend test.

    python3 tests/frontend/run_all.py

Node scripts that import the shipped static/js modules with stand-in browser
globals. Skipped, not passed, when node isn't installed. Exits non-zero if
anything failed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JS_TESTS = ["test_utils.mjs", "test_manager_link.mjs", "test_drive_track.mjs"]


def main() -> int:
    if not shutil.which("node"):
        print("SKIPPED: node not installed")
        return 0
    failures = []
    for name in JS_TESTS:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        result = subprocess.run(["node", str(HERE / "js" / name)], capture_output=True, text=True)
        print(result.stdout.rstrip() or result.stderr.rstrip())
        if result.returncode != 0:
            failures.append(name)
    print(f"\n{'=' * 62}")
    print(f"{len(JS_TESTS) - len(failures)} of {len(JS_TESTS)} test files passed")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
