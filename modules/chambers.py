"""
Chamber registry — the one notion of "a room in the home".

`chambers:` in config.yaml is the registry. A heating room and a floor-plan
room are facets of a chamber: same id, and the name is the registry's.
`reconcile` holds that at every write. No FastAPI; wired in by
routes/chamber_routes.py. See docs/chambers.md.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.chambers")

SCHEMA_VERSION = 1

# Same id convention as modules/floor_plan.py — chamber ids and room ids must
# stay interchangeable, so the grammar cannot diverge.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

DEFAULT_ICON = "fa-cube"

MAX_NAME_LEN = 64


# id helpers

def slugify(s: Any) -> str:
    """
    Best-effort id from free text; "" when nothing usable survives.

    Deliberately unlike ``floor_plan._slugify``, which falls back to the literal
    ``"id"`` when input slugifies to nothing. Here that fallback would turn a
    chamber named "!!!" into a chamber called "id" — so unusable input fails
    instead, and the caller decides what to do about it.
    """
    s = str(s or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def valid_id(s: Any) -> Optional[str]:
    """Return the normalised id if it satisfies the shared grammar, else None."""
    if not isinstance(s, str):
        return None
    s = s.strip().lower()
    return s if _ID_RE.match(s) else None


def coerce_id(raw: Any, name: Any = None) -> Optional[str]:
    """
    Accept an explicit id, slugify it if it's close enough, else derive one from
    ``name``. None when there is nothing usable to work with.

    Deriving from empty input must fail rather than fall back to ``slugify("")``,
    which would happily mint a chamber called "id".
    """
    cid = valid_id(raw)
    if cid:
        return cid
    if isinstance(raw, str) and raw.strip():
        cid = valid_id(slugify(raw))
        if cid:
            return cid
    if isinstance(name, str) and name.strip():
        return valid_id(slugify(name))
    return None


# cleaning

def clean_chamber(raw: Any, existing_ids: Optional[set] = None) -> Optional[dict]:
    """
    Normalise one user-supplied chamber definition.

    Returns None when the entry cannot be salvaged (no usable id/name). Unlike
    the floor-plan cleaner this does NOT de-duplicate by suffixing: a chamber
    id collision means the caller is redefining the same room, which is an
    error worth surfacing rather than silently creating ``kitchen_2``.
    """
    if not isinstance(raw, dict):
        return None

    name = str(raw.get("name") or "").strip()[:MAX_NAME_LEN]
    cid = coerce_id(raw.get("id"), name)
    if not cid:
        return None
    if existing_ids is not None and cid in existing_ids:
        return None
    if existing_ids is not None:
        existing_ids.add(cid)

    out: Dict[str, Any] = {
        "id": cid,
        "name": name or cid,
    }

    level = valid_id(raw.get("level"))
    if level:
        out["level"] = level

    icon = str(raw.get("icon") or "").strip()
    if icon and len(icon) <= 40:
        out["icon"] = icon

    return out


def clean_chambers(raw: Any) -> List[dict]:
    """Normalise the whole ``chambers:`` block. Invalid entries are dropped."""
    if not isinstance(raw, list):
        return []
    seen: set = set()
    out: List[dict] = []
    for entry in raw:
        c = clean_chamber(entry, seen)
        if c:
            out.append(c)
        else:
            logger.debug("chambers: dropped invalid entry %r", entry)
    return out


# facets — what heating and the floor plan hold for a chamber

def _circuit_lists(cfg: Dict[str, Any]) -> List[list]:
    """Both circuit copies heating keeps (docs/heating-controller.md § Config modes)."""
    heating = cfg.get("heating")
    if not isinstance(heating, dict):
        return []
    controller = heating.get("controller")
    lists = [controller.get("circuits") if isinstance(controller, dict) else None,
             heating.get("circuits")]
    return [l for l in lists if isinstance(l, list) and l]


def _heating_room_dicts(cfg: Dict[str, Any]) -> List[dict]:
    return [room
            for circuits in _circuit_lists(cfg)
            for circuit in circuits if isinstance(circuit, dict)
            for room in circuit.get("rooms") or [] if isinstance(room, dict)]


def _plan_room_dicts(plan: Optional[Dict[str, Any]]) -> List[Tuple[Optional[str], dict]]:
    if not isinstance(plan, dict):
        return []
    return [(valid_id(level.get("id")), room)
            for level in plan.get("levels") or [] if isinstance(level, dict)
            for room in level.get("rooms") or [] if isinstance(room, dict)]


def _room_name(room: dict, rid: str) -> str:
    return str(room.get("name") or rid).strip()[:MAX_NAME_LEN] or rid


def heating_room_names(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Heated chamber id -> the name heating holds for it."""
    out: Dict[str, str] = {}
    for room in _heating_room_dicts(cfg):
        rid = valid_id(room.get("id"))
        if rid:
            out.setdefault(rid, _room_name(room, rid))
    return out


