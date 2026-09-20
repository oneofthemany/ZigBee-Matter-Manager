#!/usr/bin/env python3
"""
Run every media test.

    python3 tests/media/run_all.py

No test framework and no network, matching tests/fuel and tests/swarm. Nothing
here imports tidalapi or FastAPI: these exercise the per-user Tidal account
registry, which is plain Python over the filesystem.

Exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

PY_MODULES = ["test_tidal_accounts", "test_tidal_owner",
              "test_tidal_routes",
              "test_tidal_manifest_token", "test_tidal_automation",
              "test_sonos_player", "test_airplay_player",
              "test_zone_reload_probe", "test_zone_model_trim",
              "test_zone_trim_graph", "test_zone_align"]


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
