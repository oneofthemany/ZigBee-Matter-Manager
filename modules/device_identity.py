"""
The device identity panel's model: what was decided per endpoint, from which
source and why, and the user's confirmations and corrections
(docs/plans/zmm-quirks.md §8).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.device_identity")

# Subjects a user may set, and the values each accepts (None: free text).
SETTABLE: Dict[str, Optional[frozenset]] = {
    "kind":     frozenset({"light", "switch"}),
    "metering": frozenset({"self", "device_total", "none"}),
    "label":    None,
}
LABEL_MAX = 40


def endpoint_label(device, ep_id: int) -> Optional[str]:
    """The user's label for an endpoint, else its ZMM entry or profile label."""
    from modules.device_facts import user_facts
    from modules.device_profiles import profile_for_device

    label = user_facts(str(device.ieee)).get((ep_id, "label"))
    if not label:
        profile = profile_for_device(device) or {}
        label = ((profile.get("endpoints") or {}).get(str(ep_id)) or {}).get("label")
    return str(label) if label else None


def validate(subject: str, value: Any) -> Optional[str]:
    """Error text, or None when (subject, value) may be stored. None resets."""
    if subject not in SETTABLE:
        return f"'{subject}' cannot be set"
    if value is None:
        return None
    allowed = SETTABLE[subject]
    if allowed is None:
        if not isinstance(value, str) or not value.strip() or len(value) > LABEL_MAX:
            return f"a {subject} is 1-{LABEL_MAX} characters"
        return None
    if subject == "metering":
        from modules.device_profiles import valid_metering
        return None if valid_metering(value) else \
            "metering must be self, device_total, none or measures:<ep>[,<ep>...]"
    return None if value in allowed else f"{subject} must be one of {sorted(allowed)}"


def _iso(ts) -> Optional[str]:
    # Stored naive UTC; say so, so the browser converts to local time.
    return ts.isoformat() + "Z" if ts is not None else None


def identity(device) -> Dict[str, Any]:
    from device.core import quirk_name_of
    from modules.device_facts import RANK, user_facts
    from modules.device_profiles import profile_for_device
    from modules.zigbee_cache import get_decisions, get_facts

    ieee = str(device.ieee)
    zdev = device.zigpy_dev
    profile = profile_for_device(device)
    # Re-run the evidence-driven rules first: their conclusions can move
    # without an announce (the first power report after start).
    for h in set(device.handlers.values()):
        if hasattr(h, "_record_scope"):
            h._record_scope()
    # The in-memory record is current; the table lags the write queue.
    from modules.device_decisions import records
    stored = {(d["endpoint_id"], d["subject"]): d for d in get_decisions(ieee)}
    for (ep, subject), (value, source, reason, previous) in records(ieee).items():
        row = stored.setdefault((ep, subject), {"changed_at": None})
        row.update(value=value, source=source, reason=reason, previous_value=previous)
    user = user_facts(ieee)
    facts = get_facts(ieee)

    endpoints: List[Dict[str, Any]] = []
    for ep_id, ep in sorted((zdev.endpoints or {}).items()):
        if ep_id == 0 or ep is None:
            continue
        decisions = []
        handler = device.handlers.get((ep_id, 0x0006))
        live = handler.endpoint_kind() if handler is not None and hasattr(handler, "endpoint_kind") else None
        subjects = {s for (e, s) in stored if e == ep_id} | ({"kind"} if live else set())
        for subject in sorted(subjects):
            row = stored.get((ep_id, subject)) or {}
            value, source, reason = row.get("value"), row.get("source"), row.get("reason")
            if subject == "kind" and live:
                value, source, reason = live.kind, live.source, live.reason
            decisions.append({
                "subject": subject, "value": value, "source": source, "reason": reason,
                "previous_value": row.get("previous_value"),
                "changed_at": _iso(row.get("changed_at")),
                "user_set": (ep_id, subject) in user,
            })
        endpoints.append({
            "id": ep_id,
            "device_type": f"0x{int(ep.device_type or 0):04X}",
            "in_clusters": [f"0x{int(c):04X}" for c in sorted(ep.in_clusters or {})],
            "label": endpoint_label(device, ep_id),
            "label_user_set": (ep_id, "label") in user,
            "decisions": decisions,
        })

    by_source: Dict[str, int] = {}
    for f in facts:
        by_source[f["source"]] = by_source.get(f["source"], 0) + 1
    return {
        "success": True,
        "ieee": ieee,
        "model": str(zdev.model or ""),
        "manufacturer": str(zdev.manufacturer or ""),
        "quirk": quirk_name_of(zdev),
        "profile": ({"id": profile["id"], "source": (profile.get("meta") or {}).get("source")}
                    if profile else None),
        "facts_by_source": dict(sorted(by_source.items(), key=lambda kv: RANK.get(kv[0], 99))),
        "endpoints": endpoints,
    }


def apply_user_fact(device, endpoint_id: int, subject: str, value: Any) -> Optional[str]:
    """Store (or, for None, reset) a user fact and drop the cached conclusions
    that read it. Error text, or None on success."""
    from modules.device_facts import clear_user_fact, set_user_fact

    err = validate(subject, value)
    if err:
        return err
    if endpoint_id not in (device.zigpy_dev.endpoints or {}):
        return f"endpoint {endpoint_id} not found"
    ieee = str(device.ieee)
    if value is None:
        clear_user_fact(ieee, endpoint_id, subject)
    else:
        set_user_fact(ieee, endpoint_id, subject, value.strip() if isinstance(value, str) else value)
    for h in set(device.handlers.values()):
        if hasattr(h, "_kind"):
            h._kind = None
    if hasattr(device, "capabilities"):
        device.capabilities._detect_capabilities()
    return None
