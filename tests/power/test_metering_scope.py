"""
Step 6 of docs/plans/zmm-quirks.md: scaling and metering scope.

Scaling came only from configure(), which runs at join, so after a restart
the handler fell back to 1 (0.2 W read as 2 W), and a device that did not
answer its current divisor had the 1000 default overwritten with 1. Metering
scope is what each EP's power actually measures: the rules can only say which
EPs reported, so the scope itself comes from the user or a ZMM entry, and it
decides whether an EP publishes power and what the device total is.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from handlers.power import ElectricalMeasurementHandler
from modules import device_decisions, device_facts
from modules.device_facts import Fact, _j, record

IEEE = "54:ef:44:10:01:5a:14:eb"


class _Store:
    def __init__(self, profile=None):
        self.profile = profile

    def get_profile_for_device(self, **_):
        return self.profile


class _Cluster:
    cluster_id = 0x0B04

    def __init__(self, ep_id, cache=None, answers=None):
        self.endpoint = NS(endpoint_id=ep_id)
        self.cache = cache or {}
        self.answers = answers or {}
        self.sent = []

    def add_listener(self, _l):
        pass

    def get(self, attr):
        return self.cache.get(attr)

    async def bind(self):
        return [0]

    async def configure_reporting_multiple(self, records):
        self.sent.append(("configure_reporting", sorted(records)))
        return [[]]

    async def read_attributes(self, attrs, **_):
        return [{a: self.answers[a] for a in attrs if a in self.answers}, {}]


def _device(model="lumi.plug.aeu002"):
    return NS(ieee=IEEE, model=model, zigpy_dev=NS(model=model, manufacturer="Aqara"),
              state={}, handlers={}, update_state=lambda d, **_: None)


def _em(dev, ep, **kw):
    h = ElectricalMeasurementHandler(dev, _Cluster(ep, **kw))
    dev.handlers[(ep, 0x0B04)] = h
    return h


def _decision(ep, subject):
    for d in zigbee_cache.get_decisions(IEEE):
        if (d["endpoint_id"], d["subject"]) == (ep, subject):
            return d
    return None


def run() -> Checker:
    c = Checker("metering_scope")
    tmp = tempfile.mkdtemp(prefix="zmm_meter_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    device_facts.forget(IEEE)
    device_decisions.forget(IEEE)
    zigbee_cache.warm()
    device_profiles._store = _Store()
    try:
        c.section("scaling survives a restart")
        dev = _device()
        h = _em(dev, 1, cache={"ac_power_multiplier": 1, "ac_power_divisor": 10})
        h.device.update_state = lambda d, **_: dev.state.update(d)
        h.attribute_updated(h.ATTR_ACTIVE_POWER, 2)
        c.check("a fresh handler takes the divisor zigpy cached: 2 reads as 0.2 W",
                dev.state.get("power_1") == 0.2, dev.state)
        d = _decision(1, "scaling:active_power")
        c.check("and records where it came from",
                d and (d["value"], d["source"]) == ("x1/10", "answered"), d)

        aurora = _device("DoubleSocket50AU")
        ha = _em(aurora, 1, cache={"ac_power_divisor": 10})
        c.check("a power-only model keeps whole watts, whatever zigpy cached",
                ha._power_divisor == 1, ha._power_divisor)

        c.section("an unanswered divisor keeps its default")
        dev = _device("SmartPlug51AU")
        h = _em(dev, 1, answers={"ac_power_divisor": 10})
        asyncio.run(h.configure())
        c.check("current stays /1000 when the device does not answer it",
                h._current_divisor == 1000, h._current_divisor)
        c.check("power takes the answered /10", h._power_divisor == 10)
        d = _decision(1, "scaling:rms_current")
        c.check("the current default is recorded as a default",
                d and d["source"] == "default", d)

        c.section("a ZMM entry's scaling and absences win")
        entry = {"id": "lumi.plug.aeu002", "meta": {"source": "zmm"}, "endpoints": {},
                 "zmm": {"measurements": {"active_power": {"multiplier": 1, "divisor": 10},
                                          "rms_voltage": None, "rms_current": None}}}
        device_profiles._store = _Store(entry)
        dev = _device()
        h = _em(dev, 3)
        c.check("the entry's divisor applies with no device answer", h._power_divisor == 10)
        ids = [x["object_id"] for x in h.get_discovery_configs()]
        c.check("measurements the entry marks absent are never published", ids == ["power_3"], ids)
        c.check("nor polled", list(h.get_pollable_attributes()) == [h.ATTR_ACTIVE_POWER])

        c.section("metering scope")
        device_profiles._store = _Store()
        dev = _device()
        h1, h2, h3 = _em(dev, 1), _em(dev, 2), _em(dev, 3)
        record(IEEE, [Fact(1, "reports:0x0B04/0x050B", "observed", _j({"nonzero": True, "last": 2})),
                      Fact(2, "reports:0x0B04/0x050B", "observed", _j({"nonzero": True, "last": 2})),
                      Fact(3, "reports:0x0B04/0x050B", "observed", _j({"nonzero": False, "last": 0}))])
        h3.get_discovery_configs()
        d = _decision(3, "metering")
        c.check("with no setting, the rules say unknown and name the EPs that showed power",
                d["value"] == "unknown" and "EP1, EP2" in d["reason"], d)

        device_facts.set_user_fact(IEEE, 3, "metering", "none")
        c.check("an EP set to 'none' publishes no power sensor",
                h3.get_discovery_configs() == [] and h3.get_pollable_attributes() == {})
        c.check("and its old sensor is retracted",
                "power_3" in [x["object_id"] for x in h3.get_retired_discovery_configs()])
        dev.state.update({"power_1": 30.0, "power_2": 12.0, "power_3": 5.0})
        c.check("and it is left out of the device total",
                h1._total_power(1, 30.0) == 42.0, h1._total_power(1, 30.0))

        device_facts.set_user_fact(IEEE, 1, "metering", "device_total")
        dev.state["power"] = 30.0                   # the whole-device EP's reading
        c.check("with a whole-device EP, the total is that EP alone (no double count)",
                h2._total_power(2, 12.0) == 30.0 and h1._total_power(1, 31.0) == 31.0)
        name = h1.get_discovery_configs()[0]["config"]["name"]
        c.check("and its sensor says so", name == "Power (whole device)", name)
        c.check("the decision is the user's", _decision(1, "metering")["source"] == "user")

        c.section("an EP whose reading is another socket's (Aqara aeu002)")
        device_facts.set_user_fact(IEEE, 2, "metering", "measures:1,3")
        device_facts.set_user_fact(IEEE, 3, "metering", "measures:2")
        dev.state.clear()
        dev.update_state = lambda d, **_: dev.state.update(d)
        h1.attribute_updated(h1.ATTR_ACTIVE_POWER, 2000)     # EP1: the whole device
        h2.attribute_updated(h2.ATTR_ACTIVE_POWER, 1500)     # EP2: socket 1 + USB
        h3.attribute_updated(h3.ATTR_ACTIVE_POWER, 500)      # EP3: socket 2
        c.check("EP2's reading is published as socket 1's power, EP3's as socket 2's",
                dev.state.get("power_1") == 1500.0 and dev.state.get("power_2") == 500.0
                and "power_3" not in dev.state, dev.state)
        c.check("and the device total is EP1's reading, never a sum",
                dev.state.get("power") == 2000.0, dev.state)
        names = {h.endpoint.endpoint_id: h.get_discovery_configs()[0]["config"] for h in (h1, h2, h3)}
        c.check("HA names each sensor for what it measures",
                names[2]["name"] == "Power EP1 + EP3" and names[3]["name"] == "Power EP2"
                and names[1]["name"] == "Power (whole device)", names)
        c.check("and reads it from the right key",
                "power_1" in names[2]["value_template"] and "value_json.power " in
                names[1]["value_template"] + " ", names)
        c.check("an invalid scope is refused",
                __import__("modules.device_identity", fromlist=["x"]).validate("metering", "measures:x")
                is not None)
        for ep in (1, 2, 3):
            device_facts.clear_user_fact(IEEE, ep, "metering")

        entry = {"id": "x", "meta": {"source": "zmm"}, "endpoints": {"2": {"metering": "self"}}}
        device_profiles._store = _Store(entry)
        h2.get_discovery_configs()
        c.check("a ZMM entry's scope is used where the user has set none",
                (_decision(2, "metering")["value"], _decision(2, "metering")["source"]) == ("self", "zmm"))
    finally:
        device_profiles._store = None
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_facts.forget(IEEE)
        device_decisions.forget(IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
