"""
Shared scaffolding for the Swarm Intelligence tests.

Plain scripts, matching tests/fuel: the project carries no test framework, and
each module exposes `run()` driven by tests/swarm/run_all.py.

The fake devices here stand in for the four registry shapes the resolver has to
cope with — a Zigbee device with a DeviceCapabilities object, a Matter device
with a list accessor, a duck-typed provider device (Nuki), and a presence user.
Building them by hand rather than importing the real classes keeps the tests
runnable without zigpy, a Matter server or a Nuki bridge.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def stub_duckdb() -> None:
    """Stand in for duckdb so modules that import it can be exercised without it.

    Only the name is needed: nothing in these tests opens a database, and the
    message store is driven through a fake. Mirrors how tests/fuel stubs
    aiohttp for the provider parsers.
    """
    if "duckdb" in sys.modules:
        return
    stub = types.ModuleType("duckdb")

    class _Conn:
        def execute(self, *a, **k):
            raise RuntimeError("the tests must not open a database")

    stub.DuckDBPyConnection = _Conn
    stub.connect = lambda *a, **k: _Conn()
    sys.modules["duckdb"] = stub


class Checker:
    """Collects pass/fail lines so a module can report as a group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, label: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failures.append(f"{self.name}: {label}")
            print(f"    FAIL {label}  <- {detail!r}"[:400])
        return bool(ok)


class FakeCapabilities:
    """Stands in for DeviceCapabilities / _LockCapabilities."""

    def __init__(self, caps: List[str]) -> None:
        self._caps = list(caps)

    def has_capability(self, cap: str) -> bool:
        return cap in self._caps

    def get_capabilities(self) -> List[str]:
        return list(self._caps)


class FakeDevice:
    """A device in the shape the automation engine's merged registry holds."""

    def __init__(self, ieee: str, name: str, state: Dict[str, Any],
                 commands: Optional[List[Dict[str, Any]]] = None,
                 capabilities: Any = None, model: str = "Test Model",
                 manufacturer: str = "Test") -> None:
        self.ieee = ieee
        self.friendly_name = name
        self.state = dict(state)
        self.model = model
        self.manufacturer = manufacturer
        self._commands = commands or []
        if capabilities is not None:
            self.capabilities = capabilities

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return list(self._commands)


class FakeMatterDevice(FakeDevice):
    """Matter devices expose their capabilities through a method, not an object."""

    def __init__(self, *args, matter_caps: Optional[List[str]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._matter_caps = matter_caps or []

    def _get_capabilities(self) -> List[str]:
        return list(self._matter_caps)


# Command sets mirroring device.commands.get_control_commands output.

ONOFF_COMMANDS = [
    {"command": "on", "label": "On", "endpoint_id": 1},
    {"command": "off", "label": "Off", "endpoint_id": 1},
    {"command": "toggle", "label": "Toggle", "endpoint_id": 1},
]

DIMMER_COMMANDS = ONOFF_COMMANDS + [
    {"command": "brightness", "label": "Brightness", "type": "slider",
     "min": 0, "max": 100, "endpoint_id": 1},
]

COVER_COMMANDS = [
    {"command": "open", "label": "Open", "endpoint_id": 1},
    {"command": "close", "label": "Close", "endpoint_id": 1},
    {"command": "stop", "label": "Stop", "endpoint_id": 1},
    {"command": "position", "label": "Position", "type": "slider",
     "min": 0, "max": 100, "endpoint_id": 1},
]

LOCK_COMMANDS = [
    {"command": "lock", "label": "Lock", "endpoint_id": None},
    {"command": "unlock", "label": "Unlock", "endpoint_id": None},
    {"command": "unlatch", "label": "Unlatch", "endpoint_id": None},
    # Dispatchable, and a valid step in a hand-built rule, but the swarm
    # vocabulary declares no action for it — so it is never *suggested*.
    {"command": "lock_n_go", "label": "Lock 'n' Go", "endpoint_id": None},
    # Nothing dispatches this. It exists to keep the gate honest.
    {"command": "polish_handle", "label": "Polish Handle", "endpoint_id": None},
]

THERMOSTAT_COMMANDS = [
    {"command": "temperature", "label": "Temp Setpoint", "type": "number",
     "unit": "C", "endpoint_id": 1},
]


def sample_network() -> Dict[str, Any]:
    """A small house: a hallway, a lounge, a front door and two people."""
    return {
        # Tuya radar reporting presence and lux from one device.
        "0xradar": FakeDevice(
            "0xradar", "Radar Sensor - Hallway",
            {"presence": True, "illuminance_lux": 4, "battery": 88},
            capabilities=FakeCapabilities(["presence_sensor", "radar_sensor",
                                           "occupancy_sensing", "tuya"]),
        ),
        # Dimmable light, same room.
        "0xhalllight": FakeDevice(
            "0xhalllight", "Light - Hallway",
            {"state": "OFF", "brightness": 180},
            commands=DIMMER_COMMANDS,
            capabilities=FakeCapabilities(["on_off", "light", "level_control"]),
        ),
        # Contact sensor on the front door, Zigbee polarity (True == closed).
        "0xfrontdoor": FakeDevice(
            "0xfrontdoor", "Front Door Contact",
            {"contact": True, "battery": 95},
            capabilities=FakeCapabilities(["contact_sensor", "battery"]),
        ),
        # Nuki lock — duck-typed provider device, no Zigbee clusters at all.
        "nuki_1": FakeDevice(
            "nuki_1", "Front Door Lock",
            {"locked": True, "lock_state": "locked", "door_state": "closed"},
            commands=LOCK_COMMANDS,
            capabilities=FakeCapabilities(["lock"]),
        ),
        # Matter plug with power metering, in the lounge.
        "matter_plug": FakeMatterDevice(
            "matter_plug", "Lounge Plug",
            {"state": "ON", "power": 42.0, "energy": 1.2},
            commands=ONOFF_COMMANDS,
            matter_caps=["matter", "on_off", "power_monitoring"],
        ),
        # Lounge TRV.
        "0xtrv": FakeDevice(
            "0xtrv", "Lounge TRV",
            {"local_temperature": 17.5, "pi_heating_demand": 40,
             "occupied_heating_setpoint": 21.0, "battery": 70},
            commands=THERMOSTAT_COMMANDS,
            capabilities=FakeCapabilities(["thermostat", "hvac", "battery"]),
        ),
        # Presence users — house scope, no room.
        "user::sean": FakeDevice(
            "user::sean", "Sean",
            {"presence": "home", "place": "home", "distance_m": 0.0},
        ),
        "user::charlie": FakeDevice(
            "user::charlie", "Charlie",
            {"presence": "away", "place": "away", "distance_m": 4200.0},
        ),
    }


SAMPLE_NAMES = {
    "0xradar": "Radar Sensor - Hallway",
    "0xhalllight": "Light - Hallway",
    "0xfrontdoor": "Front Door Contact",
    "nuki_1": "Front Door Lock",
    "matter_plug": "Lounge Plug",
    "0xtrv": "Lounge TRV",
    "user::sean": "Sean",
    "user::charlie": "Charlie",
}

SAMPLE_SETTINGS = {
    "0xradar": {"chamber": "hallway"},
    "0xhalllight": {"chamber": "hallway"},
    "0xfrontdoor": {"chamber": "hallway"},
    "matter_plug": {"chamber": "lounge"},
    "0xtrv": {"chamber": "lounge"},
}

SAMPLE_ROOMS = {"hallway": "Hallway", "lounge": "Lounge"}


def offer(offers, key):
    """Find one offer by its stable key."""
    for o in offers:
        if o["key"] == key:
            return o
    return None
