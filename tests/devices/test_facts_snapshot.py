"""
facts_snapshot serves get_facts from one read of the table, so building the
device list costs one DuckDB query instead of several per device.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from harness import Checker

import modules.zigbee_cache as zigbee_cache
from modules.device_facts import facts_from_probe, record

A, B, NONE = "aa:aa:aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb:bb:bb", "cc:cc:cc:cc:cc:cc:cc:cc"

PROBE = {"endpoints": {
    "1": {"clusters": {"in 0xFCC0": {"attributes": {
        "0x0009": {"discovered_type": "0x20/uint8", "acl": "RWP", "status": "0x00",
                   "raw": {"value": 0}, "mfr": "0x115F"},
        "0x0010": {"discovered_type": "0x10/bool", "acl": "RW", "status": "0x00",
                   "raw": {"value": True}, "mfr": "0x115F"}}}}},
    "2": {"clusters": {"in 0x0006": {"attributes": {
        "0x0000": {"discovered_type": "0x10/bool", "acl": "RP", "status": "0x00",
                   "raw": {"value": False}}}}}},
}}


def run() -> Checker:
    c = Checker("facts snapshot")
    tmp = tempfile.mkdtemp(prefix="zmm_snap_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db = None
    zigbee_cache._INITIALISED = False
    real_execute = zigbee_cache._safe_execute
    queries = []

    def counting(sql, params=None, **kw):
        if "FROM device_facts" in sql:
            queries.append(sql)
        return real_execute(sql, params, **kw)

    try:
        record(A, facts_from_probe(PROBE))
        record(B, facts_from_probe({"endpoints": {"1": PROBE["endpoints"]["1"]}}))
        live = {ieee: zigbee_cache.get_facts(ieee) for ieee in (A, B, NONE)}
        c.check("the fixture gives both devices facts", live[A] and live[B] and not live[NONE],
                {k: len(v) for k, v in live.items()})

        zigbee_cache._safe_execute = counting
        with zigbee_cache.facts_snapshot():
            snap = {ieee: zigbee_cache.get_facts(ieee) for ieee in (A, B, NONE)}
            for _ in range(8):
                zigbee_cache.get_facts(A)
                zigbee_cache.get_facts(B)
        c.check("each device reads the same rows, in the same order, as a live query",
                snap == live)
        c.check("a device with no facts reads none", snap[NONE] == [])
        c.check("every read in the block is served by one query", len(queries) == 1, len(queries))

        queries.clear()
        with zigbee_cache.facts_snapshot():
            zigbee_cache.get_facts(NONE)
            zigbee_cache.get_facts(NONE)
        c.check("a block whose first read finds nothing still loads only once",
                len(queries) == 1, len(queries))

        zigbee_cache._safe_execute = real_execute
        zigbee_cache.purge_device(B)
        c.check("outside a block, reads are live again", zigbee_cache.get_facts(B) == [])
        with zigbee_cache.facts_snapshot():
            c.check("and a new block sees the change", zigbee_cache.get_facts(B) == [])
    finally:
        zigbee_cache._safe_execute = real_execute
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db = None
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
