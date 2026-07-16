"""
modules/frames.py
=================
Frames — dynamically-generated dashboards, laid out from device chamber + type.

Pure module: no I/O, no FastAPI, no global state. Wired in by
``routes/frame_routes.py``.

Terminology
-----------
Hive = the install. Frame = a dashboard. Chamber = a room (see
``modules/chambers.py``). **Cell = one device tile** — a honeycomb cell is the
individual hexagon.

Cell kind is precedence-ordered
-------------------------------
A device carries many capabilities at once — a smart bulb reporting power and
battery is still a light — so the cell's identity is its most specific *control*
surface, resolved in this order::

    climate > cover > light > switch > lock > sensor > unknown

``battery`` / ``power_monitoring`` / ``lqi`` never decide cell kind; they render
as badges. ``unknown`` still renders (name + last seen) rather than being
silently dropped: a device you can't see is worse than a device you can't use.

Why this module does NOT use CAPABILITY_TO_HA
---------------------------------------------
There are two capability vocabularies in this codebase and they do not match.
``DeviceCapabilities.get_capabilities()`` (modules/device_capabilities.py) emits
``contact_sensor`` / ``motion_sensor`` / ``level_control`` / ``temperature_sensor``.
``CAPABILITY_TO_HA`` (modules/device_profiles.py) keys are ``contact`` / ``motion``
/ ``brightness`` / ``temperature``. Only ``on_off``, ``cover``, ``thermostat`` and
``battery`` coincide. CAPABILITY_TO_HA belongs to the *profiles* layer and is not
usable against ``capability_list``.

Two deliberate subtleties
-------------------------
1. The switch cell matches ``switch``, never ``on_off``. The quirk system adds
   ``on_off`` whenever the cluster exists but *discards* ``switch`` to mean "this
   is not a controllable actuator" — a Philips SML motion sensor has the OnOff
   cluster on a controller endpoint, and ``lumi.sensor_magnet`` likewise. Matching
   ``on_off`` would render motion and contact sensors as toggles.

2. Contact vs motion is disambiguated by state, not capability alone.
   ``device_capabilities`` maps IAS_ZONE to ``motion_sensor`` for every model
   except ``lumi.sensor_magnet``, so non-Aqara door sensors (Sonoff, Tuya) arrive
   typed as motion. A ``contact`` / ``is_open`` state key is stronger evidence and
   wins — the same reasoning as ``overview.js:hasContactSensing()``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.frames")

SCHEMA_VERSION = 1

# ── cell kinds ──────────────────────────────────────────────────────

CELL_CLIMATE = "climate"
CELL_COVER = "cover"
CELL_LIGHT = "light"
CELL_SWITCH = "switch"
CELL_LOCK = "lock"
CELL_SENSOR = "sensor"
CELL_UNKNOWN = "unknown"

#: Most specific control surface first. The first match decides the cell.
#:
#: ``lock`` is declared but nothing emits it yet: DeviceCapabilities has no lock
#: detection (locks are Nuki, via modules/nuki_controller.py, outside the Zigbee
#: capability path). It stays here so the precedence order is the whole story
#: rather than a half-truth, and lands with the non-Zigbee adapters.
_CONTROL_PRECEDENCE: Tuple[Tuple[str, frozenset], ...] = (
    (CELL_CLIMATE, frozenset({"thermostat", "fan_control"})),
    (CELL_COVER, frozenset({"cover", "window_covering"})),
    (CELL_LIGHT, frozenset({"light"})),
    # NOT "on_off" — see module docstring.
    (CELL_SWITCH, frozenset({"switch"})),
    (CELL_LOCK, frozenset({"lock"})),
)

CELL_LABELS = {
    CELL_CLIMATE: "Climate",
    CELL_COVER: "Blinds & Covers",
    CELL_LIGHT: "Lights",
    CELL_SWITCH: "Switches",
    CELL_LOCK: "Locks",
    CELL_SENSOR: "Sensors",
    CELL_UNKNOWN: "Other",
}

#: Order device-type groups appear in a type-split frame. Things you act on
#: first, things you only read last.
CELL_ORDER = (CELL_LIGHT, CELL_SWITCH, CELL_COVER, CELL_CLIMATE, CELL_LOCK, CELL_SENSOR, CELL_UNKNOWN)

UNASSIGNED_KEY = "__unassigned__"
UNASSIGNED_LABEL = "Unassigned"

# ── sensor readouts ─────────────────────────────────────────────────

#: Read-only sensor kinds → the state keys that carry them, in display order.
#: A single device may produce several (an Aqara THP is temp + humidity + pressure).
_SENSOR_READOUTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("contact", ("contact", "is_open")),
    ("motion", ("occupancy", "motion", "presence")),
    ("leak", ("water_leak",)),
    ("smoke", ("smoke", "co_detected")),
    ("vibration", ("vibration",)),
    ("temperature", ("temperature", "local_temperature", "device_temperature")),
    ("humidity", ("humidity",)),
    ("illuminance", ("illuminance_lux", "illuminance")),
    ("pressure", ("pressure",)),
    ("co2", ("co2",)),
    ("power", ("power", "active_power")),
    ("energy", ("energy", "daily_energy")),
)

#: Capabilities that imply a readout even before the device has reported state.
#: Without these a battery sensor that hasn't woken yet would render as `unknown`.
_CAPABILITY_READOUTS: Tuple[Tuple[str, str], ...] = (
    ("contact_sensor", "contact"),
    ("motion_sensor", "motion"),
    ("occupancy_sensing", "motion"),
    ("presence_sensor", "motion"),
    ("radar_sensor", "motion"),
    ("temperature_sensor", "temperature"),
    ("humidity_sensor", "humidity"),
    ("illuminance_sensor", "illuminance"),
    ("pressure_sensor", "pressure"),
    ("power_monitoring", "power"),
    ("metering", "power"),
)

#: Sensor kinds that are binary states, not numbers — the renderer shows a pill.
BINARY_SENSOR_KINDS = frozenset({"contact", "motion", "leak", "smoke", "vibration"})

#: Motion capabilities that are trustworthy on their own: a real OccupancySensing
#: cluster, or Tuya radar detection.
_RELIABLE_MOTION_CAPS = frozenset({"occupancy_sensing", "presence_sensor", "radar_sensor"})

#: Sensor kinds whose presence in state disproves an IAS-derived motion claim.
#: device_capabilities maps IAS_ZONE to `motion_sensor` for every model except
#: `lumi.sensor_magnet`, so door/leak/smoke/vibration sensors all arrive claiming
#: motion. What the device actually reports wins.
_DISPROVES_MOTION = frozenset({"contact", "leak", "smoke", "vibration"})


def _caps(device: Dict[str, Any]) -> set:
    caps = device.get("capability_list")
    return set(caps) if isinstance(caps, list) else set()


def _state(device: Dict[str, Any]) -> Dict[str, Any]:
    s = device.get("state")
    return s if isinstance(s, dict) else {}


def control_kind(device: Dict[str, Any]) -> Optional[str]:
    """The device's most specific control surface, or None if it isn't controllable."""
    caps = _caps(device)
    for kind, needed in _CONTROL_PRECEDENCE:
        if caps & needed:
            return kind
    return None


def _has_contact_evidence(device: Dict[str, Any]) -> bool:
    """
    True when state says this is a contact sensor, whatever capability detection
    decided. See module docstring: IAS_ZONE devices arrive typed as motion.
    """
    s = _state(device)
    return s.get("contact") is not None or s.get("is_open") is not None


def sensor_readouts(device: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Readouts this device can show, as ``[{kind, key, binary}]``.

    Derived from reported state first (authoritative — it's what the device
    actually sends), then topped up from capabilities so a sleepy device that
    hasn't reported yet still renders something meaningful.
    """
    s = _state(device)
    caps = _caps(device)
    out: List[Dict[str, Any]] = []
    seen: set = set()

    contact_evidence = _has_contact_evidence(device)

    for kind, keys in _SENSOR_READOUTS:
        for key in keys:
            if key in s and s[key] is not None:
                if kind in seen:
                    break
                # A device reporting `contact` is a contact sensor, so don't also
                # claim motion from an IAS-derived motion_sensor capability.
                if kind == "motion" and contact_evidence:
                    break
                seen.add(kind)
                out.append({"kind": kind, "key": key, "binary": kind in BINARY_SENSOR_KINDS})
                break

    # An IAS-derived motion claim is only worth adding when nothing the device
    # actually reports contradicts it, or when a reliable motion cluster backs it up.
    motion_disproved = bool(seen & _DISPROVES_MOTION) or contact_evidence
    for cap, kind in _CAPABILITY_READOUTS:
        if cap not in caps or kind in seen:
            continue
        if kind == "motion" and motion_disproved and not (caps & _RELIABLE_MOTION_CAPS):
            continue
        seen.add(kind)
        # No key yet — the device hasn't reported one. The renderer shows the
        # readout as pending rather than inventing a value.
        out.append({"kind": kind, "key": None, "binary": kind in BINARY_SENSOR_KINDS})

    return out


