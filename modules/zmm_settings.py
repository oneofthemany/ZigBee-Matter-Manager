"""
Device settings declared by a ZMM entry (docs/device-profiles.md §4).

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
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.zmm_settings")

ZONES = "zones"         # modules.device_profiles.ZONES
OCTSTR = 0x41

# What a sleeping device was not there for, done the next time it is heard
# from (docs/device-profiles.md §4, "Sleepy devices"). Persisted to QUEUE_PATH.
# Settings to send, {ieee: {name: value as shown}}.
_PENDING: Dict[str, Dict[str, Any]] = {}
# Writes it took whose read-back it missed, {ieee: {name: check}}; see _queue_confirm.
_CONFIRM: Dict[str, Dict[str, Dict[str, Any]]] = {}
# Entry settings to read at the next wake, {ieee: {setting id}}: values a
# caller needs that the device has never reported.
_PENDING_READS: Dict[str, set] = {}
# A full Reconfigure it did not answer.
_RECONFIGURE: set = set()
# A busy sensor wakes every few seconds: one read attempt per this long.
_READ_RETRY_SEC = 600.0
_READ_TRIED: Dict[str, float] = {}
_RECONF_RETRY_SEC = 60.0
_RECONF_TRIED: Dict[str, float] = {}
_FLUSHING: set = set()

import os as _os
QUEUE_PATH = _os.path.join(_os.environ.get("ZMM_DATA_DIR", "./data"), "settings_queue.json")
_LOADED = False


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import json
    import os
    if not os.path.exists(QUEUE_PATH):
        return
    try:
        with open(QUEUE_PATH) as f:
            saved = json.load(f) or {}
    except (OSError, ValueError) as e:
        logger.warning(f"Settings queue {QUEUE_PATH} unreadable, starting empty: {e}")
        return
    for ieee, q in (saved.get("settings") or {}).items():
        _PENDING.setdefault(ieee, {}).update(q)
    for ieee, q in (saved.get("confirm") or {}).items():
        _CONFIRM.setdefault(ieee, {}).update(q)
    for ieee, ids in (saved.get("reads") or {}).items():
        _PENDING_READS.setdefault(ieee, set()).update(ids)
    _RECONFIGURE.update(saved.get("reconfigure") or [])
    waiting = sorted(set(_PENDING) | set(_CONFIRM) | set(_PENDING_READS) | _RECONFIGURE)
    if waiting:
        logger.info(f"Settings queue: {len(waiting)} device(s) have work waiting for their next wake")


_SAVE_LOCK = threading.Lock()
_SAVE_SEQ = {"issued": 0, "written": 0}


def _write_queue(snapshot: Dict[str, Any], seq: int = 0) -> None:
    import json
    import os
    # Writes run on executor threads: an older snapshot must not land last.
    with _SAVE_LOCK:
        if seq and seq < _SAVE_SEQ["written"]:
            return
        _SAVE_SEQ["written"] = seq
        _write_queue_now(snapshot, json, os)


def _write_queue_now(snapshot: Dict[str, Any], json, os) -> None:
    try:
        os.makedirs(os.path.dirname(QUEUE_PATH) or ".", exist_ok=True)
        tmp = f"{QUEUE_PATH}.tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=1, default=str)
        os.replace(tmp, QUEUE_PATH)
    except OSError as e:
        logger.warning(f"Settings queue not saved: {e}")


def _save() -> None:
    """Write the queue atomically, off the loop when there is one."""
    snapshot = {"settings": {k: dict(v) for k, v in _PENDING.items() if v},
                "confirm": {k: dict(v) for k, v in _CONFIRM.items() if v},
                "reads": {k: sorted(v) for k, v in _PENDING_READS.items() if v},
                "reconfigure": sorted(_RECONFIGURE)}
    import asyncio
    with _SAVE_LOCK:
        _SAVE_SEQ["issued"] += 1
        seq = _SAVE_SEQ["issued"]
    try:
        asyncio.get_running_loop().run_in_executor(None, _write_queue, snapshot, seq)
    except RuntimeError:
        _write_queue(snapshot, seq)


def _reads_due(ieee: str) -> bool:
    import time
    return bool(_PENDING_READS.get(ieee)) and time.time() - _READ_TRIED.get(ieee, 0.0) > _READ_RETRY_SEC


def _undelivered(e: Exception) -> bool:
    """Whether a write failed for want of the device, not because it said no."""
    if isinstance(e, TimeoutError):
        return True
    try:
        from zigpy.exceptions import DeliveryError
        return isinstance(e, DeliveryError)
    except ImportError:
        return False


def _sleepy(device) -> bool:
    try:
        return bool(device._is_battery_powered())
    except Exception:
        return False


def pending(device) -> Dict[str, Any]:
    """What waits for this device to wake, to send or to confirm: {name: value as asked}."""
    _ensure_loaded()
    ieee = str(device.ieee)
    return {**(_PENDING.get(ieee) or {}),
            **{n: c.get("shown") for n, c in (_CONFIRM.get(ieee) or {}).items()}}


def _drop(table: Dict[str, Dict[str, Any]], ieee: str, name: str) -> bool:
    queue = table.get(ieee)
    if not queue or name not in queue:
        return False
    queue.pop(name)
    if not queue:
        table.pop(ieee, None)
    return True


def _settle(device, name: str, outcome: str, shown: Any = None) -> None:
    """Record a write's outcome against the queue: `sent` or `refused` clear
    it, `undelivered` holds the value for a sleeping device."""
    _ensure_loaded()
    ieee = str(device.ieee)
    if outcome == "undelivered" and _sleepy(device):
        _drop(_CONFIRM, ieee, name)
        _PENDING.setdefault(ieee, {})[name] = shown
        logger.info(f"[{ieee}] {name} = {shown} waits for the device to wake")
        _save()
        return
    if _drop(_PENDING, ieee, name) | _drop(_CONFIRM, ieee, name):
        _save()


def _queue_confirm(device, name: str, check: Dict[str, Any]) -> None:
    """A write the device took but whose read-back it missed. `check` holds
    where to read (ep, cluster, attr, mfr), the raw value `expected`, what to
    `publish` once it matches, the state `keys` a different read lands in,
    and the value as `shown`."""
    _ensure_loaded()
    ieee = str(device.ieee)
    _drop(_PENDING, ieee, name)
    _CONFIRM.setdefault(ieee, {})[name] = check
    logger.info(f"[{ieee}] {name} = {check.get('shown')} written; reading it back when the device next wakes")
    _save()


def request_read(device, setting_ids) -> None:
    """Read these entry settings the next time the device is heard from."""
    _ensure_loaded()
    ids = {str(s) for s in (setting_ids or []) if s}
    ieee = str(getattr(device, "ieee", "") or "")
    if not ids or not ieee:
        return
    queue = _PENDING_READS.setdefault(ieee, set())
    if not ids <= queue:
        queue |= ids
        logger.info(f"[{ieee}] Will read {sorted(queue)} when the device wakes")
        _save()


def reconfigure_waiting(device) -> bool:
    _ensure_loaded()
    return str(device.ieee) in _RECONFIGURE


def set_reconfigure_waiting(device, waiting: bool) -> None:
    """A full Reconfigure the sleeping device did not answer runs at its next wake."""
    _ensure_loaded()
    ieee = str(device.ieee)
    if waiting == (ieee in _RECONFIGURE):
        return
    if waiting:
        _RECONFIGURE.add(ieee)
        logger.info(f"[{ieee}] The device did not answer; Reconfigure runs when it next wakes")
    else:
        _RECONFIGURE.discard(ieee)
    _save()


def _reconfigure_due(ieee: str) -> bool:
    import time
    return ieee in _RECONFIGURE and time.time() - _RECONF_TRIED.get(ieee, 0.0) > _RECONF_RETRY_SEC


def on_heard(device) -> None:
    """The device just sent something, so it is awake for a moment: send what
    it missed. Called for every frame; does nothing unless something waits."""
    _ensure_loaded()
    ieee = str(device.ieee)
    if ieee in _FLUSHING or not (_PENDING.get(ieee) or _CONFIRM.get(ieee) or _reads_due(ieee)
                                 or _reconfigure_due(ieee)):
        return
    import asyncio
    _FLUSHING.add(ieee)
    try:
        asyncio.get_running_loop().create_task(_flush(device))
    except RuntimeError:
        _FLUSHING.discard(ieee)


async def _flush(device) -> None:
    import time
    _ensure_loaded()
    ieee = str(device.ieee)
    try:
        wanted = dict(_PENDING.get(ieee) or {})
        if wanted:
            logger.info(f"[{ieee}] Awake: sending {len(wanted)} waiting setting(s): {sorted(wanted)}")
            await _send_waiting(device, wanted)
        if _CONFIRM.get(ieee):
            await _check_confirms(device)
        if _reads_due(ieee):
            _READ_TRIED[ieee] = time.time()
            await read_settings(device, set(_PENDING_READS[ieee]))
        if _reconfigure_due(ieee):
            _RECONF_TRIED[ieee] = time.time()
            await _run_reconfigure(device)
    except Exception as e:
        logger.warning(f"[{ieee}] Sending waiting settings failed: {type(e).__name__}: {e}")
    finally:
        _FLUSHING.discard(ieee)


async def _send_waiting(device, wanted: Dict[str, Any]) -> None:
    """As a save would: through the service, so what is taken is stored too."""
    svc = getattr(device, "service", None)
    if callable(getattr(svc, "configure_device", None)):
        await svc.configure_device(str(device.ieee), {"updates": wanted})
        return
    from device.handlers import DeviceHandlerManagerMixin
    await DeviceHandlerManagerMixin.configure(device, {"updates": wanted})


async def _check_confirms(device) -> None:
    ieee = str(device.ieee)
    for name, chk in list((_CONFIRM.get(ieee) or {}).items()):
        ep = (device.zigpy_dev.endpoints or {}).get(int(chk["ep"]))
        cluster = (getattr(ep, "in_clusters", None) or {}).get(int(chk["cluster"])) if ep else None
        status, read = ("unreadable", None) if cluster is None else \
            await _read_back(cluster, int(chk["attr"]), chk.get("mfr"))
        if status == "undelivered":
            return          # back asleep: the rest wait for the next wake
        _drop(_CONFIRM, ieee, name)
        if status == "unreadable":
            _publish(device, chk.get("publish") or {})
            logger.info(f"[{ieee}] {name} = {chk.get('shown')} set; the device does not let it be read back")
        elif _same(read, chk.get("expected")):
            _publish(device, chk.get("publish") or {})
            logger.info(f"[{ieee}] {name} = {chk.get('shown')} confirmed by read-back")
        else:
            _publish(device, {k: read for k in chk.get("keys") or []})
            logger.warning(f"[{ieee}] {name}: wrote {chk.get('shown')}, the device reads back {read}: not set")
    _save()


async def _run_reconfigure(device) -> None:
    """The queued Reconfigure, observed as the button's is (docs/device-ops.md)."""
    svc = getattr(device, "service", None)
    run = getattr(svc, "reconfigure_device", None)
    if callable(run):
        await run(str(device.ieee))


