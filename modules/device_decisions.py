"""
Conclusions drawn from device evidence, stored with their source and reason
(docs/plans/zmm-quirks.md §5, §8).

A decision is written only when it differs from the stored one, so a
re-announce costs nothing and a change is kept as old -> new and logged.
Writes share the zigbee cache's loop-thread connection, one row at a time.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger("modules.device_decisions")

# ieee -> {(endpoint_id, subject): (value, source, reason)}
_last: Dict[str, Dict[Tuple[int, str], Tuple[str, str, str]]] = {}


def _loaded(ieee: str) -> Dict[Tuple[int, str], Tuple[str, str, str]]:
    if ieee not in _last:
        import modules.zigbee_cache as zigbee_cache
        if zigbee_cache._db is None:
            return {}       # not open yet: warm() pays that, off the loop
        try:
            from modules.zigbee_cache import get_decisions
            _last[ieee] = {(r["endpoint_id"], r["subject"]): (r["value"], r["source"], r["reason"])
                           for r in get_decisions(ieee)}
        except Exception as e:
            logger.debug(f"[{ieee}] stored decisions unavailable: {e}")
            _last[ieee] = {}
    return _last[ieee]


def record(ieee: str, endpoint_id: int, subject: str, value: str,
           source: str, reason: str) -> bool:
    """Store a decision if it changed. Returns True when it did."""
    import modules.zigbee_cache as zigbee_cache
    from modules.device_facts import utc_now

    if zigbee_cache._db is None:
        return False
    known = _loaded(ieee)
    prev = known.get((endpoint_id, subject))
    if prev == (value, source, reason):
        return False
    now = utc_now()
    value_changed = prev is not None and prev[0] != value
    if value_changed:
        logger.info(f"[{ieee}] EP{endpoint_id} {subject}: {prev[0]} -> {value} ({reason})")
    try:
        zigbee_cache.upsert_decision(ieee, endpoint_id, subject, value, source, reason, now,
                        previous_value=prev[0] if value_changed else _stored_previous(ieee, endpoint_id, subject),
                        changed_at=now if value_changed else None)
    except Exception as e:
        logger.debug(f"[{ieee}] decision not stored: {e}")
        return False
    known[(endpoint_id, subject)] = (value, source, reason)
    return value_changed


def _stored_previous(ieee: str, endpoint_id: int, subject: str) -> Optional[str]:
    """Keep the last real change when only the reason moved."""
    from modules.zigbee_cache import get_decisions
    for r in get_decisions(ieee):
        if r["endpoint_id"] == endpoint_id and r["subject"] == subject:
            return r["previous_value"]
    return None


def stored(ieee: str) -> Dict[Tuple[int, str], Tuple[str, str, str]]:
    """{(endpoint_id, subject): (value, source, reason)} as last recorded."""
    return dict(_loaded(ieee))


def forget(ieee: str) -> None:
    _last.pop(ieee, None)
