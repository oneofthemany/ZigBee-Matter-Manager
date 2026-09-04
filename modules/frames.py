"""
Frames — dynamically-generated dashboards, laid out from device chamber + type.

Pure module: no I/O, no FastAPI, no global state. Wired in by
routes/frame_routes.py. Cell-kind precedence and the two capability
vocabularies this deliberately does not mix: docs/frames.md.
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

# cell kinds

CELL_CLIMATE = "climate"
CELL_COVER = "cover"
CELL_LIGHT = "light"
CELL_SWITCH = "switch"
CELL_LOCK = "lock"
CELL_SENSOR = "sensor"
CELL_UNKNOWN = "unknown"

#: Most specific control surface first. The first match decides the cell.
#: ``lock`` is declared but unemitted: DeviceCapabilities has no lock detection
#: (locks are Nuki, outside the Zigbee capability path). Kept so the precedence
#: order is the whole story rather than a half-truth.
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

#: Catch-all tab keys. Chambers the floor plan doesn't place, and devices with
#: no chamber at all, are different problems and get different tabs.
TAB_OTHER_KEY = "__other__"
TAB_OTHER_LABEL = "Other"
TAB_UNASSIGNED_KEY = "__unassigned__"

# sensor readouts

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

# control surface

#: ZCL clusters a control surface is built from — the same ids
#: ``modal/control.js`` renders from.
#:
#: ``capability_list`` is a union over the whole device, so it can say "this
#: thing switches" but never "it switches twice": a two-gang socket and a
#: one-gang socket carry an identical list. The per-endpoint cluster lists in
#: ``capabilities`` can tell them apart, and reading the same source the device
#: modal reads is what keeps a cell and the modal offering the same controls.
CLUSTER_ON_OFF = 0x0006
CLUSTER_LEVEL_CONTROL = 0x0008
CLUSTER_WINDOW_COVERING = 0x0102
CLUSTER_THERMOSTAT = 0x0201
CLUSTER_COLOR_CONTROL = 0x0300

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


#: Where a device's "don't show me a tile" flag lives.
#:
#: Namespaced because device_settings is shared with every other subsystem and a
#: bare ``hidden`` would not say hidden from what — the device table, MQTT
#: discovery and Frames are three different answers. Rides on device_settings
#: for the same reasons ``chamber`` does: see docs/frames.md.
HIDDEN_KEY = "frames_hidden"


def is_hidden(device: Dict[str, Any]) -> bool:
    """True when the user has hidden this device from Frames."""
    settings = device.get("settings")
    return bool(isinstance(settings, dict) and settings.get(HIDDEN_KEY))


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


def _cluster_ids(endpoint: Dict[str, Any], *sides: str) -> set:
    ids = set()
    for side in sides:
        for cluster in endpoint.get(side) or []:
            cid = cluster.get("id") if isinstance(cluster, dict) else cluster
            if isinstance(cid, int):
                ids.add(cid)
    return ids


def control_endpoints(device: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """
    One descriptor per controllable endpoint, as ``[{id, type, features}]``.

    Two gangs are two endpoints carrying OnOff, so a cell that renders one row
    per entry offers both switches instead of one. Everything a light can do
    (level, colour temperature, colour) is decided the same way, from the
    clusters actually present on that endpoint.

    Non-control kinds get nothing at all, which is what keeps the "switch cell
    matches ``switch``, never ``on_off``" rule intact: a Philips SML carries the
    OnOff cluster on a controller endpoint, and returning it here would put a
    toggle on a motion sensor.
    """
    if kind in (CELL_SENSOR, CELL_UNKNOWN):
        return []

    caps = _caps(device)
    endpoints = device.get("capabilities")
    if not isinstance(endpoints, list):
        return []

    # An actuator's cluster is a server cluster: OnOff on the *client* side is a
    # remote asking someone else to switch, which is why control_group only ever
    # looks at in_clusters. Reading both sides is the fallback, not the rule, so
    # a device whose only OnOff is a client cluster still renders the toggle it
    # renders today rather than losing its controls to a stricter reading.
    for sides in (("inputs",), ("inputs", "outputs")):
        found = _endpoints_from(endpoints, caps, sides)
        if found:
            return found
    return []


def _endpoints_from(endpoints: List[Any], caps: set, sides: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """One pass of ``control_endpoints`` over the given cluster sides."""
    out: List[Dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("id"), int):
            continue

        clusters = _cluster_ids(endpoint, *sides)
        ep_features: List[str] = []

        # Same precedence as the cell itself: the most specific surface wins, so
        # a cover whose position rides LevelControl is a cover, not a dimmer.
        if CLUSTER_WINDOW_COVERING in clusters:
            ep_type = CELL_COVER
            ep_features = ["open", "close", "stop", "position"]
        elif CLUSTER_THERMOSTAT in clusters:
            ep_type = CELL_CLIMATE
            ep_features = ["setpoint", "mode"]
            if "fan_control" in caps:
                ep_features.append("fan")
        else:
            ep_type = CELL_LIGHT if clusters & {CLUSTER_LEVEL_CONTROL, CLUSTER_COLOR_CONTROL} else CELL_SWITCH
            if CLUSTER_ON_OFF in clusters:
                ep_features.append("toggle")
            if CLUSTER_LEVEL_CONTROL in clusters:
                ep_features.append("brightness")
            if CLUSTER_COLOR_CONTROL in clusters:
                # One cluster, two surfaces — the modal offers a white slider and
                # a colour picker off the same 0x0300, so a cell offers both too.
                ep_features.extend(["color_temp", "color"])

        if ep_features:
            out.append({"id": endpoint["id"], "type": ep_type, "features": ep_features})

    return out


def _capability_features(device: Dict[str, Any], kind: str) -> List[str]:
    """
    Quick actions from ``capability_list`` alone.

    The fallback for a device that ships no endpoint descriptors — a Matter or
    AC entry, or a Zigbee device still mid-interview. Endpoint-blind, so it can
    only ever describe one gang.
    """
    caps = _caps(device)
    out: List[str] = []

    if kind == CELL_LIGHT:
        if "on_off" in caps:
            out.append("toggle")
        if "level_control" in caps:
            out.append("brightness")
        if "color_control" in caps:
            out.extend(["color_temp", "color"])
    elif kind == CELL_SWITCH:
        out.append("toggle")
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


def features(device: Dict[str, Any], kind: str) -> List[str]:
    """
    Quick actions this cell can offer, flattened across its endpoints.

    This is the "quick action based on control availability" rule: a bulb with
    LevelControl gets a brightness slider, one without gets only a toggle.

    The flat list is what decides whether a cell is "active" and what a
    single-endpoint tile renders; ``endpoints`` carries the per-gang detail.
    ``multi_endpoint`` says the two disagree — render per endpoint, not once.
    """
    endpoints = control_endpoints(device, kind)
    if not endpoints:
        return _capability_features(device, kind)

    out: List[str] = []
    for endpoint in endpoints:
        for feature in endpoint["features"]:
            if feature not in out:
                out.append(feature)

    if len(endpoints) > 1:
        out.append("multi_endpoint")

    # A lock has no cluster of its own in this path (see _CONTROL_PRECEDENCE).
    if kind == CELL_LOCK:
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

        {ieee, name, chamber, hidden, kind, features, endpoints, readouts, badges, available}

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
        "hidden": is_hidden(device),
        "kind": kind,
        "features": features(device, kind),
        # Per-gang detail. One entry for an ordinary device, one per gang for a
        # multi-gang socket — the renderer walks this, not `features`.
        "endpoints": control_endpoints(device, kind),
        "readouts": readouts,
        "badges": badges(device),
        "available": bool(device.get("available")),
        "last_seen_ts": device.get("last_seen_ts"),
    }


#: modules/groups.py's own capability vocabulary — NOT the same one `features()`
#: reads (see module docstring). A group's capabilities come from
#: ``DeviceCapability`` (on_off/brightness/color_temp/color_xy/color_hs/position/
#: lock), so group cell features are derived here, independently.
_GROUP_TYPE_TO_KIND = {
    "light": CELL_LIGHT,
    "switch": CELL_SWITCH,
    "cover": CELL_COVER,
    "lock": CELL_LOCK,
}


def group_features(group: Dict[str, Any], kind: str) -> List[str]:
    """Quick actions for a group's own control tile, mirroring ``features()``."""
    caps = set(group.get("capabilities") or [])
    out: List[str] = []

    if kind == CELL_LIGHT:
        if "on_off" in caps:
            out.append("toggle")
        if "brightness" in caps:
            out.append("brightness")
        if "color_temp" in caps:
            out.append("color_temp")
        if caps & {"color_xy", "color_hs"}:
            out.append("color")
    elif kind == CELL_SWITCH:
        out.append("toggle")
    elif kind == CELL_COVER:
        out.extend(["open", "close", "stop"])
        if "position" in caps:
            out.append("position")
    # No CELL_LOCK branch: GroupManager.control_group has no lock/unlock
    # command handler, so a lock-type group renders with no quick actions
    # rather than offering a button that would silently do nothing.

    return out


