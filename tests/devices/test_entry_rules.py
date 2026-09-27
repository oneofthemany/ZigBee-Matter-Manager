"""
Step 8 of docs/plans/zmm-quirks.md: model-name quirks give way to entries.

The Philips SML controller quirk and the Tuya model-string guesses now apply
only where no entry describes the device, and an entry that names its maker
never describes an unrelated device sharing a generic model (Tuya TS0601).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
from handlers.tuya import TuyaDeviceTypeDetector
from modules.device_capabilities import DeviceCapabilities
from modules.device_profiles import ProfileStore, profile_for_device

REPO = Path(__file__).resolve().parents[2]

ACTUATOR_CAPS = {"switch", "light", "on_off", "level_control", "color_control"}


class _Cl:
    def __init__(self, cid):
        self.cluster_id = cid


def _ep(ep_id, ins, outs=(), dtype=0x0000):
    return NS(endpoint_id=ep_id, profile_id=0x0104, device_type=dtype,
              in_clusters={c: _Cl(c) for c in ins}, out_clusters={c: _Cl(c) for c in outs})


def _device(model, manufacturer, eps, ieee="00:17:88:01:09:16:37:1b"):
    zdev = NS(model=model, manufacturer=manufacturer, endpoints={0: None, **eps},
              node_desc=NS(is_mains_powered=False))
    return NS(ieee=ieee, zigpy_dev=zdev, state={}, handlers={}, is_coordinator=False)


def _sml(model="SML001"):
    return _device(model, "Philips", {
        1: _ep(1, (0x0000,), (0x0000, 0x0003, 0x0004, 0x0005, 0x0006, 0x0008, 0x0300), 0x0850),
        2: _ep(2, (0x0000, 0x0001, 0x0003, 0x0400, 0x0402, 0x0406), (0x0019,), 0x0107)})


def _store(tmp: Path, zmm_dir: Path) -> ProfileStore:
    return ProfileStore(user_dir=str(tmp / "u"), bundled_dir=str(tmp / "b"),
                        ieee_overrides_file=str(tmp / "i.json"), zmm_dir=str(zmm_dir))


def run() -> Checker:
    c = Checker("entry_rules")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_rules_"))
    try:
        c.section("the Philips Hue motion sensor")
        device_profiles._store = _store(tmp / "s1", REPO / "zmm_quirks")
        caps = DeviceCapabilities(_sml())
        c.check("the SML001 entry marks EP1 a controller", caps.get_endpoint_role(1) == "controller")
        c.check("so the sensor has no switch or light capabilities",
                not (caps.get_capabilities() & ACTUATOR_CAPS), caps.get_capabilities())
        c.check("and is still a motion sensor", "motion_sensor" in caps.get_capabilities())
        caps = DeviceCapabilities(_sml("SML004"))
        c.check("a model with no entry still gets the old quirk",
                caps.get_endpoint_role(1) == "controller"
                and not (caps.get_capabilities() & ACTUATOR_CAPS), caps.get_capabilities())

        c.section("a controller EP does not strip a real actuator")
        zdir = tmp / "z2"
        zdir.mkdir()
        (zdir / "combo.json").write_text(json.dumps(
            {"id": "combo", "match": {"model": "combo"},
             "endpoints": {"1": {"role": "controller"}}}))
        device_profiles._store = _store(tmp / "s2", zdir)
        combo = _device("combo", "Acme", {1: _ep(1, (0x0000,), (0x0006,)),
                                          2: _ep(2, (0x0006,), (), 0x010A)})
        caps = DeviceCapabilities(combo)
        c.check("EP1 is a controller, EP2 keeps its switch",
                caps.get_endpoint_role(1) == "controller" and "switch" in caps.get_capabilities(),
                caps.get_capabilities())

        c.section("Tuya devices follow an entry")
        (zdir / "blind.json").write_text(json.dumps(
            {"id": "TS0601-_TZE200_zah67ekd", "device_type": "blind",
             "match": {"model": "TS0601", "manufacturer": "_TZE200_zah67ekd"},
             "capabilities": ["cover"]}))
        device_profiles._store = _store(tmp / "s3", zdir)
        blind = _device("TS0601", "_TZE200_zah67ekd",
                        {1: _ep(1, (0x0000, 0x0004, 0x0005, 0xEF00))}, ieee="34:10:f4:ff:fe:e3:dd:6c")
        c.check("the type comes from the entry, not the model string",
                TuyaDeviceTypeDetector.detect_device_type(blind) == "cover")
        caps = DeviceCapabilities(blind).get_capabilities()
        c.check("and no presence sensor is guessed", "presence_sensor" not in caps, caps)

        c.section("an entry never describes another maker's device")
        radar = _device("TS0601", "_TZE204_radar", {1: _ep(1, (0x0000, 0x0004, 0x0005, 0xEF00))},
                        ieee="aa:bb:cc:dd:ee:ff:00:11")
        c.check("the store's fuzzy lookup skips a ZMM entry naming another maker",
                device_profiles._store.get_profile_for_device(model="TS0601",
                                                              manufacturer="_TZE204_radar") is None)
        c.check("so the decision paths see nothing", profile_for_device(radar) is None)
        c.check("and the old guess still applies to it",
                "presence_sensor" in DeviceCapabilities(radar).get_capabilities())
    finally:
        device_profiles._store = None
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
