"""
Step 5 of docs/plans/zmm-quirks.md: ZMM entries (the format, where they load
from and what they override), the shipped lumi.plug.aeu002 entry, and the
draft generator that starts one from a device's evidence.
"""

from __future__ import annotations

import json
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
from modules.device_facts import Fact, _j, record
from modules.device_profiles import ProfileStore, normalise_profile
from modules.endpoint_kind import classify_device_endpoint
from modules.quirk_draft import draft_entry

REPO = Path(__file__).resolve().parents[2]
IEEE = "54:ef:44:10:01:5a:14:eb"


class _Cl:
    def __init__(self, cid, ep):
        self.cluster_id, self.endpoint = cid, ep

    def add_listener(self, _l):
        pass


def _outlet(dtype=0x0000):
    eps = {}
    for ep_id, ins in ((1, (0x0006, 0x0012, 0x0702, 0x0B04, 0xFCC0)),
                       (2, (0x0006, 0x0012, 0x0B04, 0xFCC0)),
                       (3, (0x0006, 0x0B04, 0xFCC0))):
        ep = NS(endpoint_id=ep_id, profile_id=0x0104, device_type=dtype, out_clusters={})
        ep.in_clusters = {c: _Cl(c, ep) for c in ins}
        eps[ep_id] = ep
    zdev = NS(endpoints={0: None, **eps}, node_desc=NS(is_mains_powered=True),
              model="lumi.plug.aeu002", manufacturer="Aqara")
    dev = NS(ieee=IEEE, zigpy_dev=zdev, state={}, handlers={})
    for ep_id, ep in eps.items():
        dev.handlers[(ep_id, 0x0006)] = OnOffHandler(dev, ep.in_clusters[0x0006])
        dev.handlers[(ep_id, 0x0B04)] = ElectricalMeasurementHandler(dev, ep.in_clusters[0x0B04])
    return dev


def _write(d: Path, name: str, body: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(body))


