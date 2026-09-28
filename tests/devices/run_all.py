#!/usr/bin/env python3
"""
Run every device-classification test.

    python3 tests/devices/run_all.py

Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

PY_MODULES = ["test_endpoint_kind", "test_quirk_corpus", "test_coordinator_quirk", "test_aqara_attributes", "test_cluster_discovery", "test_device_facts", "test_device_decisions", "test_device_identity", "test_probe_lite", "test_zmm_entries", "test_zmm_settings", "test_entry_rules", "test_reporting_heal"]


def main() -> int:
    passed, failures = 0, []
    for name in PY_MODULES:
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
