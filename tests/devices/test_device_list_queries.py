"""
/api/devices runs on the event loop, so its DuckDB reads stall every other
request: building the list reads the facts table once, however many devices
there are and however many settings each one asks about.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

import modules.device_profiles as device_profiles
import modules.zigbee_cache as zigbee_cache
from core.service import ZigbeeService
from device.commands import DeviceCommandExecutorMixin
from device.discovery import DeviceDiscoveryProviderMixin
from modules import device_facts
from modules.device_facts import Fact, _j, record
from modules.device_profiles import normalise_profile

# A made-up valve whose entry declares settings, two of them valve controls.
ENTRY = normalise_profile({
    "id": "VALVE-1", "match": {"model": "VALVE-1", "manufacturer": "Acme"},
    "zmm": {"settings": [
        {"id": "child_lock", "type": "bool", "ep": 1, "cluster": "0xFC99", "attr": "0x0001"},
        {"id": "sensor_type", "type": "uint8", "ep": 1, "cluster": "0xFC99", "attr": "0x0004",
         "values": {"0": "Internal", "1": "External"}},
    ]},
})


class _Store:
    def get_profile_for_device(self, **_):
        return ENTRY

    def get_ieee_mappings(self, _):
        return {}

    def get_ieee_pin(self, _):
        return None


class _Cluster:
    def __init__(self, cid, name):
        self.cluster_id, self.name = cid, name


class _Valve(DeviceDiscoveryProviderMixin, DeviceCommandExecutorMixin):
    """The settings half of a real device: config schema and supports_setting
    are the shipped mixins, so they read facts the way the service's do."""

    def __init__(self, ieee):
        self.ieee = ieee
        ep = NS(endpoint_id=1, device_type=0x0301, profile_id=0x0104,
                in_clusters={0xFC99: _Cluster(0xFC99, "valve"), 0x0201: _Cluster(0x0201, "thermostat")},
                out_clusters={})
        self.zigpy_dev = NS(ieee=ieee, nwk=0x1234, model="VALVE-1", manufacturer="Acme",
                            lqi=120, rssi=-60, node_desc=None, endpoints={0: None, 1: ep})
        self.handlers = {}
        self.state = {}
        self.last_seen = 0
        self.capabilities = NS(get_capabilities=lambda: set())

    def get_role(self):
        return "EndDevice"

    def is_available(self):
        return True


def _service(n):
    devices = {}
    for i in range(n):
        ieee = f"00:00:00:00:00:00:{i // 256:02x}:{i % 256:02x}"
        devices[ieee] = _Valve(ieee)
        record(ieee, [
            Fact(1, "attr:0xFC99/0x0001", "answered", _j({"type": "0x10/bool", "acl": "RW", "value": 0})),
            Fact(1, "attr:0xFC99/0x0004", "answered", _j({"type": "0x20/uint8", "acl": "RW", "value": 1})),
        ])
    return NS(devices=devices, friendly_names={}, device_settings={},
              polling_scheduler=NS(_intervals={}))


def run() -> Checker:
    c = Checker("device list queries")
    saved_store = device_profiles._store
    device_profiles._store = _Store()
    tmp = tempfile.mkdtemp(prefix="zmm_list_")
    zigbee_cache.DB_PATH = str(Path(tmp) / "cache.duckdb")
    zigbee_cache._db, zigbee_cache._INITIALISED = None, False
    real_execute = zigbee_cache._safe_execute
    queries = []

    def counting(sql, params=None, **kw):
        if "FROM device_facts" in sql:
            queries.append(sql)
        return real_execute(sql, params, **kw)

    try:
        zigbee_cache.warm()
        zigbee_cache._safe_execute = counting
        for n in (3, 12):
            svc = _service(n)
            for ieee in svc.devices:
                device_facts.forget(ieee)
            queries.clear()
            listed = ZigbeeService.get_device_list(svc)
            c.section(f"{n} devices")
            c.check("every device is listed", len(listed) == n, len(listed))
            schema = {o["name"] for o in (listed[0].get("config_schema") or [])} if listed else set()
            c.check("each carries the settings its facts show", {"child_lock", "sensor_type"} <= schema,
                    sorted(schema))
            c.check("and the valve controls it takes",
                    listed and set(listed[0].get("valve_settings") or []) == {"child_lock", "sensor_type"},
                    listed and listed[0].get("valve_settings"))
            c.check("the facts table is read once", len(queries) == 1, len(queries))

        queries.clear()
        dev = _service(1).devices
        next(iter(dev.values())).get_device_config_schema()
        c.check("a single device's schema outside the list still reads live", len(queries) >= 1, len(queries))
    finally:
        zigbee_cache._safe_execute = real_execute
        device_profiles._store = saved_store
        if zigbee_cache._db is not None:
            zigbee_cache._db.close()
        zigbee_cache._db = None
        shutil.rmtree(tmp, ignore_errors=True)
    return c


if __name__ == "__main__":
    run()