def run() -> Checker:
    c = Checker("zmm_entries")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_ent_"))
    try:
        c.section("the entry format")
        n = normalise_profile({
            "id": "x", "match": {"model": "x"},
            "endpoints": {"1": {"metering": "device_total", "actions": "multistate"},
                          "2": {"metering": "lots", "actions": "buttons"}},
            "zmm": {"corrections": {"device_type": "ignore", "model": "ignore"},
                    "measurements": {"rms_voltage": None,
                                     "active_power": {"cluster": "0x0B04", "attr": "0x050B", "divisor": 10},
                                     "junk": {"cluster": "nope"}},
                    "settings": [{"id": "led", "type": "bool", "ep": 1, "cluster": "0xFCC0",
                                  "attr": "0x0203", "mfr": "0x115F", "label": "Button LEDs"},
                                 {"id": "Bad Id", "type": "bool", "ep": 1, "cluster": 1, "attr": 1},
                                 {"id": "each_gang", "type": "uint8", "ep": "each", "cluster": 1, "attr": 2}],
                    "evidence": {"probes": ["p.json"]}}})
        c.check("endpoint metering and actions are kept when valid",
                n["endpoints"]["1"].get("metering") == "device_total"
                and n["endpoints"]["1"].get("actions") == "multistate"
                and "metering" not in n["endpoints"]["2"] and "actions" not in n["endpoints"]["2"])
        z = n["zmm"]
        c.check("only known corrections survive", z["corrections"] == {"device_type": "ignore"})
        c.check("a known-absent measurement is kept as null",
                "rms_voltage" in z["measurements"] and z["measurements"]["rms_voltage"] is None)
        c.check("a malformed measurement is dropped", "junk" not in z["measurements"])
        c.check("valid settings survive, invalid ones are dropped",
                [s["id"] for s in z["settings"]] == ["led", "each_gang"], z["settings"])
        c.check("evidence is kept", z["evidence"] == {"probes": ["p.json"]})

        c.section("where entries come from, and what wins")
        user, comm, zmm = tmp / "user", tmp / "community", tmp / "zmm"
        base = {"match": {"model": "m"}, "endpoints": {"1": {"label": ""}}}
        _write(comm, "m", {**base, "id": "m", "endpoints": {"1": {"label": "community"}}})
        _write(zmm, "m", {**base, "id": "m", "endpoints": {"1": {"label": "zmm"}}})
        st = ProfileStore(user_dir=str(user), bundled_dir=str(comm),
                          ieee_overrides_file=str(tmp / "i.json"), zmm_dir=str(zmm))
        p = st.get_profile_for_device(model="m")
        c.check("a ZMM entry beats a community profile",
                (p["meta"]["source"], p["endpoints"]["1"]["label"]) == ("zmm", "zmm"), p["meta"])
        saved = st.upsert_profile({"id": "m", "match": {"model": "m"},
                                   "endpoints": {"1": {"label": "mine"}}, "meta": {"source": "zmm"}})
        c.check("a saved profile can never claim to be a ZMM entry", saved["meta"]["source"] == "user")
        c.check("and the user's own beats the ZMM entry",
                st.get_profile_for_device(model="m")["endpoints"]["1"]["label"] == "mine")

        c.section("the shipped lumi.plug.aeu002 entry")
        shipped = ProfileStore(user_dir=str(tmp / "u2"), bundled_dir=str(tmp / "b2"),
                               ieee_overrides_file=str(tmp / "i2.json"),
                               zmm_dir=str(REPO / "zmm_quirks"))
        device_profiles._store = shipped
        entry = shipped.get_profile_for_device(model="lumi.plug.aeu002", manufacturer="Aqara")
        c.check("it loads as a ZMM entry", entry and entry["meta"]["source"] == "zmm")
        dev = _outlet()
        k = classify_device_endpoint(dev, dev.zigpy_dev.endpoints[3])
        c.check("it decides EP3, and says so",
                (k.kind, k.source, k.reason) == ("switch", "zmm", "ZMM entry lumi.plug.aeu002"), k)
        c.check("its labels name the HA entities",
                dev.handlers[(3, 0x0006)].get_discovery_configs()[0]["config"]["name"] == "USB"
                and [dev.handlers[(e, 0x0B04)].get_discovery_configs()[0]["config"]["name"]
                     for e in (1, 2, 3)]
                == ["Power (whole device)", "Power Socket 1 + USB", "Power Socket 2"])

        c.section("an entry can mark a model's declared type as wrong")
        _write(tmp / "z3", "lumi.plug.aeu002",
               {"id": "lumi.plug.aeu002", "match": {"model": "lumi.plug.aeu002"},
                "zmm": {"corrections": {"device_type": "ignore"}}})
        device_profiles._store = ProfileStore(user_dir=str(tmp / "u3"), bundled_dir=str(tmp / "b3"),
                                              ieee_overrides_file=str(tmp / "i3.json"),
                                              zmm_dir=str(tmp / "z3"))
        lying = _outlet(dtype=0x0009)       # declares Mains Power Outlet
        k = classify_device_endpoint(lying, lying.zigpy_dev.endpoints[3])
        c.check("the declared type is not used as evidence", "device type" not in k.reason, k)

        c.section("drafting an entry from evidence")
        device_profiles._store = ProfileStore(user_dir=str(tmp / "u4"), bundled_dir=str(tmp / "b4"),
                                              ieee_overrides_file=str(tmp / "i4.json"),
                                              zmm_dir=str(tmp / "z4"))
        zigbee_cache.DB_PATH = str(tmp / "cache.duckdb")
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_facts.forget(IEEE)
        device_decisions.forget(IEEE)
        zigbee_cache.warm()
        dev = _outlet()
        record(IEEE, [
            Fact(1, "attr:0x0B04/0x050B", "answered", _j({"type": "0x29/int16", "acl": "RP", "value": 0})),
            Fact(1, "attr:0x0B04/0x0605", "answered", _j({"type": "0x21/uint16", "acl": "RP", "value": 10})),
            Fact(1, "attr:0x0702/0x0000", "answered", _j({"type": "0x25/uint48", "acl": "RP", "value": 0})),
            Fact(1, "attr:0x0702/0x0302", "answered", _j({"type": "0x22/uint24", "acl": "RP", "value": 1000})),
            Fact(2, "attr:0xFCC0/0x0286@0x115F", "answered", _j({"type": "0x20/uint8", "acl": "RWP", "value": 1})),
            Fact(1, "attr:0xFCC0/0x0006@0x115F", "answered", _j({"type": "0x41/octstr", "acl": "RP"})),
        ])
        device_facts.set_user_fact(IEEE, 3, "label", "USB")
        d = draft_entry(dev)
        e = d["entry"]
        c.check("kinds and the user's label", e["endpoints"]["3"] == {"kind": "switch", "label": "USB"},
                e["endpoints"])
        c.check("button actions where the EP has Multistate",
                e["endpoints"]["1"].get("actions") == "multistate" and "actions" not in e["endpoints"]["3"])
        c.check("the controller type on actuator EPs is marked wrong",
                e["zmm"].get("corrections") == {"device_type": "ignore"})
        m = e["zmm"]["measurements"]
        c.check("power with its answered scaling", m["active_power"].get("divisor") == 10, m)
        c.check("voltage and current the device never answered are marked absent",
                m.get("rms_voltage", "?") is None and m.get("rms_current", "?") is None, m)
        c.check("energy on the EP that has it", m["energy"].get("ep") == 1
                and m["energy"].get("divisor") == 1000, m)
        c.check("metering scope is left out until something settles it",
                all("metering" not in v for v in e["endpoints"].values()))
        c.check("writable manufacturer attributes are candidates, read-only ones are not",
                [(x["ep"], x["attr"]) for x in d["candidates"]] == [(2, "0x0286")], d["candidates"])
        c.check("the draft is a valid entry", normalise_profile(e)["zmm"]["measurements"]["active_power"]
                ["divisor"] == 10)
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
