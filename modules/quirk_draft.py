"""
Draft a ZMM entry from a device's evidence and decisions
(docs/plans/zmm-quirks.md §6, §7).

Only what the evidence supports goes in; anything it cannot settle is left
out, never guessed. Writable manufacturer attributes are returned beside the
draft as candidates: a person names them by trying them, then they become
settings.
"""
from __future__ import annotations

import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# Device types that describe a controller (a remote or wall switch sending
# commands). Declared on an EP that takes On/Off commands, the type is wrong.
CONTROLLER_TYPES = frozenset({0x0000, 0x0001, 0x0006, 0x0103, 0x0104, 0x0105, 0x0107, 0x0820})

_ATTR = re.compile(r"^(attr|attr_unsupported|reports):0x([0-9A-F]{4})/0x([0-9A-F]{4})(?:@0x([0-9A-F]{4}))?$")


def _parse(subject: str) -> Optional[Tuple[str, int, int, Optional[int]]]:
    m = _ATTR.match(subject)
    if not m:
        return None
    return m.group(1), int(m.group(2), 16), int(m.group(3), 16), \
        int(m.group(4), 16) if m.group(4) else None


def _facts_by_ep(facts: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for f in facts:
        try:
            out.setdefault(f["endpoint_id"], {})[(f["subject"], f["source"])] = json.loads(f["value"])
        except (TypeError, ValueError):
            continue
    return out


def _answered(ep_facts: Dict, cluster: int, attr: int) -> Optional[Dict]:
    return ep_facts.get((f"attr:0x{cluster:04X}/0x{attr:04X}", "answered"))


def _cluster_answered(ep_facts: Dict, cluster: int) -> bool:
    prefix = f"attr:0x{cluster:04X}/"
    return any(s.startswith(prefix) and src == "answered" for s, src in ep_facts)


def draft_entry(device) -> Dict[str, Any]:
    from modules.device_decisions import stored
    from modules.device_facts import user_facts
    from modules.zigbee_cache import get_facts

    ieee = str(device.ieee)
    zdev = device.zigpy_dev
    facts = _facts_by_ep(get_facts(ieee))
    decisions = stored(ieee)
    user = user_facts(ieee)

    endpoints: Dict[str, Dict[str, Any]] = {}
    kinds, controller_declared = set(), False
    for ep_id, ep in sorted((zdev.endpoints or {}).items()):
        if ep_id == 0 or ep is None:
            continue
        ins = set(getattr(ep, "in_clusters", None) or {})
        e: Dict[str, Any] = {}
        handler = device.handlers.get((ep_id, 0x0006))
        live = handler.endpoint_kind() if handler is not None and hasattr(handler, "endpoint_kind") else None
        kind = live.kind if live else (decisions.get((ep_id, "kind")) or (None,))[0]
        if kind:
            e["kind"] = kind
            kinds.add(kind)
            if int(getattr(ep, "device_type", 0) or 0) in CONTROLLER_TYPES:
                controller_declared = True
        if user.get((ep_id, "label")):
            e["label"] = user[(ep_id, "label")]
        if 0x0012 in ins:
            e["actions"] = "multistate"
        metering = user.get((ep_id, "metering")) or (decisions.get((ep_id, "metering")) or (None,))[0]
        if metering in ("self", "device_total", "none"):
            e["metering"] = metering
        if e:
            endpoints[str(ep_id)] = e

    zmm: Dict[str, Any] = {}
    if controller_declared:
        zmm["corrections"] = {"device_type": "ignore"}
    measurements = _measurements(facts)
    if measurements:
        zmm["measurements"] = measurements
    probes = sorted(os.path.basename(p) for p in
                    glob.glob(os.path.join("data", "probes", f"{ieee.replace(':', '')}_*.json")))
    if probes:
        zmm["evidence"] = {"probes": probes}

    model = str(zdev.model or "")
    metered = any(0x0B04 in (getattr(ep, "in_clusters", None) or {}) or
                  0x0702 in (getattr(ep, "in_clusters", None) or {})
                  for i, ep in (zdev.endpoints or {}).items() if i and ep is not None)
    device_type = "light" if "light" in kinds else ("plug" if metered else "switch")
    caps = sorted(c for c in getattr(getattr(device, "capabilities", None), "get_capabilities",
                                     lambda: set())() if c in ("on_off", "power_monitoring", "light", "switch"))
    return {
        "entry": {
            # With the maker: generic models (Tuya TS0601) name unrelated devices
            "id": f"{model}-{zdev.manufacturer}" if zdev.manufacturer else model,
            "protocol": "zigbee",
            "match": {"model": model, "manufacturer": str(zdev.manufacturer or "")},
            "device_type": device_type, "capabilities": caps,
            "endpoints": endpoints, "zmm": zmm,
            "meta": {"source": "user", "author": "zmm draft"},
        },
        "candidates": _setting_candidates(facts),
    }


def _measurements(facts: Dict[int, Dict]) -> Dict[str, Any]:
    """Electrical measurement and metering, from answered attributes: what the
    device has, its scaling, and what it lacks (null: do not configure)."""
    out: Dict[str, Any] = {}
    em = [ep for ep, f in facts.items() if _cluster_answered(f, 0x0B04)]
    if em:
        f = facts[em[0]]
        if _answered(f, 0x0B04, 0x050B):
            m: Dict[str, Any] = {"cluster": "0x0B04", "attr": "0x050B"}
            for k, attr in (("multiplier", 0x0604), ("divisor", 0x0605)):
                v = (_answered(f, 0x0B04, attr) or {}).get("value")
                if isinstance(v, int) and v:
                    m[k] = v
            out["active_power"] = m
        for name, attr in (("rms_voltage", 0x0505), ("rms_current", 0x0508)):
            if not any(_answered(facts[ep], 0x0B04, attr) for ep in em):
                out[name] = None
    meter = [ep for ep, f in facts.items() if _answered(f, 0x0702, 0x0000)]
    if meter:
        m = {"ep": meter[0], "cluster": "0x0702", "attr": "0x0000"}
        v = (_answered(facts[meter[0]], 0x0702, 0x0302) or {}).get("value")
        if isinstance(v, int) and v:
            m["divisor"] = v
        out["energy"] = m
    return out


def _setting_candidates(facts: Dict[int, Dict]) -> List[Dict[str, Any]]:
    out = []
    for ep, f in sorted(facts.items()):
        for (subject, source), v in sorted(f.items()):
            p = _parse(subject)
            if not p or p[0] != "attr" or source != "answered" or p[3] is None:
                continue
            if "W" not in str((v or {}).get("acl") or ""):
                continue
            out.append({"ep": ep, "cluster": f"0x{p[1]:04X}", "attr": f"0x{p[2]:04X}",
                        "mfr": f"0x{p[3]:04X}", "type": (v or {}).get("type"),
                        "value": (v or {}).get("value")})
    return out
