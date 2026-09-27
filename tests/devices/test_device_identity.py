"""
Step 3 of docs/plans/zmm-quirks.md: the identity panel's model shows each
endpoint's decisions with source and reason, and a user's confirmation or
correction is stored, applied everywhere and resettable.
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
from handlers.power import ElectricalMeasurementHandler
from modules import device_decisions, device_facts
from modules.device_capabilities import DeviceCapabilities
from modules.device_identity import apply_user_fact, identity

IEEE = "54:ef:44:10:01:5a:14:eb"


class _Store:
    def __init__(self, profile=None):
        self.profile = profile

    def get_profile_for_device(self, **_):
        return self.profile


class _Cl:
    def __init__(self, cid, ep):
        self.cluster_id, self.endpoint = cid, ep

    def add_listener(self, _l):
        pass


def _ep(ep_id, ins):
    ep = NS(endpoint_id=ep_id, profile_id=0x0104, device_type=0x0000, out_clusters={})
    ep.in_clusters = {c: _Cl(c, ep) for c in ins}
    return ep


def _outlet():
    eps = {1: _ep(1, (0x0006, 0x0012, 0x0B04, 0xFCC0)),
           2: _ep(2, (0x0006, 0x0012, 0x0B04, 0xFCC0)),
           3: _ep(3, (0x0006, 0x0B04, 0xFCC0))}
    zdev = NS(endpoints={0: None, **eps}, node_desc=NS(is_mains_powered=True),
              model="lumi.plug.aeu002", manufacturer="Aqara")
    dev = NS(ieee=IEEE, zigpy_dev=zdev, state={}, handlers={})
    for ep_id, ep in eps.items():
        dev.handlers[(ep_id, 0x0006)] = OnOffHandler(dev, ep.in_clusters[0x0006])
        dev.handlers[(ep_id, 0x0B04)] = ElectricalMeasurementHandler(dev, ep.in_clusters[0x0B04])
    dev.capabilities = DeviceCapabilities(dev)
    return dev


def _kind(ident, ep_id):
    ep = next(e for e in ident["endpoints"] if e["id"] == ep_id)
    return next(d for d in ep["decisions"] if d["subject"] == "kind")


def run() -> Checker:
    c = Checker("device_identity")
    device_profiles._store = _Store()
    tmp = tempfile.mkdtemp(prefix="zmm_id_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    device_facts.forget(IEEE)
    device_decisions.forget(IEEE)
    zigbee_cache.warm()
    try:
        dev = _outlet()
        c.section("what the panel shows")
        ident = identity(dev)
        c.check("every endpoint is listed", [e["id"] for e in ident["endpoints"]] == [1, 2, 3])
        k = _kind(ident, 3)
        c.check("EP3's kind, from the rules, with the reason",
                (k["value"], k["source"], k["user_set"]) == ("switch", "rule", False)
                and "0x0B04" in k["reason"], k)

        c.section("confirm")
        c.check("confirming stores the current value as the user's",
                apply_user_fact(dev, 3, "kind", "switch") is None
                and _kind(identity(dev), 3)["source"] == "user")

        c.section("correct")
        apply_user_fact(dev, 3, "kind", "light")
        k = _kind(identity(dev), 3)
        c.check("a correction is shown as the user's", (k["value"], k["source"]) == ("light", "user"), k)
        h3 = dev.handlers[(3, 0x0006)]
        c.check("HA discovery follows it",
                [x["component"] for x in h3.get_discovery_configs()] == ["light"])
        c.check("so do device capabilities", "light" in dev.capabilities.get_capabilities())
        c.check("the change is kept as old -> new",
                _kind(identity(dev), 3)["previous_value"] == "switch")

        c.section("labels name the endpoint everywhere")
        c.check("a label is accepted", apply_user_fact(dev, 3, "label", "  USB ") is None)
        ep3 = next(e for e in identity(dev)["endpoints"] if e["id"] == 3)
        c.check("and trimmed", ep3["label"] == "USB" and ep3["label_user_set"], ep3)
        c.check("the HA light takes the name", h3.get_discovery_configs()[0]["config"]["name"] == "USB")
        power = dev.handlers[(3, 0x0B04)].get_discovery_configs()[0]["config"]["name"]
        c.check("and its power sensor", power == "Power USB", power)
        c.check("an unlabelled EP keeps its number",
                dev.handlers[(2, 0x0006)].get_discovery_configs()[0]["config"]["name"] == "Switch 2")

        c.section("bad input is refused")
        c.check("an unknown kind", "must be one of" in (apply_user_fact(dev, 3, "kind", "bulb") or ""))
        c.check("an unknown subject", "cannot be set" in (apply_user_fact(dev, 3, "model", "x") or ""))
        c.check("an empty label", apply_user_fact(dev, 3, "label", "  ") is not None)
        c.check("an overlong label", apply_user_fact(dev, 3, "label", "x" * 41) is not None)
        c.check("a missing endpoint", "not found" in (apply_user_fact(dev, 9, "kind", "switch") or ""))

        c.section("reset")
        apply_user_fact(dev, 3, "kind", None)
        apply_user_fact(dev, 3, "label", None)
        k = _kind(identity(dev), 3)
        c.check("the rules decide again", (k["value"], k["source"], k["user_set"]) == ("switch", "rule", False), k)
        c.check("and the name returns to the number",
                h3.get_discovery_configs()[0]["config"]["name"] == "Switch 3")
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