async def read_settings(device, ids) -> Dict[str, Any]:
    """Read entry settings by id into device.state, one request per
    (ep, cluster, mfr) so a brief wake covers them. {id: value read}."""
    import asyncio
    ieee = str(device.ieee)
    groups: Dict[Tuple[int, int, Optional[int]], List[Dict[str, Any]]] = {}
    for st in _entry_settings(device):
        if st["id"] in ids and st["ep"] != "each" and st["type"] != ZONES:
            key = (int(st["ep"]), int(st["cluster"], 16), int(st["mfr"], 16) if st.get("mfr") else None)
            groups.setdefault(key, []).append(st)
    got: Dict[str, Any] = {}
    for (ep, cid, mfr), sts in groups.items():
        ep_obj = (device.zigpy_dev.endpoints or {}).get(ep)
        cluster = (ep_obj.in_clusters or {}).get(cid) if ep_obj else None
        if cluster is None:
            continue
        try:
            async with asyncio.timeout(8.0):
                rsp = await cluster.read_attributes_raw([int(s["attr"], 16) for s in sts], manufacturer=mfr)
        except Exception as e:
            logger.debug(f"[{ieee}] settings read of 0x{cid:04X} failed: {type(e).__name__}: {e}")
            continue
        by_attr = {int(s["attr"], 16): s for s in sts}
        for rec in getattr(rsp, "status_records", None) or []:
            st = by_attr.get(int(rec.attrid))
            if st is None or int(rec.status) != 0 or rec.value is None:
                continue
            got[st["id"]] = _from_raw(st, rec.value.value)
    if got:
        _publish(device, got)
    queue = _PENDING_READS.get(ieee)
    if queue is not None:
        queue -= set(got)
        if not queue:
            _PENDING_READS.pop(ieee, None)
    if got:
        logger.info(f"[{ieee}] Read settings at wake: {got}")
    return got