def features(device: Dict[str, Any], kind: str) -> List[str]:
    """
    Quick actions this cell can offer, from what the device actually supports.

    This is the "quick action based on control availability" rule: a bulb with
    LevelControl gets a brightness slider, one without gets only a toggle.
    """
    caps = _caps(device)
    out: List[str] = []

    if kind == CELL_LIGHT:
        if "on_off" in caps:
            out.append("toggle")
        if "level_control" in caps:
            out.append("brightness")
        if "color_control" in caps:
            out.append("color")
    elif kind == CELL_SWITCH:
        out.append("toggle")
        if "multi_switch" in caps:
            out.append("multi_endpoint")
    elif kind == CELL_COVER:
        out.extend(["open", "close", "stop"])
        if "level_control" in caps or "window_covering" in caps:
            out.append("position")
    elif kind == CELL_CLIMATE:
        if "thermostat" in caps:
            out.extend(["setpoint", "mode"])
        if "fan_control" in caps:
            out.append("fan")
    elif kind == CELL_LOCK:
        out.extend(["lock", "unlock"])

    return out


def badges(device: Dict[str, Any]) -> List[str]:
    """
    Secondary indicators. Never decide cell kind — a light that reports power is
    still a light.
    """
    caps = _caps(device)
    s = _state(device)
    out: List[str] = []
    if "battery" in caps or "battery" in s:
        out.append("battery")
    if caps & {"power_monitoring", "metering"}:
        out.append("power")
    return out


