"""
docs/device-profiles.md §4, "Reporting interval": the device modal's max
report interval is stored only once the device takes it, waits for a sleeping
device to wake, survives later saves, and can be cleared back to defaults.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles_apply as dpa
from modules import reporting_interval as ri, zmm_settings

IEEE = "aa:bb:cc:00:00:00:10:01"


def _fq(out):
    return {k: out.get(k) for k in ("failed", "queued")}


class _Measuring:
    """A handler with set_reporting_max: accepts, refuses, or isn't reachable."""
    CLUSTER_ID = 0x0402

    def __init__(self, mode="accept"):
        self.mode, self.sent, self.configured = mode, [], 0

    async def set_reporting_max(self, value):
        if self.mode == "asleep":
            raise asyncio.TimeoutError()
        self.sent.append(value)
        return self.mode == "accept"

    async def configure(self):
        self.configured += 1


def _device(handler, battery=True):
    saves = []
    service = NS(device_settings={}, _save_json=lambda path, data: saves.append(path))
    dev = NS(ieee=IEEE, state={}, handlers={(1, 0x0402): handler, 0x0402: handler},
             service=service, _is_battery_powered=lambda: battery,
             zigpy_dev=NS(endpoints={}))
    return dev, saves


def _configure(dev, updates):
    from device.handlers import DeviceHandlerManagerMixin
    return asyncio.run(DeviceHandlerManagerMixin.configure(dev, {"updates": updates}))


def run() -> Checker:
    c = Checker("reporting_interval")
    saved_owns = dpa.entry_owns_reporting
    dpa.entry_owns_reporting = lambda d: False
    zmm_settings._PENDING.clear()
    try:
        _accepted_and_refused(c)
        _sleepy(c)
        _merge(c)
        _clear(c)
        _settings_only(c)
    finally:
        dpa.entry_owns_reporting = saved_owns
        zmm_settings._PENDING.clear()
    return c


def _accepted_and_refused(c):
    c.section("stored only when the device takes it")
    h = _Measuring()
    dev, saves = _device(h)
    out = _configure(dev, {"reporting_max": 1200})
    c.check("an accepted interval is stored", ri.stored(dev) == 1200 and _fq(out) == {"failed": [], "queued": []},
            (dev.service.device_settings, out))
    c.check("and saved to disk", saves == [ri.SETTINGS_PATH], saves)
    c.check("the handler got the number", h.sent == [1200], h.sent)

    h = _Measuring("refuse")
    dev, saves = _device(h)
    out = _configure(dev, {"reporting_max": 900})
    c.check("r2: a refused interval is not stored", ri.stored(dev) is None, dev.service.device_settings)
    c.check("r2: and the save names it as not set", out["failed"] == ["reporting_max"], out)

    dev, _ = _device(None)
    dev.handlers = {}
    out = _configure(dev, {"reporting_max": 900})
    c.check("a device with nothing that measures says not set", out["failed"] == ["reporting_max"], out)


def _sleepy(c):
    c.section("a sleeping device gets it when it wakes")
    h = _Measuring("asleep")
    dev, _ = _device(h)
    out = _configure(dev, {"reporting_max": 1800})
    c.check("r3: an undelivered change on a battery device is queued",
            _fq(out) == {"failed": [], "queued": ["reporting_max"]}, out)
    c.check("r3: and not stored yet", ri.stored(dev) is None, dev.service.device_settings)
    h.mode = "accept"
    asyncio.run(zmm_settings._flush(dev))
    c.check("r3: the next frame sends it, and only then is it stored",
            h.sent == [1800] and ri.stored(dev) == 1800, (h.sent, dev.service.device_settings))
    c.check("r3: and the queue is empty", not zmm_settings.pending(dev), zmm_settings.pending(dev))

    h = _Measuring("asleep")
    dev, _ = _device(h, battery=False)
    out = _configure(dev, {"reporting_max": 1800})
    c.check("a mains device that doesn't answer is not set, not queued",
            _fq(out) == {"failed": ["reporting_max"], "queued": []}, out)


