"""
What a device is — one decision, read by every consumer that used to guess it
from a model name. Order: the user's correction, then a ZMM entry's or
profile's `device_type`, then rules over evidence the device itself gave
(endpoint functions, clusters, IAS zone type, power source). Rules and their
evidence: docs/endpoint-classification.md § Device class.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Iterable, NamedTuple, Optional, Set

from modules.endpoint_kind import COVER, LIGHT, LOCK, SWITCH, THERMOSTAT

logger = logging.getLogger("modules.device_class")

SUBJECT = "device_type"

ON_OFF, LEVEL, COLOR = 0x0006, 0x0008, 0x0300
MULTISTATE_IN = 0x0012
IAS_ZONE = 0x0500
OCCUPANCY = 0x0406
TEMPERATURE, HUMIDITY = 0x0402, 0x0405
METERING, ELECTRICAL = 0x0702, 0x0B04

# IAS Zone `zone_type` (0x0001): the device's own statement of what it senses.
ZONE_TYPES = {
    0x000D: "motion_sensor",
    0x0015: "contact_sensor",
    0x0028: "smoke_sensor",
    0x002A: "leak_sensor",
    0x002D: "vibration_sensor",
}

# Settings only a device that drives a valve pin takes.
VALVE_SETTINGS = frozenset({"valve_detection", "motor_calibration",
                            "valve_opening_degree", "valve_closing_degree"})

# What a remote's endpoint sends rather than serves.
CONTROL_OUTPUTS = frozenset({ON_OFF, LEVEL, COLOR})


class DeviceClass(NamedTuple):
    kind: str               # a key of device_profiles.DEVICE_TYPES
    reason: str
    source: str = "rule"    # user | zmm | profile | rule


class Evidence(NamedTuple):
    """Everything the rules read, gathered once so they stay pure."""
    functions: Dict[int, str]           # ep -> endpoint function (endpoint_kind)
    clusters_in: Dict[int, Set[int]]    # ep -> server clusters
    clusters_out: Dict[int, Set[int]]   # ep -> client clusters
    zone_types: Set[int]
    capabilities: Set[str]              # handler knowledge: Tuya datapoints, model quirks
    settings: FrozenSet[str] = frozenset()  # of VALVE_SETTINGS, those the device takes


def classify(ev: Evidence) -> DeviceClass:
    """Device class from evidence alone; first match wins. An actuator outranks
    a sensor on the same device, and a sensor outranks a remote's outputs."""
    fns = set(ev.functions.values())
    all_in = set().union(*ev.clusters_in.values()) if ev.clusters_in else set()
    all_out = set().union(*ev.clusters_out.values()) if ev.clusters_out else set()

    if THERMOSTAT in fns:
        if ev.settings & VALVE_SETTINGS:
            return DeviceClass("trv", "a thermostat endpoint that takes a valve setting")
        return DeviceClass("thermostat", "a thermostat endpoint")
    if COVER in fns or "cover" in ev.capabilities:
        return DeviceClass("blind", "a window-covering endpoint" if COVER in fns
                           else "cover capability from its handler")
    if LOCK in fns:
        return DeviceClass("lock", "a door-lock endpoint")
    if LIGHT in fns:
        coloured = any(COLOR in ev.clusters_in.get(ep, ()) for ep, f in ev.functions.items()
                       if f == LIGHT)
        return DeviceClass("color_light" if coloured else "light",
                           "a light endpoint with colour control" if coloured
                           else "a light endpoint")
    if SWITCH in fns:
        metered = bool(all_in & {METERING, ELECTRICAL})
        return DeviceClass("plug" if metered else "switch",
                           "a switch endpoint that meters its load" if metered
                           else "a switch endpoint")

    for zone_type in sorted(ev.zone_types):
        kind = ZONE_TYPES.get(zone_type)
        if kind:
            return DeviceClass(kind, f"IAS zone type 0x{zone_type:04X}")
    if "contact_sensor" in ev.capabilities:
        return DeviceClass("contact_sensor", "contact capability from its handler")
    if OCCUPANCY in all_in or ev.capabilities & {"motion_sensor", "presence_sensor"}:
        return DeviceClass("motion_sensor", "occupancy sensing" if OCCUPANCY in all_in
                           else "motion capability from its handler")
    if TEMPERATURE in all_in:
        return DeviceClass("temperature_sensor", "temperature measurement")
    if HUMIDITY in all_in:
        return DeviceClass("humidity_sensor", "humidity measurement")

    if (all_out & CONTROL_OUTPUTS and not all_in & CONTROL_OUTPUTS) or MULTISTATE_IN in all_in:
        return DeviceClass("button", "sends on/off or level commands, serves none"
                           if all_out & CONTROL_OUTPUTS else "multistate input")
    return DeviceClass("generic", "no function-specific evidence")


