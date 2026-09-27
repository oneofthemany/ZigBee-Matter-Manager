"""
Step 2 of docs/plans/zmm-quirks.md: conclusions are stored with their source
and reason, change is kept as old -> new, and a user's correction outranks
every rule.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from handlers.general import OnOffHandler
from modules import device_decisions, device_facts
from modules.endpoint_kind import LIGHT, SWITCH, classify_device_endpoint

IEEE = "54:ef:44:10:01:5a:14:eb"


class _Store:
    def get_profile_for_device(self, **_):
        return None


class _Cl:
    def __init__(self, cid, ep):
        self.cluster_id, self.endpoint = cid, ep

    def add_listener(self, _l):
        pass


def _usb_ep():
    ep = NS(endpoint_id=3, profile_id=0x0104, device_type=0x0000, out_clusters={})
    ep.in_clusters = {c: _Cl(c, ep) for c in (0x0004, 0x0005, 0x0006, 0x0B04, 0xFCC0)}
    return ep


def _device(ep):
    zdev = NS(endpoints={0: None, 3: ep}, node_desc=NS(is_mains_powered=True),
              model="lumi.plug.aeu002", manufacturer="Aqara")
    return NS(ieee=IEEE, zigpy_dev=zdev, state={}, handlers={})


def run() -> Checker:
    c = Checker("device_decisions")
    device_profiles._store = _Store()
    tmp = tempfile.mkdtemp(prefix="zmm_dec_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    device_facts.forget(IEEE)
    device_decisions.forget(IEEE)
    try:
        c.section("before the cache is open")
        c.check("corrections are not read, so the DB is never opened here",
                device_facts.user_facts(IEEE) == {} and zigbee_cache._db is None)
        c.check("decisions are not written",
                device_decisions.record(IEEE, 3, "kind", SWITCH, "rule", "x") is False
                and zigbee_cache._db is None)

        zigbee_cache.warm()
        c.section("decisions")
        rec = device_decisions.record
        c.check("a first decision is stored, not reported as a change",
                rec(IEEE, 3, "kind", LIGHT, "rule", "vendor cluster") is False
                and zigbee_cache.get_decisions(IEEE)[0]["value"] == LIGHT)
        before = zigbee_cache.get_decisions(IEEE)[0]["decided_at"]
        rec(IEEE, 3, "kind", LIGHT, "rule", "vendor cluster")
        c.check("an unchanged decision is not rewritten",
                zigbee_cache.get_decisions(IEEE)[0]["decided_at"] == before)
        c.check("a change is reported",
                rec(IEEE, 3, "kind", SWITCH, "rule", "load cluster 0x0B04") is True)
        row = zigbee_cache.get_decisions(IEEE)[0]
        c.check("and kept as old -> new with when",
                (row["previous_value"], row["value"]) == (LIGHT, SWITCH)
                and row["changed_at"] is not None, row)
        rec(IEEE, 3, "kind", SWITCH, "zmm", "ZMM entry lumi.plug.aeu002")
        row = zigbee_cache.get_decisions(IEEE)[0]
        c.check("a new reason for the same value keeps the last real change",
                row["previous_value"] == LIGHT and row["source"] == "zmm", row)

        c.section("a user's correction outranks the rules")
        ep = _usb_ep()
        dev = _device(ep)
        c.check("the rules say switch", classify_device_endpoint(dev, ep).kind == SWITCH)
        device_facts.set_user_fact(IEEE, 3, "kind", LIGHT)
        k = classify_device_endpoint(dev, ep)
        c.check("a correction makes it a light, attributed to the user",
                (k.kind, k.source) == (LIGHT, "user"), k)
        h = OnOffHandler(dev, ep.in_clusters[0x0006])
        c.check("HA discovery follows", [x["component"] for x in h.get_discovery_configs()]
                == ["light"])
        c.check("and the decision is stored as the user's",
                zigbee_cache.get_decisions(IEEE)[0]["source"] == "user")
        device_facts.forget(IEEE)
        c.check("the correction survives a restart (reloaded from the store)",
                device_facts.user_facts(IEEE).get((3, "kind")) == LIGHT)
        device_facts.clear_user_fact(IEEE, 3, "kind")
        c.check("resetting it returns to the rules",
                classify_device_endpoint(dev, ep).kind == SWITCH)
        c.check("and removes the stored row",
                not any(f["source"] == "user" for f in zigbee_cache.get_facts(IEEE)))
    finally:
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_facts.forget(IEEE)
        device_decisions.forget(IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
