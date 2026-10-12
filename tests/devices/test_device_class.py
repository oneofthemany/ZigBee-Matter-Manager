"""
What a device is, decided once from its own evidence — and the evidence that
feeds it. The shapes are real devices: endpoints, clusters and power source as
they declare them.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from modules import device_class, device_facts, learning_ops, learning_recipes
from modules.device_class import Evidence, classify, gather
from modules.device_profiles import normalise_profile
from modules.device_profiles_apply import transform_state_with_profile

# name -> (functions per EP, server clusters per EP, client clusters per EP, capabilities)
SHAPES = {
    "metered socket":   ({1: "switch", 2: "switch"}, {1: {0, 6, 8, 0xB04}, 2: {0, 6, 0xB04}}, {1: {0x19}}, set()),
    "plain socket":     ({1: "switch", 2: "switch"}, {1: {0, 4, 5, 6}, 2: {4, 5, 6}}, {1: {0x19}}, set()),
    "colour bulb":      ({11: "light"}, {11: {0, 6, 8, 0x300, 0x1000}}, {11: {0x19}}, set()),
    "white bulb":       ({11: "light"}, {11: {0, 6, 8, 0x1000}}, {11: {0x19}}, set()),
    "boiler receiver":  ({5: "thermostat", 6: "thermostat"}, {5: {0, 3, 0x201}, 6: {0x201}}, {5: {0x402}}, set()),
    "radiator valve":   ({1: "thermostat"}, {1: {0, 1, 6, 0x20, 0x201, 0xFC11}}, {1: {0xA, 0x19}}, set()),
    "motion sensor":    ({}, {1: {0}, 2: {0, 1, 0x400, 0x402, 0x406}}, {1: {6, 8, 0x300}}, set()),
    "datapoint blind":  ({}, {1: {0, 4, 5, 0xEF00}}, {1: {0xA, 0x19}}, {"cover"}),
    "door magnet":      ({}, {1: {0, 3, 6, 0xFFFF}}, {1: {0, 4}}, {"contact_sensor"}),
    "wall remote":      ({}, {1: {0, 1, 3}}, {1: {6, 8}}, set()),
    "temperature puck": ({}, {1: {0, 1, 0x402, 0x405}}, {}, set()),
    "bare router":      ({}, {1: {0, 3}}, {}, set()),
}
WANT = {
    "metered socket": "plug", "plain socket": "switch", "colour bulb": "color_light",
    "white bulb": "light", "boiler receiver": "thermostat", "radiator valve": "thermostat",
    "motion sensor": "motion_sensor", "datapoint blind": "blind", "door magnet": "contact_sensor",
    "wall remote": "button", "temperature puck": "temperature_sensor", "bare router": "generic",
}


class _Store:
    def __init__(self, profile=None):
        self.profile = profile

    def get_profile_for_device(self, **_):
        return self.profile

    def get_ieee_mappings(self, _ieee):
        return {}


def _device(in_clusters, mains=False, model="X1", manufacturer="Acme"):
    eps = {i: NS(endpoint_id=i, in_clusters={c: NS(cluster_id=c) for c in cl}, out_clusters={},
                 profile_id=0x0104, device_type=0x0301) for i, cl in in_clusters.items()}
    return NS(ieee="aa:bb:cc:00:00:00:00:02", state={}, handlers={},
              zigpy_dev=NS(endpoints={0: None, **eps}, model=model, manufacturer=manufacturer,
                           node_desc=NS(is_mains_powered=mains)))


def run() -> Checker:
    c = Checker("device_class")

    c.section("the rules, on real device shapes")
    for name, (fns, cin, cout, caps) in SHAPES.items():
        got = classify(Evidence(fns, cin, cout, set(), caps))
        c.check(f"a {name} is a {WANT[name]}", got.kind == WANT[name], got)
    for zone, want in ((0x0015, "contact_sensor"), (0x002A, "leak_sensor"), (0x0028, "smoke_sensor"),
                       (0x000D, "motion_sensor")):
        got = classify(Evidence({}, {1: {0, 1, 0x500}}, {}, {zone}, set()))
        c.check(f"an IAS zone of type 0x{zone:04X} is a {want}", got.kind == want, got)
    got = classify(Evidence({}, {1: {0, 1, 0x500}}, {}, set(), set()))
    c.check("an IAS zone that never gave its type is not guessed at", got.kind == "generic", got)
    got = classify(Evidence({1: "thermostat"}, {1: {6, 0x201, 0x402}}, {}, set(), {"motion_sensor"}))
    c.check("an actuator outranks a sensor on the same device", got.kind == "thermostat", got)
    valve = Evidence({1: "thermostat"}, {1: {0, 1, 0x201}}, {}, set(), set(),
                     frozenset({"motor_calibration"}))
    c.check("a thermostat that calibrates a valve is a radiator valve",
            classify(valve).kind == "trv", classify(valve))
    stat = NS(ieee="", capabilities=None, supports_setting=lambda n: False,
              zigpy_dev=NS(endpoints={1: NS(endpoint_id=1, in_clusters={0x201: NS()}, out_clusters={},
                                            profile_id=0x104, device_type=0x301)}))
    c.check("a thermostat that takes no valve setting is not one",
            classify(gather(stat)).kind == "thermostat", classify(gather(stat)))
    stat.supports_setting = lambda n: n == "valve_detection"
    c.check("the valve settings a device takes are gathered as evidence",
            classify(gather(stat)).kind == "trv", classify(gather(stat)))

    c.section("overrides, and where the answer is read")
    device_profiles._store = _Store()
    dev = _device({1: [0, 1, 6, 0x201, 0xFC11]})
    c.check("with no entry, the valve is what its endpoint says",
            device_class.device_class(dev).kind == "thermostat")
    device_profiles._store = _Store(normalise_profile(
        {"id": "X1", "match": {"model": "X1", "manufacturer": "Acme"}, "device_type": "trv",
         "meta": {"source": "zmm"}}))
    dc = device_class.device_class(dev)
    c.check("the cached answer holds until something changes", dc.kind == "thermostat")
    device_class.invalidate(dev)
    dc = device_class.device_class(dev)
    c.check("an entry names it a radiator valve", (dc.kind, dc.source) == ("trv", "zmm"), dc)
    c.check("an entry for another maker's model does not",
            device_class.device_class(_device({1: [0x201]}, manufacturer="Other")).kind == "thermostat")
    described = device_class.describe(dev)
    c.check("the API carries the label, the reason and the power source",
            described["label"] == "Radiator valve" and described["power"] == "battery"
            and described["reason"], described)
    c.check("power comes from the node descriptor, not the model's name",
            device_class.is_mains_powered(_device({1: [6]}, mains=True, model="battery thing")) is True
            and device_class.is_mains_powered(_device({1: [6]}, mains=False, model="smart plug")) is False)

    c.section("an entry names an attribute, however its raw key is spelled")
    device_profiles._store = _Store(normalise_profile(
        {"id": "X1", "match": {"model": "X1", "manufacturer": "Acme"},
         "endpoints": {"1": {"clusters": {"0xFC99": {"attributes": {
             "0x0002": {"name": "floor_temperature", "scale": 100},
             "0x0005": {"name": "mode", "value_map": {"0": "eco", "1": "comfort"}}}}}}}}))
    out = transform_state_with_profile(dev, {"attr_fc99_0002": 700, "cluster_fc99_attr_0005": 1,
                                             "attr_fc99_0009": 4})
    c.check("a registered handler's fallback key is named and scaled",
            out.get("floor_temperature") == 7.0, out)
    c.check("the generic handler's key still is", out.get("mode") == "comfort", out)
    c.check("an attribute the entry does not name is left alone",
            out.get("attr_fc99_0009") == 4 and len(out) == 5, out)
    device_profiles._store = None

    c.section("learning a numeric setting by setting it on the device")
    writable = {(1, 0xFC99, 0x0002, None): "int16", (1, 0xFC99, 0x0003, None): "uint8"}
    base = {(1, 0xFC99, 0x0002, None): 700, (1, 0xFC99, 0x0003, None): 100}
    rec = lambda attr, value: {"ep": 1, "cluster": 0xFC99, "attr": attr, "mfr": None, "value": value}  # noqa: E731
    step = {"inputs": {"setting_id": "floor_temperature", "label": "Floor", "new_value": 9.5, "unit": "°C"}}
    got = learning_ops.attribute_that_became([rec(0x0002, 950)], base, step, {"writable": writable})
    c.check("the attribute that took the value is proposed, with the scale it implies",
            got[0]["path"] == "zmm.settings" and got[0]["value"]["attr"] == "0x0002"
            and got[0]["value"]["scale"] == 100 and got[0]["value"]["type"] == "int16"
            and got[0]["value"]["mfr"] is None and got[0]["value"]["unit"] == "°C", got)
    c.check("the proposal carries the evidence", "700 -> 950" in got[0]["evidence"], got[0]["evidence"])
    got = learning_ops.attribute_that_became(
        [rec(0x0003, 40)], base, {"inputs": {"setting_id": "limit", "new_value": 40}}, {"writable": writable})
    c.check("a value stored as-is has no scale", got[0]["value"].get("scale") is None
            and got[0]["value"]["attr"] == "0x0003", got)
    got = learning_ops.attribute_that_became([rec(0x0002, 812)], base, step, {"writable": writable})
    c.check("a change that is not that value proposes nothing", got[0]["path"] == "", got)
    got = learning_ops.attribute_that_became([rec(0x0002, 950), rec(0x0003, 95)], base, step,
                                             {"writable": writable})
    c.check("two candidates are not chosen between", got[0]["path"] == "" and got[0]["confidence"] == "low", got)
    toggled = learning_ops.attribute_that_toggled(
        [rec(0x0003, 0)], base, {"inputs": {"setting_id": "lock"}}, {"writable": writable})
    c.check("a vendor cluster's toggle is learned without a manufacturer code",
            toggled[0]["path"] == "zmm.settings" and toggled[0]["value"]["mfr"] is None, toggled)
    recipe = learning_recipes.load()["manufacturer_setting"]
    c.check("the settings recipe applies to any maker's cluster",
            learning_recipes.applies(recipe, _device({1: [0, 0xFC99]}))
            and not learning_recipes.applies(recipe, _device({1: [0, 6]})))
    lamp = learning_recipes.load()["light"]
    c.check("the lamp recipe is for a switched load, not a thermostat's power control",
            learning_recipes.applies(lamp, _device({1: [0, 6]}))
            and not learning_recipes.applies(lamp, _device({1: [0, 6, 0x201, 0xFC11]})))

    c.section("a step that reads the device's history")
    import asyncio
    from modules import device_learning
    trv = _device({1: [0, 0x201, 0xFC99]})
    real = device_learning._history
    device_learning._history = lambda ieee: (
        [{"t": t, "ep": 1, "cluster": 0xFC99, "attr": 2, "mfr": None, "value": v}
         for t, v in ((5, 1900), (105, 1600), (204, 1900), (303, 1500))],
        {"heating_setpoint": [(0, 19.0), (100, 16.0), (200, 19.0), (300, 15.0)]}, {})
    try:
        device_learning.start(trv)
        out = asyncio.run(device_learning.begin(trv, "manufacturer_setting.from_history", {}))
        step = next(x for x in out["steps"] if x["key"] == "manufacturer_setting.from_history")
        c.check("is done as soon as it is started, with its proposals",
                step["status"] == "done" and step["proposals"], step)
        asyncio.run(device_learning.end(trv))
    finally:
        device_learning._history = real

    c.section("a discovery is evidence, like a probe")
    tmp = tempfile.mkdtemp(prefix="zmm_cls_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    ieee = "aa:bb:cc:00:00:00:00:03"
    device_facts.forget(ieee)
    zigbee_cache.warm()
    try:
        zigbee_cache.record_attribute_metadata(ieee, 1, 0xFC99, [
            {"id_int": 2, "name": "0x0002", "type": "0x29/int16", "readable": True, "writable": True, "value": 700},
            {"id_int": 7, "name": "0x0007", "type": "0x21/uint16", "readable": True, "writable": False}])
        facts = {f["subject"]: f for f in zigbee_cache.get_facts(ieee) if f["source"] == "answered"}
        c.check("discovered attributes become answered facts, with type and access",
                '"acl": "RW"' in facts.get("attr:0xFC99/0x0002", {}).get("value", "")
                and '"acl": "R"' in facts.get("attr:0xFC99/0x0007", {}).get("value", ""), facts)
        zigbee_cache._safe_execute("DELETE FROM device_facts WHERE ieee = ?", [ieee])
        c.check("a device discovered before that is backfilled, once",
                zigbee_cache.backfill_answered_facts() == 1 and zigbee_cache.backfill_answered_facts() == 0)
        c.check("with the value its history last held",
                any('"value": 700' in f["value"] for f in zigbee_cache.get_facts(ieee)
                    if f["subject"] == "attr:0xFC99/0x0002"))
    finally:
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_facts.forget(ieee)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
