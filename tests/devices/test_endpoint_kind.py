"""
Light or switch is decided once, in modules/endpoint_kind.py, and every
consumer agrees with it: HA discovery, the Control tab, device capabilities.

The real device driving this is the Aqara dual outlet with USB
(lumi.plug.aeu002): two sockets with Multistate, a switched USB port without,
and 0xFCC0 on all three. 0xFCC0 used to make the USB port a light.
"""

from __future__ import annotations

from harness import Checker

import modules.device_profiles as device_profiles
from handlers.aqara import MultistateInputHandler
from handlers.general import OnOffHandler
from modules.device_capabilities import DeviceCapabilities
from modules.endpoint_kind import LIGHT, SWITCH, classify
from modules.groups import GroupManager


class _Store:
    def __init__(self, profile=None):
        self.profile = profile

    def get_profile_for_device(self, **_):
        return self.profile


class _Stub:
    def __init__(self, cluster_id: int):
        self.cluster_id = cluster_id


class _Endpoint:
    def __init__(self, ep_id: int, in_clusters, profile_id=0x0104, device_type=0x0100):
        self.endpoint_id = ep_id
        self.in_clusters = {c: _Stub(c) for c in in_clusters}
        self.out_clusters = {}
        self.profile_id = profile_id
        self.device_type = device_type


class _Cluster:
    def __init__(self, ep, cluster_id=0x0006):
        self.endpoint = ep
        self.cluster_id = cluster_id

    def add_listener(self, _l):
        pass


class _NodeDesc:
    def __init__(self, mains: bool):
        self.is_mains_powered = mains


class _ZigpyDev:
    model = "lumi.plug.aeu002"
    manufacturer = "Aqara"

    def __init__(self, eps, mains):
        self.endpoints = {0: None, **{e.endpoint_id: e for e in eps}}
        self.node_desc = _NodeDesc(mains)


class _Device:
    ieee = "54:ef:44:10:01:5a:14:eb"

    def __init__(self, eps, mains=True):
        self.zigpy_dev = _ZigpyDev(eps, mains)
        self.state: dict = {}
        self.handlers: dict = {}
        self.events: list = []

    def update_state(self, d, endpoint_id=None, **_):
        self.state.update(d)

    def emit_event(self, name, data):
        self.events.append((name, data))


def _build(ep_clusters: dict, mains=True, **ep_kw):
    eps = [_Endpoint(i, cl, **ep_kw) for i, cl in ep_clusters.items()]
    return _Device(eps, mains), eps


def _onoff(dev, ep):
    return OnOffHandler(dev, _Cluster(ep))


SOCKET = [0x0000, 0x0004, 0x0005, 0x0006, 0x0012, 0x0702, 0x0B04, 0xFCC0]
SOCKET2 = [0x0004, 0x0005, 0x0006, 0x0012, 0x0B04, 0xFCC0]
USB = [0x0004, 0x0005, 0x0006, 0x0B04, 0xFCC0]
OUTLET = {1: SOCKET, 2: SOCKET2, 3: USB}


def _kind(clusters, **kw):
    r = classify(clusters, **kw)
    return r.kind if r else None


