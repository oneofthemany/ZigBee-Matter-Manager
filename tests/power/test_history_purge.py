"""
History rows a device could not physically have produced are found with the
same bounds the live check uses (modules/measurement_sanity.py), and deleted
together with the device totals computed from them. The Aurora double socket
wrote a month of -8011 W / +8011 W before that check existed.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import duckdb

import modules.device_profiles as device_profiles
import modules.telemetry_db as telemetry_db
from handlers.power import ElectricalMeasurementHandler
from modules.measurement_sanity import history_bounds

AURORA = "00:15:8d:00:02:56:f8:bf"
CLAMP = "aa:bb:cc:dd:ee:ff:00:01"


class _Store:
    def get_profile_for_device(self, **_):
        return None


class _Cluster:
    cluster_id = 0x0B04

    def __init__(self, ep_id, switched=True):
        self.endpoint = NS(endpoint_id=ep_id, in_clusters={0x0006: None} if switched else {})

    def add_listener(self, _l):
        pass

    def get(self, _attr):
        return None


def _device(ieee, model, eps, switched=True):
    dev = NS(ieee=ieee, model=model, zigpy_dev=NS(model=model, manufacturer="x"),
             state={}, handlers={})
    for ep in eps:
        dev.handlers[(ep, 0x0B04)] = ElectricalMeasurementHandler(dev, _Cluster(ep, switched))
    return dev


def _rows(db, ieee):
    return db.execute("SELECT attribute, numeric_val FROM device_states WHERE ieee = ? "
                      "ORDER BY ts, attribute", [ieee]).fetchall()


def run() -> Checker:
    c = Checker("history_purge")
    device_profiles._store = _Store()
    tmp = tempfile.mkdtemp(prefix="zmm_purge_")
    saved = telemetry_db._db
    db = duckdb.connect(str(Path(tmp) / "t.duckdb"))
    db.execute("""CREATE TABLE device_states (ts TIMESTAMP NOT NULL, ieee VARCHAR NOT NULL,
                  attribute VARCHAR NOT NULL, value VARCHAR, numeric_val DOUBLE)""")

    def put(minute, ieee, attr, val, second=0):
        db.execute("INSERT INTO device_states VALUES (TIMESTAMP '2026-09-26 05:00:00' "
                   "+ to_minutes(?) + to_seconds(?), ?, ?, ?, ?)",
                   [minute, second, ieee, attr, str(val), val])

    put(0, AURORA, "power_1", 36.0)          # before the power cut
    put(0, AURORA, "power", 36.0)
    for m in range(1, 6):                    # the fault: both EPs, the total derived
        put(m, AURORA, "power_2", 8011.0)
        put(m, AURORA, "power_1", -8011.0, second=1)
        put(m, AURORA, "power", 0.0, second=1)
    put(10, AURORA, "power_1", 38.0)          # after the second power cycle
    put(10, AURORA, "power", 38.0)
    put(3, CLAMP, "power_1", -500.0)          # a meter exporting: real
    telemetry_db._db = db
    try:
        aurora = _device(AURORA, "DoubleSocket50AU", (1, 2))
        bounds, aliases = history_bounds(aurora)
        c.check("bounds come from each EP's live check",
                bounds.get("power_1") == (0.0, 4000.0) and bounds.get("power_2") == (0.0, 4000.0),
                bounds)
        c.check("the total is tied to the EPs it sums", aliases.get("power") == ["power_1", "power_2"],
                aliases)

        counts = telemetry_db.implausible_states(AURORA, bounds, aliases)
        c.check("counting finds every impossible reading and the totals computed from them",
                counts == {"readings": 10, "derived": 5}, counts)
        c.check("and deletes nothing", len(_rows(db, AURORA)) == 19)

        counts = telemetry_db.implausible_states(AURORA, bounds, aliases, delete=True)
        left = _rows(db, AURORA)
        c.check("deleting removes exactly those", counts == {"readings": 10, "derived": 5}
                and len(left) == 4, left)
        c.check("readings from before and after the fault stay",
                left == [("power", 36.0), ("power_1", 36.0), ("power", 38.0), ("power_1", 38.0)], left)
        c.check("a second pass finds nothing",
                telemetry_db.implausible_states(AURORA, bounds, aliases) == {"readings": 0, "derived": 0})

        clamp = _device(CLAMP, "CT clamp", (1,), switched=False)
        cb, ca = history_bounds(clamp)
        c.check("a meter that switches nothing has no power bounds to enforce",
                "power_1" not in cb and telemetry_db.implausible_states(CLAMP, cb, ca)
                == {"readings": 0, "derived": 0})
        c.check("so its export stays", _rows(db, CLAMP) == [("power_1", -500.0)])
    finally:
        telemetry_db._db = saved
        device_profiles._store = None
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
