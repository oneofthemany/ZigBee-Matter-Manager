"""
What a device has already said on its maker's clusters, from zigpy's
attribute cache (which survives restarts and holds every report, including
those no handler was given).

Surfaced as evidence and as state, so a device's vendor attributes are all
visible without waiting for each to be reported again. State is only filled
in, never replaced: a cached value may be older than what a handler holds.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.vendor_cache")

Cached = Tuple[int, int, int, Optional[int], Any]      # ep, cluster, attr, mfr, value

# A level of 0 is what an unused Power Configuration cluster leaves behind.
NO_READING = {"battery": 0, "battery_voltage": 0}


def _plain(v: Any) -> Any:
    v = getattr(v, "value", v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return int(v)       # zigpy's enums and sized ints, as a frame carries them
    return v


def cached(zigpy_dev) -> List[Cached]:
    """Every vendor-scoped attribute value zigpy holds for the device. zigpy
    keeps attributes it has no definition for by id alone: on a maker's
    cluster those carry the node's manufacturer code, as their reports do."""
    from modules.device_facts import VENDOR_CLUSTER_MIN, vendor_scoped
    node_mfr = getattr(getattr(zigpy_dev, "node_desc", None), "manufacturer_code", None)
    out: Dict[Tuple[int, int, int, Optional[int]], Any] = {}
    for ep_id, ep in (getattr(zigpy_dev, "endpoints", None) or {}).items():
        if not ep_id or ep is None:
            continue
        for cid, cluster in (getattr(ep, "in_clusters", None) or {}).items():
            cache = getattr(cluster, "_attr_cache", None)
            known = getattr(cache, "_cache", None) or {}
            legacy = getattr(cache, "_legacy_cache", None) or {}
            scope = int(node_mfr) if node_mfr and int(cid) >= VENDOR_CLUSTER_MIN else None
            for aid, item in legacy.items():
                if vendor_scoped(int(cid), scope):
                    out[(ep_id, int(cid), int(aid), scope)] = _plain(item.value)
            for (aid, mfr), item in known.items():
                mfr = int(mfr) if mfr else scope
                if vendor_scoped(int(cid), mfr):
                    out[(ep_id, int(cid), int(aid), mfr)] = _plain(item.value)
    return [(*k, v) for k, v in sorted(out.items(), key=lambda kv: (kv[0][:3], kv[0][3] or 0))]


def _dry_run(device, handler, attr: int, value: Any) -> Dict[str, Any]:
    """The state keys a handler makes of an attribute, without applying them.
    Handlers write through device.update_state, which would publish and fire
    automations on a value that may be hours old."""
    captured: Dict[str, Any] = {}
    own = device.__dict__.get("update_state")
    device.update_state = lambda data, **_: captured.update(data or {})
    try:
        handler.attribute_updated(attr, value)
    except Exception as e:
        logger.debug(f"[{device.ieee}] cached 0x{attr:04X} not decoded: {e}")
    finally:
        if own is None:
            del device.update_state
        else:
            device.update_state = own
    return captured


def seed(device) -> int:
    """Record the cached vendor attributes as observed reports and add the
    state keys they decode to where the device has none. Returns keys added."""
    from modules.device_facts import Fact, _j, attr_subject, is_nonzero, record
    ieee = str(device.ieee)
    rows = cached(device.zigpy_dev)
    if not rows:
        return 0
    facts, added = [], {}
    reported: Dict[Tuple[int, int], set] = {}
    for ep, cid, aid, mfr, value in rows:
        reported.setdefault((ep, cid), set()).add(aid)
        shown = value.hex() if isinstance(value, bytes) else value
        facts.append(Fact(ep, attr_subject("reports", cid, aid, mfr), "observed",
                          _j({"nonzero": is_nonzero(value), "last": shown})))
        handler = (device.handlers or {}).get((ep, cid))
        if handler is None:
            continue
        for key, v in _dry_run(device, handler, aid, value).items():
            held = device.state.get(key)
            if key not in device.state or held is None or NO_READING.get(key, object()) == held:
                added[key] = v
    for (ep, cid), ids in reported.items():
        handler = (device.handlers or {}).get((ep, cid))
        if hasattr(handler, "forget_unreported"):
            handler.forget_unreported(ids)
    if added:
        device.state.update(added)
        service = getattr(device, "service", None)
        if hasattr(service, "state_cache"):
            service.state_cache[ieee] = dict(device.state)
            service._cache_dirty = True
        logger.info(f"[{ieee}] {len(added)} state keys filled from cached vendor attributes: "
                    f"{', '.join(sorted(added))}")
    try:
        record(ieee, facts)
    except Exception as e:
        logger.debug(f"[{ieee}] cached vendor facts not recorded: {e}")
    return len(added)