def plan_room_names(plan: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Drawn chamber id -> the name the floor plan holds for it."""
    out: Dict[str, str] = {}
    for _, room in _plan_room_dicts(plan):
        rid = valid_id(room.get("id"))
        if rid:
            out.setdefault(rid, _room_name(room, rid))
    return out


def room_facets(cfg: Dict[str, Any], plan: Optional[Dict[str, Any]]) -> Dict[str, dict]:
    """Chamber id -> ``{name, heated, mapped, level}`` for every heating or plan room."""
    out: Dict[str, dict] = {}
    for rid, name in heating_room_names(cfg).items():
        out[rid] = {"name": name, "heated": True, "mapped": False, "level": None}
    for level_id, room in _plan_room_dicts(plan):
        rid = valid_id(room.get("id"))
        if not rid:
            continue
        f = out.setdefault(rid, {"name": _room_name(room, rid), "heated": False,
                                 "mapped": False, "level": None})
        if not f["mapped"]:
            f["mapped"], f["level"] = True, level_id
    return out


def heating_device_rooms(cfg: Dict[str, Any]) -> Dict[str, str]:
    """ieee -> the chamber whose heating binds it (TRVs and room sensors)."""
    out: Dict[str, str] = {}
    for room in _heating_room_dicts(cfg):
        rid = valid_id(room.get("id"))
        if not rid:
            continue
        ieees = [t.get("ieee") if isinstance(t, dict) else t for t in room.get("trvs") or []]
        ieees += list(room.get("trv_ieees") or [])
        ieees.append(room.get("temperature_sensor_ieee"))
        ieees += [s.get("ieee") for s in room.get("temperature_sensors") or []
                  if isinstance(s, dict)]
        for ieee in ieees:
            if isinstance(ieee, str) and ieee.strip():
                out.setdefault(ieee.strip().lower(), rid)
    return out


def _advisor_zones(cfg: Dict[str, Any]) -> List[dict]:
    zones = (cfg.get("heating") or {}).get("zones")
    return [z for z in zones if isinstance(z, dict)] if isinstance(zones, list) else []


def _move_zone(cfg: Dict[str, Any], old: str, new: Optional[str]) -> None:
    """Re-key the advisor zone on ``old`` to ``new``, or drop it; one zone per chamber."""
    zones = _advisor_zones(cfg)
    if not any(z.get("id") == old for z in zones):
        return
    taken = new is not None and any(z.get("id") == new for z in zones)
    kept = []
    for z in zones:
        if z.get("id") == old:
            if new is None or taken:
                continue
            z = {**z, "id": new}
        kept.append(z)
    cfg["heating"]["zones"] = kept


def _load_plan() -> Optional[Dict[str, Any]]:
    from modules.floor_plan_store import load_plan
    return load_plan()


def levels(plan: Optional[Dict[str, Any]] = None) -> List[dict]:
    """
    Known levels (``id``, ``name``, ``index``), from the floor plan if drawn.

    Used to order and group chambers in the UI. Empty when no plan exists —
    chambers stay flat in that case, which is fine.
    """
    plan = plan if plan is not None else _load_plan()
    if not isinstance(plan, dict):
        return []
    out: List[dict] = []
    for level in plan.get("levels") or []:
        if not isinstance(level, dict):
            continue
        lid = valid_id(level.get("id"))
        if not lid:
            continue
        out.append({
            "id": lid,
            "name": str(level.get("name") or lid).strip()[:MAX_NAME_LEN] or lid,
            "index": int(level.get("index") or 0),
        })
    out.sort(key=lambda l: (l["index"], l["name"]))
    return out


# registry

def build_registry(cfg: Dict[str, Any],
                   plan: Optional[Dict[str, Any]] = None) -> List[dict]:
    """
    Every chamber, ordered by level index (per the floor plan), then name.

    Each entry carries::

        id      — stable identifier; heating's room.id and the plan's room.id
        name    — display name
        level   — level id, when known (the plan's, once drawn)
        icon    — Font Awesome class
        heated  — heating has a room for it
        mapped  — the floor plan has an outline for it
        in_use  — heated or mapped, so it cannot simply be deleted
        zoned   — the heating advisor has a zone on it

    A heating or plan room that `reconcile` has not registered yet is still
    listed, so a config from before the single registry reads complete.
    """
    plan = plan if plan is not None else _load_plan()
    facets = room_facets(cfg, plan)
    by_id: Dict[str, dict] = {c["id"]: dict(c) for c in clean_chambers(cfg.get("chambers"))}
    for rid, f in facets.items():
        by_id.setdefault(rid, {"id": rid, "name": f["name"]})

    for rid, c in by_id.items():
        f = facets.get(rid) or {}
        c["heated"] = bool(f.get("heated"))
        c["mapped"] = bool(f.get("mapped"))
        c["in_use"] = c["heated"] or c["mapped"]
        if f.get("level"):
            c["level"] = f["level"]
        c.setdefault("icon", DEFAULT_ICON)

    zoned = {z.get("id") for z in _advisor_zones(cfg)}
    for rid, c in by_id.items():
        c["zoned"] = rid in zoned

    level_index = {l["id"]: l["index"] for l in levels(plan)}

    def sort_key(c: dict):
        lvl = c.get("level")
        # Chambers on no known level sort last, not first — an unplaced room is
        # less interesting than a placed one.
        return (level_index.get(lvl, 10_000) if lvl else 10_000, c["name"].lower())

    return sorted(by_id.values(), key=sort_key)


def registry_ids(cfg: Dict[str, Any]) -> set:
    """Set of every valid chamber id. Used to reject assignments to phantom rooms."""
    return {c["id"] for c in build_registry(cfg)}


def get_chamber(cfg: Dict[str, Any], chamber_id: Any) -> Optional[dict]:
    """One registry entry by id, or None."""
    cid = valid_id(chamber_id)
    if not cid:
        return None
    for c in build_registry(cfg):
        if c["id"] == cid:
            return c
    return None


def _name_key(name: Any) -> str:
    return " ".join(str(name or "").casefold().split())


def _loose_key(name: Any) -> str:
    """Name with possessives, punctuation and a trailing "room" dropped."""
    words = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").casefold().replace("'s", "")).split()
    if len(words) > 1 and words[-1] == "room":
        words = words[:-1]
    return " ".join(words)


def reconcile(cfg: Dict[str, Any], plan: Optional[Dict[str, Any]],
              edited: Optional[str] = None,
              prev_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Make the registry, heating's rooms and the plan's rooms one set of chambers.

    Every heating or plan room is registered under its own id. The registry's
    name is then written into both, unless ``edited`` ("heating" or
    "floor_plan") names the side the user just saved: there a name that moved
    from ``prev_names`` is a rename and the registry takes it. A room new to the
    registry absorbs an unused chamber of the same name rather than doubling it.

    Mutates ``cfg`` and ``plan``. Returns ``{plan_changed, heating_changed,
    merged: {gone_id: surviving_id}}``; the caller re-points ``merged``.
    """
    configured = clean_chambers(cfg.get("chambers"))
    by_id = {c["id"]: c for c in configured}
    facets = room_facets(cfg, plan)
    merged: Dict[str, str] = {}

    for rid, f in facets.items():
        c = by_id.get(rid)
        if c is None:
            c = {"id": rid, "name": f["name"]}
            twin = next((x for x in configured if x["id"] not in facets
                         and _name_key(x["name"]) == _name_key(f["name"])), None)
            if twin is not None:
                configured.remove(twin)
                del by_id[twin["id"]]
                merged[twin["id"]] = rid
                c.update({k: twin[k] for k in ("name", "icon") if k in twin})
            configured.append(c)
            by_id[rid] = c
        elif edited and f["name"] != c["name"] and (prev_names or {}).get(rid) != f["name"]:
            c["name"] = f["name"]

    heating_changed = plan_changed = False
    for room in _heating_room_dicts(cfg):
        c = by_id.get(valid_id(room.get("id")) or "")
        if c and room.get("name") != c["name"]:
            room["name"] = c["name"]
            heating_changed = True
    for _, room in _plan_room_dicts(plan):
        c = by_id.get(valid_id(room.get("id")) or "")
        if c and room.get("name") != c["name"]:
            room["name"] = c["name"]
            plan_changed = True

    if configured or "chambers" in cfg:
        cfg["chambers"] = configured
    return {"plan_changed": plan_changed, "heating_changed": heating_changed, "merged": merged}


def upsert_chamber(cfg: Dict[str, Any], raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """
    Add or update a chamber in the ``chambers:`` block of ``cfg`` (mutated in place).

    Returns ``(chamber, None)`` on success or ``(None, error)`` on failure. A
    new chamber may not reuse an existing name — that is the duplicate this
    registry exists to prevent. `reconcile` carries a rename into heating and
    the plan.
    """
    chamber = clean_chamber(raw)
    if not chamber:
        return None, "chamber needs a valid id or name"

    cleaned = clean_chambers(cfg.get("chambers"))
    registry = build_registry(cfg)
    if not (isinstance(raw, dict) and valid_id(raw.get("id")) == chamber["id"]):
        # An id slugged from the name must not land on another chamber's.
        taken, base, n = {c["id"] for c in registry}, chamber["id"], 2
        while chamber["id"] in taken:
            chamber["id"] = f"{base[:60]}_{n}"
            n += 1
    for c in registry:
        if c["id"] != chamber["id"] and _name_key(c["name"]) == _name_key(chamber["name"]):
            return None, f"a chamber called '{c['name']}' already exists"

    for i, c in enumerate(cleaned):
        if c["id"] == chamber["id"]:
            cleaned[i] = {**c, **chamber}
            chamber = cleaned[i]
            break
    else:
        cleaned.append(chamber)

    cfg["chambers"] = cleaned
    return chamber, None


def delete_chamber(cfg: Dict[str, Any], chamber_id: Any,
                   plan: Optional[Dict[str, Any]] = None) -> tuple[bool, Optional[str]]:
    """
    Remove a chamber from the registry (``cfg`` mutated in place).

    Refused while heating or the floor plan still holds a room for it: the
    chamber would be registered again on the next save there. Its advisor
    zone goes with it.
    """
    cid = valid_id(chamber_id)
    if not cid:
        return False, "invalid chamber id"

    entry = next((c for c in build_registry(cfg, plan) if c["id"] == cid), None)
    if entry is None:
        return False, f"no chamber '{cid}'"
    if entry["in_use"]:
        where = " and ".join(w for w, on in (("heating", entry["heated"]),
                                             ("the floor plan", entry["mapped"])) if on)
        return False, (f"'{entry['name']}' is still used by {where} — "
                       f"remove it there first")

    cfg["chambers"] = [c for c in clean_chambers(cfg.get("chambers")) if c["id"] != cid]
    _move_zone(cfg, cid, None)
    return True, None


def merge_chambers(cfg: Dict[str, Any], plan: Optional[Dict[str, Any]],
                   source_id: Any, into_id: Any) -> tuple[Optional[dict], Optional[str]]:
    """
    Fold one chamber into another (``cfg`` and ``plan`` mutated in place).

    The result keeps ``into``'s name. Its id is whichever of the two heating or
    the plan already uses — learned heating data and history are keyed on it —
    so two chambers that both have one cannot be merged.

    Returns ``({survivor, gone, merged, plan_changed, heating_changed}, None)`` or
    ``(None, error)``; the caller re-points devices, groups and frames.
    """
    src, dst = valid_id(source_id), valid_id(into_id)
    reg = {c["id"]: c for c in build_registry(cfg, plan)}
    if not src or not dst or src not in reg or dst not in reg:
        return None, "both chambers must exist"
    if src == dst:
        return None, "a chamber cannot be merged into itself"
    if reg[src]["in_use"] and reg[dst]["in_use"]:
        return None, (f"'{reg[src]['name']}' and '{reg[dst]['name']}' are both used by "
                      f"heating or the floor plan — remove one of them there first")

    survivor, gone = (src, dst) if reg[src]["in_use"] else (dst, src)
    kept = {"id": survivor, "name": reg[dst]["name"]}
    for key in ("icon", "level"):
        value = next((reg[i][key] for i in (dst, src)
                      if reg[i].get(key) and reg[i][key] != DEFAULT_ICON), None)
        if value:
            kept[key] = value

    cleaned = [c for c in clean_chambers(cfg.get("chambers")) if c["id"] not in (src, dst)]
    cleaned.append(kept)
    cfg["chambers"] = cleaned
    # into's zone settings win, as its name does.
    if survivor == src:
        if any(z.get("id") == dst for z in _advisor_zones(cfg)):
            _move_zone(cfg, src, None)
        _move_zone(cfg, dst, src)
    else:
        _move_zone(cfg, src, dst)
    result = reconcile(cfg, plan)
    return {"survivor": survivor, "gone": gone, "merged": result["merged"],
            "plan_changed": result["plan_changed"],
            "heating_changed": result["heating_changed"]}, None


def duplicate_candidates(registry: List[dict]) -> List[dict]:
    """
    Pairs of chambers that look like one room, as ``{a, b, exact}``.

    Only pairs `merge_chambers` would accept. A differing digit is a different
    room ("Bedroom 1" / "Bedroom 2"), however close the spelling.
    """
    from difflib import SequenceMatcher
    out: List[dict] = []
    for i, a in enumerate(registry):
        for b in registry[i + 1:]:
            if a["in_use"] and b["in_use"]:
                continue
            ka, kb = _loose_key(a["name"]), _loose_key(b["name"])
            if not ka or not kb or re.findall(r"\d+", ka) != re.findall(r"\d+", kb):
                continue
            exact = _name_key(a["name"]) == _name_key(b["name"])
            if exact or ka == kb or SequenceMatcher(None, ka, kb).ratio() >= 0.88:
                out.append({"a": a["id"], "b": b["id"], "exact": exact})
    return out