def resolve_group_cell(group: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build one cell descriptor for a Zigbee group's own control tile.

    A group assigned a chamber (``group["chamber"]``) appears as a single
    controllable cell in that chamber's Frames section, alongside its member
    devices' own cells — the same "one thing, one control surface" idea as
    ``resolve_cell``, just applied to the group as a whole rather than to any
    one device. Quick actions act on every member at once, via
    ``GroupManager.control_group`` (``POST /api/groups/{id}/control``), not the
    single-device command path.

    ``is_group`` distinguishes this from a device cell — ``ieee`` is None,
    there's nothing to look up in ``state.deviceCache`` by that key. ``members``
    ships the ieees so the frontend can derive an on/off/brightness readout by
    looking at what the group's own devices are currently doing.
    """
    kind = _GROUP_TYPE_TO_KIND.get(group.get("type"), CELL_UNKNOWN)
    return {
        "group_id": group.get("id"),
        "ieee": None,
        "is_group": True,
        "name": group.get("name") or f"Group {group.get('id')}",
        "chamber": group.get("chamber"),
        # A group tile is the thing hiding its members exists to leave behind,
        # so it is never itself hidden — unassign its chamber to drop it.
        "hidden": False,
        "kind": kind,
        "features": group_features(group, kind),
        "readouts": [],
        "badges": [],
        "available": True,
        "last_seen_ts": None,
        "members": list(group.get("members") or []),
    }


def is_zigbee(device: Dict[str, Any]) -> bool:
    """Frames is Zigbee-only for now; AC/media/heating adapters come later."""
    protocol = device.get("protocol")
    return not protocol or protocol == "zigbee"


# layout

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
    levels: Optional[List[dict]] = None,
    tabs: Optional[List[dict]] = None,
    device_groups: Optional[List[dict]] = None,
    include_hidden: bool = False,
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

    ``device_groups`` (Zigbee groups, each carrying its own ``chamber``) are
    used only for split="chamber": a group assigned a chamber gets its own
    controllable cell in that chamber's section, via ``resolve_group_cell``.
    Groups without a chamber don't appear in Frames at all.

    ``include_hidden`` returns hidden devices anyway, still flagged. Only the
    visibility editor asks for that — it is the one place you need to see a
    device in order to unhide it.
    """
    if split not in VALID_SPLITS:
        split = SPLIT_CHAMBER

    chambers = chambers or []
    chamber_names = {c["id"]: c["name"] for c in chambers}
    chamber_rank = {c["id"]: i for i, c in enumerate(chambers)}

    cells = [resolve_cell(d) for d in devices if is_zigbee(d)]

    # A group assigned a chamber becomes its own controllable cell in that
    # chamber's section — visible without having to name a single device.
    # Only meaningful for the chamber split: a group tile has no natural home
    # in a device-type section.
    if split == SPLIT_CHAMBER:
        cells.extend(resolve_group_cell(g) for g in (device_groups or []) if g.get("chamber"))

    # Before every other filter: a hidden device is one the user has said they
    # never want to see, not one this particular frame happens to exclude.
    if not include_hidden:
        cells = [c for c in cells if not c["hidden"]]

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

    # The user's own tabs win; otherwise fall back to floor-derived tabs, which
    # only apply to a chamber split (a type split has no levels to group by).
    if tabs:
        frame_tabs = apply_saved_tabs(out, tabs)
    elif split == SPLIT_CHAMBER:
        frame_tabs = auto_tabs(out, chambers, levels)
    else:
        frame_tabs = []

    return {
        "version": SCHEMA_VERSION,
        "split": split,
        "groups": out,
        "tabs": frame_tabs,
        "total": len(cells),
    }


# tabs

MAX_TABS = 12


def auto_tabs(
    groups: List[dict],
    chambers: Optional[List[dict]] = None,
    levels: Optional[List[dict]] = None,
) -> List[dict]:
    """
    Derive tabs from the floor a chamber is on — free, no configuration.

    Only meaningful for a chamber split: a type split has no levels to group by.

    Returns ``[]`` unless this produces at least two tabs AND at least one real
    level tab. A single tab is a tab bar that does nothing, and [Other] +
    [Unassigned] alone (no floor plan drawn) is noise pretending to be structure.
    """
    level_of = {c["id"]: c.get("level") for c in (chambers or [])}
    level_names = {l["id"]: l["name"] for l in (levels or [])}
    level_order = [l["id"] for l in (levels or [])]

    buckets: Dict[str, List[str]] = {}
    for g in groups:
        key = g["key"]
        if key == UNASSIGNED_KEY:
            bucket = TAB_UNASSIGNED_KEY
        else:
            bucket = level_of.get(key) or TAB_OTHER_KEY
        buckets.setdefault(bucket, []).append(key)

    out: List[dict] = []
    for lid in level_order:
        if lid in buckets:
            out.append({"id": lid, "name": level_names.get(lid, lid), "groups": buckets[lid]})
    real_level_tabs = len(out)

    if TAB_OTHER_KEY in buckets:
        out.append({"id": TAB_OTHER_KEY, "name": TAB_OTHER_LABEL, "groups": buckets[TAB_OTHER_KEY]})
    if TAB_UNASSIGNED_KEY in buckets:
        out.append({"id": TAB_UNASSIGNED_KEY, "name": UNASSIGNED_LABEL, "groups": buckets[TAB_UNASSIGNED_KEY]})

    if real_level_tabs == 0 or len(out) < 2:
        return []
    return out


def apply_saved_tabs(groups: List[dict], saved: List[dict]) -> List[dict]:
    """
    Lay groups out across the user's own tabs.

    Any group not named by a tab lands in the FIRST tab rather than vanishing —
    a chamber you add next month must show up somewhere without you having to
    remember to edit every frame. Tabs that end up empty are dropped.
    """
    present = {g["key"] for g in groups}
    claimed: set = set()
    out: List[dict] = []

    for t in saved:
        keys = [k for k in t.get("groups", []) if k in present and k not in claimed]
        claimed.update(keys)
        out.append({"id": t["id"], "name": t["name"], "groups": keys})

    if not out:
        return []

    # Appended, not prepended: a chamber added months later must show up, but it
    # shouldn't shove itself to the top of an arrangement the user built by hand.
    leftover = [g["key"] for g in groups if g["key"] not in claimed]
    out[0]["groups"] = out[0]["groups"] + leftover

    out = [t for t in out if t["groups"]]
    return out if len(out) > 1 else []


# saved frames

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
        "tabs": _clean_tabs(raw.get("tabs")),
    }


def _clean_tabs(raw: Any) -> List[dict]:
    """
    Normalise a frame's tabs. Empty means "no tabs" — fall back to auto.

    A tab with no name is dropped; a tab with no groups is kept, because the
    builder legitimately creates an empty tab a moment before you fill it.
    """
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    seen: set = set()
    for t in raw[:MAX_TABS]:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()[:MAX_FRAME_NAME]
        tid = valid_id(t.get("id")) or (valid_id(slugify(name)) if name else None)
        if not tid or tid in seen:
            continue
        seen.add(tid)
        groups = t.get("groups")
        out.append({
            "id": tid,
            "name": name or tid,
            "groups": [str(g).strip().lower() for g in groups if str(g or "").strip()]
                      if isinstance(groups, list) else [],
        })
    return out


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
    levels: Optional[List[dict]] = None,
    device_groups: Optional[List[dict]] = None,
    include_hidden: bool = False,
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
        levels=levels,
        tabs=saved.get("tabs"),
        device_groups=device_groups,
        include_hidden=include_hidden,
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
