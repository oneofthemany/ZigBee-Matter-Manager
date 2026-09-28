"""
The evidence store records what each endpoint declares, answers and reports,
and how each fact was learned (docs/plans/zmm-quirks.md §4).

Step 1 of the plan: facts are written, nothing decides from them yet.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.zigbee_cache as zigbee_cache
from modules.device_facts import (DEVICE, RANK, facts_from_device, facts_from_probe,
                                  record, record_declared)
from zigpy.quirks import CustomDevice


def _fresh_db():
    d = tempfile.mkdtemp(prefix="zmm_facts_")
    zigbee_cache.DB_PATH = str(Path(d) / "cache.duckdb")
    zigbee_cache._db = None
    zigbee_cache._INITIALISED = False
    return d


class _Cl:
    def __init__(self, cid):
        self.cluster_id = cid


def _ep(ins, outs=(), profile=0x0104, dtype=0x0000):
    return NS(profile_id=profile, device_type=dtype,
              in_clusters={c: _Cl(c) for c in ins}, out_clusters={c: _Cl(c) for c in outs})


def _aeu002():
    return NS(model="lumi.plug.aeu002", manufacturer="Aqara",
              node_desc=NS(logical_type=1, is_mains_powered=True, manufacturer_code=0x115F),
              endpoints={0: None, 1: _ep([0x0006, 0x0012, 0x0B04, 0xFCC0], [0x0019]),
                         3: _ep([0x0006, 0x0B04, 0xFCC0])})


# The shape device_probe writes (54ef4410015a14eb_20260927_131518.json)
PROBE = {
    "endpoints": {
        "1": {"clusters": {
            "in 0x0B04": {"attributes": {
                "0x050B": {"discovered_type": "0x29/int16", "acl": "RP", "status": "0x00",
                           "raw": {"value": 0}},
                "0x0505": {"status": "0x86"}}},
            "in 0xFCC0": {"attributes": {
                "0x0009": {"discovered_type": "0x20/uint8", "acl": "RWP", "status": "0x00",
                           "raw": {"value": 0}, "mfr": "0x115F"}}},
            "out 0x0019": {"attributes": {}}}},
    },
    "frames": [
        {"phase": "listen", "dir": "RX", "src_ep": 2, "cluster": "0x0B04",
         "command": "0x0A report", "records": [{"attr": "0x050B", "value": 2}]},
        {"phase": "listen", "dir": "RX", "src_ep": 2, "cluster": "0x0B04",
         "command": "0x0A report", "records": [{"attr": "0x050B", "value": 0}]},
        {"phase": "probe EP1 0x0B04", "dir": "RX", "src_ep": 1, "cluster": "0x0B04",
         "command": "0x01 read_rsp", "records": [{"attr": "0x050B", "value": 0}]},
    ],
}


def run() -> Checker:
    c = Checker("device_facts")
    tmp = _fresh_db()
    try:
        c.section("declared facts")
        facts = {(f.endpoint_id, f.subject): f for f in facts_from_device(_aeu002())}
        c.check("the model is a whole-device fact",
                json.loads(facts[(DEVICE, "model")].value) == "lumi.plug.aeu002")
        c.check("the node descriptor is recorded",
                json.loads(facts[(DEVICE, "mains_powered")].value) is True
                and json.loads(facts[(DEVICE, "manufacturer_code")].value) == 0x115F)
        c.check("each EP's device type is recorded, from the device itself",
                facts[(3, "device_type")].source == "declared_device"
                and json.loads(facts[(3, "device_type")].value) == 0)
        c.check("clusters by direction", (1, "cluster_in:0x0B04") in facts
                and (1, "cluster_out:0x0019") in facts and (3, "cluster_out:0x0019") not in facts)

        class LumiQuirk(CustomDevice):
            pass
        q = object.__new__(LumiQuirk)
        plain = _aeu002()
        q._model, q._manufacturer = plain.model, plain.manufacturer   # zigpy's backing fields
        q.node_desc, q.endpoints = plain.node_desc, plain.endpoints
        qf = {(f.endpoint_id, f.subject): f for f in facts_from_device(q)}
        c.check("a quirked device's structure is attributed to the quirk",
                qf[(1, "device_type")].source == "declared_quirk"
                and json.loads(qf[(DEVICE, "quirk")].value) == "LumiQuirk")

        c.section("probe facts")
        pf = {(f.endpoint_id, f.subject): f for f in facts_from_probe(PROBE)}
        c.check("an answered attribute, with type, access and value",
                json.loads(pf[(1, "attr:0x0B04/0x050B")].value)
                == {"type": "0x29/int16", "acl": "RP", "value": 0})
        c.check("an unsupported attribute", (1, "attr_unsupported:0x0B04/0x0505") in pf)
        c.check("manufacturer attributes are scoped by the maker's code",
                (1, "attr:0xFCC0/0x0009@0x115F") in pf)
        rep = pf[(2, "reports:0x0B04/0x050B")]
        c.check("reports seen while listening, on the EP that sent them",
                rep.source == "observed"
                and json.loads(rep.value) == {"count": 2, "last": 0, "nonzero": True},
                rep)
        c.check("reads during the probe are not counted as reports",
                (1, "reports:0x0B04/0x050B") not in pf)

        c.section("storage")
        n = record_declared("54:ef:44:10:01:5a:14:eb", _aeu002())
        rows = zigbee_cache.get_facts("54:ef:44:10:01:5a:14:eb")
        c.check("declared facts are stored", n == len(rows) and n > 10, (n, len(rows)))
        first = {(r["endpoint_id"], r["subject"]): r for r in rows}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ts = first[(DEVICE, "model")]["first_seen"]
        c.check("timestamps are UTC", abs(ts - now) < timedelta(minutes=1), (ts, now))
        record_declared("54:ef:44:10:01:5a:14:eb", _aeu002())
        again = {(r["endpoint_id"], r["subject"]): r
                 for r in zigbee_cache.get_facts("54:ef:44:10:01:5a:14:eb")}
        c.check("re-recording updates in place", len(again) == len(first))
        c.check("and keeps first_seen", again[(DEVICE, "model")]["first_seen"] == ts)
        record("54:ef:44:10:01:5a:14:eb", facts_from_probe(PROBE))
        subjects = {r["subject"] for r in zigbee_cache.get_facts("54:ef:44:10:01:5a:14:eb")}
        c.check("probe facts sit beside declared ones", "reports:0x0B04/0x050B" in subjects)
        zigbee_cache.purge_device("54:ef:44:10:01:5a:14:eb")
        c.check("removing the device removes its facts",
                zigbee_cache.get_facts("54:ef:44:10:01:5a:14:eb") == [])

        c.section("the attribute cache follows discovery")
        zigbee_cache.record_attribute_metadata("bb:bb", 3, 0x0B04, [
            {"id_int": 0x0000, "name": "measurement_type", "readable": True},
            {"id_int": 0x0001, "name": "0x0001", "readable": False}])
        zigbee_cache.keep_only_attributes("bb:bb", 3, 0x0B04, [0x0000])
        left = [r[0] for r in zigbee_cache._get_db().execute(
            "SELECT attribute_id FROM device_attributes WHERE ieee = 'bb:bb'").fetchall()]
        c.check("an attribute a fresh discovery no longer lists is dropped", left == [0], left)

        c.section("writes never run on the event loop")
        import asyncio
        import time

        async def loop_stays_live():
            ticks = []

            async def ticker():
                for _ in range(10):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.02)

            def slow_write(cur):
                time.sleep(0.5)           # an upsert on a busy file
            t = asyncio.create_task(ticker())
            started = time.monotonic()
            zigbee_cache.submit(slow_write)
            queued_in = time.monotonic() - started
            await t
            await zigbee_cache.drain()
            return queued_in, max(b - a for a, b in zip(ticks, ticks[1:]))

        queued_in, worst_gap = asyncio.run(loop_stays_live())
        c.check("a slow write is queued, not run, on the loop", queued_in < 0.05, queued_in)
        c.check("and the loop keeps ticking while it runs", worst_gap < 0.2, worst_gap)

        async def declared_in_loop():
            record_declared("aa:aa:aa:aa:aa:aa:aa:aa", _aeu002())
            before = len(zigbee_cache.get_facts("aa:aa:aa:aa:aa:aa:aa:aa"))
            await zigbee_cache.drain()
            return before, len(zigbee_cache.get_facts("aa:aa:aa:aa:aa:aa:aa:aa"))
        before, after = asyncio.run(declared_in_loop())
        c.check("start-up facts land after the loop moves on", before == 0 and after > 10,
                (before, after))

        rows = [(f.endpoint_id, f.subject, f.source, f.value) for f in facts_from_device(_aeu002())]
        cur = zigbee_cache._get_db().cursor()
        c.check("unchanged facts are not rewritten",
                zigbee_cache._upsert_facts(cur, "aa:aa:aa:aa:aa:aa:aa:aa", rows,
                                           datetime(2026, 1, 1)) == 0)

        c.section("ranking")
        c.check("a user's word outranks everything, the device's own claim ranks last",
                RANK["user"] == 0 and RANK["declared_device"] == max(RANK.values()))
    finally:
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db = None
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
