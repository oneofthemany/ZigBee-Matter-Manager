"""
A DB warm-up leaves nothing slow for the first query on the loop.

DuckDB imports pandas the first time an execute binds parameters. Startup runs
each warm() on a thread; without that import in it, the first classification
paid it on the event loop. A fresh interpreter each, since an import is per
process, and a temp working directory, since telemetry_db opens ./data.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from harness import REPO, Checker

_PROBE = """
import json, sys
import modules.zigbee_cache as zc
zc.DB_PATH = sys.argv[1] + "/cache.duckdb"
before = "pandas" in sys.modules
zc.warm()
loaded = set(sys.modules)
zc.get_facts("00:11:22:33:44:55:66:77")
print(json.dumps({"before": before, "after_warm": "pandas" in loaded,
                  "added_by_query": sorted(m for m in set(sys.modules) - loaded
                                           if m.split(".")[0] in ("pandas", "numpy"))}))
"""


_TELEMETRY_PROBE = """
import json, sys
import modules.telemetry_db as t
before = "pandas" in sys.modules
t.warm()
loaded = set(sys.modules)
with t.read_cursor() as cur:
    cur.execute("SELECT count(*) FROM device_states WHERE ieee = ?", ["x"]).fetchall()
print(json.dumps({"before": before, "after_warm": "pandas" in loaded,
                  "added_by_query": sorted(m for m in set(sys.modules) - loaded
                                           if m.split(".")[0] in ("pandas", "numpy"))}))
"""


def _probe(c: Checker, name: str, code: str) -> None:
    tmp = tempfile.mkdtemp(prefix="zmm_warm_")
    out = subprocess.run([sys.executable, "-c", code, tmp], cwd=tmp, capture_output=True,
                         text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(REPO)})
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        c.check(f"the {name} probe ran", False, out.stderr[-400:])
        return
    c.check(f"nothing has imported pandas before the {name} warm-up", got["before"] is False, got)
    c.check("the warm-up imports it, off the loop", got["after_warm"] is True, got)
    c.check("so the first parameterised query imports nothing more",
            got["added_by_query"] == [], got)


def run() -> Checker:
    c = Checker("cache_warm")
    c.section("zigbee_cache.warm() pays DuckDB's pandas import")
    _probe(c, "zigbee cache", _PROBE)
    c.section("telemetry_db.warm() pays it too")
    _probe(c, "telemetry", _TELEMETRY_PROBE)
    return c
