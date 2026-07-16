"""
modules/chambers.py
===================
Chamber registry — the Frames-side notion of "a room in the home".

Pure module: no I/O, no FastAPI, no global state. Wired in by
``routes/chamber_routes.py``.

Terminology
-----------
A *chamber* is a room. The name follows the beehive metaphor used by Frames
(Hive = the install, Frame = a dashboard, Chamber = a room, Cell = a device
tile).

``chamber`` is **Frames-side vocabulary only**. The heating subsystem and the
floor-plan editor both say ``room`` internally and deliberately keep doing so —
renaming them would mean migrating the config schema underneath
``heating_controller.py`` / ``thermal_profile.py`` / the floor-plan projection
for no functional gain. Translation happens here, at the boundary, and nowhere
else.

The registry is a UNION, not a migration
----------------------------------------
Rooms are already described in two places, and this module adopts both rather
than replacing either::

    chambers:                             -> source="config"      (this module)
    heating.circuits[].rooms[]            -> source="heating"     (adopted)
    heating.floor_plan.levels[].rooms[]   -> source="floor_plan"  (adopted)

A chamber's ``id`` is the SAME string heating uses as its ``room.id`` (e.g.
``living``). That is what keeps "Living Room" a single chamber instead of two.
When a configured chamber shadows an adopted room id, the configured entry
wins and is flagged ``adopted: True`` — meaning heating also defines this room,
so its id must not be renamed or the two drift apart.

Nothing here ever writes to the heating config.

Note: ``zones`` / ``config/zones.yaml`` are RSSI presence-detection zones, NOT
rooms. They are unrelated to chambers.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.chambers")

SCHEMA_VERSION = 1

# Same id convention as modules/floor_plan.py — chamber ids and room ids must
# stay interchangeable, so the grammar cannot diverge.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

SOURCE_CONFIG = "config"
SOURCE_HEATING = "heating"
SOURCE_FLOOR_PLAN = "floor_plan"

#: Sources that this module may not create, rename, or delete — they are owned
#: by the heating config and only ever read here.
ADOPTED_SOURCES = (SOURCE_HEATING, SOURCE_FLOOR_PLAN)

DEFAULT_ICON = "fa-cube"

MAX_NAME_LEN = 64


# ───────────────────────────── id helpers ─────────────────────────────

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


# ─────────────────────────── cleaning ───────────────────────────

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


# ───────────────────── adoption from existing config ─────────────────────

def _circuits(cfg: Dict[str, Any]) -> List[dict]:
    """
    Heating circuits, from either supported location.

    ``heating.controller.circuits`` and ``heating.circuits`` are both live in
    the wild (routes/floor_plan_routes.py reads both, in that order), so both
    are honoured here.
    """
    heating = cfg.get("heating")
    if not isinstance(heating, dict):
        return []
    controller = heating.get("controller")
    circuits = None
    if isinstance(controller, dict):
        circuits = controller.get("circuits")
    if not circuits:
        circuits = heating.get("circuits")
    return circuits if isinstance(circuits, list) else []


def heating_rooms(cfg: Dict[str, Any]) -> List[dict]:
    """Adopt ``heating.circuits[].rooms[]`` as chambers. Read-only."""
    out: List[dict] = []
    seen: set = set()
    for circuit in _circuits(cfg):
        if not isinstance(circuit, dict):
            continue
        for room in circuit.get("rooms") or []:
            if not isinstance(room, dict):
                continue
            rid = coerce_id(room.get("id"), room.get("name"))
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append({
                "id": rid,
                "name": str(room.get("name") or rid).strip()[:MAX_NAME_LEN] or rid,
                "source": SOURCE_HEATING,
            })
    return out


def floor_plan_rooms(cfg: Dict[str, Any]) -> List[dict]:
    """Adopt ``heating.floor_plan.levels[].rooms[]`` as chambers. Read-only."""
    heating = cfg.get("heating")
    if not isinstance(heating, dict):
        return []
    plan = heating.get("floor_plan")
    if not isinstance(plan, dict):
        return []

    out: List[dict] = []
    seen: set = set()
    for level in plan.get("levels") or []:
        if not isinstance(level, dict):
            continue
        level_id = valid_id(level.get("id"))
        for room in level.get("rooms") or []:
            if not isinstance(room, dict):
                continue
            rid = coerce_id(room.get("id"), room.get("name"))
            if not rid or rid in seen:
                continue
            seen.add(rid)
            entry = {
                "id": rid,
                "name": str(room.get("name") or rid).strip()[:MAX_NAME_LEN] or rid,
                "source": SOURCE_FLOOR_PLAN,
            }
            if level_id:
                entry["level"] = level_id
            out.append(entry)
    return out


def levels(cfg: Dict[str, Any]) -> List[dict]:
    """
    Known levels (``id``, ``name``, ``index``), from the floor plan if drawn.

    Used to order and group chambers in the UI. Empty when no plan exists —
    chambers stay flat in that case, which is fine.
    """
    heating = cfg.get("heating")
    if not isinstance(heating, dict):
        return []
    plan = heating.get("floor_plan")
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


# ───────────────────────────── registry ─────────────────────────────

def build_registry(cfg: Dict[str, Any]) -> List[dict]:
    """
    The full chamber registry: configured chambers unioned with adopted rooms.

    Precedence: an explicitly configured chamber wins over an adopted room with
    the same id, and is flagged ``adopted=True`` to record that heating also
    defines it (so its id is load-bearing and must not be renamed).

    Each entry carries::

        id       — stable identifier, shared with heating's room.id
        name     — display name
        level    — level id, when known
        icon     — Font Awesome class
        source   — "config" | "heating" | "floor_plan"
        adopted  — True when another subsystem also defines this id
        editable — False for rooms this module does not own

    Ordering is by level index (per the floor plan), then name — so a frame
    split by chamber reads ground floor first, as you'd walk the house.
    """
    configured = clean_chambers(cfg.get("chambers"))
    by_id: Dict[str, dict] = {}

    for c in configured:
        by_id[c["id"]] = {
            **c,
            "source": SOURCE_CONFIG,
            "adopted": False,
            "editable": True,
        }

    for room in heating_rooms(cfg) + floor_plan_rooms(cfg):
        rid = room["id"]
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = {
                **room,
                "adopted": False,
                "editable": False,
            }
            continue
        # Configured chamber shadows an adopted room: config wins on presentation,
        # but the id is shared, so flag it and don't lose a level we learned.
        existing["adopted"] = True
        if room.get("level") and not existing.get("level"):
            existing["level"] = room["level"]

    level_index = {l["id"]: l["index"] for l in levels(cfg)}

    def sort_key(c: dict):
        lvl = c.get("level")
        # Chambers on no known level sort last, not first — an unplaced room is
        # less interesting than a placed one.
        return (level_index.get(lvl, 10_000) if lvl else 10_000, c["name"].lower())

    out = sorted(by_id.values(), key=sort_key)
    for c in out:
        c.setdefault("icon", DEFAULT_ICON)
    return out


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


def upsert_chamber(cfg: Dict[str, Any], raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """
    Add or update a chamber in the ``chambers:`` block of ``cfg`` (mutated in place).

    Returns ``(chamber, None)`` on success or ``(None, error)`` on failure.
    Adopting a heating/floor-plan room id here is legitimate and expected — it
    is how you give an adopted room an icon or a level without touching heating.
    """
    chamber = clean_chamber(raw)
    if not chamber:
        return None, "chamber needs a valid id or name"

    existing = cfg.get("chambers")
    if not isinstance(existing, list):
        existing = []
    cleaned = clean_chambers(existing)

    for i, c in enumerate(cleaned):
        if c["id"] == chamber["id"]:
            cleaned[i] = chamber
            break
    else:
        cleaned.append(chamber)

    cfg["chambers"] = cleaned
    return chamber, None


def delete_chamber(cfg: Dict[str, Any], chamber_id: Any) -> tuple[bool, Optional[str]]:
    """
    Remove a chamber from the ``chambers:`` block (``cfg`` mutated in place).

    Refuses to delete a chamber that only exists because heating or the floor
    plan defines it — deleting the config entry would not remove the room, it
    would just reappear on next read with its adopted defaults. Say so rather
    than pretending it worked.
    """
    cid = valid_id(chamber_id)
    if not cid:
        return False, "invalid chamber id"

    cleaned = clean_chambers(cfg.get("chambers"))
    remaining = [c for c in cleaned if c["id"] != cid]
    was_configured = len(remaining) != len(cleaned)

    adopted_ids = {r["id"] for r in heating_rooms(cfg) + floor_plan_rooms(cfg)}
    if cid in adopted_ids:
        if not was_configured:
            return False, (
                f"'{cid}' is defined by heating or the floor plan and cannot be "
                f"deleted here — remove it there instead"
            )
        # It was configured AND adopted: drop the config entry, but the chamber
        # itself survives via adoption. The caller must not report "deleted".
        cfg["chambers"] = remaining
        return False, (
            f"'{cid}' still exists because heating or the floor plan defines it; "
            f"only its Frames-specific settings were cleared"
        )

    if not was_configured:
        return False, f"no chamber '{cid}'"

    cfg["chambers"] = remaining
    return True, None
