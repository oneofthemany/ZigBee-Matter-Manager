"""
Light-vs-switch classification for an On/Off endpoint — the single source for
HA discovery, device capabilities and the Control tab. Rules and the evidence
behind their order: docs/endpoint-classification.md.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple, Optional

LIGHT = "light"
SWITCH = "switch"

ON_OFF        = 0x0006
LEVEL         = 0x0008
WINDOW_COVER  = 0x0102
COLOR         = 0x0300
LIGHTLINK     = 0x1000
MULTISTATE_IN = 0x0012
METERING      = 0x0702
ELECTRICAL    = 0x0B04
SONOFF        = 0xFC11

# Clusters a lamp never carries: an EP with one drives a load or a button.
LOAD_CLUSTERS = frozenset({ELECTRICAL, METERING, MULTISTATE_IN, SONOFF})

PROFILE_HA  = 0x0104
PROFILE_ZLL = 0xC05E

# Device types that only an outlet or relay reports. "On/Off Light" (0x0100)
# is deliberately absent: Tuya and Aqara relays report it on every gang.
SWITCH_DEVICE_TYPES = {
    PROFILE_HA:  frozenset({0x0002, 0x0009, 0x0051, 0x010A}),
    PROFILE_ZLL: frozenset({0x0010}),
}

# Dimmable/colour light types. Trusted only to settle an EP that has Level:
# on an On/Off-only EP they are wrong as often as right (lumi.relay.c2acn01).
DIMMABLE_LIGHT_TYPES = {
    PROFILE_HA:  frozenset({0x0101, 0x0102, 0x010C, 0x010D}),
    PROFILE_ZLL: frozenset({0x0100, 0x0200, 0x0210, 0x0220}),
}


class EndpointKind(NamedTuple):
    kind: str       # LIGHT | SWITCH
    reason: str


def _as_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def classify(in_clusters: Iterable[int], profile_id=None, device_type=None,
             override: Optional[str] = None) -> Optional[EndpointKind]:
    """Kind of an EP with On/Off as an input cluster; None for any other EP.

    Vendor clusters (0xFCC0, 0xEF00) are never evidence: they mark who made
    the device, not what it drives.
    """
    ids = set(in_clusters)
    if ON_OFF not in ids:
        return None
    if override in (LIGHT, SWITCH):
        return EndpointKind(override, "profile override")
    if COLOR in ids:
        return EndpointKind(LIGHT, "colour control")
    dt, profile = _as_int(device_type), _as_int(profile_id)
    if dt in SWITCH_DEVICE_TYPES.get(profile, ()):
        return EndpointKind(SWITCH, f"outlet device type 0x{dt:04X}")
    load = ids & LOAD_CLUSTERS
    load_names = ",".join(f"0x{c:04X}" for c in sorted(load))
    if LEVEL in ids and WINDOW_COVER not in ids:
        if not load:
            return EndpointKind(LIGHT, "level control")
        # Metered wall dimmers and LED-dimming sockets share this shape;
        # only the declared type tells them apart.
        if dt in DIMMABLE_LIGHT_TYPES.get(profile, ()):
            return EndpointKind(LIGHT, f"level with dimmable-light type 0x{dt:04X}")
        return EndpointKind(SWITCH, f"level beside load cluster {load_names}")
    if load:
        return EndpointKind(SWITCH, f"load cluster {load_names}")
    if LIGHTLINK in ids:
        return EndpointKind(LIGHT, "touchlink")
    return EndpointKind(SWITCH, "on/off only")


def classify_endpoint(endpoint, override: Optional[str] = None) -> Optional[EndpointKind]:
    """classify() for a zigpy Endpoint."""
    return classify(getattr(endpoint, "in_clusters", None) or {},
                    getattr(endpoint, "profile_id", None),
                    getattr(endpoint, "device_type", None),
                    override)


def profile_override(device, ep_id: int) -> Optional[str]:
    """`endpoints[ep].kind` from the device's profile, if one pins it."""
    try:
        from modules.device_profiles import get_profile_store
        z = getattr(device, "zigpy_dev", None)
        profile = get_profile_store().get_profile_for_device(
            ieee=str(getattr(device, "ieee", "")),
            model=str(getattr(z, "model", "") or ""),
            manufacturer=str(getattr(z, "manufacturer", "") or ""))
    except Exception:
        return None
    kind = ((profile or {}).get("endpoints") or {}).get(str(ep_id), {}).get("kind")
    return kind if kind in (LIGHT, SWITCH) else None


def classify_device_endpoint(device, endpoint) -> Optional[EndpointKind]:
    return classify_endpoint(endpoint, profile_override(device, endpoint.endpoint_id))
