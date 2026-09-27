"""
Passive observation of what each endpoint actually reports
(docs/plans/zmm-quirks.md §3.5, §4).

Fed every received frame by ZigbeeService.handle_message, so it sees reports
whichever handler (if any) consumes them. Only Report Attributes frames are
decoded, and a fact is written only on a transition: an attribute's first
report on an EP this run, and its first non-zero value. Everything else is a
dictionary lookup.
"""
from __future__ import annotations

import logging
from typing import Dict, Tuple

logger = logging.getLogger("modules.device_observer")

# (ieee, ep, cluster, attr, mfr) -> nonzero seen this run
_seen: Dict[Tuple, bool] = {}

REPORT_ATTRIBUTES = 0x0A


def _is_report(message: bytes) -> bool:
    """General-command Report Attributes, from the ZCL header alone."""
    if len(message) < 3 or (message[0] & 0x03) != 0:
        return False
    i = 3 if message[0] & 0x04 else 1        # manufacturer code present
    return len(message) > i + 1 and message[i + 1] == REPORT_ATTRIBUTES


def observe(ieee: str, profile: int, cluster: int, src_ep: int, message: bytes) -> int:
    """Record transitions in this frame's reports. Returns facts written."""
    if not profile or not message or not _is_report(message):
        return 0
    import modules.zigbee_cache as zigbee_cache
    if zigbee_cache._db is None:
        return 0
    from modules.device_facts import Fact, _j, attr_subject, is_nonzero, record
    from modules.zcl_decode import parse_zcl

    frame = parse_zcl(bytes(message))
    mfr = int(frame["manufacturer"], 16) if frame.get("manufacturer") else None
    facts = []
    for rec in frame.get("records") or []:
        aid = int(rec["attr"], 16)
        value = rec.get("value")
        key = (ieee, src_ep, cluster, aid, mfr)
        nonzero = is_nonzero(value)
        prev = _seen.get(key)
        if prev is not None and (prev or not nonzero):
            continue
        _seen[key] = nonzero
        facts.append(Fact(src_ep, attr_subject("reports", cluster, aid, mfr), "observed",
                          _j({"nonzero": nonzero, "last": value})))
    if not facts:
        return 0
    try:
        return record(ieee, facts)
    except Exception as e:
        logger.debug(f"[{ieee}] observed facts not recorded: {e}")
        return 0


def forget(ieee: str) -> None:
    for key in [k for k in _seen if k[0] == ieee]:
        del _seen[key]
