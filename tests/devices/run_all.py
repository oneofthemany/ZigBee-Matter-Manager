#!/usr/bin/env python3
"""
Run every device-classification test.

    python3 tests/devices/run_all.py

Exits non-zero if anything failed.
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

PY_MODULES = ["test_endpoint_kind", "test_quirk_corpus", "test_coordinator_quirk", "test_aqara_attributes", "test_cluster_discovery", "test_device_facts", "test_device_decisions", "test_device_identity", "test_probe_lite", "test_zmm_entries", "test_zmm_settings", "test_entry_rules", "test_reporting_heal", "test_learning_ops", "test_device_learning", "test_learning_write", "test_nuki_names", "test_cache_warm", "test_groups_delete"]

# The entry editor's form logic, run from the shipped .js.
JS_TESTS = ["test_quirk_editor.js"]


def run_node() -> list[str]:
    if not shutil.which("node"):
        print("  skipped the JS tests (node not installed)")
        return []
    failures = []
    for name in JS_TESTS:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        result = subprocess.run(["node", str(HERE / "js" / name)], capture_output=True, text=True)
        print(result.stdout.rstrip() or result.stderr.rstrip())
        if result.returncode != 0:
            failures.append(name)
    return failures


def main() -> int:
    passed, failures = 0, []
    for name in PY_MODULES:
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")
        checker = importlib.import_module(name).run()
        passed += checker.passed
        failures.extend(checker.failures)
    failures.extend(run_node())

    print(f"\n{'=' * 62}")
    print(f"{passed} passed, {len(failures)} failed")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
