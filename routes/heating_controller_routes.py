"""
Heating Controller API — tick state and forcing, dry-run toggle, circuits config,
device and sensor candidate lists, and per-TRV settings, calibration and
config re-apply. See docs/heating-controller.md.
"""
import copy
import logging
import os
import yaml
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI

from modules.floor_plan import (
    apply_circuits_to_plan,
    clean_floor_plan,
    geometry_change_notes,
    project_floor_plan_to_circuits,
)
from modules.floor_plan_store import load_plan, save_plan
from modules.heating_controller import circuits_for_mode

logger = logging.getLogger("routes.heating_controller")

CONFIG_PATH = "./config/config.yaml"

VALID_EXT_MODES = ("off", "advisory", "push")

VALID_CONFIG_MODES = ("floor_plan", "manual")

# Per-valve settings the controller and its UI know how to offer.
TRV_SETTINGS = ("window_detection", "child_lock", "valve_detection",
                "motor_calibration", "external_temp", "sensor_type")


VALID_FLOOR_TYPES = (
    "solid",
    "suspended",
    "carpet_over_concrete",
    "tile_over_concrete",
    "wooden",
    "carpet_over_wooden",
    "unknown",
)
VALID_GLAZING = ("single", "double", "triple")
VALID_ORIENTATIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW", "unknown")
VALID_DOOR_TYPES = ("external", "internal")
VALID_CEILING_TYPES = ("insulated", "uninsulated", "flat_roof", "unknown")
VALID_WALLS = ("front", "back", "left", "right")
VALID_RADIATOR_TYPES = ("single_panel", "double_panel_single_conv",
                        "double_panel_double_conv", "triple_panel",
                        "column", "towel_rail", "underfloor", "unknown")

VALID_SENSOR_KINDS = ("temp_sensor", "thermostat", "room_stat")
VALID_RADIATOR_PLACEMENT = ("under_window", "external_wall",
                            "internal_wall", "unknown")


def _clean_window(w: dict) -> Optional[dict]:
    if not isinstance(w, dict):
        return None
    area = _as_float(w.get("area_m2"))
    if area is None or area <= 0:
        return None
    glazing = str(w.get("glazing", "double")).lower()
    if glazing not in VALID_GLAZING:
        glazing = "double"
    orient = str(w.get("orientation", "unknown")).upper()
    if orient.lower() == "unknown":
        orient = "unknown"
    elif orient not in VALID_ORIENTATIONS:
        orient = "unknown"
    return {"area_m2": round(area, 2), "glazing": glazing, "orientation": orient}

def _clean_radiator_dict(rd: Any) -> Optional[dict]:
    """
    Normalise one radiator dict. Accepts both manual-config shape (with
    legacy `wall` slot) and floor-plan-projected shape (with `wall_id`,
    `offset_m`, free `x`/`y`, `length_m`, `trv_ieee`). Returns ``None`` if
    no positive wattage can be derived.
    """
    if not isinstance(rd, dict):
        return None
    watts = _as_float(rd.get("watts_at_dt50"))
    btu_hr = _as_float(rd.get("btu_hr_at_dt50"))
    if not watts and btu_hr and btu_hr > 0:
        watts = btu_hr * 0.2931
    if not watts or watts <= 0:
        return None

    cleaned: Dict[str, Any] = {"watts_at_dt50": round(watts, 0)}

    flow_c = _as_float(rd.get("flow_temperature_c"))
    if flow_c and 30 <= flow_c <= 90:
        cleaned["flow_temperature_c"] = round(flow_c, 1)

    desc = rd.get("description")
    if desc:
        cleaned["description"] = str(desc)[:100]

    wall = str(rd.get("wall") or "").lower()
    if wall in VALID_WALLS:
        cleaned["wall"] = wall

    placement = str(rd.get("placement") or "").lower()
    if placement in VALID_RADIATOR_PLACEMENT:
        cleaned["placement"] = placement

    has_reflector = _as_bool(rd.get("reflective_panel"), None)
    if has_reflector is not None:
        cleaned["reflective_panel"] = has_reflector

    rtype = str(rd.get("type") or "").lower()
    if rtype in VALID_RADIATOR_TYPES:
        cleaned["type"] = rtype

    # Floor-plan-only fields (preserved verbatim for the editor + future
    # thermal_profile use; legacy reader code paths simply ignore them).
    for k in ("id", "room_id", "wall_id", "offset_m",
              "x", "y", "length_m", "height_m", "trv_ieee"):
        v = rd.get(k)
        if v is not None:
            cleaned[k] = v

    return cleaned


def _clean_sensor_dict(s: Any) -> Optional[dict]:
    """
    Normalise one temperature-sensor dict. Returns ``None`` if no IEEE.
    """
    if not isinstance(s, dict):
        return None
    ieee = str(s.get("ieee") or "").strip()
    if not ieee:
        return None

    out: Dict[str, Any] = {"ieee": ieee}

    kind = str(s.get("kind") or "temp_sensor").lower()
    out["kind"] = kind if kind in VALID_SENSOR_KINDS else "temp_sensor"

    primary = _as_bool(s.get("primary"), None)
    if primary is not None:
        out["primary"] = primary

    h = _as_float(s.get("height_m"))
    if h is not None and 0 <= h <= 10:
        out["height_m"] = round(h, 2)

    # Floor-plan coords (optional)
    for k in ("id", "room_id", "x", "y"):
        v = s.get(k)
        if v is not None:
            out[k] = v

    return out

def _clean_door(d: dict) -> Optional[dict]:
    if not isinstance(d, dict):
        return None
    area = _as_float(d.get("area_m2"))
    if area is None or area <= 0:
        return None
    typ = str(d.get("type", "internal")).lower()
    if typ not in VALID_DOOR_TYPES:
        typ = "internal"
    return {"area_m2": round(area, 2), "type": typ}