def _entry_settings(device) -> List[Dict[str, Any]]:
    from modules.device_profiles import profile_for_device
    return ((profile_for_device(device) or {}).get("zmm") or {}).get("settings") or []


def _listed(device) -> Dict[Tuple[int, str], Any]:
    """{(ep, subject): fact value} for attributes the device has shown: one it
    answered a discovery or read for, or one it reported (`seen:`, under the
    subject its frame gave it, with or without a manufacturer code)."""
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
    # Reports are filed with the frame's manufacturer code, or with none.
    for seen in (f"seen:{_subject(st)[len('attr:'):]}", f"seen:{st['cluster']}/{st['attr']}"):
        if hit is None:
            hit = listed.get((ep, seen))
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
        if st["type"] == ZONES and not isinstance(current, int):
            # A mask, from wherever the bytes are: the name may still hold them
            # raw (named once as a reading), else the raw key or the evidence.
            key = _raw_state_key(device, int(st["cluster"], 16), int(st["attr"], 16))
            current = _zones_mask(st, current)
            if current is None and key:
                current = _zones_mask(st, device.state.get(key))
        if current is None:
            listed = listed if listed is not None else _listed(device)
            current = _from_raw(st, (_shown(listed, ep, st) or {}).get("value"))
        waiting = pending(device)
        description = st["description"]
        if name in waiting:
            # What was asked for, so the form keeps it rather than the old value.
            current = waiting[name]
            description = "Waiting for the device to wake to set this. " + description
        # `entry`: the device's own definition offers it, so the UI need not
        # second-guess it with a maker test.
        opt: Dict[str, Any] = {"name": name, "label": label, "description": description.strip(),
                               "entry": True, "pending": name in waiting,
                               "attribute_id": int(st["attr"], 16), "current_value": current}
        for k in ("min", "max", "step", "unit"):
            if st.get(k) is not None:
                opt[k] = st[k]
        if st["type"] == ZONES:
            opt.update(type="zones", bits=st["bits"])
        elif st["type"] == "bool":
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


