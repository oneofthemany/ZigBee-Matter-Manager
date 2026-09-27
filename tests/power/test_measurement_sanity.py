"""
Readings a device cannot physically produce are held back, and the device is
flagged, without a list of known bad numbers.

After a power cut the Aurora double socket (Socket - Media) reported -8011 W
on EP1 and +8011 W on EP2 for a month: in state, in history, and summed to a
device total of 0 that hid it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

import modules.app_alerts as app_alerts
import modules.device_profiles as device_profiles
from handlers.power import ElectricalMeasurementHandler
from modules import measurement_sanity

IEEE = "00:15:8d:00:02:56:f8:bf"


class _Store:
    def __init__(self, profile=None):
        self.profile = profile

    def get_profile_for_device(self, **_):
        return self.profile


class _Cluster:
    cluster_id = 0x0B04

    def __init__(self, ep_id, switched=True):
        self.endpoint = NS(endpoint_id=ep_id, in_clusters={0x0006: None} if switched else {})
        self.read_values = {}

    def add_listener(self, _l):
        pass

    def get(self, _attr):
        return None

    async def read_attributes(self, attrs, **_):
        return [{a: self.read_values[a] for a in attrs if a in self.read_values}, {}]


def _device(model="DoubleSocket50AU"):
    updates = []

    def update_state(d, **_):
        updates.append(dict(d))
        dev.state.update(d)
    dev = NS(ieee=IEEE, model=model, zigpy_dev=NS(model=model, manufacturer="Aurora"),
             state={}, handlers={}, update_state=update_state,
             service=NS(friendly_names={IEEE: "Socket - Media"}))
    return dev, updates


def _em(dev, ep, switched=True):
    h = ElectricalMeasurementHandler(dev, _Cluster(ep, switched))
    dev.handlers[(ep, 0x0B04)] = h
    return h


def run() -> Checker:
    c = Checker("measurement_sanity")
    raised, resolved = [], []
    real = (app_alerts.raise_alert, app_alerts.resolve_alert)
    app_alerts.raise_alert = lambda *a, **k: raised.append((a, k))
    app_alerts.resolve_alert = lambda key: resolved.append(key) or 1
    device_profiles._store = _Store()
    measurement_sanity.forget(IEEE)
    try:
        c.section("the Aurora after a power cut")
        dev, updates = _device()
        h1, h2 = _em(dev, 1), _em(dev, 2)
        h1.attribute_updated(h1.ATTR_ACTIVE_POWER, 36)
        h2.attribute_updated(h2.ATTR_ACTIVE_POWER, 0)
        updates.clear()
        h1.attribute_updated(h1.ATTR_ACTIVE_POWER, -8011)
        c.check("a switched socket reporting negative power is held back, its value blanked once",
                updates == [{"power_1": None, "power": 0.0}], updates)
        c.check("one alert, naming the device",
                len(raised) == 1 and raised[0][0][2] == "Socket - Media needs a power cycle", raised)
        h1.attribute_updated(h1.ATTR_ACTIVE_POWER, -8011)
        c.check("repeats change nothing and raise nothing more", len(updates) == 1 and len(raised) == 1)
        h2.attribute_updated(h2.ATTR_ACTIVE_POWER, 8011)
        c.check("+8011 W on the other EP is held back too",
                dev.state.get("power_2") is None and len(raised) == 2, dev.state)
        c.check("the alert names both EPs and why",
                "EP1 active power -8011 W is below 0 W" in raised[1][0][3]
                and "EP2 active power 8011 W is above 4000 W" in raised[1][0][3], raised[1][0][3])
        c.check("the device total never carries the glitch",
                all(abs(u.get("power") or 0) < 100 for u in updates), updates)

        c.section("recovery")
        h1.attribute_updated(h1.ATTR_ACTIVE_POWER, 36)
        c.check("a sane reading on one EP shows again, the alert stays for the other",
                dev.state.get("power_1") == 36.0 and resolved == [])
        h2.attribute_updated(h2.ATTR_ACTIVE_POWER, 0)
        c.check("once every EP reads sensibly, the alert clears",
                resolved == [f"implausible_readings:{IEEE}"] and measurement_sanity.faults_for(IEEE) == {})

        c.section("polled readings are screened the same way")
        dev, updates = _device()
        h = _em(dev, 1)
        h.cluster.read_values = {h.ATTR_ACTIVE_POWER: -8011}
        polled = asyncio.run(h.poll())
        c.check("the impossible value is not returned", polled.get("power_1") is None
                and "power_1_raw" not in polled, polled)
        measurement_sanity.forget(IEEE)

        c.section("what is not a fault")
        dev, updates = _device("CT clamp")
        meter = _em(dev, 1, switched=False)
        meter.attribute_updated(meter.ATTR_ACTIVE_POWER, -500)
        c.check("a meter that switches nothing may export", dev.state.get("power_1") == -500.0, dev.state)
        c.check("and may carry more than a socket", not measurement_sanity.implausible(
            meter, "active_power", 9000.0))
        raised.clear()

        dev, updates = _device("SmartPlug51AU")
        h = _em(dev, 1)
        h.attribute_updated(h.ATTR_RMS_VOLTAGE, 2400)
        c.check("2400 V from a fresh device is held back as unscaled, not blamed on it",
                dev.state.get("voltage_1") is None and raised == [], (dev.state, raised))
        c.check("and says which scale would fit",
                "/10 would fit" in measurement_sanity.judge(h, "rms_voltage", 2400.0)[1])
        h.attribute_updated(h.ATTR_RMS_VOLTAGE, 240)
        c.check("a real voltage shows", dev.state.get("voltage_1") == 240.0)
        h.attribute_updated(h.ATTR_RMS_VOLTAGE, 2400)
        c.check("the same number after sensible readings is a glitch, and is flagged",
                len(raised) == 1, raised)
        measurement_sanity.forget(IEEE)
        raised.clear()

        c.section("a model's own rating")
        device_profiles._store = _Store({"id": "m", "meta": {"source": "zmm"}, "endpoints": {},
                                         "zmm": {"measurements": {"active_power": {"max": 2500.0}}}})
        dev, updates = _device("SmallPlug")
        h = _em(dev, 1)
        c.check("3000 W is impossible for a plug rated 2500 W",
                "rating in its ZMM entry" in (measurement_sanity.implausible(h, "active_power", 3000.0) or ""))
    finally:
        app_alerts.raise_alert, app_alerts.resolve_alert = real
        device_profiles._store = None
        measurement_sanity.forget(IEEE)
    return c


if __name__ == "__main__":
    run()