def _clean_dimensions(d: dict) -> Optional[dict]:
    """
    New schema: X (width) × Y (depth) × height derives per-wall areas.

    Walls are named by orientation from the room's main viewpoint:
      front / back = X-axis walls  (the wider walls if X > Y)
      left / right = Y-axis walls
    Wall insulation overrides are allowed per-wall (the house may have one
    external wall renovated, or a party wall on just one side).

    Each window / door / radiator carries a `wall` field identifying which
    of the four walls it sits on. The thermal profile calculation uses this
    to compute each wall's *net* area (gross minus openings).
    """
    if not isinstance(d, dict):
        return None

    x_m = _as_float(d.get("width_m") or d.get("x_m"))
    y_m = _as_float(d.get("depth_m") or d.get("y_m"))
    ceiling_h = _as_float(d.get("ceiling_height_m"), 2.4) or 2.4

    # The plan's polygon area beats the bounding box for any non-rectangular room.
    floor_area = _as_float(d.get("floor_area_m2"))
    if not floor_area or floor_area <= 0:
        floor_area = round(x_m * y_m, 2) if x_m and y_m and x_m > 0 and y_m > 0 else None
    else:
        floor_area = round(floor_area, 2)

    # Per-wall types: each wall is external | party | internal | unknown.
    # Default all-external on a detached; user overrides per-wall as needed.
    walls_in = d.get("walls") if isinstance(d.get("walls"), dict) else {}
    walls_out = {}
    for w_name in VALID_WALLS:
        w_def = walls_in.get(w_name) or {}
        if not isinstance(w_def, dict):
            w_def = {}
        kind = str(w_def.get("type", "external")).lower()
        if kind not in ("external", "party", "internal", "unknown"):
            kind = "external"
        walls_out[w_name] = {"type": kind}

    # Helper to validate wall attribution
    def _valid_wall(v):
        s = str(v or "").lower()
        return s if s in VALID_WALLS else None

    # Windows
    raw_windows = d.get("windows") if isinstance(d.get("windows"), list) else []
    windows = []
    for w in raw_windows:
        if not isinstance(w, dict):
            continue
        area = _as_float(w.get("area_m2"))
        if not area or area <= 0:
            continue
        glazing = str(w.get("glazing", "double")).lower()
        if glazing not in VALID_GLAZING:
            glazing = "double"
        orient = str(w.get("orientation", "unknown")).upper()
        if orient.lower() == "unknown" or orient not in VALID_ORIENTATIONS:
            orient = "unknown"
        wall = _valid_wall(w.get("wall"))
        entry = {
            "area_m2": round(area, 2),
            "glazing": glazing,
            "orientation": orient,
            "wall": wall,
        }
        facing = _as_float(w.get("facing_deg"))
        if facing is not None:
            entry["facing_deg"] = round(facing % 360.0, 1)
        windows.append(entry)

    # Doors
    raw_doors = d.get("doors") if isinstance(d.get("doors"), list) else []
    doors = []
    for dr in raw_doors:
        if not isinstance(dr, dict):
            continue
        area = _as_float(dr.get("area_m2"))
        if not area or area <= 0:
            continue
        typ = str(dr.get("type", "internal")).lower()
        if typ not in VALID_DOOR_TYPES:
            typ = "internal"
        wall = _valid_wall(dr.get("wall"))
        doors.append({
            "area_m2": round(area, 2),
            "type": typ,
            "wall": wall,
        })

    floor_type = str(d.get("floor_type", "unknown")).lower()
    if floor_type not in VALID_FLOOR_TYPES:
        floor_type = "unknown"
    ceiling_type = str(d.get("ceiling_type", "unknown")).lower()
    if ceiling_type not in VALID_CEILING_TYPES:
        ceiling_type = "unknown"

    has_content = bool(floor_area or x_m or y_m or windows or doors
                       or any(w["type"] != "external" for w in walls_out.values()))
    if not has_content:
        return None

    out = {
        "width_m": round(x_m, 2) if x_m else None,
        "depth_m": round(y_m, 2) if y_m else None,
        "ceiling_height_m": round(max(1.5, min(5.0, ceiling_h)), 2),
        "floor_area_m2": floor_area,
        "walls": walls_out,
        "windows": windows,
        "doors": doors,
        "floor_type": floor_type,
        "ceiling_type": ceiling_type,
    }
    return out


