"""
Learning recipes (docs/plans/device-learning.md §4): the steps that teach ZMM
a kind of device, as data. Shipped in `learning_recipes/` beside the code, and
the user's own in `<data>/learning_recipes/` (same id: the user's wins).

A recipe orders instructions and names inference operations; loading rejects
anything else, so a recipe cannot run code or write outside its `yields`.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from modules.device_facts import VENDOR_CLUSTER_MIN

logger = logging.getLogger("modules.learning_recipes")

SHIPPED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "learning_recipes")
USER_DIR = os.path.join(os.environ.get("ZMM_DATA_DIR", "./data"), "learning_recipes")

FOR_EACH = ("switch_endpoint", "light_endpoint")          # or "endpoint_with:0xXXXX"
INPUT_TYPES = ("number", "text", "select")
# observe: the user acts while frames are captured. try_write: ZMM flips a
# setting. history, lookup: concluded from what is already recorded (lookup
# with the user's inputs). discover: the device is asked what it has.
# write_probe: each unlisted attribute's own value is written back to learn
# whether it can be written.
# find_all: discover then write_probe, as one step.
MODES = ("observe", "try_write", "history", "lookup", "discover", "write_probe", "find_all")
# Steps that question the device over many seconds and infer nothing.
BACKGROUND = ("discover", "write_probe", "find_all")
# simple: shown to everyone. advanced: the parts a simple step runs, and
# experiments that need some knowledge of the device; shown when asked for.
LEVELS = ("simple", "advanced")
WINDOW_S = (5, 900)
_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")


def _hex(v) -> Optional[int]:
    try:
        return int(str(v), 16)
    except (TypeError, ValueError):
        return None


def validate(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The recipe normalised, or None if any part is malformed."""
    from modules.learning_ops import OPS
    if not isinstance(raw, dict) or not _ID.fullmatch(str(raw.get("id") or "")):
        return None
    aw = raw.get("applies_when") or {}
    clusters_any = [_hex(c) for c in aw.get("in_clusters_any") or []]
    clusters_none = [_hex(c) for c in aw.get("not_in_clusters_any") or []]
    if None in clusters_any or None in clusters_none or aw.get("kind") not in (None, "light", "switch"):
        return None
    if not isinstance(aw.get("vendor_cluster", False), bool):
        return None
    device_types = [_hex(d) for d in aw.get("device_types_any") or []]
    if None in device_types or not isinstance(aw.get("battery_powered", False), bool):
        return None
    steps = []
    for st in raw.get("steps") or []:
        if not isinstance(st, dict) or not _ID.fullmatch(str(st.get("id") or "")):
            return None
        fe = st.get("for_each")
        if fe is not None and fe not in FOR_EACH and not (
                isinstance(fe, str) and fe.startswith("endpoint_with:") and _hex(fe.split(":", 1)[1])):
            return None
        inputs = []
        for i in st.get("inputs") or []:
            if not isinstance(i, dict) or not _ID.fullmatch(str(i.get("name") or "")) \
                    or i.get("type") not in INPUT_TYPES:
                return None
            inputs.append({"name": i["name"], "label": str(i.get("label") or i["name"]),
                           "type": i["type"], "options": [str(o) for o in i.get("options") or []],
                           "min": i.get("min")})
        infer = []
        for op in st.get("infer") or []:
            yields = op.get("yields") if isinstance(op, dict) else None
            yields = [yields] if isinstance(yields, str) else yields
            if op.get("op") not in OPS or not yields or not all(isinstance(y, str) for y in yields):
                return None
            infer.append({"op": op["op"], "yields": yields})
        window = int(st.get("window_s") or 30)
        mode = st.get("mode") or "observe"
        if not (WINDOW_S[0] <= window <= WINDOW_S[1]) or (not infer and mode not in BACKGROUND):
            return None
        if st.get("expect_kind") not in (None, "light", "switch"):
            return None
        if st.get("expect") not in (None, "occupied", "clear") or st.get("level", "simple") not in LEVELS:
            return None
        if mode not in MODES or (mode in BACKGROUND and infer) or \
                (mode == "try_write") != (bool(infer) and all(i["op"] == "confirmed_write" for i in infer)):
            return None                 # only a write step may confirm a write, and only that
        watch = st.get("watch") or {}
        steps.append({"id": st["id"], "name": str(st.get("name") or "")[:60], "for_each": fe,
                      "instruction": str(st.get("instruction") or ""),
                      "inputs": inputs, "infer": infer, "window_s": window,
                      "expect_kind": st.get("expect_kind") or "switch",
                      "expect": st.get("expect"), "mode": mode, "level": st.get("level", "simple"),
                      "watch": {k: _hex(v) for k, v in watch.items() if _hex(v) is not None}})
    if not steps:
        return None
    return {"id": raw["id"], "title": str(raw.get("title") or raw["id"]),
            "applies_when": {"in_clusters_any": clusters_any, "not_in_clusters_any": clusters_none,
                             "kind": aw.get("kind"), "device_types_any": device_types,
                             "battery_powered": bool(aw.get("battery_powered", False)),
                             "vendor_cluster": bool(aw.get("vendor_cluster", False))},
            "steps": steps}


