"""
docs/device-profiles.md §4, "Accepted": a setting counts as set only once the
device took it (write SUCCESS and a read-back that matches). A sleeping
device's write, read-back or Reconfigure waits for its next wake, and the
queue survives a restart.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

from device.handlers import DeviceHandlerManagerMixin
from handlers import tuya
from handlers.sensors import OccupancySensingHandler
from handlers.tuya import TuyaClusterHandler, TuyaDP
from modules import device_ops, zmm_settings

TIMEOUT = OccupancySensingHandler.ATTR_PIR_O_TO_U_DELAY


class _Occ:
    """A 0x0406 cluster: what it keeps of a write, and how it answers."""
    cluster_id = 0x0406

    def __init__(self, *, keeps=True, refuse=False, readable=True, asleep=False, read_asleep=False):
        self.keeps, self.refuse, self.readable = keeps, refuse, readable
        self.asleep, self.read_asleep = asleep, read_asleep
        self.held, self.writes = {}, []
        self.endpoint = NS(endpoint_id=2, in_clusters={0x0406: self})

    async def write_attributes(self, attrs, manufacturer=None):
        if self.asleep:
            raise asyncio.TimeoutError()
        self.writes.append(dict(attrs))
        if self.refuse:
            return [[NS(status=0x88)]]
        self.held.update({a: (v if self.keeps else 0) for a, v in attrs.items()})
        return [[NS(status=0)]]

    async def read_attributes_raw(self, attrs, manufacturer=None):
        if self.asleep or self.read_asleep:
            raise asyncio.TimeoutError()
        if not self.readable:
            return NS(status_records=[NS(attrid=a, status=0x86, value=None) for a in attrs])
        return NS(status_records=[NS(attrid=a, status=0, value=NS(value=self.held.get(a))) for a in attrs])


def _device(n, cluster, sleepy):
    dev = NS(ieee=f"aa:bb:cc:00:00:00:20:{n:02x}", state={}, service=NS(device_settings={}),
             _is_battery_powered=lambda: sleepy,
             zigpy_dev=NS(node_desc=NS(manufacturer_code=0x100B),
                          endpoints={0: None, 2: cluster.endpoint}))
    dev.update_state = lambda d: dev.state.update(d)
    h = object.__new__(OccupancySensingHandler)
    h.device, h.cluster, h.endpoint, h.cluster_id = dev, cluster, cluster.endpoint, cluster.cluster_id
    dev.handlers = {(2, 0x0406): h}
    return dev


def _save(dev, updates):
    return asyncio.run(DeviceHandlerManagerMixin.configure(dev, {"updates": updates}))


def _restart(path):
    """As a hub restart leaves the module: nothing in memory, the file on disk."""
    for table in (zmm_settings._PENDING, zmm_settings._CONFIRM, zmm_settings._PENDING_READS,
                  zmm_settings._READ_TRIED, zmm_settings._RECONF_TRIED):
        table.clear()
    zmm_settings._RECONFIGURE.clear()
    zmm_settings.QUEUE_PATH, zmm_settings._LOADED = str(path), False


def _handler_settings(c: Checker) -> None:
    c.section("c1: a handler setting is set only once the device took it")
    dev = _device(1, _Occ(), sleepy=False)
    out = _save(dev, {"motion_timeout": 120})
    c.check("taken and read back as written: set, and shown",
            out["applied"] == {"motion_timeout": 120} and not out["failed"]
            and dev.state.get("motion_timeout") == 120 and dev.state.get("pir_o_to_u_delay") == 120, (out, dev.state))

    dev = _device(2, _Occ(refuse=True), sleepy=False)
    dev.state["motion_timeout"] = 30
    out = _save(dev, {"motion_timeout": 120})
    c.check("refused: not set, and the old value still shown",
            out["failed"] == ["motion_timeout"] and dev.state["motion_timeout"] == 30, (out, dev.state))

    dev = _device(3, _Occ(keeps=False), sleepy=False)
    dev.state["motion_timeout"] = 30
    out = _save(dev, {"motion_timeout": 120})
    c.check("SUCCESS but read back different: not set, and what the device holds is shown",
            out["failed"] == ["motion_timeout"] and dev.state["motion_timeout"] == 0, (out, dev.state))

    dev = _device(4, _Occ(readable=False), sleepy=False)
    out = _save(dev, {"motion_timeout": 120})
    c.check("SUCCESS but the device refuses the read: set, marked unconfirmed",
            out["unconfirmed"] == ["motion_timeout"] and out["applied"] == {"motion_timeout": 120}
            and not out["failed"] and dev.state.get("motion_timeout") == 120, out)

    dev = _device(5, _Occ(asleep=True), sleepy=False)
    out = _save(dev, {"motion_timeout": 120})
    c.check("a mains device that does not answer: not set, not queued",
            out["failed"] == ["motion_timeout"] and not out["queued"], out)


def _sleepy(c: Checker, tmp: Path) -> None:
    c.section("c2/c3: a sleeping device's setting waits for it, through a restart")
    path = tmp / "queue.json"
    _restart(path)
    cl = _Occ(asleep=True)
    dev = _device(6, cl, sleepy=True)
    out = _save(dev, {"motion_timeout": 120})
    c.check("a write it missed is queued, and nothing is shown as set",
            out["queued"] == ["motion_timeout"] and "motion_timeout" not in dev.state, (out, dev.state))
    saved = json.loads(path.read_text())
    c.check("the queue is on disk", saved["settings"].get(dev.ieee) == {"motion_timeout": 120}, saved)

    _restart(path)
    c.check("after a restart it still waits", zmm_settings.pending(dev) == {"motion_timeout": 120},
            zmm_settings.pending(dev))
    cl.asleep = False
    asyncio.run(zmm_settings._flush(dev))
    c.check("at its next wake it is sent, read back and shown",
            cl.writes == [{TIMEOUT: 120}] and dev.state.get("motion_timeout") == 120
            and zmm_settings.pending(dev) == {}, (cl.writes, dev.state))
    c.check("and the queue on disk is empty again",
            json.loads(path.read_text())["settings"] == {}, path.read_text())

    c.section("c2: a write it took whose read-back it slept through waits to confirm")
    cl = _Occ(read_asleep=True)
    dev = _device(7, cl, sleepy=True)
    out = _save(dev, {"motion_timeout": 90})
    c.check("reported as queued, not set", out["queued"] == ["motion_timeout"] and not out["applied"], out)
    c.check("the form keeps what was asked for meanwhile", zmm_settings.pending(dev) == {"motion_timeout": 90})
    _restart(path)
    c.check("the confirmation survives a restart", dev.ieee in zmm_settings.pending(dev) or
            zmm_settings.pending(dev) == {"motion_timeout": 90}, zmm_settings.pending(dev))
    cl.read_asleep = False
    asyncio.run(zmm_settings._flush(dev))
    c.check("at its next wake it is read back and shown, without writing again",
            dev.state.get("motion_timeout") == 90 and cl.writes == [{TIMEOUT: 90}]
            and zmm_settings.pending(dev) == {}, (dev.state, cl.writes))

    cl = _Occ(read_asleep=True)
    dev = _device(8, cl, sleepy=True)
    _save(dev, {"motion_timeout": 90})
    cl.read_asleep, cl.held = False, {TIMEOUT: 10}
    asyncio.run(zmm_settings._flush(dev))
    c.check("one that reads back different at wake is not set, and the device's value is shown",
            dev.state.get("motion_timeout") == 10 and zmm_settings.pending(dev) == {}, dev.state)


class _Step:
    ALWAYS_CONFIGURE = True
    cluster_id = 0x0001

    def __init__(self):
        self.endpoint, self.asleep, self.runs = NS(endpoint_id=1), True, 0

    async def configure(self):
        self.runs += 1
        if self.asleep:
            raise asyncio.TimeoutError()
        return True


def _reconfigure(c: Checker, tmp: Path) -> None:
    c.section("c4: a Reconfigure a sleeping device does not answer runs at its next wake")
    path = tmp / "queue.json"
    _restart(path)
    step = _Step()
    dev = NS(ieee="aa:bb:cc:00:00:00:20:10", state={}, _is_battery_powered=lambda: True,
             handlers={(1, 1): step}, zigpy_dev=NS(endpoints={}))
    ran = []

    async def reconfigure_device(ieee):
        ran.append(ieee)
        return await DeviceHandlerManagerMixin.configure(dev)
    dev.service = NS(device_settings={}, reconfigure_device=reconfigure_device)

    stats = asyncio.run(DeviceHandlerManagerMixin.configure(dev))
    c.check("it is reported as waiting, not failed", stats.get("waiting") is True, stats)
    c.check("and queued, on disk", zmm_settings.reconfigure_waiting(dev)
            and json.loads(path.read_text())["reconfigure"] == [dev.ieee])
    rep = asyncio.run(device_ops.run(dev.ieee, "reconfigure", lambda: _done(stats), lambda *a: None))
    c.check("the action's verdict says it waits for the device", rep["verdict"] == "waiting"
            and "next time it wakes" in rep["advice"][0]["text"], rep["verdict"])
    _restart(path)
    step.asleep = False
    asyncio.run(zmm_settings._flush(dev))
    c.check("at its next frame it runs, and nothing waits any more",
            ran == [dev.ieee] and step.runs == 2 and not zmm_settings.reconfigure_waiting(dev), (ran, step.runs))

    mains_step = _Step()
    mains = NS(ieee="aa:bb:cc:00:00:00:20:11", state={}, _is_battery_powered=lambda: False,
               handlers={(1, 1): mains_step}, zigpy_dev=NS(endpoints={}), service=NS(device_settings={}))
    stats = asyncio.run(DeviceHandlerManagerMixin.configure(mains))
    c.check("a mains device that does not answer has failed; it is not asleep",
            stats.get("waiting") is False and stats["failed"] == 1 and not zmm_settings.reconfigure_waiting(mains),
            stats)


async def _done(v):
    return {"success": True, **v}


def _tuya(c: Checker) -> None:
    c.section("c5: a Tuya setting counts once the device reports its datapoint back")
    tuya.TUYA_ECHO_WAIT_SEC = 0.3

    def handler(sleepy, echo):
        dev = NS(ieee="aa:bb:cc:00:00:00:20:20", state={}, _is_battery_powered=lambda: sleepy,
                 zigpy_dev=NS(model="TS0601", manufacturer="_TZE200_test"))
        dev.update_state = lambda d: dev.state.update(d)
        h = object.__new__(TuyaClusterHandler)
        h.device, h._seq, h._echo = dev, 0, {}
        h._dp_map = {102: TuyaDP(102, "presence_sensitivity")}

        async def send_dp(dp_id, dp_type, value, raise_errors=False):
            if echo == "asleep":
                raise asyncio.TimeoutError()
            if echo is not None:
                data = bytes([0x00, 0x01, dp_id, dp_type, 0x00, 0x04]) + int(echo).to_bytes(4, "big", signed=True)
                asyncio.get_running_loop().call_later(0.05, h._handle_data_report, [data])
            return True
        h.send_dp = send_dp
        return h

    got = asyncio.run(handler(False, 7).apply_configuration({"presence_sensitivity": 7}))
    c.check("echoed with the written value: set", got == {"presence_sensitivity": "set"}, got)
    got = asyncio.run(handler(False, 3).apply_configuration({"presence_sensitivity": 7}))
    c.check("echoed with another value: not set", got == {"presence_sensitivity": "failed"}, got)
    got = asyncio.run(handler(False, None).apply_configuration({"presence_sensitivity": 7}))
    c.check("no echo within the wait: not set", got == {"presence_sensitivity": "failed"}, got)
    got = asyncio.run(handler(True, "asleep").apply_configuration({"presence_sensitivity": 7}))
    c.check("a sleeping device that missed it: queued", got == {"presence_sensitivity": "queued"}, got)
    got = asyncio.run(handler(False, 7).apply_configuration({"motion_timeout": 60}))
    c.check("a setting with no datapoint is left to its own handler", got == {}, got)


def _export(c: Checker) -> None:
    c.section("c9: the export reads zigpy's attribute cache, and never shows an unread table as empty")
    from modules.vendor_cache import cluster_cache
    try:
        from zigpy.zcl.helpers import AttributeCache, CacheItem
    except ImportError:
        return
    now = datetime.now(timezone.utc)
    cache = AttributeCache(NS())
    cache._cache[(0x0010, None)] = CacheItem(value=30, last_updated=now)
    cache._cache[(0x0030, 0x100B)] = CacheItem(value=2, last_updated=now)
    cache._legacy_cache[0x4000] = CacheItem(value=7, last_updated=now)
    rows = sorted(cluster_cache(NS(_attr_cache=cache)), key=lambda r: r[0])
    c.check("zigpy's AttributeCache, which has no items(), is read whole, with maker codes",
            rows == [(0x0010, None, 30), (0x0030, 0x100B, 2), (0x4000, None, 7)] and not hasattr(cache, "items"),
            rows)
    c.check("a plain dict cache still reads", cluster_cache(NS(_attr_cache={1: NS(value=5)})) == [(1, None, 5)])

    from core.service import ZigbeeService

    async def no_answer(*_):
        raise asyncio.TimeoutError()
    zdev = NS(nwk=0x1234, manufacturer="Signify", model="SML001", is_initialized=True, lqi=0, rssi=None,
              last_seen=None, node_desc=None, endpoints={}, zdo=NS(Mgmt_Bind_req=no_answer))
    svc = NS(devices={"x": NS(zigpy_dev=zdev, state={})}, device_settings={})
    cfg = asyncio.run(ZigbeeService.get_device_config(svc, "x")).get("config") or {}
    c.check("a binding table the device did not answer is null with the reason, not []",
            cfg.get("bindings") is None and cfg.get("bindings_error"), (cfg.get("bindings"), cfg.get("bindings_error")))


def run() -> Checker:
    c = Checker("settings_accepted")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_accept_"))
    keep = zmm_settings.QUEUE_PATH
    _restart(tmp / "queue.json")
    try:
        _handler_settings(c)
        _sleepy(c, tmp)
        _reconfigure(c, tmp)
        _tuya(c)
        _export(c)
    finally:
        _restart(Path(keep))
    return c


if __name__ == "__main__":
    raise SystemExit(1 if run().failures else 0)