def _ids(clusters: Any) -> Set[int]:
    return {int(c) for c in (clusters or {})}


def _valve_settings(device) -> FrozenSet[str]:
    supports = getattr(device, "supports_setting", None)
    if supports is None:
        return frozenset()
    try:
        return frozenset(n for n in VALVE_SETTINGS if supports(n))
    except Exception as e:
        logger.debug(f"[{getattr(device, 'ieee', '?')}] valve settings unavailable: {e}")
        return frozenset()


def gather(device) -> Evidence:
    from modules.endpoint_kind import device_endpoint_function

    functions: Dict[int, str] = {}
    clusters_in: Dict[int, Set[int]] = {}
    clusters_out: Dict[int, Set[int]] = {}
    zone_types: Set[int] = set()
    for ep_id, ep in (getattr(device.zigpy_dev, "endpoints", None) or {}).items():
        if not ep_id or ep is None:
            continue
        clusters_in[ep_id] = _ids(getattr(ep, "in_clusters", None))
        clusters_out[ep_id] = _ids(getattr(ep, "out_clusters", None))
        fn = device_endpoint_function(device, ep)
        if fn:
            functions[ep_id] = fn.kind
        zone = (getattr(ep, "in_clusters", None) or {}).get(IAS_ZONE)
        if zone is not None:
            try:
                zt = zone.get("zone_type")
                if zt is not None:
                    zone_types.add(int(zt))
            except Exception:
                pass    # never read, or a cluster stub without a cache
    caps = getattr(device, "capabilities", None)
    capabilities = set(caps.get_capabilities()) if caps is not None else set()
    settings = _valve_settings(device) if THERMOSTAT in functions.values() else frozenset()
    return Evidence(functions, clusters_in, clusters_out, zone_types, capabilities, settings)


def pinned_class(device) -> Optional[DeviceClass]:
    """What a person said the device is — the user's correction, else an
    entry's or profile's `device_type` — or None. Reads no capabilities, so
    capability detection can ask it."""
    from modules.device_facts import DEVICE, user_facts
    from modules.device_profiles import DEVICE_TYPES, profile_for_device

    ieee = str(getattr(device, "ieee", "") or "")
    user = user_facts(ieee).get((DEVICE, SUBJECT)) if ieee else None
    if user in DEVICE_TYPES:
        return DeviceClass(user, "set by user", "user")
    profile = profile_for_device(device)
    pinned = (profile or {}).get("device_type")
    if pinned in DEVICE_TYPES and pinned != "generic":
        zmm = (profile.get("meta") or {}).get("source") == "zmm"
        return DeviceClass(pinned, f"{'ZMM entry' if zmm else 'profile'} {profile['id']}",
                           "zmm" if zmm else "profile")
    return None


def device_class(device, refresh: bool = False) -> DeviceClass:
    """The device's class with its overrides, cached on the device and
    recorded as a decision when it changes."""
    cached = getattr(device, "_device_class", None)
    if cached is not None and not refresh:
        return cached

    from modules.device_decisions import record
    from modules.device_facts import DEVICE

    ieee = str(getattr(device, "ieee", "") or "")
    result = pinned_class(device) or classify(gather(device))

    try:
        device._device_class = result
    except Exception:
        pass
    if ieee:
        record(ieee, DEVICE, SUBJECT, result.kind, result.source, result.reason)
    return result


def invalidate(device) -> None:
    """Drop the cached class: after a correction, a new entry, or re-detection."""
    try:
        device._device_class = None
    except Exception:
        pass


def is_mains_powered(device) -> Optional[bool]:
    """The node descriptor's own answer; None when the device never gave one.
    A model name containing "plug" or "bulb" is not evidence of anything."""
    nd = getattr(getattr(device, "zigpy_dev", None), "node_desc", None)
    if nd is None:
        return None
    mains = getattr(nd, "is_mains_powered", None)
    return None if mains is None else bool(mains)


def describe(device) -> Dict[str, Any]:
    """The class as the API carries it."""
    from modules.device_profiles import DEVICE_TYPES
    dc = device_class(device)
    mains = is_mains_powered(device)
    return {"type": dc.kind, "label": DEVICE_TYPES[dc.kind]["label"],
            "source": dc.source, "reason": dc.reason,
            "power": None if mains is None else ("mains" if mains else "battery")}


def has_function(device, function: str) -> bool:
    """Whether any endpoint of the device has the given endpoint function."""
    return function in gather(device).functions.values()


def kinds() -> Iterable[str]:
    from modules.device_profiles import DEVICE_TYPES
    return DEVICE_TYPES.keys()
