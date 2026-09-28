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

logger = logging.getLogger("modules.learning_recipes")

SHIPPED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "learning_recipes")
USER_DIR = os.path.join(os.environ.get("ZMM_DATA_DIR", "./data"), "learning_recipes")

FOR_EACH = ("switch_endpoint", "light_endpoint")          # or "endpoint_with:0xXXXX"
INPUT_TYPES = ("number", "text", "select")
WINDOW_S = (5, 300)
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
        if not (WINDOW_S[0] <= window <= WINDOW_S[1]) or not infer:
            return None
        if st.get("expect_kind") not in (None, "light", "switch"):
            return None
        mode = st.get("mode") or "observe"
        if mode not in ("observe", "try_write") or \
                (mode == "try_write") != all(i["op"] == "confirmed_write" for i in infer):
            return None                 # only a write step may confirm a write, and only that
        watch = st.get("watch") or {}
        steps.append({"id": st["id"], "for_each": fe, "instruction": str(st.get("instruction") or ""),
                      "inputs": inputs, "infer": infer, "window_s": window,
                      "expect_kind": st.get("expect_kind") or "switch", "mode": mode,
                      "watch": {k: _hex(v) for k, v in watch.items() if _hex(v) is not None}})
    if not steps:
        return None
    return {"id": raw["id"], "title": str(raw.get("title") or raw["id"]),
            "applies_when": {"in_clusters_any": clusters_any, "not_in_clusters_any": clusters_none,
                             "kind": aw.get("kind")},
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


def _kind(device, ep_id) -> Optional[str]:
    h = (device.handlers or {}).get((ep_id, 0x0006))
    k = h.endpoint_kind() if h is not None and hasattr(h, "endpoint_kind") else None
    return k.kind if k else None


def applies(recipe: Dict[str, Any], device) -> bool:
    aw = recipe["applies_when"]
    eps = _eps(device)
    if aw["in_clusters_any"] and not any(set(ep.in_clusters or {}) & set(aw["in_clusters_any"])
                                         for ep in eps.values()):
        return False
    if aw["not_in_clusters_any"] and any(set(ep.in_clusters or {}) & set(aw["not_in_clusters_any"])
                                         for ep in eps.values()):
        return False
    if aw["kind"] and not any(_kind(device, e) == aw["kind"] for e in eps):
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
