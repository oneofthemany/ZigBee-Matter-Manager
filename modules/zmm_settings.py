"""
Device settings declared by a ZMM entry (docs/plans/zmm-quirks.md §5, step 7).

An entry names a setting (cluster, attribute, manufacturer code, type,
values). It is offered only on endpoints whose discovery listed that
attribute, and written raw with the entry's manufacturer code, so it works
whether or not zigpy's schema knows the attribute.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.zmm_settings")


def _entry_settings(device) -> List[Dict[str, Any]]:
    from modules.device_profiles import profile_for_device
    return ((profile_for_device(device) or {}).get("zmm") or {}).get("settings") or []


def _listed(device) -> Dict[Tuple[int, str], Any]:
    """{(ep, subject): fact value} for attributes the device has answered."""
    import json
    from modules.zigbee_cache import get_facts
    out = {}
    for f in get_facts(str(device.ieee)):
        if f["source"] == "answered" and f["subject"].startswith("attr:"):
            try:
                out[(f["endpoint_id"], f["subject"])] = json.loads(f["value"])
            except ValueError:
                continue
    return out


def _subject(st: Dict[str, Any]) -> str:
    s = f"attr:{st['cluster']}/{st['attr']}"
    return f"{s}@{st['mfr']}" if st.get("mfr") else s


def _instances(device) -> List[Tuple[str, int, Dict[str, Any]]]:
    """(option name, ep, setting) for every entry setting an EP lists."""
    import modules.zigbee_cache as zigbee_cache
    if zigbee_cache._db is None:
        return []
    settings = _entry_settings(device)
    if not settings:
        return []
    listed = _listed(device)
    eps = sorted(e for e in (device.zigpy_dev.endpoints or {}) if e)
    out = []
    for st in settings:
        targets = eps if st["ep"] == "each" else [st["ep"]]
        for ep in targets:
            if (ep, _subject(st)) in listed:
                name = f"{st['id']}_{ep}" if st["ep"] == "each" else st["id"]
                out.append((name, ep, st))
    return out


def options(device) -> List[Dict[str, Any]]:
    """Config-schema options, in the shape handlers return."""
    from modules.device_identity import endpoint_label
    listed = None
    opts = []
    for name, ep, st in _instances(device):
        label = st["label"]
        if st["ep"] == "each":
            label = f"{label} ({endpoint_label(device, ep) or f'EP{ep}'})"
        current = device.state.get(name)
        if current is None:
            listed = listed if listed is not None else _listed(device)
            current = (listed.get((ep, _subject(st))) or {}).get("value")
        opt: Dict[str, Any] = {"name": name, "label": label, "description": st["description"],
                               "attribute_id": int(st["attr"], 16), "current_value": current}
        if st["type"] == "bool":
            opt.update(type="select", options=[{"value": 0, "label": "Off"}, {"value": 1, "label": "On"}])
        elif st["values"]:
            opt.update(type="select", options=[{"value": int(k, 0), "label": v}
                                               for k, v in st["values"].items()])
        else:
            opt.update(type="number")
        if st.get("mfr"):
            opt["manufacturer_code"] = int(st["mfr"], 16)
        opts.append(opt)
    return opts


async def apply(device, updates: Dict[str, Any]) -> Dict[str, bool]:
    """Write every entry setting named in `updates`. {name: succeeded}."""
    from zigpy.zcl import foundation
    from modules.device_profiles import SETTING_TYPES

    results: Dict[str, bool] = {}
    for name, ep, st in _instances(device):
        if name not in updates:
            continue
        cluster = (device.zigpy_dev.endpoints[ep].in_clusters or {}).get(int(st["cluster"], 16))
        if cluster is None:
            results[name] = False
            continue
        try:
            value = int(updates[name])
            tv = foundation.TypeValue()
            tv.type = SETTING_TYPES[st["type"]]
            tv.value = bool(value) if st["type"] == "bool" else value
            attr = foundation.Attribute()
            attr.attrid = int(st["attr"], 16)
            attr.value = tv
            mfr = int(st["mfr"], 16) if st.get("mfr") else None
            rsp = await cluster.write_attributes_raw([attr], manufacturer=mfr)
            ok = _write_ok(rsp)
        except Exception as e:
            logger.warning(f"[{device.ieee}] ZMM setting {name} write failed: {e}")
            ok = False
        results[name] = ok
        if ok:
            device.state[name] = value
            logger.info(f"[{device.ieee}] ZMM setting {name} = {value}")
        else:
            logger.warning(f"[{device.ieee}] ZMM setting {name} refused by the device")
    return results


def _write_ok(rsp: Any) -> bool:
    records = rsp
    while isinstance(records, (list, tuple)) and len(records) == 1 \
            and isinstance(records[0], (list, tuple)):
        records = records[0]
    if not isinstance(records, (list, tuple)) or not records:
        return False
    status: Optional[Any] = getattr(records[0], "status", records[0])
    try:
        return int(status) == 0
    except (TypeError, ValueError):
        return False