def load() -> Dict[str, Dict[str, Any]]:
    """Every valid recipe by id; the user's override a shipped one."""
    out: Dict[str, Dict[str, Any]] = {}
    for d in (SHIPPED_DIR, USER_DIR):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name)) as f:
                    recipe = validate(json.load(f))
            except (OSError, ValueError) as e:
                logger.warning(f"Recipe {name} unreadable: {e}")
                continue
            if recipe is None:
                logger.warning(f"Recipe {name} rejected: malformed")
                continue
            out[recipe["id"]] = recipe
    return out


def yields_allows(patterns: List[str], path: str) -> bool:
    """A proposal's path must fall inside the step's declared yields."""
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(path, p + ".*")
               for p in patterns)


def _eps(device):
    return {e: ep for e, ep in (device.zigpy_dev.endpoints or {}).items() if e and ep is not None}


def _own_clusters(ep) -> set:
    """The EP's server clusters, less the On/Off and Level that only power a
    thermostat, cover or lock: those are not a lamp or a socket to learn."""
    from modules.endpoint_kind import primary_function
    clusters = set(ep.in_clusters or {})
    return clusters - {0x0006, 0x0008} if primary_function(clusters) else clusters


def _kind(device, ep_id) -> Optional[str]:
    h = (device.handlers or {}).get((ep_id, 0x0006))
    k = h.endpoint_kind() if h is not None and hasattr(h, "endpoint_kind") else None
    return k.kind if k else None


def applies(recipe: Dict[str, Any], device) -> bool:
    aw = recipe["applies_when"]
    eps = _eps(device)
    if aw["in_clusters_any"] and not any(_own_clusters(ep) & set(aw["in_clusters_any"])
                                         for ep in eps.values()):
        return False
    if aw["not_in_clusters_any"] and any(set(ep.in_clusters or {}) & set(aw["not_in_clusters_any"])
                                         for ep in eps.values()):
        return False
    if aw["kind"] and not any(_kind(device, e) == aw["kind"] for e in eps):
        return False
    # What the device says it is only offers the steps: the user's walk settles it.
    if aw["device_types_any"] and not any(getattr(ep, "device_type", None) in aw["device_types_any"]
                                          for ep in eps.values()):
        return False
    if aw.get("battery_powered"):
        from modules.device_class import is_mains_powered
        if is_mains_powered(device) is not False:
            return False
    # Any maker's own cluster, by id range: no list of vendors to keep up.
    if aw.get("vendor_cluster") and not any(c >= VENDOR_CLUSTER_MIN for ep in eps.values()
                                            for c in (ep.in_clusters or {})):
        return False
    return True


def expand(recipe: Dict[str, Any], device) -> List[Dict[str, Any]]:
    """The recipe's steps for this device, one per EP where a step repeats."""
    from modules.device_identity import endpoint_label
    eps = _eps(device)
    out = []
    for st in recipe["steps"]:
        fe = st["for_each"]
        if fe is None:
            targets = [None]
        elif fe == "switch_endpoint":
            targets = [e for e in sorted(eps) if _kind(device, e) == "switch"]
        elif fe == "light_endpoint":
            targets = [e for e in sorted(eps) if _kind(device, e) == "light"]
        else:
            cid = _hex(fe.split(":", 1)[1])
            targets = [e for e in sorted(eps) if cid in (eps[e].in_clusters or {})]
        for ep in targets:
            label = (endpoint_label(device, ep) or f"EP{ep}") if ep is not None else "the device"
            out.append({**st, "key": f"{recipe['id']}.{st['id']}" + (f".{ep}" if ep else ""),
                        "recipe": recipe["id"], "ep": ep, "label": label,
                        "instruction": st["instruction"].replace("{label}", label)})
    return out