# A setting counts as set only once the device has taken it: write SUCCESS
# and a read-back that matches (docs/device-profiles.md §4, "Accepted").
# Verdicts: set | unconfirmed (taken, but the device refuses the read) |
# queued (a sleeping device missed the write or its read-back) | failed.
SET, UNCONFIRMED, QUEUED, FAILED = "set", "unconfirmed", "queued", "failed"
TAKEN = (SET, UNCONFIRMED)


def _plain(v: Any) -> Any:
    v = getattr(v, "value", v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return int(v)       # zigpy's sized ints and enums
    return v


def _same(read: Any, written: Any) -> bool:
    a, b = _plain(read), _plain(written)
    if isinstance(a, (bytes, bytearray, str)) or isinstance(b, (bytes, bytearray, str)):
        oa, ob = _octets(a), _octets(b)
        return oa == ob if oa is not None and ob is not None else str(a) == str(b)
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _jsonable(v: Any) -> Any:
    v = _plain(v)
    return v.hex() if isinstance(v, (bytes, bytearray)) else v


async def _read_back(cluster, attr_id: int, mfr: Optional[int]) -> Tuple[str, Any]:
    """('ok', value) | ('unreadable', None): the device refuses the read |
    ('undelivered', None): it was not there to answer."""
    import asyncio
    reader = getattr(cluster, "read_attributes_raw", None)
    if reader is None:
        return "unreadable", None
    try:
        async with asyncio.timeout(8.0):
            rsp = await reader([attr_id], manufacturer=mfr)
    except Exception as e:
        return ("undelivered" if _undelivered(e) else "unreadable"), None
    records = getattr(rsp, "status_records", None)
    if records is None:
        records = rsp
        while isinstance(records, (list, tuple)) and len(records) == 1 \
                and isinstance(records[0], (list, tuple)):
            records = records[0]
    for rec in records if isinstance(records, (list, tuple)) else []:
        if int(getattr(rec, "attrid", attr_id)) != attr_id:
            continue
        try:
            ok = int(getattr(rec, "status", 0)) == 0
        except (TypeError, ValueError):
            ok = False
        if not ok or getattr(rec, "value", None) is None:
            return "unreadable", None
        return "ok", _plain(rec.value)
    return "unreadable", None


async def write_checked(device, cluster, attr_id: int, value: Any, *, mfr: Optional[int] = None,
                        type_id: Optional[int] = None) -> Tuple[str, Any]:
    """Write one attribute and read it back: (outcome, value read) with outcome
    accepted | unconfirmed | mismatch | confirm_later | refused | undelivered.
    With `type_id` the write is raw (no schema lookup); else zigpy types it."""
    from zigpy.zcl import foundation
    try:
        if type_id is not None:
            attr = foundation.Attribute()
            attr.attrid = attr_id
            attr.value = typed_value(type_id, value)
            # Positional: zigpy names this parameter `manufacturer_code`.
            rsp = await cluster.write_attributes_raw([attr], mfr)
        elif mfr is not None:
            rsp = await cluster.write_attributes({attr_id: value}, manufacturer=mfr)
        else:
            rsp = await cluster.write_attributes({attr_id: value})
    except Exception as e:
        logger.warning(f"[{device.ieee}] write 0x{attr_id:04X} failed: {type(e).__name__}: {e}")
        return ("undelivered" if _undelivered(e) else "refused"), None
    if not _write_ok(rsp):
        return "refused", None
    status, read = await _read_back(cluster, attr_id, mfr)
    if status == "undelivered":
        return "confirm_later", None
    if status == "unreadable":
        return "unconfirmed", None
    return ("accepted" if _same(read, value) else "mismatch"), read


def conclude(device, name: str, outcome: str, shown: Any, read: Any, check: Dict[str, Any]) -> str:
    """Settle a write_checked outcome: queue, publish, log and record the step.
    `check` is as _queue_confirm takes it, less `shown`. Returns the verdict."""
    from modules import device_ops
    ieee = str(device.ieee)
    sleepy = _sleepy(device)
    if outcome in ("accepted", "unconfirmed"):
        _settle(device, name, "sent")
        _publish(device, check.get("publish") or {})
        if outcome == "accepted":
            logger.info(f"[{ieee}] {name} = {shown}: set, read back as written")
        else:
            logger.info(f"[{ieee}] {name} = {shown}: set; the device does not let it be read back to confirm")
        device_ops.step(f"Set {name}", True, None if outcome == "accepted" else "unconfirmed")
        return SET if outcome == "accepted" else UNCONFIRMED
    if outcome == "confirm_later" and sleepy:
        _queue_confirm(device, name, {**check, "shown": _jsonable(shown)})
        return QUEUED
    if outcome == "undelivered" and sleepy:
        _settle(device, name, "undelivered", shown)
        return QUEUED
    _settle(device, name, "refused")
    if outcome == "mismatch":
        _publish(device, {k: read for k in check.get("keys") or []})
        logger.warning(f"[{ieee}] {name}: wrote {shown}, the device reads back {read}: not set")
        reason = "mismatch"
    elif outcome == "no_echo":
        logger.warning(f"[{ieee}] {name} = {shown}: the device did not report it back: not set")
        reason = "no echo"
    elif outcome in ("undelivered", "confirm_later"):
        logger.warning(f"[{ieee}] {name}: the device did not answer: not set")
        reason = "timeout"
    else:
        logger.warning(f"[{ieee}] {name} = {shown}: refused by the device")
        reason = "refused"
    device_ops.step(f"Set {name}", False, reason)
    return FAILED


async def handler_setting(handler, name: str, attr_id: int, value: Any, *, cluster=None,
                          mfr: Optional[int] = None, type_id: Optional[int] = None,
                          keys: Optional[List[str]] = None, shown: Any = None) -> str:
    """A handler's setting, written and judged as entry settings are: the
    state `keys` (default [name]) show `shown` only once the device took it."""
    device = handler.device
    cluster = cluster if cluster is not None else handler.cluster
    shown = value if shown is None else shown
    keys = list(keys or [name])
    outcome, read = await write_checked(device, cluster, attr_id, value, mfr=mfr, type_id=type_id)
    check = {"ep": int(cluster.endpoint.endpoint_id), "cluster": int(cluster.cluster_id),
             "attr": attr_id, "mfr": mfr, "expected": _jsonable(value),
             "publish": {k: shown for k in keys}, "keys": keys}
    return conclude(device, name, outcome, shown, read, check)


def verdicts_of(results: Dict[str, Any], device) -> Dict[str, str]:
    """Verdicts from a {name: bool} result, where a False that waits is queued."""
    waiting = pending(device)
    return {n: (v if isinstance(v, str) else SET if v else QUEUED if n in waiting else FAILED)
            for n, v in results.items()}


async def apply(device, updates: Dict[str, Any]) -> Dict[str, bool]:
    """Write every entry setting named in `updates`. {name: taken}."""
    return {n: v in TAKEN for n, v in (await apply_checked(device, updates)).items()}


async def apply_checked(device, updates: Dict[str, Any]) -> Dict[str, str]:
    """Write every entry setting named in `updates`. {name: verdict}."""
    from modules.device_profiles import SETTING_TYPES

    results: Dict[str, str] = {}
    for name, ep, st in _instances(device):
        if name not in updates:
            continue
        cluster = (device.zigpy_dev.endpoints[ep].in_clusters or {}).get(int(st["cluster"], 16))
        if cluster is None:
            results[name] = FAILED
            continue
        cid, aid = int(st["cluster"], 16), int(st["attr"], 16)
        key = _raw_state_key(device, cid, aid)
        try:
            shown = updates[name]
            if st["type"] == ZONES:
                current = device.state.get(key) if key else None
                if current is None:
                    current = (_shown(_listed(device), ep, st) or {}).get("value")
                value = _zones_write(st, current, int(float(shown)))
                if isinstance(current, str):
                    value = value.hex()         # as the cache and the handler's state hold it
                type_id, wire = OCTSTR, bytes.fromhex(value) if isinstance(value, str) else value
            else:
                value = _to_raw(st, shown)
                lo, hi = TYPE_RANGES.get(st["type"], (0, 1))
                if not lo <= value <= hi:
                    raise ValueError(f"{value} does not fit {st['type']} (check the setting's scale and kind)")
                type_id, wire = SETTING_TYPES[st["type"]], value
            mfr = int(st["mfr"], 16) if st.get("mfr") else None
            outcome, read = await write_checked(device, cluster, aid, wire, mfr=mfr, type_id=type_id)
        except Exception as e:
            logger.warning(f"[{device.ieee}] ZMM setting {name} write failed: {type(e).__name__}: {e}")
            outcome, read, value, mfr = ("undelivered" if _undelivered(e) else "refused"), None, None, None
        # The raw key too: the profile recomputes the setting from it, and a
        # stale one would put the old value back on the next report.
        publish = {**({key: value} if key else {}), name: _from_raw(st, value)} if value is not None else {}
        check = {"ep": ep, "cluster": cid, "attr": aid, "mfr": mfr, "expected": _jsonable(value),
                 "publish": publish, "keys": []}
        results[name] = conclude(device, name, outcome, updates[name], read, check)
    return results


def _publish(device, data: Dict[str, Any]) -> None:
    """Record an accepted write as a report would be: through update_state, so
    the browser and MQTT hear of it. The state is set either way, since
    update_state may filter keys and a test stub has none."""
    update = getattr(device, "update_state", None)
    if callable(update):
        try:
            update(dict(data))
        except Exception as e:
            logger.debug(f"[{getattr(device, 'ieee', '?')}] publishing {sorted(data)} failed: {e}")
    device.state.update(data)


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


def _octets(raw: Any) -> Optional[bytes]:
    """An octet string as handlers and the cache hold it: bytes, or their hex."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None
    return None


def _zone_width(st: Dict[str, Any]) -> int:
    return (st["bits"] + 7) // 8


def _zones_mask(st: Dict[str, Any], raw: Any) -> Optional[int]:
    """Zone i on is bit i of the trailing bytes, read big-endian."""
    b = _octets(raw)
    if b is None or len(b) < _zone_width(st):
        return None
    return int.from_bytes(b[-_zone_width(st):], "big") & ((1 << st["bits"]) - 1)


def _zones_write(st: Dict[str, Any], current: Any, mask: int) -> bytes:
    """`current` with its zone bits replaced; the leading bytes are the device's."""
    b = _octets(current)
    if b is None or len(b) < _zone_width(st):
        raise ValueError("the device's current value is not known yet: read it first")
    if not 0 <= mask < (1 << st["bits"]):
        raise ValueError(f"{mask} is more than {st['bits']} zones")
    width = _zone_width(st)
    keep = int.from_bytes(b[-width:], "big") & ~((1 << st["bits"]) - 1)
    return b[:-width] + (keep | mask).to_bytes(width, "big")


def _from_raw(st: Dict[str, Any], raw: Any) -> Any:
    if st["type"] == ZONES:
        return _zones_mask(st, raw)
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


# Unnamed attributes (device-profiles.md, "Every writable attribute"): any the
# device has shown can be written, by discovery or the write probe, offered as
# a control under its id until an entry names it.

RAW_PREFIX = "raw_"
# What each type can hold, so the control refuses what the device would.
TYPE_RANGES = {"uint8": (0, 0xFF), "uint16": (0, 0xFFFF), "uint32": (0, 0xFFFFFFFF),
               "int8": (-0x80, 0x7F), "int16": (-0x8000, 0x7FFF), "enum8": (0, 0xFF)}


def _raw_name(ep: int, cid: int, aid: int) -> str:
    return f"{RAW_PREFIX}{ep}_{cid:04x}_{aid:04x}"


def _writable(device) -> Dict[str, Dict[str, Any]]:
    """{control name: (ep, cluster, attr, mfr, type, value)} for every maker's
    attribute the device has shown to accept writes, of a type ZMM can write."""
    import json
    from modules.device_facts import parse_attr_subject, vendor_scoped
    from modules.device_learning import NEVER_WRITE
    from modules.device_profiles import SETTING_TYPES
    from modules.zigbee_cache import get_facts
    import modules.zigbee_cache as zigbee_cache
    if zigbee_cache._db is None:
        return {}
    out = {}
    for f in get_facts(str(device.ieee)):
        parsed = parse_attr_subject(f["subject"]) if f["source"] == "answered" else None
        if parsed is None or not vendor_scoped(parsed[0], parsed[2]) or parsed[:2] in NEVER_WRITE:
            continue
        try:
            meta = json.loads(f["value"])
        except ValueError:
            continue
        type_name = str((meta or {}).get("type") or "").split("/")[-1]
        if "W" not in str((meta or {}).get("acl") or "") or type_name not in SETTING_TYPES:
            continue
        cid, aid, mfr = parsed
        out[_raw_name(f["endpoint_id"], cid, aid)] = {
            "ep": f["endpoint_id"], "cluster": cid, "attr": aid, "mfr": mfr,
            "type": type_name, "value": meta.get("value")}
    return out


def _raw_state_key(device, cid: int, aid: int) -> Optional[str]:
    """The state key a handler files this attribute under, if it has one."""
    for key in (f"opple_0x{aid:04x}" if cid == 0xFCC0 else None,
                f"attr_{cid:04x}_{aid:04x}", f"cluster_{cid:04x}_attr_{aid:04x}"):
        if key and key in device.state:
            return key
    return None


def raw_options(device, covered) -> List[Dict[str, Any]]:
    """A control for each writable attribute no entry setting or handler
    option already offers (`covered`: their attribute ids)."""
    from modules.device_profiles import profile_for_device
    eps = (profile_for_device(device) or {}).get("endpoints") or {}
    opts = []
    for name, a in sorted(_writable(device).items()):
        if a["attr"] in covered:
            continue
        # Named as a reading but not yet as a setting: show the name it has.
        called = ((((eps.get(str(a["ep"])) or {}).get("clusters") or {}).get(f"0x{a['cluster']:04X}") or {})
                  .get("attributes") or {}).get(f"0x{a['attr']:04X}", {}).get("name")
        key = _raw_state_key(device, a["cluster"], a["attr"])
        current = device.state.get(key) if key else a["value"]
        if isinstance(current, bool):
            current = int(current)
        waiting = pending(device)
        if name in waiting:
            current = waiting[name]
        opt: Dict[str, Any] = {
            "name": name, "entry": True, "pending": name in waiting,
            "label": f"{called} (0x{a['cluster']:04X}/0x{a['attr']:04X})" if called
            else f"0x{a['cluster']:04X}/0x{a['attr']:04X}",
            "group": "unnamed", "attribute_id": a["attr"], "current_value": current,
            "description": ("Waiting for the device to wake to set this. " if name in waiting else "")
            + f"{a['type']}; not named yet, so what it does is not known",
        }
        if a["type"] == "bool":
            opt.update(type="select", options=[{"value": 0, "label": "Off"}, {"value": 1, "label": "On"}])
        else:
            lo, hi = TYPE_RANGES[a["type"]]
            opt.update(type="number", min=lo, max=hi, step=1)
        if a["mfr"]:
            opt["manufacturer_code"] = a["mfr"]
        opts.append(opt)
    return opts


async def apply_raw(device, updates: Dict[str, Any]) -> Dict[str, bool]:
    """Write each `raw_...` control named in `updates`. {name: taken}."""
    return {n: v in TAKEN for n, v in (await apply_raw_checked(device, updates)).items()}


async def apply_raw_checked(device, updates: Dict[str, Any]) -> Dict[str, str]:
    """Write each `raw_...` control named in `updates`. {name: verdict}."""
    from modules.device_profiles import SETTING_TYPES
    results: Dict[str, str] = {}
    wanted = {k: v for k, v in updates.items() if str(k).startswith(RAW_PREFIX)}
    if not wanted:
        return results
    known = _writable(device)
    for name, shown in wanted.items():
        a = known.get(name)
        cluster = (device.zigpy_dev.endpoints[a["ep"]].in_clusters or {}).get(a["cluster"]) if a else None
        if cluster is None:
            results[name] = FAILED
            continue
        value = None
        try:
            value = int(float(shown))
            lo, hi = TYPE_RANGES.get(a["type"], (0, 1))
            if a["type"] == "bool":
                value = int(bool(value))
            elif not lo <= value <= hi:
                raise ValueError(f"{value} is outside {a['type']}")
            outcome, read = await write_checked(device, cluster, a["attr"], value, mfr=a["mfr"],
                                                type_id=SETTING_TYPES[a["type"]])
        except Exception as e:
            logger.warning(f"[{device.ieee}] {name} write failed: {type(e).__name__}: {e}")
            outcome, read = ("undelivered" if _undelivered(e) else "refused"), None
        key = _raw_state_key(device, a["cluster"], a["attr"])
        check = {"ep": a["ep"], "cluster": a["cluster"], "attr": a["attr"], "mfr": a["mfr"],
                 "expected": value, "publish": {key: value} if key and value is not None else {},
                 "keys": [key] if key else []}
        results[name] = conclude(device, name, outcome, shown, read, check)
    return results

