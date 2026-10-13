"""
A device's battery level comes from whichever source gives a real one
(docs/device-profiles.md, Battery): no entry says where to look.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
from handlers.aqara import AqaraManufacturerCluster
from handlers.sensors import PowerConfigurationHandler
from modules.device_profiles import ProfileStore

# lumi.sensor_occupy.agl8's 0xF7 blob: tag 0x17 = 3222 mV, tag 0x18 = 100 %
AGL8_F7 = bytes.fromhex("052101000a21529c0c200a0d232a4100001320001721960c1820641c1000642000652002672000")


def _device(mains=False, cached_volts=None):
    """A device no entry describes, with Power Configuration and an Aqara cluster."""
    dev = NS(ieee="aa:bb:00:00:00:00:00:07", state={}, handlers={},
             zigpy_dev=NS(model="unknown.sensor", manufacturer="Acme",
                          node_desc=NS(is_mains_powered=mains, logical_type=2, mac_capability_flags=0)))
    dev.update_state = lambda d, **_: dev.state.update(d)
    ep = NS(endpoint_id=1, in_clusters={})
    power = PowerConfigurationHandler(dev, NS(
        cluster_id=0x0001, endpoint=ep, add_listener=lambda _l: None,
        get=lambda attr, default=None: cached_volts if attr == 0x0020 else default))
    blob = AqaraManufacturerCluster(dev, NS(cluster_id=0xFCC0, endpoint=ep,
                                            add_listener=lambda _l: None))
    dev.handlers = {(1, 0x0001): power, (1, 0xFCC0): blob}
    return dev, power, blob


def _f7(*tags) -> bytes:
    return b"".join(bytes([tag, 0x21]) + v.to_bytes(2, "little") if v > 255
                    else bytes([tag, 0x20, v]) for tag, v in tags)


def _level(dev):
    return dev.state.get("battery"), dev.state.get("battery_voltage")


def run() -> Checker:
    c = Checker("battery")
    d = tempfile.mkdtemp()
    device_profiles._store = ProfileStore(user_dir=d + "/u", bundled_dir=d + "/b",
                                          ieee_overrides_file=d + "/i.json", zmm_dir=d + "/z")
    try:
        c.section("a device that answers Power Configuration properly")
        dev, power, blob = _device()
        power.attribute_updated(0x0021, 170)
        power.attribute_updated(0x0020, 29)
        c.check("its own level and voltage are used: 85 % at 2.9 V", _level(dev) == (85, 2.9), dev.state)
        blob.attribute_updated(0x00F7, _f7((0x17, 3100), (0x18, 60)))
        c.check("a blob saying otherwise does not replace what the standard cluster reports",
                _level(dev) == (85, 2.9), dev.state)

        c.section("a device whose Power Configuration answers zeros (the FP300)")
        dev, power, blob = _device()
        power.attribute_updated(0x0021, 0)
        power.attribute_updated(0x0020, 0)
        c.check("0 V is never stored as a voltage", "battery_voltage" not in dev.state, dev.state)
        blob.attribute_updated(0x00F7, AGL8_F7)
        c.check("the level in its status blob is found with no entry: 100 % at 3.222 V",
                _level(dev) == (100, 3.222), dev.state)
        power.attribute_updated(0x0021, 0)
        power.attribute_updated(0x0020, 0)
        c.check("and the cluster's zeros never overwrite it again", _level(dev) == (100, 3.222), dev.state)

        dev, power, blob = _device(cached_volts=0)
        dev.state = {"battery": 100, "battery_voltage": 3.222}
        power.attribute_updated(0x0021, 0)
        c.check("after a restart the 0 % is dropped at once: zigpy remembers the 0 V beside it",
                _level(dev) == (100, 3.222), dev.state)

        c.section("a device that gives only a voltage")
        dev, power, blob = _device()
        power.attribute_updated(0x0020, 28)
        c.check("a level is derived from it: 2.8 V is 45 %", _level(dev) == (45, 2.8), dev.state)
        power.attribute_updated(0x0021, 120)
        c.check("until the device states one itself", _level(dev) == (60, 2.8), dev.state)
        power.attribute_updated(0x0020, 27)
        c.check("which a later voltage no longer overrides", _level(dev) == (60, 2.7), dev.state)
        dev, power, blob = _device()
        power.attribute_updated(0x0020, 60)
        c.check("a 6 V pack gets no level from a coin cell's curve", _level(dev) == (None, 6.0), dev.state)

        c.section("an older Aqara sensor: voltage only, in blob tag 0x01")
        dev, power, blob = _device()
        blob.attribute_updated(0x00F7, _f7((0x01, 2950)))
        c.check("its voltage and a derived level are the battery: 90 % at 2.95 V",
                _level(dev) == (90, 2.95), dev.state)

        c.section("what is not a battery reading")
        dev, power, blob = _device(mains=True)
        blob.attribute_updated(0x00F7, AGL8_F7)
        c.check("a mains device's tags are never read as a battery", _level(dev) == (None, None), dev.state)
        dev, power, blob = _device()
        blob.attribute_updated(0x00F7, _f7((0x17, 40018), (0x18, 60)))
        c.check("a tag outside any cell's range is not a voltage, nor the tag beside it a level",
                _level(dev) == (None, None), dev.state)
    finally:
        device_profiles._store = None
    return c


if __name__ == "__main__":
    run()
