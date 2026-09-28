"""
Conclusions drawn from device evidence, stored with their source and reason
(docs/plans/zmm-quirks.md §5, §8).

A decision is written only when it differs from the stored one, so a
re-announce costs nothing and a change is kept as old -> new and logged.
Writes go through the zigbee cache's write queue, off the event loop.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger("modules.device_decisions")

# ieee -> {(endpoint_id, subject): (value, source, reason, previous_value)}.
# The source of truth between writes: the table lags behind the write queue.
_last: Dict[str, Dict[Tuple[int, str], Tuple[str, str, str, Optional[str]]]] = {}


def _loaded(ieee: str) -> Dict[Tuple[int, str], Tuple[str, str, str, Optional[str]]]:
    if ieee not in _last:
        import modules.zigbee_cache as zigbee_cache
        if zigbee_cache._db is None:
            return {}       # not open yet: warm() pays that, off the loop
        try:
            _last[ieee] = {(r["endpoint_id"], r["subject"]):
                           (r["value"], r["source"], r["reason"], r["previous_value"])
                           for r in zigbee_cache.get_decisions(ieee)}
        except Exception as e:
            logger.debug(f"[{ieee}] stored decisions unavailable: {e}")
            _last[ieee] = {}
    return _last[ieee]


def record(ieee: str, endpoint_id: int, subject: str, value: str,
           source: str, reason: str) -> bool:
    """Store a decision if it changed. Returns True when its value did."""
    import modules.zigbee_cache as zigbee_cache
    from modules.device_facts import utc_now

    if zigbee_cache._db is None:
        return False
    known = _loaded(ieee)
    prev = known.get((endpoint_id, subject))
    if prev is not None and prev[:3] == (value, source, reason):
        return False
    now = utc_now()
    value_changed = prev is not None and prev[0] != value
    # A new reason for the same value keeps the last real change.
    previous = prev[0] if value_changed else (prev[3] if prev else None)
    if value_changed:
        logger.info(f"[{ieee}] EP{endpoint_id} {subject}: {prev[0]} -> {value} ({reason})")
    try:
        zigbee_cache.upsert_decision(ieee, endpoint_id, subject, value, source, reason, now,
                                     previous_value=previous,
                                     changed_at=now if value_changed else None)
    except Exception as e:
        logger.debug(f"[{ieee}] decision not stored: {e}")
        return False
    known[(endpoint_id, subject)] = (value, source, reason, previous)
    return value_changed


def stored(ieee: str) -> Dict[Tuple[int, str], Tuple[str, str, str]]:
    """{(endpoint_id, subject): (value, source, reason)} as last recorded."""
    return {k: v[:3] for k, v in _loaded(ieee).items()}


def forget(ieee: str) -> None:
    _last.pop(ieee, None)