# YAML helpers
def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _as_float(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_bool(v, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "enable", "enabled", "lock"):
            return True
        if s in ("0", "false", "no", "off", "disable", "disabled", "unlock"):
            return False
    return default


def _slugify(s: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return slug or "item"


def _clean_trvs(room_in: dict) -> List[dict]:
    """
    Normalise a room's TRV list. Accept either:
      trvs: [{ieee, window_detection?, child_lock?, valve_detection?}, ...]
      trv_ieees: ["aa:bb:...", ...]   (legacy)
    Dict form wins on conflict.
    """
    by_ieee: Dict[str, dict] = {}

    legacy = room_in.get("trv_ieees") or []
    if isinstance(legacy, list):
        for ieee in legacy:
            if not ieee:
                continue
            ieee_s = str(ieee).strip()
            if ieee_s:
                by_ieee[ieee_s] = {"ieee": ieee_s}

    new = room_in.get("trvs") or []
    if isinstance(new, list):
        for t in new:
            if isinstance(t, str):
                ieee_s = t.strip()
                if ieee_s:
                    by_ieee.setdefault(ieee_s, {"ieee": ieee_s})
            elif isinstance(t, dict):
                ieee_s = str(t.get("ieee") or "").strip()
                if not ieee_s:
                    continue
                entry: Dict[str, Any] = {"ieee": ieee_s}
                for k in ("window_detection", "child_lock", "valve_detection"):
                    if k in t:
                        b = _as_bool(t.get(k), None)
                        if b is not None:
                            entry[k] = b
                min_sp = _as_float(t.get("min_setpoint"))
                if min_sp is not None and 4.0 <= min_sp <= 30.0:
                    entry["min_setpoint"] = round(min_sp, 1)
                by_ieee[ieee_s] = entry

    return list(by_ieee.values())


def _clean_room(r: dict, existing_ids: Optional[set] = None) -> Optional[dict]:
    if not isinstance(r, dict) or not r.get("name"):
        return None
    existing_ids = existing_ids or set()
    rid = str(r.get("id") or _slugify(r["name"]))
    base = rid; n = 2
    while rid in existing_ids:
        rid = f"{base}_{n}"; n += 1

    trvs = _clean_trvs(r)

    schedule = r.get("schedule") or []
    if not isinstance(schedule, list):
        schedule = []
    clean_sched = []
    for slot in schedule:
        if not isinstance(slot, dict):
            continue
        days = slot.get("days") or []
        if not isinstance(days, list):
            days = []
        clean_sched.append({
            "days": [d for d in days if d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
            "start": str(slot.get("start", "07:00")),
            "end": str(slot.get("end", "22:00")),
            "temp": _as_float(slot.get("temp"), 20.0),
        })

    # Temperature sensors (canonical: plural; derive legacy single)
    sensors_clean: List[dict] = []
    if isinstance(r.get("temperature_sensors"), list):
        for s in r["temperature_sensors"]:
            c = _clean_sensor_dict(s)
            if c:
                sensors_clean.append(c)
    elif "temperature_sensor_ieee" in r:
        legacy = r.get("temperature_sensor_ieee")
        if isinstance(legacy, str):
            legacy = legacy.strip()
            if legacy:
                sensors_clean = [{"ieee": legacy, "kind": "temp_sensor", "primary": True}]
                
    # Ensure exactly one primary (first listed if none flagged)
    if sensors_clean and not any(s.get("primary") for s in sensors_clean):
        sensors_clean[0]["primary"] = True

    primary_sensor = next((s for s in sensors_clean if s.get("primary")), None)
    if primary_sensor is None and sensors_clean:
        primary_sensor = sensors_clean[0]
    sensor_ieee = primary_sensor["ieee"] if primary_sensor else None

    mode = str(r.get("external_temp_mode", "advisory" if sensor_ieee else "off")).lower()
    if mode not in VALID_EXT_MODES:
        mode = "advisory" if sensor_ieee else "off"
    if not sensor_ieee and mode == "push":
        mode = "off"

    push_interval_raw = _as_float(r.get("external_temp_push_interval_sec"), 300.0)
    push_interval = int(push_interval_raw) if push_interval_raw else 300

    # Per-room data freshness threshold (minutes). Clamped to a sensible
    # range so a typo can't disable the health check or cause a constant
    # alert flood. Stored only when the user has set it, so the controller
    # falls back to its module default for unset rooms.
    freshness_raw = _as_float(r.get("freshness_threshold_minutes"))
    freshness_min: Optional[int] = None
    if freshness_raw is not None and freshness_raw > 0:
        freshness_min = max(3, min(120, int(freshness_raw)))

    dimensions = _clean_dimensions(r.get("dimensions"))

    # Radiators (canonical: plural list; derive legacy single)
    # The plural list carries plan ids and positions; the legacy single is
    # derived from it, so it is only the source when no list exists.
    radiators_clean: List[dict] = []
    if isinstance(r.get("radiators"), list):
        for rd in r["radiators"]:
            c = _clean_radiator_dict(rd)
            if c:
                radiators_clean.append(c)
    elif "radiator" in r:
        single = _clean_radiator_dict(r.get("radiator"))
        if single:
            radiators_clean = [single]

    out = {
        "id": rid,
        "name": str(r["name"]),
        "target_temp": _as_float(r.get("target_temp"), 21.0),
        "night_setback": _as_float(r.get("night_setback"), 17.0),
        "min_temp": _as_float(r.get("min_temp"), 16.0),
        "temperature_sensor_ieee": sensor_ieee,
        "external_temp_mode": mode,
        "external_temp_push_interval_sec": push_interval,
        "trvs": trvs,
        "schedule": clean_sched,
        "contact_sensors": [
            {
                "ieee": str(cs.get("ieee") or "").strip(),
                "name": str(cs.get("name") or cs.get("ieee") or ""),
                "debounce_open_seconds": int(cs.get("debounce_open_seconds", 30) or 30),
                "require_temp_drop_c": float(cs.get("require_temp_drop_c", 0.5) or 0.5),
                "max_close_minutes": int(cs.get("max_close_minutes", 60) or 60),
                "enabled": bool(cs.get("enabled", True)),
            }
            for cs in (r.get("contact_sensors") or [])
            if isinstance(cs, dict) and (cs.get("ieee") or "").strip()
        ],
    }
    if freshness_min is not None:
        out["freshness_threshold_minutes"] = freshness_min
    if dimensions is not None:
        out["dimensions"] = dimensions

    # Emit plural list (canonical) AND derive legacy single from the
    # largest-watts radiator for the benefit of existing readers in
    # heating_controller.py, radiator_sizing.py, heating_routes.py, etc.
    if radiators_clean:
        out["radiators"] = radiators_clean
        primary_rad = max(
            radiators_clean,
            key=lambda x: float(x.get("watts_at_dt50") or 0),
        )
        # Strip plural-only fields when emitting the legacy single, so the
        # `radiator` block keeps its historical shape.
        legacy_only_keys = {
            "watts_at_dt50", "flow_temperature_c", "description",
            "wall", "placement", "reflective_panel", "type",
        }
        out["radiator"] = {k: v for k, v in primary_rad.items()
                           if k in legacy_only_keys}

    # Emit the plural sensor list too. Legacy single is already on `out`
    # via the `temperature_sensor_ieee` field below — no extra work here.
    if sensors_clean:
        out["temperature_sensors"] = sensors_clean

    # Per-room out-of-hours override (optional — falls back to global when absent)
    room_ooh = str(r.get("out_of_hours_action") or "").lower()
    if room_ooh in ("setback", "min_only", "off"):
        out["out_of_hours_action"] = room_ooh
    room_ooh_offset = r.get("night_setback_offset_c")
    if room_ooh_offset is not None:
        try:
            v = float(room_ooh_offset)
            if -10.0 <= v <= 0.0:
                out["night_setback_offset_c"] = round(v, 1)
        except (TypeError, ValueError):
            pass


    # Preserve floor_plan_ref so projected rooms keep their plan linkage
    # across save round-trips.
    fp_ref = r.get("floor_plan_ref")
    if isinstance(fp_ref, dict):
        out["floor_plan_ref"] = {
            "level_id": str(fp_ref.get("level_id") or ""),
            "room_id":  str(fp_ref.get("room_id")  or ""),
        }

    return out


def _clean_circuit(c: dict, existing_ids: Optional[set] = None) -> Optional[dict]:
    if not isinstance(c, dict) or not c.get("name"):
        return None
    existing_ids = existing_ids or set()
    cid = str(c.get("id") or _slugify(c["name"]))
    base = cid; n = 2
    while cid in existing_ids:
        cid = f"{base}_{n}"; n += 1

    room_ids: set = set()
    rooms = []
    for r in (c.get("rooms") or []):
        cleaned = _clean_room(r, room_ids)
        if cleaned:
            rooms.append(cleaned)
            room_ids.add(cleaned["id"])

    receiver_command = str(c.get("receiver_command", "thermostat")).lower()
    if receiver_command not in ("switch", "thermostat"):
        receiver_command = "thermostat"

    out = {
        "id": cid,
        "name": str(c["name"]),
        "receiver_ieee": (str(c.get("receiver_ieee")).strip() if c.get("receiver_ieee") else None) or None,
        "receiver_command": receiver_command,
        "receiver_endpoint": c.get("receiver_endpoint"),
        "rooms": rooms,
    }
    # Per-circuit overrides; absent means "follow the controller-wide setting".
    for k in ("operating_hours", "weather_suppression"):
        b = _as_bool(c.get(k), None)
        if b is not None:
            out[k] = b
    for k in ("receiver_call_setpoint", "receiver_idle_setpoint"):
        v = _as_float(c.get(k))
        if v is not None and 4.0 <= v <= 90.0:
            out[k] = round(v, 1)
    return out


def _clean_circuits(circuits: list) -> List[dict]:
    if not isinstance(circuits, list):
        return []
    out = []
    seen = set()
    for c in circuits:
        cleaned = _clean_circuit(c, seen)
        if cleaned:
            out.append(cleaned)
            seen.add(cleaned["id"])
    return out

def normalise_circuits(circuits: list) -> List[dict]:
    """Canonicalise a circuits list — same rules as POST /controller/config."""
    return _clean_circuits(circuits)


_MERGE_SETTINGS = ("target_temp", "night_setback", "min_temp", "schedule")


def _merge_inactive_devices(base: List[dict], inactive: List[dict],
                            note_settings: bool = True) -> Tuple[List[dict], List[str]]:
    """Union of TRVs, contacts and a missing primary sensor from `inactive`
    into the same room of `base`; settings stay `base`'s, and each one that
    differed is named so the user can choose."""
    base = copy.deepcopy(base)
    notes: List[str] = []
    other = {r.get("id"): r for c in inactive for r in (c.get("rooms") or [])}
    for c in base:
        for r in c.get("rooms") or []:
            o = other.get(r.get("id"))
            if not o:
                continue
            have = {t["ieee"] for t in r.get("trvs") or []}
            for t in o.get("trvs") or []:
                if t["ieee"] not in have:
                    r.setdefault("trvs", []).append(t)
                    notes.append(f"'{r['name']}': kept TRV {t['ieee']} from the other config copy")
            have = {x["ieee"] for x in r.get("contact_sensors") or []}
            for x in o.get("contact_sensors") or []:
                if x["ieee"] not in have:
                    r.setdefault("contact_sensors", []).append(x)
                    notes.append(f"'{r['name']}': kept contact {x['ieee']} from the other config copy")
            if not r.get("temperature_sensor_ieee") and o.get("temperature_sensor_ieee"):
                r["temperature_sensor_ieee"] = o["temperature_sensor_ieee"]
                r["temperature_sensors"] = o.get("temperature_sensors") or []
                notes.append(f"'{r['name']}': kept sensor {o['temperature_sensor_ieee']} "
                             f"from the other config copy")
            for k in _MERGE_SETTINGS if note_settings else ():
                if o.get(k) is not None and r.get(k) != o.get(k):
                    notes.append(f"'{r['name']}': {k} was {o.get(k)} in the other config "
                                 f"copy; kept {r.get(k)}")
    return base, notes


def sync_heating_config(heating: Dict[str, Any], plan: Optional[dict],
                        edited: Optional[list] = None) -> Tuple[Optional[dict], List[str]]:
    """
    Bring heating.circuits, heating.controller.circuits and the floor plan
    into one state, so either config_mode reads the same rooms
    (docs/heating-controller.md § Config modes).

    `edited` is a circuits list the user just saved; it is written into the
    plan first. Without it the current mode's circuits are the base. Mutates
    `heating`; returns the plan to persist (None when unchanged or absent)
    and notes for the user.
    """
    controller = heating.setdefault("controller", {})
    base = _clean_circuits(edited if edited is not None else circuits_for_mode(heating))
    notes: List[str] = []
    plan_out = None

    # Configs from before the sync kept two diverging copies, and the plan held
    # devices neither copy had. Once, take every device any of the three knows;
    # settings come from the user's edit, else from the plan. Afterwards the
    # copies are equal and this is a no-op.
    manual_list = heating.get("circuits") or []
    plan_list = controller.get("circuits") or []
    if manual_list and plan_list and _clean_circuits(manual_list) != _clean_circuits(plan_list):
        floor_mode = (controller.get("config_mode") or "manual") == "floor_plan"
        inactive = _clean_circuits(manual_list if floor_mode else plan_list)
        fresh = _clean_circuits(project_floor_plan_to_circuits(plan, base)[0]) if plan else base
        merged = base if edited is not None else fresh
        merged, _ = _merge_inactive_devices(merged, fresh, note_settings=False)
        merged, merge_notes = _merge_inactive_devices(merged, inactive)
        notes.extend(merge_notes)
        base = edited = merged
    if plan:
        source = plan
        if edited is not None:
            applied, apply_notes = apply_circuits_to_plan(plan, base)
            notes.extend(apply_notes)
            source = clean_floor_plan(applied) or plan
            if source != plan:
                plan_out = source
        projected, proj_notes = project_floor_plan_to_circuits(source, base)
        notes.extend(proj_notes)
        circuits = _clean_circuits(projected)
        notes.extend(geometry_change_notes(base, circuits))
    else:
        circuits = base
    heating["circuits"] = circuits
    controller["circuits"] = copy.deepcopy(circuits)
    return plan_out, notes


def _find_trv_in_config(cfg: Dict[str, Any], ieee: str):
    """Locate a TRV in the active mode's circuits. Returns (circuit, room, trv_dict) or None."""
    heating = cfg.get("heating") or {}
    for c in circuits_for_mode(heating):
        for r in c.get("rooms") or []:
            # Check new-style list
            for t in (r.get("trvs") or []):
                if isinstance(t, dict) and t.get("ieee") == ieee:
                    return c, r, t
            # Fall back to legacy list — create a dict inline
            legacy = r.get("trv_ieees") or []
            if ieee in legacy:
                # Promote to new-style entry
                trvs_list = r.setdefault("trvs", [])
                # Ensure not double-listed
                for tt in trvs_list:
                    if isinstance(tt, dict) and tt.get("ieee") == ieee:
                        return c, r, tt
                new_entry = {"ieee": ieee}
                trvs_list.append(new_entry)
                return c, r, new_entry
    return None


# ═══════════════════════════════════════════════════════════════════
def register_heating_controller_routes(app: FastAPI, get_controller, get_zigbee_service=None):

    def _resolve():
        c = get_controller()
        if callable(c):
            try:
                c = c()
            except Exception:
                pass
        return c

    def _devices() -> Dict[str, Any]:
        if not get_zigbee_service:
            return {}
        try:
            zs = get_zigbee_service()
            if zs and hasattr(zs, "devices"):
                return zs.devices or {}
        except Exception as e:
            logger.debug(f"zigbee_service access failed: {e}")
        return {}

    @app.get("/api/heating/controller/state")
    async def controller_state():
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        return {"success": True, "state": ctrl.get_state()}

    @app.get("/api/heating/controller/managed")
    async def managed_ieees():
        """
        Return { enabled, ieees[] } — IEEEs currently managed by the heating
        controller (receivers + TRVs). Used by the device modal to disable
        direct heating controls when the controller is running.
        """
        ctrl = _resolve()
        if not ctrl:
            return {"success": True, "enabled": False, "ieees": []}
        managed = set()
        for c in ctrl.circuits:
            rx = c.get("receiver_ieee")
            if rx:
                managed.add(str(rx))
            for r in c.get("rooms", []) or []:
                for t in r.get("trvs", []) or []:
                    if isinstance(t, dict) and t.get("ieee"):
                        managed.add(str(t["ieee"]))
                for legacy in r.get("trv_ieees", []) or []:
                    if legacy:
                        managed.add(str(legacy))
        return {
            "success": True,
            "enabled": bool(getattr(ctrl, "enabled", False)),
            "ieees": sorted(managed),
        }

    @app.post("/api/heating/controller/tick")
    async def controller_tick():
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        if not ctrl.enabled:
            return {"success": False, "error": "Controller not enabled"}
        try:
            state = await ctrl.force_tick()
            return {"success": True, "state": state}
        except Exception as e:
            logger.error(f"Force tick failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/dry-run")
    async def toggle_dry_run(data: dict):
        """Persisted, so a restart keeps it; the live controller crosses the
        dry-run boundary through set_dry_run (stand-down, cache reset)."""
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        dry_run = bool(data.get("dry_run", True))
        cfg = _load_config()
        cfg.setdefault("heating", {}).setdefault("controller", {})["dry_run"] = dry_run
        _save_config(cfg)
        await ctrl.set_dry_run(dry_run, reason="api")
        return {"success": True, "dry_run": ctrl.dry_run}

    @app.post("/api/heating/controller/config-mode")
    async def set_controller_config_mode(data: dict):
        """
        Set which editor owns the heating config.

        Body: {"mode": "floor_plan" | "manual"}

        Both modes read the same circuits (sync_heating_config keeps them and
        the plan in one state), so switching copies and strips nothing. In
        floor_plan mode the plan editor owns device bindings; in manual mode the
        controller modal does, and its saves are written back into the plan.
        """
        mode = data.get("mode")
        if mode not in VALID_CONFIG_MODES:
            return {
                "success": False,
                "error": f"mode must be one of {list(VALID_CONFIG_MODES)}",
            }
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            controller_block = heating.setdefault("controller", {})
            prev_mode = controller_block.get("config_mode")

            # The mode being left holds the latest edits. Leaving manual, they
            # are written into the plan; leaving floor_plan, the plan already
            # has them.
            plan = load_plan()
            edited = heating.get("circuits") if (prev_mode or "manual") == "manual" else None
            plan_out, warnings = sync_heating_config(heating, plan, edited=edited or None)
            controller_block["config_mode"] = mode
            if plan_out is not None:
                save_plan(plan_out)
            _save_config(cfg)

            ctrl = _resolve()
            if ctrl is not None and hasattr(ctrl, "apply_config"):
                try:
                    await ctrl.apply_config(heating, reason="config-mode")
                except Exception as e:
                    logger.warning(f"controller hot-apply on mode switch failed: {e}")
                    warnings.append(f"controller hot-apply failed: {e}")

            logger.info(f"Heating controller config_mode set to {mode!r} (was {prev_mode!r})")
            return {
                "success": True,
                "mode": mode,
                "previous_mode": prev_mode,
                "stripped_floor_plan_refs": 0,
                "reprojected": bool(plan),
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"Failed to set config_mode: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/controller/config")
    async def get_controller_config():
        try:
            cfg = _load_config()
            heating = cfg.get("heating") or {}
            controller_block = heating.get("controller") or {}
            wx_block = controller_block.get("weather_suppression") or {}
            oh_block = controller_block.get("operating_hours") or {}
            return {
                "success": True,
                "config": {
                    "enabled": bool(controller_block.get("enabled", False)),
                    "dry_run": bool(controller_block.get("dry_run", False)),
                    "config_mode": controller_block.get("config_mode") or None,
                    "weather_suppression": {
                        "enabled": bool(wx_block.get("enabled", False)),
                        "off_threshold_c": float(wx_block.get("off_threshold_c", 16.0)),
                        "on_threshold_c": float(wx_block.get("on_threshold_c", 14.0)),
                        "forecast_lookahead_hours": int(wx_block.get("forecast_lookahead_hours", 6)),
                        "forecast_min_c": float(wx_block.get("forecast_min_c", 12.0)),
                    },
                    "operating_hours": {
                        "enabled": bool(oh_block.get("enabled", False)),
                        "weekday": {
                            "day_start": str((oh_block.get("weekday") or {}).get("day_start", "07:30")),
                            "day_end":   str((oh_block.get("weekday") or {}).get("day_end", "22:30")),
                        },
                        "weekend": {
                            "day_start": str((oh_block.get("weekend") or {}).get("day_start", "08:30")),
                            "day_end":   str((oh_block.get("weekend") or {}).get("day_end", "23:00")),
                        },
                        "night_setback_offset_c": float(oh_block.get("night_setback_offset_c", -3.0)),
                        "out_of_hours_action": str(oh_block.get("out_of_hours_action", "setback")).lower(),
                    },
                    "circuits": _clean_circuits(
                        controller_block.get("circuits")
                        if (controller_block.get("config_mode") or "manual") == "floor_plan"
                        else heating.get("circuits")
                             or []
                    ),
                },
            }
        except Exception as e:
            logger.error(f"Failed to read controller config: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/config")
    async def save_controller_config(data: dict):
        """
        Save circuits + controller flags and hot-reload the live controller.
        Accepts:
          { "config": { "enabled": bool, "dry_run": bool, "circuits": [...] } }

        The save is two-stage: persist to disk first (so a crash mid-reload
        doesn't leave us with an applied-but-unpersisted state), then ask
        the live controller to apply_config(). The response includes the
        diff so the frontend can show a meaningful toast.
        """
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            incoming = data.get("config", data) if isinstance(data, dict) else {}

            # Diagnostic: what did the client send?
            try:
                incoming_circuits = incoming.get("circuits") or []
                logger.debug(
                    f"[save_config] entry: top_keys={list(incoming.keys())} "
                    f"enabled={incoming.get('enabled')} "
                    f"dry_run={incoming.get('dry_run')} "
                    f"circuit_count={len(incoming_circuits)}"
                )
                for ci, c in enumerate(incoming_circuits):
                    if not isinstance(c, dict):
                        continue
                    for ri, r in enumerate(c.get("rooms") or []):
                        if not isinstance(r, dict):
                            continue
                        logger.debug(
                            f"[save_config] client room "
                            f"circuit[{ci}].id={c.get('id')} "
                            f"room[{ri}].id={r.get('id')} "
                            f"name={r.get('name')!r} "
                            f"target_temp={r.get('target_temp')!r}"
                        )
            except Exception as diag_err:
                logger.warning(f"[save_config] entry-diag failed: {diag_err}")

            controller_block = heating.setdefault("controller", {})
            if "enabled" in incoming:
                controller_block["enabled"] = bool(incoming["enabled"])
            if "dry_run" in incoming:
                controller_block["dry_run"] = bool(incoming["dry_run"])
            if "config_mode" in incoming:
                cm = incoming.get("config_mode")
                if cm in VALID_CONFIG_MODES:
                    controller_block["config_mode"] = cm
                elif cm is None:
                    controller_block.pop("config_mode", None)
                # else: silently ignore garbage rather than failing the whole save

            if "weather_suppression" in incoming and isinstance(
                    incoming["weather_suppression"], dict
            ):
                wx_in = incoming["weather_suppression"]
                wx_out = controller_block.setdefault("weather_suppression", {})
                if "enabled" in wx_in:
                    wx_out["enabled"] = bool(wx_in["enabled"])
                if "off_threshold_c" in wx_in:
                    try:
                        wx_out["off_threshold_c"] = float(wx_in["off_threshold_c"])
                    except (TypeError, ValueError):
                        pass
                if "on_threshold_c" in wx_in:
                    try:
                        wx_out["on_threshold_c"] = float(wx_in["on_threshold_c"])
                    except (TypeError, ValueError):
                        pass
                if "forecast_lookahead_hours" in wx_in:
                    try:
                        wx_out["forecast_lookahead_hours"] = max(
                            1, int(wx_in["forecast_lookahead_hours"])
                        )
                    except (TypeError, ValueError):
                        pass
                if "forecast_min_c" in wx_in:
                    try:
                        wx_out["forecast_min_c"] = float(wx_in["forecast_min_c"])
                    except (TypeError, ValueError):
                        pass

            if "operating_hours" in incoming and isinstance(
                    incoming["operating_hours"], dict
            ):
                oh_in = incoming["operating_hours"]
                oh_out = controller_block.setdefault("operating_hours", {})
                if "enabled" in oh_in:
                    oh_out["enabled"] = bool(oh_in["enabled"])
                for grp in ("weekday", "weekend"):
                    if grp in oh_in and isinstance(oh_in[grp], dict):
                        gout = oh_out.setdefault(grp, {})
                        for k in ("day_start", "day_end"):
                            v = oh_in[grp].get(k)
                            if isinstance(v, str) and len(v) == 5 and v[2] == ":":
                                gout[k] = v
                if "night_setback_offset_c" in oh_in:
                    try:
                        oh_out["night_setback_offset_c"] = float(oh_in["night_setback_offset_c"])
                    except (TypeError, ValueError):
                        pass
                if "out_of_hours_action" in oh_in:
                    a = str(oh_in["out_of_hours_action"]).lower()
                    if a in ("setback", "off", "min_only"):
                        oh_out["out_of_hours_action"] = a

            sync_notes: List[str] = []
            if "circuits" in incoming:
                from modules.chambers import heating_room_names
                from routes.chamber_routes import apply_room_edit
                prev_names = heating_room_names(cfg)
                plan = load_plan()
                plan_out, sync_notes = sync_heating_config(
                    heating, plan, edited=incoming["circuits"])
                # A heated room is a chamber: registered here, renames carried both ways.
                current = plan_out if plan_out is not None else plan
                if apply_room_edit(cfg, current, edited="heating",
                                   prev_names=prev_names)["plan_changed"]:
                    plan_out = current
                if plan_out is not None:
                    save_plan(plan_out)

            _save_config(cfg)
            logger.info("Heating controller config saved via API"
                        + (f" ({'; '.join(sync_notes)})" if sync_notes else ""))

            # ── Diagnostic: what's about to be passed to apply_config? ─
            try:
                heating_circuits = heating.get("circuits") or []
                logger.debug(
                    f"[save_config] pre-apply heating dict: "
                    f"keys={list(heating.keys())} "
                    f"top_enabled={heating.get('enabled')} "
                    f"controller.enabled={(heating.get('controller') or {}).get('enabled')} "
                    f"circuit_count={len(heating_circuits)}"
                )
                for ci, c in enumerate(heating_circuits):
                    for ri, r in enumerate(c.get("rooms") or []):
                        logger.debug(
                            f"[save_config] pre-apply room "
                            f"circuit[{ci}].id={c.get('id')} "
                            f"room[{ri}].id={r.get('id')} "
                            f"target_temp={r.get('target_temp')!r}"
                        )
            except Exception as diag_err:
                logger.warning(f"[save_config] pre-apply-diag failed: {diag_err}")

            # Hot-reload the live controller. We pass it the freshly-saved
            # heating block — same shape its constructor reads.
            ctrl = _resolve()
            apply_result: Dict[str, Any] = {"applied": False}
            logger.debug(
                f"[save_config] resolved controller: "
                f"present={ctrl is not None} "
                f"has_apply_config={hasattr(ctrl, 'apply_config') if ctrl else False}"
            )
            if ctrl is not None and hasattr(ctrl, "apply_config"):
                try:
                    apply_result = await ctrl.apply_config(
                        heating, reason="api-config-save"
                    )
                except Exception as e:
                    # Persisted to disk but live reload failed — surface
                    # this clearly so the user knows a restart is needed
                    # to pick up the saved changes.
                    logger.error(f"apply_config raised: {e}", exc_info=True)
                    return {
                        "success": True,
                        "persisted": True,
                        "applied": False,
                        "error": f"Saved to disk but live reload failed: {e}",
                        "restart_required": True,
                    }
            else:
                # Older controller without apply_config — fall back to the
                # original behaviour. Shouldn't happen in this build but
                # it's a graceful degradation path.
                return {
                    "success": True,
                    "persisted": True,
                    "applied": False,
                    "message": "Saved. Restart to apply (controller does not support hot-reload).",
                    "restart_required": True,
                }

            response = {
                "success": True,
                "persisted": True,
                "warnings": sync_notes,
                "applied": apply_result.get("applied", False),
                "diff": apply_result.get("diff", {}),
                "tick_triggered": apply_result.get("tick_triggered", False),
                "restart_required": False,
            }
            logger.debug(
                f"[save_config] returning to client: "
                f"applied={response['applied']} "
                f"tick_triggered={response['tick_triggered']} "
                f"diff.any_changes={(response.get('diff') or {}).get('any_changes')}"
            )
            return response
        except Exception as e:
            logger.error(f"Failed to save controller config: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/room/target")
    async def set_room_target(data: dict):
        """
        Fast-path endpoint for the most common edit: change one room's
        target temperature. Avoids round-tripping the entire config.

        Body:
            {"circuit_id": "circuit_1", "room_id": "room_1", "target_temp": 21.5}

        Behaviour:
          - Loads config, updates the named room's target_temp, persists.
          - Calls controller.apply_config() to hot-reload — also schedules
            an immediate tick so the UI sees the effect within a second.
          - Returns 404 (success=False) if the circuit/room isn't found,
            without touching disk or live state.
        """
        try:
            circuit_id = data.get("circuit_id")
            room_id = data.get("room_id")
            target_temp = data.get("target_temp")
            if not circuit_id or not room_id:
                return {"success": False, "error": "circuit_id and room_id required"}
            try:
                target_temp = float(target_temp)
            except (TypeError, ValueError):
                return {"success": False, "error": "target_temp must be numeric"}
            # Soft sanity range — matches the controller's own clamps
            # elsewhere. 5–32 °C is roughly the union of consumer Zigbee
            # thermostat limits.
            if target_temp < 5.0 or target_temp > 32.0:
                return {"success": False, "error": "target_temp must be between 5 and 32 °C"}

            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            controller_block = heating.setdefault("controller", {})
            config_mode = controller_block.get("config_mode") or "manual"
            if config_mode == "floor_plan":
                circuits = controller_block.get("circuits") or []
            else:
                circuits = heating.get("circuits") or []
            target_circuit = next(
                (c for c in circuits if c.get("id") == circuit_id), None
            )
            if target_circuit is None:
                return {"success": False, "error": f"circuit '{circuit_id}' not found"}
            target_room = next(
                (r for r in (target_circuit.get("rooms") or [])
                 if r.get("id") == room_id),
                None,
            )
            if target_room is None:
                return {
                    "success": False,
                    "error": f"room '{room_id}' not found in circuit '{circuit_id}'",
                }

            old_value = target_room.get("target_temp")
            target_room["target_temp"] = target_temp

            plan_out, _ = sync_heating_config(heating, load_plan(), edited=circuits)
            if plan_out is not None:
                save_plan(plan_out)
            _save_config(cfg)

            ctrl = _resolve()
            apply_result: Dict[str, Any] = {"applied": False}
            if ctrl is not None and hasattr(ctrl, "apply_config"):
                try:
                    apply_result = await ctrl.apply_config(
                        heating, reason=f"room-target ({room_id})"
                    )
                except Exception as e:
                    logger.error(f"apply_config raised: {e}", exc_info=True)
                    return {
                        "success": True,
                        "persisted": True,
                        "applied": False,
                        "error": f"Saved but live reload failed: {e}",
                        "restart_required": True,
                    }

            logger.info(
                f"Room target updated: circuit={circuit_id} room={room_id} "
                f"{old_value} → {target_temp}°C"
            )
            return {
                "success": True,
                "persisted": True,
                "applied": apply_result.get("applied", False),
                "tick_triggered": apply_result.get("tick_triggered", False),
                "circuit_id": circuit_id,
                "room_id": room_id,
                "target_temp": target_temp,
                "previous": old_value,
            }
        except Exception as e:
            logger.error(f"Failed to set room target: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/controller/devices")
    async def list_controllable_devices():
        """
        Return receiver candidates and TRV candidates.
        Receivers = mains-powered devices with thermostat cluster (system_mode).
        TRVs = any device with thermostat attributes (temperature, setpoint).
        """
        try:
            devices = _devices()
            receivers = []
            thermostats = []

            cfg = _load_config()
            assignments_recv: Dict[str, Dict] = {}
            assignments_trv: Dict[str, Dict] = {}
            for c in (cfg.get("heating") or {}).get("circuits") or []:
                if c.get("receiver_ieee"):
                    assignments_recv[c["receiver_ieee"]] = {
                        "circuit_id": c.get("id"), "circuit_name": c.get("name"),
                    }
                for r in c.get("rooms") or []:
                    trv_list = []
                    for t in (r.get("trvs") or []):
                        if isinstance(t, dict) and t.get("ieee"):
                            trv_list.append(t["ieee"])
                    for legacy_ieee in (r.get("trv_ieees") or []):
                        if legacy_ieee and legacy_ieee not in trv_list:
                            trv_list.append(legacy_ieee)
                    for t_ieee in trv_list:
                        assignments_trv[t_ieee] = {
                            "circuit_id": c.get("id"), "circuit_name": c.get("name"),
                            "room_id": r.get("id"), "room_name": r.get("name"),
                        }

            for ieee, dev in devices.items():
                if isinstance(dev, dict):
                    state = dev.get("state") or {}
                    name = dev.get("friendly_name") or dev.get("name") or str(ieee)
                    manuf = dev.get("manufacturer"); model = dev.get("model")
                    power_source = dev.get("power_source")
                else:
                    state = getattr(dev, "state", None) or {}
                    name = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
                    if not name:
                        service = getattr(dev, "service", None)
                        if service:
                            name = (getattr(service, "friendly_names", None) or {}).get(str(ieee))
                    name = name or str(ieee)
                    manuf = getattr(dev, "manufacturer", None)
                    model = getattr(dev, "model", None)
                    power_source = getattr(dev, "power_source", None)
                    if power_source is None:
                        zigpy_dev = getattr(dev, "zigpy_dev", None)
                        if zigpy_dev:
                            try:
                                power_source = zigpy_dev.node_desc.mac_capability_flags.mains_powered
                            except Exception:
                                pass

                ieee_s = str(ieee)
                model_s = str(model or "").upper()
                base = {"ieee": ieee_s, "name": name, "manufacturer": manuf, "model": model}

                # A real device answers from its own evidence: a thermostat
                # endpoint, the node descriptor's power source, the settings it
                # takes (modules/device_class.py). The state and model tests
                # below only serve a device that is a plain dict.
                real = not isinstance(dev, dict) and getattr(dev, "zigpy_dev", None) is not None
                if real:
                    from modules import device_class
                    has_thermostat = device_class.has_function(dev, "thermostat")
                else:
                    has_thermostat = any(k in state for k in (
                        "system_mode", "occupied_heating_setpoint",
                        "local_temperature", "current_temperature",
                        "heating_demand", "hvac_action"
                    ))

                if not has_thermostat:
                    continue

                mains = device_class.is_mains_powered(dev) if real else None
                if mains is not None:
                    is_mains = mains
                else:
                    is_mains = False
                    if power_source is True or power_source == "Mains":
                        is_mains = True
                    elif "SLR" in model_s or "RECEIVER" in model_s:
                        is_mains = True
                    elif "SLT" not in model_s and "TRV" not in model_s:
                        if "on_off" in state or state.get("power_source") == "Mains":
                            is_mains = True

                # Drives the UI's calibrate / child-lock / window-detection
                # controls: whether the device takes them, not who made it.
                ask = getattr(dev, "supports_setting", None)
                if callable(ask):
                    settings = sorted(n for n in TRV_SETTINGS if ask(n))
                    is_aqara_trv = "motor_calibration" in settings
                else:
                    settings = []
                    manuf_s = str(manuf or "").lower()
                    is_aqara_trv = (
                                           ("lumi" in manuf_s or "aqara" in manuf_s) and
                                           any(marker in model_s.lower() for marker in ("airrtc", "agl001", "thermostat"))
                                   ) or "AGL001" in model_s

                entry = dict(base)
                entry["temperature"] = state.get("local_temperature") or state.get("current_temperature")
                entry["setpoint"] = state.get("occupied_heating_setpoint")
                entry["system_mode"] = state.get("system_mode")
                entry["hvac_action"] = state.get("hvac_action")
                entry["is_aqara_trv"] = is_aqara_trv

                if is_mains:
                    entry["assigned"] = assignments_recv.get(ieee_s)
                    receivers.append(entry)

                entry_trv = dict(base)
                entry_trv["temperature"] = entry["temperature"]
                entry_trv["setpoint"] = entry["setpoint"]
                entry_trv["is_aqara_trv"] = is_aqara_trv
                entry_trv["settings"] = settings
                if real:
                    entry_trv["device_class"] = device_class.describe(dev)
                entry_trv["assigned"] = assignments_trv.get(ieee_s)
                thermostats.append(entry_trv)

            return {
                "success": True,
                "receivers": receivers,
                "thermostats": thermostats,
            }
        except Exception as e:
            logger.error(f"Failed to list controllable devices: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/controller/contact-sensors")
    async def list_contact_sensor_candidates():
        """
        Return devices that look like door/window contact sensors —
        anything reporting `is_open` or `contact` in state. Used to
        populate the room editor dropdown so the user only sees relevant
        devices rather than the full device list.
        """
        try:
            devices = _devices()
            sensors = []

            for ieee, dev in devices.items():
                if isinstance(dev, dict):
                    state = dev.get("state") or {}
                    name = dev.get("friendly_name") or dev.get("name") or str(ieee)
                    manuf = dev.get("manufacturer")
                    model = dev.get("model")
                else:
                    state = getattr(dev, "state", None) or {}
                    name = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
                    if not name:
                        service = getattr(dev, "service", None)
                        if service:
                            name = (getattr(service, "friendly_names", None) or {}).get(str(ieee))
                    name = name or str(ieee)
                    manuf = getattr(dev, "manufacturer", None)
                    model = getattr(dev, "model", None)

                # Must look like a contact sensor — i.e. expose is_open or contact
                if "is_open" in state:
                    is_open = bool(state.get("is_open"))
                elif "contact" in state:
                    is_open = not bool(state.get("contact"))
                else:
                    continue

                sensors.append({
                    "ieee": str(ieee),
                    "name": name,
                    "manufacturer": manuf,
                    "model": model,
                    "is_open": is_open,
                })

            sensors.sort(key=lambda s: s["name"].lower())
            return {"success": True, "sensors": sensors}
        except Exception as e:
            logger.error(f"Failed to list contact sensors: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/api/heating/controller/sensors")
    async def list_sensor_candidates():
        """
        Return devices that can be used as a room temperature source for a circuit room.
        Includes:
          - bare temperature sensors (cluster 0x0402 / 'temperature' in state)
          - Hive SLTs and similar thermostats (they report accurate room temps)
          - any device exposing local_temperature / current_temperature / temperature
        Deliberately inclusive: the user picks which device actually sits in the room.
        """
        try:
            devices = _devices()
            sensors = []

            for ieee, dev in devices.items():
                if isinstance(dev, dict):
                    state = dev.get("state") or {}
                    name = dev.get("friendly_name") or dev.get("name") or str(ieee)
                    manuf = dev.get("manufacturer")
                    model = dev.get("model")
                else:
                    state = getattr(dev, "state", None) or {}
                    name = getattr(dev, "friendly_name", None) or getattr(dev, "name", None)
                    if not name:
                        service = getattr(dev, "service", None)
                        if service:
                            name = (getattr(service, "friendly_names", None) or {}).get(str(ieee))
                    name = name or str(ieee)
                    manuf = getattr(dev, "manufacturer", None)
                    model = getattr(dev, "model", None)

                temp = None
                temp_key = None
                for k in ("local_temperature", "current_temperature", "temperature"):
                    v = state.get(k)
                    try:
                        f = float(v) if v is not None else None
                    except (TypeError, ValueError):
                        f = None
                    if f is not None and f != 0:
                        temp = f
                        temp_key = k
                        break

                if temp is None:
                    continue

                sensors.append({
                    "ieee": str(ieee),
                    "name": name,
                    "manufacturer": manuf,
                    "model": model,
                    "temperature": temp,
                    "source_key": temp_key,
                    "is_thermostat": any(k in state for k in (
                        "system_mode", "occupied_heating_setpoint", "heating_demand"
                    )),
                })

            return {"success": True, "sensors": sensors}
        except Exception as e:
            logger.error(f"Failed to list sensor candidates: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/heating/controller/trv/settings")
    async def update_trv_settings(data: dict):
        """
        Update a TRV's persistent config (window_detection, child_lock, valve_detection)
        both in the running controller and in config.yaml on disk, then apply to the device.

        Body: { "ieee": "...", "window_detection": true, "child_lock": false,
                "valve_detection": true, "min_setpoint": 5.0 }
        min_setpoint null clears it back to the 5 °C default.
        """
        ieee = str(data.get("ieee") or "").strip()
        if not ieee:
            return {"success": False, "error": "ieee required"}

        updates = {}
        for k in ("window_detection", "child_lock", "valve_detection"):
            if k in data:
                b = _as_bool(data.get(k), None)
                if b is not None:
                    updates[k] = b

        if "min_setpoint" in data:
            v = _as_float(data.get("min_setpoint"))
            if data.get("min_setpoint") is not None and (v is None or not 4.0 <= v <= 30.0):
                return {"success": False, "error": "min_setpoint must be between 4 and 30 °C"}
            updates["min_setpoint"] = None if v is None else round(v, 1)

        if not updates:
            return {"success": False, "error": "no valid settings in request"}

        # 1) Persist to disk, and through the sync into the plan's radiator
        try:
            cfg = _load_config()
            heating = cfg.setdefault("heating", {})
            located = _find_trv_in_config(cfg, ieee)
            if not located:
                return {"success": False, "error": f"TRV {ieee} not found in any room"}
            _, _, trv_dict = located
            for k, v in updates.items():
                if v is None:
                    trv_dict.pop(k, None)
                else:
                    trv_dict[k] = v
            plan_out, _ = sync_heating_config(heating, load_plan(),
                                              edited=circuits_for_mode(heating))
            if plan_out is not None:
                save_plan(plan_out)
            _save_config(cfg)
        except Exception as e:
            logger.error(f"Failed to persist TRV settings: {e}", exc_info=True)
            return {"success": False, "error": f"persist failed: {e}"}

        # 2) Apply in-memory + push to device
        ctrl = _resolve()
        apply_result = None
        if ctrl:
            ctrl.update_trv_settings(ieee, updates)
            try:
                apply_result = await ctrl.apply_trv_config(ieee)
            except Exception as e:
                logger.error(f"apply_trv_config({ieee}) failed: {e}", exc_info=True)
                apply_result = {"success": False, "error": str(e)}

        return {
            "success": True,
            "ieee": ieee,
            "updates": updates,
            "apply_result": apply_result,
        }

    @app.post("/api/heating/controller/trv/calibrate")
    async def trigger_trv_calibration(data: dict):
        """
        One-shot: start motor calibration on an Aqara TRV.
        Body: { "ieee": "..." }
        """
        ieee = str(data.get("ieee") or "").strip()
        if not ieee:
            return {"success": False, "error": "ieee required"}
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        return await ctrl.trigger_calibration(ieee)

    @app.post("/api/heating/controller/trv/apply-config")
    async def reapply_trv_config(data: dict):
        """
        Re-send persistent settings (and sensor_type if applicable) to one TRV.
        Useful if a TRV was offline at controller startup.
        Body: { "ieee": "..." }
        """
        ieee = str(data.get("ieee") or "").strip()
        if not ieee:
            return {"success": False, "error": "ieee required"}
        ctrl = _resolve()
        if not ctrl:
            return {"success": False, "error": "Controller not initialised"}
        try:
            return await ctrl.apply_trv_config(ieee)
        except Exception as e:
            logger.error(f"apply_trv_config({ieee}) failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}