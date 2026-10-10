"""Polygon zones on a camera's detection frame: where an object has to be
standing to count. See docs/vision.md §Zones."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

Point = Tuple[float, float]
Box = Tuple[int, int, int, int]

MAX_ZONES = 8
MIN_POINTS, MAX_POINTS = 3, 24


def inside(pt: Point, poly: Sequence[Point]) -> bool:
    """Even-odd ray cast; a point on the boundary may land either way."""
    x, y = pt
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def foot(box: Box) -> Point:
    """Where an object touches the ground: the middle of its box's bottom
    edge. A person's head can be over the pavement while they stand on the
    drive. Nudged up so a box cut off by the frame's edge still lands inside
    a zone drawn to that edge."""
    x0, _y0, x1, y1 = box
    return ((x0 + x1) / 2, y1 - 2)


def prepare(zones: Any, width: int, height: int, groups: List[str]) -> List[Dict[str, Any]]:
    """Config zones (points 0-1 of the frame) as pixel polygons; raises ValueError."""
    out = []
    if not isinstance(zones, list) or len(zones) > MAX_ZONES:
        raise ValueError(f"zones must be a list of at most {MAX_ZONES}")
    for z in zones:
        zid, pts = str(z.get("id") or ""), z.get("points")
        if not zid or not isinstance(pts, list) or not MIN_POINTS <= len(pts) <= MAX_POINTS:
            raise ValueError(f"zone '{zid}' needs {MIN_POINTS}-{MAX_POINTS} points")
        poly = [(min(max(float(p[0]), 0.0), 1.0) * width, min(max(float(p[1]), 0.0), 1.0) * height) for p in pts]
        labels = [g for g in (z.get("labels") or groups) if g in groups]
        out.append({"id": zid, "poly": poly, "labels": labels})
    return out


def bounds(zones: List[Dict[str, Any]], width: int, height: int, pad: float = 0.25) -> Box:
    """The area motion has to touch for a look to be worth it when only zones
    count: their bounding box, padded because an object is taller than its feet."""
    xs = [p[0] for z in zones for p in z["poly"]]
    ys = [p[1] for z in zones for p in z["poly"]]
    px, py = (max(xs) - min(xs)) * pad, (max(ys) - min(ys)) * pad
    return (int(max(min(xs) - px, 0)), int(max(min(ys) - py * 3, 0)),
            int(min(max(xs) + px, width)), int(min(max(ys) + py, height)))


def touches(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
