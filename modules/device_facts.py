"""
Evidence for device recognition: what we know about each endpoint, and how we
learned it (docs/plans/zmm-quirks.md §4).

Rows live in the zigbee cache DB (`device_facts`), written through its write
queue (zigbee_cache.submit): off the event loop, only when a value changed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

logger = logging.getLogger("modules.device_facts")

# Strongest first. Rank is per subject where it matters: a declared cluster is
# solid, a declared device type is the weakest fact we hold.
# `learned`: a result the user demonstrated on this device (device learning),
# below only an explicit correction.
SOURCES = ("user", "learned", "zmm", "observed", "answered", "declared_quirk", "declared_device")
RANK = {s: i for i, s in enumerate(SOURCES)}

DEVICE = 0      # endpoint_id for whole-device facts


class Fact(NamedTuple):
    endpoint_id: int
    subject: str
    source: str
    value: str          # JSON


def _j(v: Any) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def attr_subject(kind: str, cluster: int, attr: int, mfr: Optional[int] = None) -> str:
    """`attr:0x0B04/0x050B`, with `@0x115F` when the id is manufacturer-scoped
    (the same id can mean different things with and without the code)."""
    s = f"{kind}:0x{cluster:04X}/0x{attr:04X}"
    return f"{s}@0x{mfr:04X}" if mfr else s


def facts_from_device(zigpy_dev) -> List[Fact]:
    """What the device (or the zhaquirks quirk replacing it) declares."""
    from device.core import quirk_name_of

    quirk = quirk_name_of(zigpy_dev)
    ep_source = "declared_quirk" if quirk else "declared_device"
    facts = [
        Fact(DEVICE, "model", "declared_device", _j(str(zigpy_dev.model or ""))),
        Fact(DEVICE, "manufacturer", "declared_device", _j(str(zigpy_dev.manufacturer or ""))),
    ]
    if quirk:
        facts.append(Fact(DEVICE, "quirk", "declared_quirk", _j(quirk)))

    nd = getattr(zigpy_dev, "node_desc", None)
    if nd is not None:
        for subject, attr in (("logical_type", "logical_type"),
                              ("mains_powered", "is_mains_powered"),
                              ("manufacturer_code", "manufacturer_code")):
            try:
                v = getattr(nd, attr)
                facts.append(Fact(DEVICE, subject, "declared_device",
                                  _j(int(v) if not isinstance(v, bool) else v)))
            except Exception:
                continue

    for ep_id, ep in (zigpy_dev.endpoints or {}).items():
        if ep_id == 0 or ep is None:
            continue
        for subject, v in (("profile", getattr(ep, "profile_id", None)),
                           ("device_type", getattr(ep, "device_type", None))):
            if v is not None:
                facts.append(Fact(ep_id, subject, ep_source, _j(int(v))))
        for direction, clusters in (("in", getattr(ep, "in_clusters", None) or {}),
                                    ("out", getattr(ep, "out_clusters", None) or {})):
            for cid, cl in clusters.items():
                facts.append(Fact(ep_id, f"cluster_{direction}:0x{int(cid):04X}", ep_source,
                                  _j(type(cl).__name__)))
    return facts


def facts_from_probe(report: Dict[str, Any]) -> List[Fact]:
    """Answered attributes and observed reports from a device_probe report."""
    facts: List[Fact] = []
    for ep_key, ep in (report.get("endpoints") or {}).items():
        ep_id = int(ep_key)
        for cl_key, cl in (ep.get("clusters") or {}).items():
            direction, _, cid_hex = cl_key.partition(" ")
            if direction != "in":
                continue
            cid = int(cid_hex, 16)
            for a_key, a in (cl.get("attributes") or {}).items():
                aid = int(a_key, 16)
                mfr = int(a["mfr"], 16) if a.get("mfr") else None
                status = int(str(a.get("status", "0x00")), 16)
                if status == 0:
                    raw = a.get("raw") or {}
                    facts.append(Fact(ep_id, attr_subject("attr", cid, aid, mfr), "answered",
                                      _j({"type": a.get("discovered_type"), "acl": a.get("acl"),
                                          "value": raw.get("value")})))
                elif status == 0x86:
                    facts.append(Fact(ep_id, attr_subject("attr_unsupported", cid, aid, mfr),
                                      "answered", _j(True)))

    reports: Dict[tuple, Dict[str, Any]] = {}
    for f in report.get("frames") or []:
        # zcl_decode labels a report "0x0A report"
        if f.get("phase") != "listen" or f.get("dir") != "RX" \
                or not str(f.get("command", "")).startswith("0x0A"):
            continue
        cid = int(str(f.get("cluster", "0")), 16)
        for rec in f.get("records") or []:
            key = (int(f.get("src_ep", 0)), cid, int(str(rec.get("attr", "0")), 16))
            seen = reports.setdefault(key, {"count": 0, "nonzero": False})
            seen["count"] += 1
            seen["last"] = rec.get("value")
            seen["nonzero"] = seen["nonzero"] or is_nonzero(rec.get("value"))
    for (ep_id, cid, aid), seen in reports.items():
        facts.append(Fact(ep_id, attr_subject("reports", cid, aid), "observed", _j(seen)))
    return facts


def is_nonzero(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record(ieee: str, facts: Iterable[Fact]) -> int:
    """Queue facts for the writer (zigbee_cache.submit): only changed values are
    written, and an observed `nonzero` never reverts to false."""
    from modules.zigbee_cache import record_facts
    rows = [(f.endpoint_id, f.subject, f.source, f.value) for f in facts]
    return record_facts(ieee, rows, utc_now())


def record_declared(ieee: str, zigpy_dev) -> int:
    """Best-effort: evidence is advisory, so a failure never stops a join."""
    try:
        return record(ieee, facts_from_device(zigpy_dev))
    except Exception as e:
        logger.warning(f"[{ieee}] Declared facts not recorded: {e}")
        return 0


# User corrections: read on every classification, so cached per device and
# kept in step with every write made through here.
_user_cache: Dict[str, Dict[tuple, Any]] = {}


def user_facts(ieee: str) -> Dict[tuple, Any]:
    """{(endpoint_id, subject): value} the user has set for this device."""
    if ieee not in _user_cache:
        import modules.zigbee_cache as zigbee_cache
        if zigbee_cache._db is None:
            return {}       # never pay the DB open here: warm() does, off the loop
        try:
            from modules.zigbee_cache import get_facts
            _user_cache[ieee] = {(r["endpoint_id"], r["subject"]): json.loads(r["value"])
                                 for r in get_facts(ieee) if r["source"] == "user"}
        except Exception as e:
            logger.debug(f"[{ieee}] user facts unavailable: {e}")
            return {}
    return _user_cache[ieee]


def set_user_fact(ieee: str, endpoint_id: int, subject: str, value: Any) -> None:
    record(ieee, [Fact(endpoint_id, subject, "user", _j(value))])
    user_facts(ieee)[(endpoint_id, subject)] = value


def clear_user_fact(ieee: str, endpoint_id: int, subject: str) -> None:
    from modules.zigbee_cache import delete_fact
    delete_fact(ieee, endpoint_id, subject, "user")
    user_facts(ieee).pop((endpoint_id, subject), None)


def forget(ieee: str) -> None:
    """Drop cached corrections, after the device's rows are purged."""
    _user_cache.pop(ieee, None)