def _merge(c):
    c.section("r1: a later save keeps what it doesn't name")
    try:
        from core.service import ZigbeeService
    except ImportError as e:
        c.check(f"core.service imports ({e.name} missing)", False, e)
        return
    h = _Measuring()
    dev, _ = _device(h)
    dev.configure = lambda config=None: _device_configure(dev, config)
    svc = NS(devices={IEEE: dev}, device_settings=dev.service.device_settings,
             _save_json=lambda path, data: None)
    dev.service = svc
    asyncio.run(ZigbeeService.configure_device(svc, IEEE, {"ieee": IEEE, "qos": 0,
                                                           "updates": {"reporting_max": 1200}}))
    asyncio.run(ZigbeeService.configure_device(svc, IEEE, {"ieee": IEEE, "qos": 2,
                                                           "updates": {"window_detection": 1}}))
    saved = svc.device_settings.get(IEEE) or {}
    c.check("r1: saving another setting keeps the reporting interval",
            saved.get("qos") == 2 and saved.get("updates") == {"reporting_max": 1200, "window_detection": 1},
            saved)
    h.mode = "refuse"
    asyncio.run(ZigbeeService.configure_device(svc, IEEE, {"ieee": IEEE, "updates": {"reporting_max": 60}}))
    c.check("a refused change leaves the stored one",
            (svc.device_settings[IEEE].get("updates") or {}).get("reporting_max") == 1200, svc.device_settings)


async def _device_configure(dev, config):
    from device.handlers import DeviceHandlerManagerMixin
    return await DeviceHandlerManagerMixin.configure(dev, config)


def _clear(c):
    c.section("r4: clearing goes back to the default reporting")
    h = _Measuring()
    dev, _ = _device(h)
    _configure(dev, {"reporting_max": 1200})
    out = _configure(dev, {"reporting_max": ri.DEFAULT})
    c.check("r4: the stored value is forgotten", ri.stored(dev) is None and out["failed"] == [],
            (dev.service.device_settings, out))
    c.check("r4: and each measuring handler's own setup runs again", h.configured == 1, h.configured)

    rows = []

    async def apply_reporting(device, profile):
        rows.append(profile)
        return {"success": True}

    saved = (dpa.entry_owns_reporting, dpa.apply_reporting, dpa.resolve_profile_for_device)
    dpa.entry_owns_reporting = lambda d: True
    dpa.apply_reporting = apply_reporting
    dpa.resolve_profile_for_device = lambda d: {"id": "entry"}
    try:
        h = _Measuring()
        dev, _ = _device(h)
        _configure(dev, {"reporting_max": 1200})
        _configure(dev, {"reporting_max": ri.DEFAULT})
        c.check("r4: an entry that owns reporting has its rows sent instead",
                rows == [{"id": "entry"}] and h.configured == 0 and ri.stored(dev) is None, (rows, h.configured))
    finally:
        dpa.entry_owns_reporting, dpa.apply_reporting, dpa.resolve_profile_for_device = saved


def _settings_only(c):
    c.section("a save with no setting changes sends the device nothing")
    h = _Measuring()
    h.endpoint, h.cluster_id = NS(endpoint_id=1), 0x0402
    dev, _ = _device(h)
    dev.capabilities = NS(is_endpoint_configurable=lambda ep: True,
                          is_cluster_configurable=lambda cid, ep: True,
                          get_endpoint_role=lambda ep: "sensor")
    from device.handlers import DeviceHandlerManagerMixin
    out = asyncio.run(DeviceHandlerManagerMixin.configure(dev, {"qos": 2, "updates": {}}))
    c.check("q1: QoS-only save (empty updates) runs no handler's setup",
            h.configured == 0 and _fq(out) == {"failed": [], "queued": []}, (h.configured, out))
    asyncio.run(DeviceHandlerManagerMixin.configure(dev, {"qos": 1}))
    c.check("q1: nor does a save with no updates key", h.configured == 0, h.configured)
    c.check("q2: and QoS is kept", dev.service.device_settings[IEEE].get("qos") == 1, dev.service.device_settings)
    asyncio.run(DeviceHandlerManagerMixin.configure(dev))
    c.check("q3: configure() with no config still runs the full setup", h.configured == 1, h.configured)

    try:
        from core.service import ZigbeeService
    except ImportError as e:
        c.check(f"core.service imports ({e.name} missing)", False, e)
        return
    polled = []
    svc = NS(devices={IEEE: dev}, device_settings=dev.service.device_settings,
             _save_json=lambda path, data: None,
             set_polling_interval=lambda ieee, s: polled.append((ieee, s)))
    dev.configure = lambda config=None: _device_configure(dev, config)
    asyncio.run(ZigbeeService.configure_device(svc, IEEE, {"ieee": IEEE, "qos": 0, "polling_interval": 60,
                                                           "updates": {}}))
    c.check("q2: a polling interval reaches the polling scheduler", polled == [(IEEE, 60)], polled)
    c.check("q2: still without reconfiguring the device", h.configured == 1, h.configured)


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
