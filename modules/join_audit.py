"""
Every trust-center join verdict on record (docs/mesh-health.md §1).

The EZSP radio reports each join, rejoin and leave it handles to bellows'
`_handle_tc_join_handler`, which drops a DENIED one without a word: a device
refused on rejoin left no trace. `install` wraps that callback on the running
application, so each event is logged and put in the logbook before bellows
sees it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("modules.join_audit")

_MARK = "_zmm_join_audit"


def _name(v: Any) -> str:
    return str(getattr(v, "name", None) or v)


def kind_of(status: str) -> str:
    """What the radio's device-update status says happened."""
    s = status.upper()
    if s == "DEVICE_LEFT":
        return "leave"
    if "UNSECURED_REJOIN" in s:
        return "unsecured rejoin"
    if "SECURED_REJOIN" in s:
        return "secured rejoin"
    if "UNSECURED_JOIN" in s:
        return "join"
    return s.lower()


def describe(app, nwk, ieee, status, decision, parent_nwk) -> Dict[str, Any]:
    """One join event as plain data, the parent named where the app knows it."""
    st, dec = _name(status), _name(decision)
    parent_ieee = None
    try:
        parent_ieee = str(app.get_device(nwk=parent_nwk).ieee)
    except Exception:
        pass
    kind = kind_of(st)
    return {"ieee": str(ieee), "nwk": f"0x{int(nwk):04X}", "kind": kind, "status": st,
            "decision": None if kind == "leave" else dec,
            "denied": kind != "leave" and "DENY" in dec.upper(),
            "parent_nwk": f"0x{int(parent_nwk):04X}" if parent_nwk is not None else None,
            "parent_ieee": parent_ieee}


def message(ev: Dict[str, Any], name_of: Callable[[Optional[str]], Optional[str]]) -> str:
    via = name_of(ev["parent_ieee"]) or ev["parent_ieee"] or ev["parent_nwk"]
    head = f"[{ev['ieee']}] ({name_of(ev['ieee']) or 'Unknown'}) {ev['kind']} via {via} as {ev['nwk']}"
    if ev["kind"] == "leave":
        return head
    verdict = "REFUSED by the coordinator" if ev["denied"] else "accepted"
    return f"{head}: {verdict} ({ev['decision']})"


def record(service, app, nwk, ieee, status, decision, parent_nwk) -> Dict[str, Any]:
    ev = describe(app, nwk, ieee, status, decision, parent_nwk)
    names = getattr(service, "friendly_names", None) or {}
    text = message(ev, lambda i: names.get(i) if i else None)
    level = "WARNING" if ev["denied"] else "INFO"
    logger.log(logging.WARNING if ev["denied"] else logging.INFO, text)
    emit = getattr(service, "_emit_sync", None)
    if callable(emit):
        emit("log", {"level": level, "message": text, "ieee": ev["ieee"],
                     "device_name": names.get(ev["ieee"], "Unknown"), "category": "connection",
                     "join": ev})
    return ev


def install(app, service) -> bool:
    """Wrap the app's trust-center join callback once. False where the radio
    library has none (only bellows does)."""
    original = getattr(app, "_handle_tc_join_handler", None)
    if not callable(original) or getattr(original, _MARK, False):
        return False

    def audited(nwk, ieee, device_update_status, decision, parent_nwk):
        try:
            record(service, app, nwk, ieee, device_update_status, decision, parent_nwk)
        except Exception as e:
            logger.debug(f"join event for {ieee} not recorded: {type(e).__name__}: {e}")
        return original(nwk, ieee, device_update_status, decision, parent_nwk)

    setattr(audited, _MARK, True)
    app._handle_tc_join_handler = audited
    logger.info("Join audit on: every join, rejoin, leave and refusal is logged")
    return True
