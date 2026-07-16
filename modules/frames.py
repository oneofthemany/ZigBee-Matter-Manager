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

# Frame ids follow the same grammar as chamber ids, including the deliberate
# refusal to fall back to a literal "id" for unusable input. Reusing the helpers
# beats a second copy of the regex that can drift.
from modules.chambers import slugify, valid_id

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
    include_devices: Optional[List[str]] = None,
    order: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Group devices into a frame.

    ``split="chamber"`` — one group per room, cells ordered by device type.
    ``split="type"``    — one group per device type, cells ordered by room.

    Both are the same grouping over two keys; only the key function and the
    labelling differ.

    ``include_chambers`` / ``include_kinds`` filter the frame to what the user
    picked. Empty or None means everything.

    ``include_devices`` is an explicit allow-list of ieees — a hand-built frame.
    It is applied AFTER the other filters and narrows further, so a frame that
    names devices and also filters by chamber shows the intersection, which is
    what the builder's two panels visibly imply.

    ``order`` is a manual arrangement of ieees. Listed devices come first, in
    that order; anything unlisted keeps its natural sort behind them, so a new
    device joining the hive appears rather than vanishing from a saved frame.

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
    if include_devices:
        wanted = set(include_devices)
        cells = [c for c in cells if c["ieee"] in wanted]

    # Manual arrangement: listed ieees first in the given order, everything else
    # keeps its natural sort behind them.
    rank_of = {ieee: i for i, ieee in enumerate(order or [])}
    unranked = len(rank_of)

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
        manual = rank_of.get(cell["ieee"], unranked)
        if split == SPLIT_CHAMBER:
            kind_rank = CELL_ORDER.index(cell["kind"]) if cell["kind"] in CELL_ORDER else len(CELL_ORDER)
            return (manual, kind_rank, cell["name"].lower())
        rank = chamber_rank.get(cell["chamber"], len(chamber_rank) + 1) if cell["chamber"] else len(chamber_rank) + 2
        return (manual, rank, cell["name"].lower())

    out = sorted(groups.values(), key=group_sort)
    for g in out:
        g["cells"].sort(key=cell_sort)

    return {
        "version": SCHEMA_VERSION,
        "split": split,
        "groups": out,
        "total": len(cells),
    }


# ── saved frames ────────────────────────────────────────────────────

MAX_FRAMES = 50
MAX_FRAME_NAME = 64


def clean_frame(raw: Any, existing_ids: Optional[set] = None) -> Optional[dict]:
    """
    Normalise one saved frame definition.

    Shape::

        {id, name, split, chambers[], kinds[], devices[], order[]}

    ``chambers`` / ``kinds`` / ``devices`` are filters — empty means "no filter",
    NOT "nothing". A frame with every list empty is the auto frame, which is a
    perfectly reasonable thing to save under a name.

    Returns None when there is nothing usable (no name/id). Unknown chamber ids
    and ieees are kept rather than validated away: a device can be offline or
    renamed mid-edit, and silently dropping it from a saved frame would be a
    nasty surprise. build_auto_frame simply won't match them.
    """
    if not isinstance(raw, dict):
        return None

    name = str(raw.get("name") or "").strip()[:MAX_FRAME_NAME]
    fid = valid_id(raw.get("id")) or (valid_id(slugify(name)) if name else None)
    if not fid:
        return None
    if existing_ids is not None:
        if fid in existing_ids:
            return None
        existing_ids.add(fid)

    split = raw.get("split")
    if split not in VALID_SPLITS:
        split = SPLIT_CHAMBER

    def _strs(key: str) -> List[str]:
        v = raw.get(key)
        if not isinstance(v, list):
            return []
        out, seen = [], set()
        for item in v:
            s = str(item or "").strip().lower()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "id": fid,
        "name": name or fid,
        "split": split,
        "chambers": _strs("chambers"),
        "kinds": [k for k in _strs("kinds") if k in CELL_ORDER],
        "devices": _strs("devices"),
        "order": _strs("order"),
    }


def clean_frames(raw: Any) -> List[dict]:
    """Normalise a whole frames file. Invalid entries are dropped."""
    if not isinstance(raw, list):
        return []
    seen: set = set()
    out: List[dict] = []
    for entry in raw:
        f = clean_frame(entry, seen)
        if f:
            out.append(f)
        else:
            logger.debug("frames: dropped invalid entry %r", entry)
    return out[:MAX_FRAMES]


def render_saved_frame(
    saved: Dict[str, Any],
    devices: List[Dict[str, Any]],
    chambers: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Build a saved frame's layout. Thin wrapper — a saved frame is just filters."""
    frame = build_auto_frame(
        devices,
        split=saved.get("split", SPLIT_CHAMBER),
        chambers=chambers,
        include_chambers=saved.get("chambers"),
        include_kinds=saved.get("kinds"),
        include_devices=saved.get("devices"),
        order=saved.get("order"),
    )
    frame["id"] = saved.get("id")
    frame["name"] = saved.get("name")
    return frame


def upsert_frame(frames: List[dict], raw: Any) -> Tuple[Optional[dict], Optional[str]]:
    """
    Add or update a frame in ``frames`` (mutated in place).

    Returns ``(frame, None)`` or ``(None, error)``.
    """
    frame = clean_frame(raw)
    if not frame:
        return None, "frame needs a name"

    for i, f in enumerate(frames):
        if f["id"] == frame["id"]:
            frames[i] = frame
            return frame, None

    if len(frames) >= MAX_FRAMES:
        return None, f"too many frames (max {MAX_FRAMES})"

    frames.append(frame)
    return frame, None


def delete_frame(frames: List[dict], frame_id: Any) -> Tuple[bool, Optional[str]]:
    """Remove a frame from ``frames`` (mutated in place)."""
    fid = valid_id(frame_id)
    if not fid:
        return False, "invalid frame id"
    for i, f in enumerate(frames):
        if f["id"] == fid:
            frames.pop(i)
            return True, None
    return False, f"no frame '{fid}'"
