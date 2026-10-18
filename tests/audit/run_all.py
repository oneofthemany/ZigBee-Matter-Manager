#!/usr/bin/env python3
"""
Run every audit-tool test.

    python3 tests/audit/run_all.py

blocking_calls and docs_drift are stdlib only. sql_check and telemetry_health
need duckdb, and the config-mirror check needs PyYAML; without them those
modules report skipped, so run under the lockfile venv for full coverage.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODULES = ["test_blocking_calls", "test_docs_drift", "test_sql_check", "test_telemetry_health"]


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
