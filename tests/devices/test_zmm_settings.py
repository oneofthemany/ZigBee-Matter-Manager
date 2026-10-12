"""
Step 7 of docs/plans/zmm-quirks.md: settings a ZMM entry declares are offered
only where the endpoint lists the attribute, and written raw with the
entry's manufacturer code.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from modules import device_facts, zmm_settings
from modules.device_facts import Fact, _j, record
from modules.device_profiles import normalise_profile

IEEE = "54:ef:44:10:01:5a:14:eb"

ENTRY = normalise_profile({
    "id": "lumi.plug.aeu002", "match": {"model": "lumi.plug.aeu002"},
    "endpoints": {"2": {"label": "Socket 2"}},
    "zmm": {"settings": [
        {"id": "button_leds", "label": "Button LEDs", "type": "bool", "ep": 1,
         "cluster": "0xFCC0", "attr": "0x0203", "mfr": "0x115F"},
        {"id": "multi_click", "label": "Multi-click", "type": "uint8", "ep": "each",
         "cluster": "0xFCC0", "attr": "0x0286", "mfr": "0x115F",
         "values": {"1": "Off", "2": "On"}},
        {"id": "never_listed", "type": "bool", "ep": 1, "cluster": "0xFCC0", "attr": "0x0999",
         "mfr": "0x115F"},
    ]},
})


# A made-up battery valve: it is never probed, so reports are all it gives.
SLEEPY = normalise_profile({
    "id": "VALVE-1", "match": {"model": "VALVE-1", "manufacturer": "Acme"},
    "zmm": {"settings": [
        {"id": "child_lock", "type": "bool", "ep": 1, "cluster": "0xFC99", "attr": "0x0001"},
        {"id": "floor_temperature", "type": "int16", "ep": 1, "cluster": "0xFC99", "attr": "0x0002",
         "scale": 100, "min": 4, "max": 35, "unit": "°C"},
        {"id": "external_temp", "type": "int16", "ep": 1, "cluster": "0xFC99", "attr": "0x0003",
         "scale": 100},
        {"id": "sensor_type", "type": "uint8", "ep": 1, "cluster": "0xFC99", "attr": "0x0004",
         "values": {"0": "Internal", "1": "External"}},
        {"id": "never_reported", "type": "uint8", "ep": 1, "cluster": "0xFC99", "attr": "0x0009"},
    ]},
})


class _Store:
    def get_profile_for_device(self, **_):
        return ENTRY


class _Fcc0:
    cluster_id = 0xFCC0

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.writes = []

    async def write_attributes_raw(self, attrs, manufacturer_code=None):
        a = attrs[0]
        self.writes.append((a.attrid, a.value.type, a.value.value, manufacturer_code))
        return [[NS(status=0x87 if self.refuse else 0)]]


def _real_library(c: Checker) -> None:
    """The write goes through zigpy itself: its parameter names and its answer."""
    import inspect
    try:
        from zigpy.zcl import Cluster, foundation
    except ImportError:
        return          # host python has no zigpy; the venv run covers this
    c.section("against zigpy itself")
    params = list(inspect.signature(Cluster.write_attributes_raw).parameters)
    c.check("the manufacturer code is the raw write's second positional parameter",
            params[:3] == ["self", "attributes", "manufacturer_code"], params)
    ok = foundation.WriteAttributesResponse(
        [foundation.WriteAttributesStatusRecord(status=foundation.Status.SUCCESS)])
    bad = foundation.WriteAttributesResponse(
        [foundation.WriteAttributesStatusRecord(status=foundation.Status.READ_ONLY, attrid=1)])
    Schema = foundation.WriteAttributesResponseSchema
    c.check("a response schema of successes is accepted",
            zmm_settings._write_ok(Schema(status_records=ok)))
    c.check("one refused record is a refusal", not zmm_settings._write_ok(Schema(status_records=bad)))
    for type_id, raw in ((0x10, 1), (0x20, 100), (0x29, 700), (0x30, 1), (0x23, 300), (0x28, -3)):
        attr = foundation.Attribute()
        attr.attrid = 0x6000
        attr.value = zmm_settings.typed_value(type_id, raw)
        try:
            ok_bytes = len(attr.serialize()) > 3
        except Exception as e:
            ok_bytes = repr(e)
        c.check(f"a value of type 0x{type_id:02X} serialises for the radio", ok_bytes is True, ok_bytes)
    c.check("a default response carries its own status",
            not zmm_settings._write_ok(foundation.DefaultResponse(
                command_id=2, status=foundation.Status.UNSUP_MANUF_GENERAL_COMMAND)))


def run() -> Checker:
    c = Checker("zmm_settings")
    _real_library(c)
    device_profiles._store = _Store()
    tmp = tempfile.mkdtemp(prefix="zmm_set_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    device_facts.forget(IEEE)
    zigbee_cache.warm()
    try:
        fc1, fc2, fc3 = _Fcc0(), _Fcc0(refuse=True), _Fcc0()
        eps = {i: NS(in_clusters={0xFCC0: fc}) for i, fc in ((1, fc1), (2, fc2), (3, fc3))}
        dev = NS(ieee=IEEE, zigpy_dev=NS(endpoints={0: None, **eps}), state={})
        record(IEEE, [
            Fact(1, "attr:0xFCC0/0x0203@0x115F", "answered", _j({"type": "0x10/bool", "acl": "RW", "value": 1})),
            Fact(2, "attr:0xFCC0/0x0286@0x115F", "answered", _j({"type": "0x20/uint8", "acl": "RWP", "value": 1})),
            Fact(3, "attr:0xFCC0/0x0286@0x115F", "answered", _j({"type": "0x20/uint8", "acl": "RWP", "value": 2})),
        ])

        c.section("offered only where the endpoint lists the attribute")
        opts = {o["name"]: o for o in zmm_settings.options(dev)}
        c.check("a one-EP setting, and a per-EP setting on each EP that lists it",
                sorted(opts) == ["button_leds", "multi_click_2", "multi_click_3"], sorted(opts))
        c.check("a setting no EP lists is not offered", "never_listed" not in opts)
        c.check("per-EP settings carry the EP's name",
                opts["multi_click_2"]["label"] == "Multi-click (Socket 2)"
                and opts["multi_click_3"]["label"] == "Multi-click (EP3)", opts["multi_click_2"])
        c.check("a boolean is an off/on choice",
                [o["value"] for o in opts["button_leds"]["options"]] == [0, 1])
        c.check("named values become the choices",
                [o["label"] for o in opts["multi_click_3"]["options"]] == ["Off", "On"])
        c.check("the current value comes from the evidence",
                opts["multi_click_3"]["current_value"] == 2)

        c.section("written raw, with the maker's code")
        res = asyncio.run(zmm_settings.apply(dev, {"button_leds": 0, "multi_click_3": 1,
                                                   "unrelated": 5}))
        c.check("each named setting is written", res == {"button_leds": True, "multi_click_3": True}, res)
        c.check("with the right type and the manufacturer code",
                fc1.writes == [(0x0203, 0x10, False, 0x115F)] and fc3.writes == [(0x0286, 0x20, 1, 0x115F)],
                (fc1.writes, fc3.writes))
        c.check("and the new value is shown", dev.state.get("multi_click_3") == 1)
        res = asyncio.run(zmm_settings.apply(dev, {"multi_click_2": 2}))
        c.check("a refused write is reported, and the state left alone",
                res == {"multi_click_2": False} and "multi_click_2" not in dev.state, (res, dev.state))

        c.section("a sleepy device: reports are the only evidence it gives")
        global ENTRY
        ENTRY = SLEEPY
        valve_ieee = "aa:bb:cc:00:00:00:00:01"
        vc = _Fcc0()
        vc.cluster_id = 0xFC99
        valve = NS(ieee=valve_ieee, state={},
                   zigpy_dev=NS(endpoints={0: None, 1: NS(in_clusters={0xFC99: vc})},
                                model="VALVE-1", manufacturer="Acme"))
        device_facts.forget(valve_ieee)
        record(valve_ieee, [
            Fact(1, "reports:0xFC99/0x0001", "observed", _j({"last": 0, "nonzero": False})),
            Fact(1, "reports:0xFC99/0x0002", "observed", _j({"last": 700, "nonzero": True})),
            Fact(1, "reports:0xFC99/0x0003", "observed", _j({"last": 2620, "nonzero": True})),
            Fact(1, "reports:0xFC99/0x0004", "observed", _j({"last": 0, "nonzero": False})),
        ])
        opts = {o["name"]: o for o in zmm_settings.options(valve)}
        c.check("settings whose attribute it has reported are offered, with no probe",
                sorted(opts) == ["child_lock", "external_temp", "floor_temperature", "sensor_type"],
                sorted(opts))
        c.check("one it has never shown is still not offered", "never_reported" not in opts)
        floor = opts["floor_temperature"]
        c.check("a scaled value is shown as a person reads it",
                floor["current_value"] == 7.0 and floor["unit"] == "°C" and floor["max"] == 35, floor)

        c.section("a setting answers to its id, as a command")
        c.check("the device says which settings it takes",
                zmm_settings.names(valve) == set(opts), zmm_settings.names(valve))
        ok = asyncio.run(zmm_settings.apply_command(valve, "external_temp", 21.37))
        c.check("a temperature is written scaled, with no manufacturer code",
                ok is True and vc.writes[-1] == (0x0003, 0x29, 2137, None), vc.writes)
        c.check("and read back unscaled", valve.state.get("external_temp") == 21.37, valve.state)
        asyncio.run(zmm_settings.apply_command(valve, "sensor_type", "external"))
        asyncio.run(zmm_settings.apply_command(valve, "child_lock", True))
        c.check("named values and booleans are understood",
                vc.writes[-2:] == [(0x0004, 0x20, 1, None), (0x0001, 0x10, True, None)], vc.writes[-2:])
        c.check("a command the device has no setting for is passed over, not failed",
                asyncio.run(zmm_settings.apply_command(valve, "valve_detection", 1)) is None)
        device_facts.forget(valve_ieee)
    finally:
        device_profiles._store = None
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db, zigbee_cache._INITIALISED = None, False
        device_facts.forget(IEEE)
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
