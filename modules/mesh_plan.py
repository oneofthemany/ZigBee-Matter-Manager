"""
The Zigbee mesh as the floor plan needs it: one link per pair of devices.

Pure: takes the service's ``get_simple_mesh()`` output, returns plain data.
A neighbour table lists each link from both ends, usually with different
LQIs, and only routers report one; both directions are kept on the one link.
docs/heating.md § Mesh on the plan.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: LQI bands, shared with static/js/mesh.js's link colours.
LQI_GOOD = 200
LQI_OK = 150
LQI_WEAK = 100

#: Zigbee neighbour-table relationship: what the neighbour is to the reporter.
RELATIONSHIPS = {0: "parent", 1: "child", 2: "sibling", 3: "none", 4: "previous child"}


def _relationship(value: Any) -> Optional[str]:
    try:
        return RELATIONSHIPS.get(int(value))
    except (TypeError, ValueError):
        text = str(value or "").strip().lower().replace("_", " ")
        return text if text in RELATIONSHIPS.values() else None


def lqi_band(lqi: Optional[float]) -> str:
    if lqi is None:
        return "unknown"
    if lqi >= LQI_GOOD:
        return "good"
    if lqi >= LQI_OK:
        return "ok"
    if lqi >= LQI_WEAK:
        return "weak"
    return "bad"


def merge_links(mesh: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``{nodes: {ieee: {...}}, links: [...]}`` with each pair once.

    A link carries ``a``/``b`` (sorted ieees), ``lqi_ab`` (as ``a`` hears
    ``b``) and ``lqi_ba``, ``lqi`` the worse of the two known, its ``band``,
    and ``rel_ab`` (what ``b`` is to ``a``: parent, child, sibling...) and
    ``rel_ba``. Links to devices the service does not know are dropped.
    """
    mesh = mesh or {}
    nodes: Dict[str, Dict[str, Any]] = {}
    for n in mesh.get("nodes") or []:
        ieee = str(n.get("id") or n.get("ieee_address") or "").lower()
        if not ieee:
            continue
        nodes[ieee] = {
            "ieee": ieee,
            "name": n.get("friendly_name") or ieee,
            "role": n.get("role") or "Unknown",
            "online": bool(n.get("online", True)),
            "lqi": n.get("lqi"),
        }

    pairs: Dict[tuple, Dict[str, Any]] = {}
    for link in mesh.get("links") or []:
        src = str(link.get("source") or "").lower()
        dst = str(link.get("target") or "").lower()
        if not src or not dst or src == dst or src not in nodes or dst not in nodes:
            continue
        a, b = sorted((src, dst))
        entry = pairs.setdefault((a, b), {"a": a, "b": b, "lqi_ab": None, "lqi_ba": None,
                                          "rel_ab": None, "rel_ba": None})
        # The reporter is the one hearing its neighbour.
        forward = src == a
        entry["lqi_ab" if forward else "lqi_ba"] = link.get("lqi")
        entry["rel_ab" if forward else "rel_ba"] = _relationship(link.get("relationship"))

    links: List[Dict[str, Any]] = []
    for entry in pairs.values():
        known = [v for v in (entry["lqi_ab"], entry["lqi_ba"]) if v is not None]
        entry["lqi"] = min(known) if known else None
        entry["band"] = lqi_band(entry["lqi"])
        entry["online"] = nodes[entry["a"]]["online"] and nodes[entry["b"]]["online"]
        links.append(entry)
    links.sort(key=lambda l: (l["a"], l["b"]))
    return {"nodes": nodes, "links": links}
