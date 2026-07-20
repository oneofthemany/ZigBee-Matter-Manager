"""
Named places — shared geofences beyond home.

"Home" is a property of each presence user: people can live in different
places, and a lodger's home is not yours. A *place* is the opposite — "the
shops", "the school", "work" mean the same coordinates for everyone in the
household, so they live in one shared registry rather than being duplicated
per person.

Resolution is server-side. The phone registers a geofence per place, but only
so the OS wakes it up on a crossing; the fix it then posts is resolved against
this registry here. That split matters:

  - the phone stays dumb, so changing a radius or adding a place needs no app
    update and no re-pair;
  - one implementation decides where someone is, rather than the phone and the
    hub disagreeing about a boundary;
  - places added while a phone is offline still resolve correctly for the
    fixes it sends afterwards.

Storage: data/places.yaml. Coordinates of *places* are configuration and are
persisted; coordinates of *people* are not, and that asymmetry is deliberate.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("modules.places")

CONFIG_PATH = Path("./data/places.yaml")

DEFAULT_PLACE_RADIUS_M = 150.0

# Android allows 100 geofences per app, and home occupies one of them. Cap
# well below that: every extra place is another region the phone must monitor,
# and the practical limit on useful named places is far lower than the
# technical one.
MAX_PLACES = 32

# Reserved: "home" is derived per-user from their own coordinates, and "away"
# is the absence of any place. Allowing either as a place id would produce two
# different answers to the same question.
RESERVED_IDS = frozenset({"home", "away", "unknown"})


def slugify(s: Any) -> str:
    """Best-effort id from free text; "" when nothing usable survives."""
    s = str(s or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@dataclass
class Place:
    id: str
    name: str
    lat: float
    lon: float
    radius_m: float = DEFAULT_PLACE_RADIUS_M
    enabled: bool = True
    icon: str = "map-marker-alt"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Place":
        pid = slugify(d.get("id") or d.get("name"))
        if not pid:
            raise ValueError("place needs an id or name")
        return Place(
            id=pid,
            name=str(d.get("name") or pid),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            radius_m=float(d.get("radius_m", DEFAULT_PLACE_RADIUS_M)),
            enabled=bool(d.get("enabled", True)),
            icon=str(d.get("icon") or "map-marker-alt"),
        )

    def contains(self, lat: float, lon: float) -> bool:
        return haversine_m(self.lat, self.lon, lat, lon) <= self.radius_m

    def distance_to(self, lat: float, lon: float) -> float:
        return haversine_m(self.lat, self.lon, lat, lon)


class PlaceManager:
    """Shared registry of named places."""

    def __init__(self, config_path: Path = CONFIG_PATH) -> None:
        self.config_path = Path(config_path)
        self.places: Dict[str, Place] = {}

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.config_path.exists():
            logger.info("[places] no config at %s; starting empty", self.config_path)
            return
        try:
            raw = yaml.safe_load(self.config_path.read_text()) or {}
        except Exception as e:                              # noqa: BLE001
            logger.error("[places] could not read %s: %s", self.config_path, e)
            return

        loaded = 0
        for entry in (raw.get("places") or []):
            try:
                p = Place.from_dict(entry)
            except Exception as e:                          # noqa: BLE001
                # One malformed entry must not cost the rest of the file.
                logger.warning("[places] skipping bad entry %r: %s", entry, e)
                continue
            self.places[p.id] = p
            loaded += 1
        logger.info("[places] loaded %d place(s)", loaded)

    def save(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(
                {"places": [p.to_dict() for p in self.places.values()]},
                sort_keys=False,
            ))
            tmp.replace(self.config_path)
        except OSError as e:
            logger.error("[places] save failed: %s", e)

    # -- CRUD --------------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        return [p.to_dict() for p in self.places.values()]

    def get(self, place_id: str) -> Optional[Place]:
        return self.places.get(slugify(place_id))

    def upsert(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            p = Place.from_dict(data)
        except Exception as e:                              # noqa: BLE001
            return {"success": False, "error": f"Bad place: {e}"}

        if p.id in RESERVED_IDS:
            return {"success": False,
                    "error": f"'{p.id}' is reserved — home is per-user and "
                             f"'away' means no place matched."}
        if p.id not in self.places and len(self.places) >= MAX_PLACES:
            return {"success": False, "error": f"Maximum {MAX_PLACES} places"}
        if not (-90 <= p.lat <= 90) or not (-180 <= p.lon <= 180):
            return {"success": False, "error": "Coordinates out of range"}
        if p.radius_m <= 0:
            return {"success": False, "error": "Radius must be positive"}

        self.places[p.id] = p
        self.save()
        return {"success": True, "place": p.to_dict()}

    def delete(self, place_id: str) -> Dict[str, Any]:
        pid = slugify(place_id)
        if pid not in self.places:
            return {"success": False, "error": "Place not found"}
        del self.places[pid]
        self.save()
        return {"success": True}

    # -- resolution --------------------------------------------------------

    def resolve(self, lat: float, lon: float) -> Optional[Place]:
        """
        The place containing this point, or None.

        Overlapping places are resolved by *distance to centre*, not by radius
        or definition order: standing inside both "town" and "the shops",
        the more specific answer is the one whose centre you are nearest.
        Order-dependent resolution would make the answer depend on the order
        rows happen to sit in a YAML file.
        """
        best: Optional[Place] = None
        best_d = float("inf")
        for p in self.places.values():
            if not p.enabled:
                continue
            d = p.distance_to(lat, lon)
            if d <= p.radius_m and d < best_d:
                best, best_d = p, d
        return best


_manager: Optional[PlaceManager] = None


def get_place_manager() -> Optional[PlaceManager]:
    return _manager


def set_place_manager(m: PlaceManager) -> None:
    global _manager
    _manager = m