def resolve_cell(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build one cell descriptor from a ``get_device_list()`` entry.

    Returns::

        {ieee, name, chamber, kind, features, readouts, badges, available}

    ``kind == "unknown"`` means no control surface and nothing readable — the
    renderer still shows name + last seen.
    """
    kind = control_kind(device)
    readouts = sensor_readouts(device)

    if kind is None:
        kind = CELL_SENSOR if readouts else CELL_UNKNOWN

    # A controllable device can still carry readouts worth showing (a TRV's
    # local temperature, a plug's power draw) — keep them as secondary detail.
    return {
        "ieee": device.get("ieee"),
        "name": device.get("friendly_name") or device.get("ieee"),
        "chamber": (device.get("settings") or {}).get("chamber"),
        "kind": kind,
        "features": features(device, kind),
        "readouts": readouts,
        "badges": badges(device),
        "available": bool(device.get("available")),
        "last_seen_ts": device.get("last_seen_ts"),
    }


def is_zigbee(device: Dict[str, Any]) -> bool:
    """Frames is Zigbee-only for now; AC/media/heating adapters come later."""
    protocol = device.get("protocol")
    return not protocol or protocol == "zigbee"


# ── layout ──────────────────────────────────────────────────────────

SPLIT_CHAMBER = "chamber"
SPLIT_TYPE = "type"
VALID_SPLITS = (SPLIT_CHAMBER, SPLIT_TYPE)


def build_auto_frame(
    devices: List[Dict[str, Any]],
    split: str = SPLIT_CHAMBER,
    chambers: Optional[List[dict]] = None,
    include_chambers: Optional[List[str]] = None,
    include_kinds: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Group devices into a frame.

    ``split="chamber"`` — one group per room, cells ordered by device type.
    ``split="type"``    — one group per device type, cells ordered by room.

    Both are the same grouping over two keys; only the key function and the
    labelling differ.

    ``include_chambers`` / ``include_kinds`` filter the frame to what the user
    picked. Empty or None means everything.

    Chamber group order follows the registry (level index, then name), so a
    chamber-split frame reads ground floor first. Unassigned devices always sort
    last — they're a to-do list, not a room.
    """
    if split not in VALID_SPLITS:
        split = SPLIT_CHAMBER

    chambers = chambers or []
    chamber_names = {c["id"]: c["name"] for c in chambers}
    chamber_rank = {c["id"]: i for i, c in enumerate(chambers)}

    cells = [resolve_cell(d) for d in devices if is_zigbee(d)]

    if include_chambers:
        wanted = set(include_chambers)
        cells = [c for c in cells if c["chamber"] in wanted]
    if include_kinds:
        wanted = set(include_kinds)
        cells = [c for c in cells if c["kind"] in wanted]

    groups: Dict[str, Dict[str, Any]] = {}
    for cell in cells:
        if split == SPLIT_CHAMBER:
            key = cell["chamber"] or UNASSIGNED_KEY
            label = chamber_names.get(cell["chamber"], UNASSIGNED_LABEL if not cell["chamber"] else cell["chamber"])
        else:
            key = cell["kind"]
            label = CELL_LABELS.get(cell["kind"], cell["kind"])

        groups.setdefault(key, {"key": key, "label": label, "cells": []})["cells"].append(cell)

    def group_sort(g: Dict[str, Any]):
        if split == SPLIT_CHAMBER:
            # Unassigned last, whatever it's called.
            if g["key"] == UNASSIGNED_KEY:
                return (2, 0, "")
            rank = chamber_rank.get(g["key"])
            # A chamber id with no registry entry (hand-edited config) sorts
            # between real chambers and Unassigned rather than vanishing.
            return (0, rank, "") if rank is not None else (1, 0, g["label"].lower())
        order = CELL_ORDER.index(g["key"]) if g["key"] in CELL_ORDER else len(CELL_ORDER)
        return (order, 0, "")

    def cell_sort(cell: Dict[str, Any]):
        if split == SPLIT_CHAMBER:
            kind_rank = CELL_ORDER.index(cell["kind"]) if cell["kind"] in CELL_ORDER else len(CELL_ORDER)
            return (kind_rank, cell["name"].lower())
        rank = chamber_rank.get(cell["chamber"], len(chamber_rank) + 1) if cell["chamber"] else len(chamber_rank) + 2
        return (rank, cell["name"].lower())

    out = sorted(groups.values(), key=group_sort)
    for g in out:
        g["cells"].sort(key=cell_sort)

    return {
        "version": SCHEMA_VERSION,
        "split": split,
        "groups": out,
        "total": len(cells),
    }
