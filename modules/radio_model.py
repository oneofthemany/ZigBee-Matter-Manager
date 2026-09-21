"""
Zigbee signal across the floor plan: learn what this house's walls and floors
cost from the links the mesh measures, then predict RSSI everywhere and say
where a repeater would help.

Pure: plan and mesh in, plain data out. Model, priors and limits:
docs/signal-coverage.md.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.floor_plan import infer_wall_type, placed_devices, polygon_centroid
from modules.mesh_plan import merge_links

# LQI → RSSI. Each radio stack maps differently, so the hub's own devices that
# hear the coordinator directly (where both are recorded) set the line; this is
# the fallback, a mid-range fit across common coordinators.
DEFAULT_CALIBRATION = {"slope": 0.2, "intercept": -95.0}
CAL_MIN_PAIRS = 4
CAL_MIN_SPREAD = 40          # LQI range the pairs must span to fit a slope
CAL_SLOPE_RANGE = (0.05, 0.6)

# RSSI = p0 − 10·n·log10(d) − ext·heavy_walls − int·light_walls − floor·floors.
# Priors are textbook 2.4 GHz indoor values; each is pulled towards the data
# in proportion to how much the data says (a MAP ridge fit).
PARAMS = ("p0", "n", "ext", "int", "floor")
PRIOR = {"p0": -40.0, "n": 2.5, "ext": 10.0, "int": 4.0, "floor": 15.0}
PRIOR_SD = {"p0": 8.0, "n": 0.6, "ext": 5.0, "int": 3.0, "floor": 6.0}
BOUNDS = {"p0": (-80.0, -10.0), "n": (1.6, 5.0), "ext": (0.0, 40.0),
          "int": (0.0, 25.0), "floor": (0.0, 40.0)}
OBS_SD_DB = 6.0              # how far one reading wanders on its own
MIN_DISTANCE_M = 0.5
LEVEL_HEIGHT_M = 2.7         # between floors, where the plan gives no heights

WEAK_DBM = -85.0             # below this a device is struggling
TARGET_DBM = -80.0           # a repeater must bring a device to at least this
MIN_GAIN_DB = 6.0            # ...and improve it by at least this much
UPLINK_DBM = -75.0           # a repeater needs this from the mesh itself
WEAK_LQI = 100               # measured links below this (mesh.js's red band)
MAX_SUGGESTIONS = 3
MIN_SPACING_M = 1.5          # a repeater beside an existing router adds nothing
#: Below this many readings, or above this fit error, the model is a guess.
ROUGH_SAMPLES = 8
ROUGH_RMSE_DB = 10.0
MAX_CELLS = 5000             # per level; the grid coarsens past this, to stay quick on a Pi


# calibration

def fit_calibration(pairs: Sequence[Tuple[float, float]]) -> Dict[str, Any]:
    """Line through (lqi, rssi) pairs, or the default if they cannot pin one."""
    pairs = [(float(l), float(r)) for l, r in pairs if l is not None and r is not None]
    if len(pairs) >= CAL_MIN_PAIRS:
        xs = [p[0] for p in pairs]
        if max(xs) - min(xs) >= CAL_MIN_SPREAD:
            mx = sum(xs) / len(xs)
            my = sum(p[1] for p in pairs) / len(pairs)
            sxx = sum((x - mx) ** 2 for x in xs)
            slope = sum((x - mx) * (y - my) for x, y in pairs) / sxx
            slope = min(max(slope, CAL_SLOPE_RANGE[0]), CAL_SLOPE_RANGE[1])
            return {"slope": round(slope, 4), "intercept": round(my - slope * mx, 2),
                    "pairs": len(pairs), "fitted": True}
    return {**DEFAULT_CALIBRATION, "pairs": len(pairs), "fitted": False}


def lqi_to_rssi(lqi: float, cal: Dict[str, Any]) -> float:
    return cal["intercept"] + cal["slope"] * lqi


def rssi_to_lqi(rssi: float, cal: Dict[str, Any]) -> int:
    return int(max(0, min(255, round((rssi - cal["intercept"]) / cal["slope"]))))


# geometry

def build_geometry(plan: Optional[dict]) -> Dict[str, Any]:
    """Per level: walls as (x1, y1, x2, y2, heavy), rooms, and its height.

    Heavy is external or party — brick; light is internal or unknown.
    """
    levels = sorted((plan or {}).get("levels") or [],
                    key=lambda l: (l.get("floor_above_ground_m") or 0.0, l.get("index") or 0))
    out: Dict[str, Any] = {}
    for rank, lvl in enumerate(levels):
        walls = []
        for w in lvl.get("walls") or []:
            explicit = str(w.get("type") or "").lower() or None
            kind = infer_wall_type(lvl, w, explicit if explicit != "unknown" else None)
            walls.append((w["x1"], w["y1"], w["x2"], w["y2"], kind in ("external", "party")))
        rooms = [{"id": r["id"], "name": r.get("name") or r["id"],
                  "polygon": [tuple(p) for p in r.get("polygon") or []]}
                 for r in lvl.get("rooms") or [] if len(r.get("polygon") or []) >= 3]
        z = lvl.get("floor_above_ground_m")
        out[lvl["id"]] = {"rank": rank, "name": lvl.get("name") or lvl["id"],
                          "z": float(z) if z else rank * LEVEL_HEIGHT_M,
                          "walls": walls, "rooms": rooms}
    return out


def _crosses(p, q, w, eps: float = 0.05) -> bool:
    """Does segment p–q pass through wall w (not merely touch it at an end)?"""
    (x1, y1), (x2, y2) = p, q
    x3, y3, x4, y4 = w[:4]
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(d) < 1e-12:
        return False
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
    seg = math.hypot(x2 - x1, y2 - y1) or 1.0
    return eps / seg < t < 1 - eps / seg and 0.0 <= u <= 1.0


def _walls_between(level: Dict[str, Any], p, q) -> Tuple[int, int]:
    heavy = light = 0
    for w in level["walls"]:
        if _crosses(p, q, w):
            if w[4]:
                heavy += 1
            else:
                light += 1
    return heavy, light


def features(geo: Dict[str, Any], a: Dict[str, Any], b: Dict[str, Any]
             ) -> Tuple[float, float, float, float]:
    """(distance m, heavy walls, light walls, floors) between two placements.

    Across floors, walls are the mean of both floors' walls under the path.
    """
    la, lb = geo[a["level_id"]], geo[b["level_id"]]
    p, q = (a["x"], a["y"]), (b["x"], b["y"])
    dist = max(MIN_DISTANCE_M, math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                                         + (la["z"] - lb["z"]) ** 2))
    ha, sa = _walls_between(la, p, q)
    if a["level_id"] == b["level_id"]:
        return dist, float(ha), float(sa), 0.0
    hb, sb = _walls_between(lb, p, q)
    return dist, (ha + hb) / 2.0, (sa + sb) / 2.0, float(abs(la["rank"] - lb["rank"]))


def predict(model: Dict[str, Any], f: Tuple[float, float, float, float]) -> float:
    d, heavy, light, floors = f
    return (model["p0"] - 10.0 * model["n"] * math.log10(d)
            - model["ext"] * heavy - model["int"] * light - model["floor"] * floors)


# fit

def _solve(a: List[List[float]], b: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting; the system is 5×5 and SPD."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(c + 1, n):
            k = m[r][c] / m[c][c]
            for j in range(c, n + 1):
                m[r][j] -= k * m[c][j]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][j] * x[j] for j in range(r + 1, n))) / m[r][r]
    return x


def fit(samples: Sequence[Tuple[Tuple[float, float, float, float], float]]) -> Dict[str, Any]:
    """MAP fit of the path-loss parameters; with no samples it is the prior.

    Each sample is (features, rssi dBm). Every parameter is shrunk towards its
    prior by its PRIOR_SD, so a house with three links still gets a sane model.
    """
    ata = [[0.0] * 5 for _ in range(5)]
    aty = [0.0] * 5
    for f, y in samples:
        d, heavy, light, floors = f
        x = [1.0, -10.0 * math.log10(d), -heavy, -light, -floors]
        for i in range(5):
            aty[i] += x[i] * y / OBS_SD_DB ** 2
            for j in range(5):
                ata[i][j] += x[i] * x[j] / OBS_SD_DB ** 2
    for i, k in enumerate(PARAMS):
        ata[i][i] += 1.0 / PRIOR_SD[k] ** 2
        aty[i] += PRIOR[k] / PRIOR_SD[k] ** 2
    theta = _solve(ata, aty)
    model = {k: round(min(max(v, BOUNDS[k][0]), BOUNDS[k][1]), 2) for k, v in zip(PARAMS, theta)}
    resid = [y - predict(model, f) for f, y in samples]
    model["samples"] = len(samples)
    model["rmse_db"] = round(math.sqrt(sum(r * r for r in resid) / len(resid)), 1) if resid else None
    # Few readings, or readings that disagree, means the numbers are a guess.
    model["rough"] = (model["samples"] < ROUGH_SAMPLES
                      or (model["rmse_db"] or 0) > ROUGH_RMSE_DB)
    return model


def link_samples(geo, placed: Dict[str, dict], merged: Dict[str, Any],
                 cal: Dict[str, Any]) -> List[Tuple[tuple, float]]:
    """Every direction of every measured link with both ends placed."""
    out = []
    for link in merged["links"]:
        a, b = placed.get(link["a"]), placed.get(link["b"])
        if not a or not b or not link["online"]:
            continue
        f = features(geo, a, b)
        for lqi in (link["lqi_ab"], link["lqi_ba"]):
            if lqi is not None:
                out.append((f, lqi_to_rssi(lqi, cal)))
    return out


# coverage

def _in_poly(x: float, y: float, poly) -> bool:
    inside = False
    for i in range(len(poly)):
        (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % len(poly)]
        if (ya > y) != (yb > y) and x < xa + (y - ya) * (xb - xa) / (yb - ya):
            inside = not inside
    return inside


def _best(geo, model, sources: List[dict], at: dict) -> Tuple[float, Optional[str]]:
    best, who = -200.0, None
    for s in sources:
        if s["ieee"] == at.get("ieee"):
            continue
        v = predict(model, features(geo, s, at))
        if v > best:
            best, who = v, s["ieee"]
    return best, who


def coverage_field(geo, model, sources: List[dict], level_id: str,
                   step: float = 0.5) -> Optional[Dict[str, Any]]:
    """Best predicted RSSI from any source, over the level's rooms.

    Same layout as the editor's thermal fields: ``data[j*nx + i]`` from the
    bottom-left cell at (x0, y0), ``inside`` masking cells outside every room.
    Every cell is computed, including those outside: a hole would make the
    editor's contour trace the walls instead of the signal.
    """
    lvl = geo[level_id]
    pts = [p for r in lvl["rooms"] for p in r["polygon"]]
    if not pts or not sources:
        return None
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    while math.ceil((x1 - x0) / step) * math.ceil((y1 - y0) / step) > MAX_CELLS:
        step *= 1.5
    nx, ny = max(1, math.ceil((x1 - x0) / step)), max(1, math.ceil((y1 - y0) / step))
    data, inside = [], []
    for j in range(ny):
        for i in range(nx):
            cx, cy = x0 + (i + 0.5) * step, y0 + (j + 0.5) * step
            inside.append(1 if any(_in_poly(cx, cy, r["polygon"]) for r in lvl["rooms"]) else 0)
            data.append(round(_best(geo, model, sources,
                                    {"level_id": level_id, "x": cx, "y": cy})[0], 1))
    return {"x0": round(x0, 3), "y0": round(y0, 3), "h": round(step, 3), "nx": nx, "ny": ny,
            "data": data, "inside": inside}


def _candidates(geo) -> List[dict]:
    """Where a plug-in repeater could go: room middles, and along inner walls."""
    out = []
    for level_id, lvl in geo.items():
        for room in lvl["rooms"]:
            cx, cy = polygon_centroid(room["polygon"])
            out.append({"level_id": level_id, "x": cx, "y": cy, "room_id": room["id"]})
        for x1, y1, x2, y2, heavy in lvl["walls"]:
            if heavy:
                continue
            L = math.hypot(x2 - x1, y2 - y1)
            if L < 0.5:
                continue
            nx, ny = -(y2 - y1) / L, (x2 - x1) / L
            k = 0.5
            while k < L:
                for side in (0.3, -0.3):
                    x = x1 + (x2 - x1) * k / L + nx * side
                    y = y1 + (y2 - y1) * k / L + ny * side
                    room = next((r for r in lvl["rooms"] if _in_poly(x, y, r["polygon"])), None)
                    if room:
                        out.append({"level_id": level_id, "x": x, "y": y, "room_id": room["id"]})
                k += 1.0
    return out


def suggest_repeaters(geo, model, sources: List[dict], weak: List[dict]) -> List[dict]:
    """Greedy: the spot that lifts the most weak devices, then the next.

    A spot must hear the mesh at UPLINK_DBM or better, stand clear of the
    devices already relaying, and a device counts as lifted when it reaches
    TARGET_DBM and gains MIN_GAIN_DB. Each chosen repeater joins the sources,
    so the next choice builds on it.
    """
    sources = list(sources)
    remaining = list(weak)
    rooms = {(lid, r["id"]): r["name"] for lid, l in geo.items() for r in l["rooms"]}
    out = []
    cands = _candidates(geo)
    while remaining and len(out) < MAX_SUGGESTIONS:
        best = None
        for c in cands:
            if any(s["level_id"] == c["level_id"]
                   and math.hypot(s["x"] - c["x"], s["y"] - c["y"]) < MIN_SPACING_M
                   for s in sources):
                continue
            uplink, via = _best(geo, model, sources, c)
            if uplink < UPLINK_DBM:
                continue
            fixes = []
            for w in remaining:
                after = predict(model, features(geo, c, w))
                if after >= TARGET_DBM and after - w["dbm"] >= MIN_GAIN_DB:
                    fixes.append({"ieee": w["ieee"], "name": w["name"],
                                  "before_dbm": round(w["dbm"], 1), "after_dbm": round(after, 1)})
            if not fixes:
                continue
            score = (len(fixes), sum(f["after_dbm"] - f["before_dbm"] for f in fixes))
            if best is None or score > best[0]:
                best = (score, c, fixes, uplink, via)
        if best is None:
            break
        _, c, fixes, uplink, via = best
        out.append({"level_id": c["level_id"], "x": round(c["x"], 2), "y": round(c["y"], 2),
                    "room_id": c["room_id"], "room_name": rooms.get((c["level_id"], c["room_id"])),
                    "uplink_dbm": round(uplink, 1), "uplink_via": via, "fixes": fixes})
        sources.append({"ieee": f"suggested:{len(out)}", **c})
        fixed = {f["ieee"] for f in fixes}
        remaining = [w for w in remaining if w["ieee"] not in fixed]
    return out


def analyse(plan: Optional[dict], mesh: Optional[dict], step: float = 0.5) -> Dict[str, Any]:
    """The whole picture for the editor: calibration, learned model, fields,
    weak devices and repeater suggestions."""
    geo = build_geometry(plan)
    merged = merge_links(mesh)
    nodes = merged["nodes"]
    placed = {d["ieee"]: d for d in placed_devices(plan) if d["level_id"] in geo}

    coord = next((i for i, n in nodes.items() if n["role"] == "Coordinator"), None)
    direct = {l["b"] if l["a"] == coord else l["a"]
              for l in merged["links"] if coord in (l["a"], l["b"])}
    cal = fit_calibration([(nodes[i]["lqi"], nodes[i].get("rssi")) for i in direct if i in nodes])

    samples = link_samples(geo, placed, merged, cal)
    # The coordinator's own RSSI of a direct neighbour is a reading in dBm.
    if coord in placed:
        for i in direct:
            rssi = nodes.get(i, {}).get("rssi")
            if rssi is not None and i in placed:
                samples.append((features(geo, placed[coord], placed[i]), float(rssi)))
    model = fit(samples)

    sources = [{"ieee": i, **placed[i]} for i, n in nodes.items()
               if i in placed and n["online"] and n["role"] in ("Router", "Coordinator")]

    best_measured: Dict[str, float] = {}
    for link in merged["links"]:
        if link["online"] and link["lqi"] is not None:
            for i in (link["a"], link["b"]):
                best_measured[i] = max(best_measured.get(i, -1), link["lqi"])
    weak = []
    for i, n in nodes.items():
        if i not in placed or not n["online"] or n["role"] == "Coordinator":
            continue
        at = {"ieee": i, **placed[i]}
        if i in best_measured:
            lqi = best_measured[i]
            dbm, measured = lqi_to_rssi(lqi, cal), True
            if lqi >= WEAK_LQI and dbm >= WEAK_DBM:
                continue
        else:
            dbm, measured = _best(geo, model, sources, at)[0], False
            if dbm >= WEAK_DBM:
                continue
        weak.append({"ieee": i, "name": n["name"], "level_id": placed[i]["level_id"],
                     "x": placed[i]["x"], "y": placed[i]["y"], "dbm": round(dbm, 1),
                     "measured": measured,
                     "lqi": best_measured.get(i)})

    levels = []
    for level_id in geo:
        f = coverage_field(geo, model, sources, level_id, step)
        if f:
            levels.append({"level_id": level_id, "field": f})

    return {
        "calibration": cal,
        "model": model,
        "prior": PRIOR,
        "sources": [s["ieee"] for s in sources],
        "levels": levels,
        "weak": weak,
        "suggestions": suggest_repeaters(geo, model, sources, weak),
        "thresholds": {"weak_dbm": WEAK_DBM, "target_dbm": TARGET_DBM, "uplink_dbm": UPLINK_DBM},
    }