def run() -> Checker:
    c = Checker("endpoint_kind")
    device_profiles._store = _Store()

    c.section("the rules")
    c.check("a vendor cluster alone is not a light",
            _kind([0x0006, 0xFCC0]) == SWITCH and _kind([0x0006, 0xEF00]) == SWITCH)
    c.check("the On/Off Light device type alone is not a light",
            _kind([0x0006], profile_id=0x0104, device_type=0x0100) == SWITCH)
    c.check("colour makes a light", _kind([0x0006, 0x0008, 0x0300, 0x0B04]) == LIGHT)
    c.check("dimming makes a light", _kind([0x0006, 0x0008, 0xFCC0]) == LIGHT)
    c.check("touchlink alone makes a light", _kind([0x0006, 0x1000]) == LIGHT)
    c.check("a Hue plug (touchlink, plug-in unit type) is a switch",
            _kind([0x0006, 0x0008, 0x1000], profile_id=0x0104, device_type=0x010A) == SWITCH)
    c.check("a ZLL plug-in unit is a switch",
            _kind([0x0006, 0x1000], profile_id=0xC05E, device_type=0x0010) == SWITCH)
    for cid in (0x0B04, 0x0702, 0x0012, 0xFC11):
        c.check(f"load cluster 0x{cid:04X} makes a switch", _kind([0x0006, 0x0008, cid]) == SWITCH)
    c.check("an EP without On/Off input is not classified", _kind([0x0008, 0x0300]) is None)
    c.check("dimming on a cover is not a light",
            _kind([0x0006, 0x0008, 0x0102]) == SWITCH)
    c.check("an override wins", _kind([0x0006, 0x0300], override=SWITCH) == SWITCH)

    c.section("the Aqara outlet, end to end")
    dev, eps = _build(OUTLET)
    comps = {e.endpoint_id: [x["component"] for x in _onoff(dev, e).get_discovery_configs()]
             for e in eps}
    c.check("HA gets three switches", comps == {1: ["switch"], 2: ["switch"], 3: ["switch"]}, comps)
    types = {e.endpoint_id: _onoff(dev, e).get_component_type() for e in eps}
    c.check("the Control tab gets three switches", set(types.values()) == {SWITCH}, types)
    caps = DeviceCapabilities(dev).get_capabilities()
    c.check("the device is a switch, not a light", "switch" in caps and "light" not in caps, caps)
    c.check("switch state is not published retained", not _onoff(dev, eps[2])._is_light_endpoint())
    retired = _onoff(dev, eps[2]).get_retired_discovery_configs()
    c.check("the old EP3 light entity is retracted",
            retired == [{"component": "light", "object_id": "light_3"}], retired)

    c.section("a profile override reaches every consumer")
    normalised = device_profiles.normalise_profile(
        {"id": "p", "match": {"model": "lumi.plug.aeu002"},
         "endpoints": {"3": {"kind": "light"}, "2": {"kind": "bulb"}}})
    c.check("the override survives profile normalisation",
            normalised["endpoints"]["3"].get("kind") == LIGHT, normalised["endpoints"])
    c.check("an invalid kind is dropped", "kind" not in normalised["endpoints"]["2"],
            normalised["endpoints"])
    device_profiles._store = _Store(normalised)
    dev, eps = _build(OUTLET)
    h3 = _onoff(dev, eps[2])
    c.check("HA gets a light on EP3",
            [x["component"] for x in h3.get_discovery_configs()] == ["light"])
    c.check("the Control tab gets a light on EP3", h3.get_component_type() == LIGHT)
    c.check("the device gains the light capability",
            "light" in DeviceCapabilities(dev).get_capabilities())
    c.check("the switch entity is retracted",
            h3.get_retired_discovery_configs() == [{"component": "switch", "object_id": "switch_3"}])
    device_profiles._store = _Store()

    c.section("real lights are still lights")
    dev, eps = _build({1: [0x0000, 0x0003, 0x0004, 0x0005, 0x0006, 0x0008, 0x0300, 0x1000]})
    h = _onoff(dev, eps[0])
    c.check("a colour bulb is a light",
            [x["component"] for x in h.get_discovery_configs()] == ["light"])
    c.check("its card is a light", h.get_component_type() == LIGHT)
    c.check("its state is published retained", h._is_light_endpoint())
    c.check("its old switch entity is retracted",
            h.get_retired_discovery_configs() == [{"component": "switch", "object_id": "switch_1"}])

    c.section("the Aurora socket quirk is unchanged")
    dev, eps = _build({1: [0x0006, 0x0008, 0x0B04]})
    comps = [x["component"] for x in _onoff(dev, eps[0]).get_discovery_configs()]
    c.check("metering plus level is a switch with an LED brightness number",
            comps == ["switch", "number"], comps)

    c.section("a mains relay is never a contact sensor")
    dev, eps = _build({1: [0x0000, 0x0004, 0x0005, 0x0006, 0xE000, 0xE001]})
    comps = [x["component"] for x in _onoff(dev, eps[0]).get_discovery_configs()]
    c.check("a small single-EP relay on mains is a switch", comps == ["switch"], comps)
    dev, eps = _build({1: [0x0000, 0x0001, 0x0006]}, mains=False)
    comps = [x["component"] for x in _onoff(dev, eps[0]).get_discovery_configs()]
    c.check("a battery device with minimal clusters is still a contact sensor",
            comps == ["binary_sensor"], comps)

    c.section("each gang reports its own button presses")
    dev, eps = _build(OUTLET)
    ms1 = MultistateInputHandler(dev, _Cluster(eps[0], 0x0012))
    ms2 = MultistateInputHandler(dev, _Cluster(eps[1], 0x0012))
    ms2.attribute_updated(ms2.ATTR_PRESENT_VALUE, 2)
    c.check("a press on socket 2 lands on EP2",
            dev.state.get("action_2") == "double" and "action_1" not in dev.state, dev.state)
    ms1.attribute_updated(ms1.ATTR_PRESENT_VALUE, 1)
    c.check("and socket 1's press does not overwrite it",
            dev.state.get("action_1") == "single" and dev.state.get("action_2") == "double",
            dev.state)
    c.check("the bare key is the last press on any gang", dev.state.get("action") == "single")
    c.check("the event names the gang", dev.events[-1][1].get("endpoint") == 1, dev.events)
    ids = [x["object_id"] for h in (ms1, ms2) for x in h.get_discovery_configs()]
    c.check("HA gets one Action sensor per gang", ids == ["action_1", "action_2"], ids)
    c.check("the old shared Action sensor is retracted",
            ms1.get_retired_discovery_configs() == [{"component": "sensor", "object_id": "action"}])

    dev, eps = _build({1: [0x0012]}, mains=False)
    ms = MultistateInputHandler(dev, _Cluster(eps[0], 0x0012))
    c.check("a lone button keeps its Action sensor id",
            [x["object_id"] for x in ms.get_discovery_configs()] == ["action"]
            and ms.get_retired_discovery_configs() == [])
    c.section("groups use the same answer")
    gm = GroupManager.__new__(GroupManager)   # skips load_groups() and its file read
    dev, _ = _build(OUTLET)
    c.check("the Aqara outlet groups as a switch", gm.get_device_type(dev) == "switch",
            gm.get_device_type(dev))
    dev, _ = _build({1: [0x0006, 0x0008, 0x0B04]})
    c.check("a socket whose Level dims its LED groups as a switch",
            gm.get_device_type(dev) == "switch", gm.get_device_type(dev))
    dev, _ = _build({1: [0x0006, 0x0008, 0x0300]})
    c.check("a colour bulb groups as a light", gm.get_device_type(dev) == "light")
    dev, _ = _build({1: [0x0006, 0x0008, 0x0102]})
    c.check("a cover with Level groups as a cover", gm.get_device_type(dev) == "cover",
            gm.get_device_type(dev))
    dev, _ = _build({1: [0x0006]})
    dev.zigpy_dev.model = "LED relay"
    c.check("a model name saying LED does not make a light",
            gm.get_device_type(dev) == "switch", gm.get_device_type(dev))
    return c


if __name__ == "__main__":
    run()
