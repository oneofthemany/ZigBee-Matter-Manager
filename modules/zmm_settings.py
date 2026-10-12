"""
Device settings declared by a ZMM entry (docs/plans/zmm-quirks.md §5, step 7).

An entry names a setting (cluster, attribute, manufacturer code, type,
values). It is offered only on endpoints the device has shown the attribute
on — listed by discovery, or seen in a report, which is all a sleepy device
ever gives — and written raw with the entry's manufacturer code, so it works
whether or not zigpy's schema knows the attribute.

A setting's id is also a command: `apply_command` lets any caller (the device
command executor, the heating controller) set "child_lock" on whichever device
has an entry setting of that name, with no knowledge of who made it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.zmm_settings")


def _entry_settings(device) -> List[Dict[str, Any]]:
    from modules.device_profiles import profile_for_device
    return ((profile_for_device(device) or {}).get("zmm") or {}).get("settings") or []


def _listed(device) -> Dict[Tuple[int, str], Any]:
    """{(ep, subject): fact value} for attributes the device has shown: one it
    answered a discovery or read for, or one it reported. A report carries no
    manufacturer code, so it is filed under the plain subject and under every
    manufacturer-scoped form an entry might name it by (`_subject`)."""
    import json
    from modules.zigbee_cache import get_facts
    out: Dict[Tuple[int, str], Any] = {}
    for f in get_facts(str(device.ieee)):
        try:
            value = json.loads(f["value"])
        except ValueError:
            continue
        subject = f["subject"]
        if f["source"] == "answered" and subject.startswith("attr:"):
            out[(f["endpoint_id"], subject)] = value
        elif f["source"] == "observed" and subject.startswith("reports:"):
            seen = {"value": value.get("last")} if isinstance(value, dict) else {}
            out.setdefault((f["endpoint_id"], "seen:" + subject[len("reports:"):]), seen)
    return out


def _shown(listed: Dict[Tuple[int, str], Any], ep: int, st: Dict[str, Any]) -> Optional[Any]:
    """The fact for setting `st` on `ep`: answered first, else a report."""
    hit = listed.get((ep, _subject(st)))
    if hit is None:
        hit = listed.get((ep, f"seen:{st['cluster']}/{st['attr']}"))
    return hit


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
            if _shown(listed, ep, st) is not None:
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
            current = _from_raw(st, (_shown(listed, ep, st) or {}).get("value"))
        # `entry`: the device's own definition offers it, so the UI need not
        # second-guess it with a maker test.
        opt: Dict[str, Any] = {"name": name, "label": label, "description": st["description"],
                               "entry": True,
                               "attribute_id": int(st["attr"], 16), "current_value": current}
        for k in ("min", "max", "step", "unit"):
            if st.get(k) is not None:
                opt[k] = st[k]
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
            shown = updates[name]
            value = _to_raw(st, shown)
            tv = typed_value(SETTING_TYPES[st["type"]], value)
            attr = foundation.Attribute()
            attr.attrid = int(st["attr"], 16)
            attr.value = tv
            mfr = int(st["mfr"], 16) if st.get("mfr") else None
            # Positional: zigpy names this parameter `manufacturer_code`.
            rsp = await cluster.write_attributes_raw([attr], mfr)
            ok = _write_ok(rsp)
        except Exception as e:
            logger.warning(f"[{device.ieee}] ZMM setting {name} write failed: {e}")
            ok = False
        results[name] = ok
        if ok:
            device.state[name] = _from_raw(st, value)
            logger.info(f"[{device.ieee}] ZMM setting {name} = {device.state[name]}")
        else:
            logger.warning(f"[{device.ieee}] ZMM setting {name} refused by the device")
    return results


def typed_value(type_id: int, value: Any):
    """A ZCL TypeValue zigpy can serialise: the value must be of zigpy's own
    type for the data type, not a bare Python int or bool."""
    from zigpy.zcl import foundation
    tv = foundation.TypeValue()
    tv.type = type_id
    try:
        tv.value = foundation.DataType.from_type_id(type_id).python_type(value)
    except (AttributeError, KeyError, ValueError):
        tv.value = value        # a zigpy without the lookup, or a test stub
    return tv


def _to_raw(st: Dict[str, Any], shown: Any) -> int:
    """The integer the device stores for a value as a person gives it."""
    if isinstance(shown, str):
        s = shown.strip().lower()
        for raw, label in (st.get("values") or {}).items():
            if s == str(label).lower():
                return int(raw, 0)
        if s in ("true", "on", "yes", "lock", "external"):
            return 1
        if s in ("false", "off", "no", "unlock", "internal"):
            return 0
        shown = float(s)
    if isinstance(shown, bool):
        return int(shown)
    return int(round(float(shown) * st["scale"])) if st.get("scale") else int(shown)


def _from_raw(st: Dict[str, Any], raw: Any) -> Any:
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return raw
    if st["type"] == "bool":
        return int(bool(raw))
    return round(raw / st["scale"], 3) if st.get("scale") else raw


def names(device) -> set:
    """Setting names this device takes: what `apply_command` can be asked for."""
    try:
        return {name for name, _, _ in _instances(device)}
    except Exception as e:
        logger.debug(f"[{getattr(device, 'ieee', '?')}] entry settings unavailable: {e}")
        return set()


async def apply_command(device, command: str, value: Any) -> Optional[bool]:
    """Write `value` to the entry setting named `command`. None when the device
    has no such setting, so a caller can fall through to another route."""
    if command not in names(device):
        return None
    return (await apply(device, {command: value})).get(command, False)


def _write_ok(rsp: Any) -> bool:
    """Whether a raw write was accepted. zigpy answers with a schema holding
    `status_records`, a default response holding `status`, or bare lists."""
    records = getattr(rsp, "status_records", None)
    if records is None and hasattr(rsp, "status") and not isinstance(rsp, (list, tuple)):
        records = [rsp]
    if records is None:
        records = rsp
    while isinstance(records, (list, tuple)) and len(records) == 1 \
            and isinstance(records[0], (list, tuple)):
        records = records[0]
    if not isinstance(records, (list, tuple)) or not records:
        return False
    try:
        return all(int(getattr(r, "status", r)) == 0 for r in records)
    except (TypeError, ValueError):
        return False
