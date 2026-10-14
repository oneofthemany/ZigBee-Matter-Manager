"""
Device learning sessions (docs/plans/device-learning.md §3).

One session per device: the steps its recipes give it, the raw frames it sends
while the session runs (captured in ZigbeeService.handle_message, before any
handler scales or reinterprets them), and each step's proposals. A step reads
the attributes its operations need at the start and at the end, since a
device may not report within the window. Accepted proposals are recorded as
`learned` facts; nothing applies to the device until an entry is saved.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.device_learning")

from modules.learning_recipes import BACKGROUND

MAX_FRAMES = 5000
READ_TIMEOUT = 6.0

_sessions: Dict[str, "Session"] = {}


class Session:
    def __init__(self, ieee: str, steps: List[Dict[str, Any]]):
        self.ieee = ieee
        self.steps = steps
        self.results: Dict[str, Dict[str, Any]] = {}
        self.frames: deque = deque(maxlen=MAX_FRAMES)
        self.active: Optional[Dict[str, Any]] = None
        self.trial: Optional[Dict[str, Any]] = None     # a write awaiting the user's answer
        self.tried: Dict[str, List[str]] = {}           # step -> attributes already ruled out
        self.started = time.time()

    def step(self, key: str) -> Optional[Dict[str, Any]]:
        return next((s for s in self.steps if s["key"] == key), None)


# capture

def capture(ieee: str, profile: int, cluster: int, src_ep: int, message: bytes) -> None:
    """Keep this device's reports and read responses while it is being learned."""
    session = _sessions.get(ieee)
    if session is None or not profile or not message:
        return
    from modules.zcl_decode import parse_zcl
    frame = parse_zcl(bytes(message))
    cmd = str(frame.get("command", ""))
    if not (cmd.startswith("0x0A") or cmd.startswith("0x01")):
        return
    mfr = int(frame["manufacturer"], 16) if frame.get("manufacturer") else None
    now = time.time()
    for rec in frame.get("records") or []:
        if rec.get("status") not in (None, "0x00"):
            continue
        session.frames.append({"t": now, "ep": src_ep, "cluster": cluster,
                               "attr": int(rec["attr"], 16), "mfr": mfr, "value": rec.get("value")})


# sampling

def _context(device, step: Dict[str, Any]) -> Dict[str, Any]:
    """What the operations may know beyond the frames."""
    import json
    from modules.measurement_sanity import SWITCHED_OUTLET_MAX_W
    from modules.zigbee_cache import get_facts
    writable = {}
    from modules.device_facts import parse_attr_subject, vendor_scoped
    for f in get_facts(str(device.ieee)):
        parsed = parse_attr_subject(f["subject"]) if f["source"] == "answered" else None
        if parsed is None or not vendor_scoped(parsed[0], parsed[2]):
            continue
        try:
            meta = json.loads(f["value"])
        except ValueError:
            continue
        if "W" not in str(meta.get("acl") or ""):
            continue
        cid, aid, mfr = parsed
        type_name = str(meta.get("type") or "").split("/")[-1]
        writable[(f["endpoint_id"], cid, aid, mfr)] = type_name
    ep = step.get("ep")
    switched = ep is not None and 0x0006 in (
        getattr(device.zigpy_dev.endpoints.get(ep), "in_clusters", None) or {})
    return {"writable": writable, "energy_kwh": device.state.get("energy"),
            "max_power_w": SWITCHED_OUTLET_MAX_W if switched else None,
            "power_scale": _power_scale(device)}


# Values a discovery reads back, so the step ends while the device is awake.
DISCOVER_READ_MAX = 96


def _vendor_values(device) -> Tuple[Dict[Any, Any], bool]:
    """({(ep, cluster, attr, mfr): last value known}, the keys a discovery
    listed) from the fact store: what the device answered, and under that
    what it has reported since. Access the write probe settled is marked
    `probed`, so an unlisted attribute stays unlisted once its access is known."""
    import json
    from modules.device_facts import parse_attr_subject, vendor_scoped
    from modules.zigbee_cache import get_facts
    values: Dict[Any, Tuple[Any, Any]] = {}
    listed = set()
    for f in get_facts(str(device.ieee)):
        kind, _, rest = f["subject"].partition(":")
        if (kind, f["source"]) not in (("attr", "answered"), ("reports", "observed")):
            continue
        parsed = parse_attr_subject("attr:" + rest)
        if parsed is None or not vendor_scoped(parsed[0], parsed[2]):
            continue
        try:
            meta = json.loads(f["value"])
        except ValueError:
            continue
        value = meta.get("value" if kind == "attr" else "last") if isinstance(meta, dict) else None
        key = (f["endpoint_id"], *parsed)
        if kind == "attr" and isinstance(meta, dict) and meta.get("acl") is not None \
                and not meta.get("probed"):
            listed.add(key)
        if value is not None and (key not in values or str(f["last_seen"]) >= str(values[key][0])):
            values[key] = (f["last_seen"], value)
    return {k: v for k, (_, v) in values.items()}, listed


def _known_access(device) -> Dict[Any, str]:
    """{(ep, cluster, attr, mfr): access letters} for the maker's attributes whose access is known."""
    import json
    from modules.device_facts import parse_attr_subject, vendor_scoped
    from modules.zigbee_cache import get_facts
    out = {}
    for f in get_facts(str(device.ieee)):
        parsed = parse_attr_subject(f["subject"]) if f["source"] == "answered" else None
        if parsed is None or not vendor_scoped(parsed[0], parsed[2]):
            continue
        try:
            acl = json.loads(f["value"]).get("acl")
        except (ValueError, AttributeError):
            continue
        if acl is not None:
            out[(f["endpoint_id"], *parsed)] = acl
    return out


async def _lookup(device, s: "Session", st: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A step concluded from the evidence already held and what the user enters."""
    from modules.learning_ops import OPS
    from modules.learning_recipes import yields_allows
    key = st["key"]
    if not inputs and (s.results.get(key) or {}).get("inputs"):
        inputs = s.results[key]["inputs"]
    clean, err = _clean_inputs(st, inputs)
    if err:
        return {"success": False, "error": err}
    values, _ = _vendor_values(device)
    ctx = {**_context(device, st), "vendor_values": values, "listed": set(_known_access(device)),
           "state_keys": set(device.state)}
    proposals = []
    for op in st["infer"]:
        try:
            got = OPS[op["op"]]([], {}, {**st, "inputs": clean}, ctx)
        except Exception as e:
            logger.warning(f"[{device.ieee}] {op['op']} failed: {e}")
            got = []
        proposals += [{**p, "op": op["op"]} for p in got
                      if not p["path"] or yields_allows(op["yields"], p["path"])]
    s.results[key] = {"status": "done", "proposals": proposals, "inputs": clean}
    logger.info(f"[{device.ieee}] Learning step {key} against {len(values)} vendor values: "
                + "; ".join(f"{p['confidence']}: {p['evidence']}" for p in proposals))
    return state(device)


async def _background(device, s: "Session", st: Dict[str, Any]) -> Dict[str, Any]:
    """Start a step that questions the device (BACKGROUND modes). It answers
    over many seconds, longer than a request should stay open, so the step
    runs like one the user acts for: `state` shows it running, then its results."""
    key = st["key"]
    if s.active:
        return state(device) if s.active["key"] == key else \
            {"success": False, "error": "finish the running step first"}
    s.results[key] = {"status": "running", "proposals": []}
    s.active = {"key": key, "t0": time.time(), "inputs": {}, "baseline": [], "ctx": {}}
    s.active["task"] = asyncio.create_task(_run_background(device, s, st, s.active))
    return state(device)


async def _run_background(device, s: "Session", st: Dict[str, Any], active: Dict[str, Any]) -> None:
    key = st["key"]
    try:
        proposals = await {"write_probe": _write_probed, "find_all": _found_all}.get(
            st["mode"], _discovered)(device)
    except Exception as e:
        logger.warning(f"[{device.ieee}] {st['mode']} failed: {e}")
        proposals = []
    if s.active is active:
        s.active = None
    if _sessions.get(str(device.ieee)) is not s:
        return
    if proposals:
        s.results[key] = {"status": "done", "proposals": proposals, "inputs": {}}
    else:
        s.results[key] = {"status": "pending", "proposals": [
            {"path": "", "value": None, "confidence": "none",
             "evidence": "the device did not answer: wake it (press its button) and start "
                         "this step again straight away"}]}
    logger.info(f"[{device.ieee}] Learning step {key}: "
                + ("; ".join(p["evidence"] for p in proposals) or "no answer from the device"))


async def _found_all(device) -> List[Dict[str, Any]]:
    """Discovery, then the write probe, summed up in plain words first, with
    each part's own report beneath for whoever wants the detail."""
    found = await _discovered(device)
    if not found:
        return []
    probed = await _write_probed(device)
    values, _ = _vendor_values(device)
    access = _known_access(device)
    writable = sum("W" in a for a in access.values())
    summary = _note("high", f"Found {len(values)} values on this device; {writable} of them can be "
                            "changed. They are now in its Configuration (unnamed ones under 'Unnamed'), "
                            "and the steps below can name them.", "find_all")
    return [summary] + found + probed


def _note(confidence: str, evidence: str, op: str) -> Dict[str, Any]:
    return {"path": "", "value": None, "confidence": confidence, "evidence": evidence, "op": op}


async def _echo(device, key, type_id: int, value: int) -> Optional[bool]:
    """Write `value` to the attribute: True accepted, False refused, None no answer."""
    from zigpy.zcl import foundation
    from modules.zmm_settings import _write_ok, typed_value
    ep, cid, aid, mfr = key
    cluster = (device.zigpy_dev.endpoints[ep].in_clusters or {}).get(cid)
    if cluster is None:
        return None
    attr = foundation.Attribute()
    attr.attrid = aid
    attr.value = typed_value(type_id, value)
    try:
        async with asyncio.timeout(READ_TIMEOUT):
            return _write_ok(await cluster.write_attributes_raw([attr], mfr))
    except Exception as e:
        logger.debug(f"[{device.ieee}] write probe of 0x{cid:04X}/0x{aid:04X} unanswered: {e}")
        return None


async def _write_probed(device) -> List[Dict[str, Any]]:
    """Learn which of the maker's attributes the device reports but does not
    list can be written, the only way such a device allows: each one is read
    (for its value and type), written back as read, and read again. Only
    whole-number and on/off types, never NEVER_WRITE; a refusal or an
    acceptance is recorded as its access, silence as nothing."""
    from modules.device_facts import Fact, _j, attr_subject, record
    from modules.device_profiles import SETTING_TYPES
    from modules.zcl_decode import type_name
    from modules.zigbee_cache import drain
    values, listed = _vendor_values(device)
    access = _known_access(device)
    open_ = [k for k in values if k not in listed and k not in access and k[1:3] not in NEVER_WRITE]
    # Text and bytes are never tried: a write of them is not one ZMM can type safely.
    unknown = sorted((k for k in open_ if isinstance(values[k], int)), key=lambda k: (k[:3], k[3] or 0))
    skipped = len(open_) - len(unknown)
    if not unknown:
        if not values:
            return [_note("none", "nothing is known of this device's own attributes yet: run "
                                  "'Find its attributes' first", "write_probe")]
        unlisted = [k for k in access if k not in listed]
        return [_note("high", "nothing left to check: of the attributes the device reports but does "
                              f"not list, {sum('W' in access[k] for k in unlisted)} can be written and "
                              f"{sum('W' not in access[k] for k in unlisted)} are read-only"
                              + (f"; {skipped} hold text or bytes and are not tried" if skipped else ""),
                      "write_probe")]
    # Read first, for the type the device stores each in: a write must carry it.
    first = {(r["ep"], r["cluster"], r["attr"], r["mfr"]): r for r in await _read(device, unknown)}
    if not first:
        return []
    names = {v: k for k, v in SETTING_TYPES.items()}
    types: Dict[Any, Tuple[str, int]] = {}
    for k in unknown:
        tid = (first.get(k) or {}).get("type")
        if tid is not None and int(tid) in names:
            types[k] = (type_name(int(tid)), int(tid))
        elif k in first:
            skipped += 1
    keys = sorted(types, key=lambda k: (k[:3], k[3] or 0))
    before = {k: first[k]["value"] for k in keys}
    accepted, refused, silent = [], [], [k for k in unknown if k not in first]
    for key in keys:
        value = before.get(key)
        if not isinstance(value, int):
            silent.append(key)
            continue
        ok = await _echo(device, key, types[key][1], int(value))
        (silent if ok is None else accepted if ok else refused).append(key)
    after = {(r["ep"], r["cluster"], r["attr"], r["mfr"]): r["value"]
             for r in await _read(device, accepted)}
    moved = [k for k in accepted if k in after and after[k] != before[k]]
    record(str(device.ieee), [
        Fact(k[0], attr_subject("attr", k[1], k[2], k[3]), "answered",
             _j({"type": types[k][0], "acl": "RW" if k in accepted else "R", "probed": True,
                 "value": after.get(k, before[k])}))
        for k in accepted + refused])
    await drain()               # the settings steps read these facts next
    named = lambda ks: ", ".join(f"0x{k[2]:04X}" for k in ks)
    notes = []
    if accepted:
        notes.append(_note("high", f"{len(accepted)} can be written (their own value was accepted "
                                   f"back): {named(accepted)}", "write_probe"))
    if refused:
        notes.append(_note("high", f"{len(refused)} are read-only (the device refused): "
                                   f"{named(refused)}", "write_probe"))
    if silent:
        notes.append(_note("low", f"{len(silent)} gave no answer, so nothing is known of them: "
                                  f"{named(silent)}. Wake the device and run this again", "write_probe"))
    if moved:
        notes.append(_note("low", "these read differently after their own value was written back; "
                                  f"check the device: {named(moved)}", "write_probe"))
    if skipped:
        notes.append(_note("none", f"{skipped} hold text or bytes and were not tried", "write_probe"))
    logger.info(f"[{device.ieee}] Write probe: accepted {named(accepted) or 'none'}; refused "
                f"{named(refused) or 'none'}; unanswered {named(silent) or 'none'}")
    return notes


async def _discovered(device) -> List[Dict[str, Any]]:
    """Discover and read the maker's attributes, record them as evidence, and
    say what was found. A device may report attributes it does not list (its
    functions, on some): those are read too, since a report proves they exist,
    and recorded without access flags, which only a listing gives. Nothing is
    written to the device."""
    from modules.device_facts import Fact, _j, attr_subject, record
    from modules.probe_lite import discover_vendor
    from modules.zcl_decode import type_name
    found = await discover_vendor(device)
    if not found:
        return []
    order = lambda k: (k[:3], k[3] or 0)
    reported, _ = _vendor_values(device)
    unlisted = sorted((k for k in reported if k not in found and k[:2] in {f[:2] for f in found}),
                      key=order)
    readable = [k for k, meta in sorted(found.items(), key=lambda kv: order(kv[0]))
                if "R" in meta["acl"] or not meta["acl"]]
    # Unlisted first: nothing else will read them, and their values name functions.
    read = {(r["ep"], r["cluster"], r["attr"], r["mfr"]): r
            for r in await _read(device, (unlisted + readable)[:DISCOVER_READ_MAX])}
    facts = [Fact(ep, attr_subject("attr", cid, aid, mfr), "answered",
                  _j({**meta, "value": (read.get((ep, cid, aid, mfr)) or {}).get("value")}))
             for (ep, cid, aid, mfr), meta in found.items()]
    # Access already settled for an unlisted attribute (the write probe) stands.
    access = _known_access(device)
    facts += [Fact(k[0], attr_subject("attr", k[1], k[2], k[3]), "answered",
                   _j({"type": type_name(read[k]["type"]) if read[k].get("type") is not None else None,
                       "acl": access.get(k), "value": read[k]["value"],
                       **({"probed": True} if k in access else {})}))
              for k in unlisted if k in read]
    record(str(device.ieee), facts)
    from modules.zigbee_cache import drain
    await drain()               # the settings steps read these facts next
    notes = []
    for ep, cid in sorted({k[:2] for k in found}):
        mine = {k: m for k, m in found.items() if k[:2] == (ep, cid)}
        writable = sum("W" in m["acl"] for m in mine.values())
        notes.append({"path": "", "value": None, "confidence": "high", "op": "discover",
                      "evidence": f"EP{ep} 0x{cid:04X} lists {len(mine)} attributes, {writable} "
                                  f"writable; {sum(k in read for k in mine)} read"})
        extra = [k for k in unlisted if k[:2] == (ep, cid)]
        unknown = sum(k not in access for k in extra)
        if extra:
            notes.append({"path": "", "value": None, "confidence": "medium", "op": "discover",
                          "evidence": f"EP{ep} 0x{cid:04X} also reports {len(extra)} attributes it "
                                      f"does not list ({sum(k in read for k in extra)} read): "
                                      + (f"whether {unknown} of those can be written is not known"
                                         if unknown else "which can be written is already known")})
    return notes


def _power_scale(device) -> Dict[int, Tuple[int, int]]:
    """Each EP's active-power (multiplier, divisor), as its handler resolved it."""
    return {k[0]: (getattr(h, "_power_multiplier", 1) or 1, getattr(h, "_power_divisor", 1) or 1)
            for k, h in (device.handlers or {}).items()
            if isinstance(k, tuple) and k[1] == 0x0B04}


def _sample_targets(device, step: Dict[str, Any], ctx) -> List[Tuple[int, int, int, Optional[int]]]:
    ops = {i["op"] for i in step["infer"]}
    targets = []
    eps = {e: ep for e, ep in (device.zigpy_dev.endpoints or {}).items() if e and ep is not None}
    if ops & {"which_endpoints_moved", "scale_from_known", "power_follows_switch"}:
        targets += [(e, 0x0B04, 0x050B, None) for e, ep in eps.items() if 0x0B04 in (ep.in_clusters or {})]
    if ops & {"which_endpoint_changed", "power_follows_switch"}:
        targets += [(e, 0x0006, 0x0000, None) for e, ep in eps.items() if 0x0006 in (ep.in_clusters or {})]
    if ops & {"attribute_that_toggled", "attribute_that_became"}:
        targets += [k for k in ctx["writable"] if k[0] in eps]
    return targets


async def _read(device, targets) -> List[Dict[str, Any]]:
    """Read targets by id (raw, so zigpy's schema need not know them)."""
    groups: Dict[Tuple[int, int, Optional[int]], List[int]] = {}
    for ep, cid, aid, mfr in targets:
        groups.setdefault((ep, cid, mfr), []).append(aid)
    out = []
    for (ep, cid, mfr), attrs in groups.items():
        cluster = (device.zigpy_dev.endpoints[ep].in_clusters or {}).get(cid)
        if cluster is None:
            continue
        for i in range(0, len(attrs), 4):
            try:
                async with asyncio.timeout(READ_TIMEOUT):
                    rsp = await cluster.read_attributes_raw(attrs[i:i + 4], manufacturer=mfr)
            except Exception as e:
                logger.debug(f"[{device.ieee}] learning read failed: {e}")
                continue
            for rec in getattr(rsp, "status_records", None) or []:
                if int(rec.status) == 0 and rec.value is not None:
                    v = rec.value.value
                    if isinstance(v, (bytes, bytearray)):
                        v = bytes(v).hex()
                    elif isinstance(v, int):
                        v = int(v)      # zigpy's Bool and enums, as a frame carries them
                    out.append({"t": time.time(), "ep": ep, "cluster": cid,
                                "attr": int(rec.attrid), "mfr": mfr, "value": v,
                                "type": getattr(rec.value, "type", None)})
    return out


# A setting's id is its command name: refused before the user acts, not after.
SETTING_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
SETTING_ID_HELP = ("give the setting a short name first: lower-case letters, digits and "
                   "underscores, e.g. child_lock")

# lifecycle

def start(device) -> Dict[str, Any]:
    from modules.learning_recipes import applies, expand, load
    from modules.vendor_cache import seed
    seed(device)        # what the device has already reported is evidence for the steps
    recipes = [r for r in load().values() if applies(r, device)]
    steps = [s for r in recipes for s in expand(r, device)]
    for s in steps:
        s["title"] = next(r["title"] for r in recipes if r["id"] == s["recipe"])
    tests = [st for st in steps if _is_power_test(st)]
    if tests:
        steps.append({**MAP_STEP, "title": tests[0]["title"]})     # shown with the tests it sums up
    _sessions[str(device.ieee)] = Session(str(device.ieee), steps)
    logger.info(f"[{device.ieee}] Learning started: {len(steps)} steps from "
                f"{', '.join(r['id'] for r in recipes) or 'no recipes'}")
    return state(device)


POWER_TEST_OPS = {"power_follows_switch", "which_endpoints_moved"}
# Built from every power test's movements; accepted like any step's proposals.
MAP_STEP = {"key": "metering_map", "recipe": "metering_map", "name": "Metering map",
            "label": "every socket", "ep": None, "inputs": [], "infer": [], "window_s": 5,
            "mode": "summary", "watch": {},
            "instruction": "Built from every switch test: whose power moved for which socket. "
                           "Accept it once every socket has been tested."}


def _is_power_test(st: Dict[str, Any]) -> bool:
    return st.get("ep") is not None and any(i["op"] in POWER_TEST_OPS for i in st["infer"])


def _update_map(device, s: "Session") -> None:
    from modules.learning_ops import metering_map
    tests = {r["target"]: r["moved"] for r in s.results.values() if r.get("moved")}
    power_eps = sorted(e for e, ep in (device.zigpy_dev.endpoints or {}).items()
                       if e and ep is not None and 0x0B04 in (ep.in_clusters or {}))
    switch_eps = sorted({st["ep"] for st in s.steps if _is_power_test(st)})
    proposals = [{**p, "op": "metering_map"} for p in metering_map(tests, power_eps, switch_eps)]
    if proposals:
        s.results[MAP_STEP["key"]] = {"status": "done", "proposals": proposals}


def state(device) -> Dict[str, Any]:
    s = _sessions.get(str(device.ieee))
    if s is None:
        return {"success": True, "active": False}
    steps = []
    for st in s.steps:
        res = s.results.get(st["key"]) or {}
        steps.append({"key": st["key"], "title": st["title"], "name": st.get("name") or "",
                      "level": st.get("level", "simple"),
                      "label": st["label"],
                      "instruction": st["instruction"], "inputs": st["inputs"],
                      "window_s": st["window_s"], "mode": st.get("mode", "observe"),
                      "status": res.get("status", "pending"), "proposals": res.get("proposals", []),
                      "tried": s.tried.get(st["key"], [])})
    trial = None
    if s.trial:
        t = s.trial
        ep, cid, aid, mfr = t["key"]
        trial = {"step": t["step"], "ep": ep, "cluster": f"0x{cid:04X}", "attr": f"0x{aid:04X}",
                 "old": t["old"], "new": t["new"],
                 "expires_in": max(0, round(t["t0"] + t["timeout"] - time.time()))}
    return {"success": True, "active": True, "running": s.active["key"] if s.active else None,
            "trial": trial, "steps": steps, "live": _live(device, s)}


def _live(device, s: "Session") -> List[Dict[str, Any]]:
    """While a step runs: each EP's latest on/off and power, and whether its
    power has moved since the step began, so the user sees where a load shows."""
    if not s.active or s.active.get("task"):
        return []
    from modules.device_identity import endpoint_label
    from modules.learning_ops import MOVED_SHARE
    t0 = s.active["t0"]
    latest: Dict[Tuple[int, Tuple[int, int]], Any] = {}
    before: Dict[int, float] = {}
    for f in list(s.active["baseline"]) + [f for f in s.frames if f["t"] < t0]:
        if (f["cluster"], f["attr"]) == (0x0B04, 0x050B) and isinstance(f["value"], (int, float)):
            before[f["ep"]] = float(f["value"])
    for f in list(s.active["baseline"]) + list(s.frames):
        latest[(f["ep"], (f["cluster"], f["attr"]))] = f["value"]
    moves = {}
    for f in s.frames:
        if f["t"] >= t0 and (f["cluster"], f["attr"]) == (0x0B04, 0x050B) \
                and isinstance(f["value"], (int, float)):
            moves[f["ep"]] = max(moves.get(f["ep"], 0.0), abs(f["value"] - before.get(f["ep"], 0.0)))
    top = max(moves.values(), default=0.0)
    scale = s.active["ctx"].get("power_scale") or {}
    out = []
    for ep_id in sorted(e for e in (device.zigpy_dev.endpoints or {}) if e):
        raw = latest.get((ep_id, (0x0B04, 0x050B)))
        mult, div = scale.get(ep_id, (1, 1))
        on = latest.get((ep_id, (0x0006, 0x0000)))
        out.append({"ep": ep_id, "label": endpoint_label(device, ep_id) or f"EP{ep_id}",
                    "on": None if on is None else bool(on),
                    "power_w": None if not isinstance(raw, (int, float)) else round(raw * mult / div, 1),
                    "moved": top > 0 and moves.get(ep_id, 0.0) >= MOVED_SHARE * top})
    return out


def _session_step(device, key: str):
    s = _sessions.get(str(device.ieee))
    if s is None:
        return None, None, "no learning session: start one"
    st = s.step(key)
    if st is None:
        return s, None, f"no step {key}"
    return s, st, None


def _clean_inputs(st: Dict[str, Any], inputs: Dict[str, Any]):
    """(inputs checked against the step's specs, error or None)."""
    clean = {}
    for spec in st["inputs"]:
        v = (inputs or {}).get(spec["name"])
        if spec["type"] == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None, f"{spec['label']} must be a number (got {v!r})"
            if spec.get("min") is not None and v < spec["min"]:
                return None, f"{spec['label']} must be at least {spec['min']}"
        elif spec["type"] == "select" and spec["options"] and v not in spec["options"]:
            return None, f"choose {spec['label']}"
        elif spec["type"] == "text":
            v = str(v or "").strip()
        clean[spec["name"]] = v
    return clean, None


async def begin(device, key: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    if st.get("mode") == "try_write":
        return {"success": False, "error": "this step flips settings one at a time: use Try"}
    if st.get("mode") == "summary":
        return {"success": False, "error": "this step is built from the others: run those"}
    if st.get("mode") == "history":
        return await _from_history(device, s, st)
    if st.get("mode") in BACKGROUND:
        return await _background(device, s, st)
    if st.get("mode") == "lookup":
        return await _lookup(device, s, st, inputs)
    if not inputs and (s.results.get(key) or {}).get("inputs"):
        inputs = s.results[key]["inputs"]        # Redo: the same inputs as last time
    clean, err = _clean_inputs(st, inputs)
    if err:
        return {"success": False, "error": err}
    if "setting_id" in clean and not SETTING_ID.fullmatch(clean["setting_id"]):
        return {"success": False, "error": SETTING_ID_HELP}
    ctx = _context(device, st)
    baseline = await _read(device, _sample_targets(device, st, ctx))
    s.active = {"key": key, "t0": time.time(), "inputs": clean, "baseline": baseline, "ctx": ctx}
    s.results[key] = {"status": "running", "proposals": []}
    logger.info(f"[{device.ieee}] Learning step {key} begun: {len(baseline)} of "
                f"{len(_sample_targets(device, st, ctx))} attributes read, inputs {clean}")
    return state(device)


def _history(ieee: str):
    """(vendor records, numeric states, text states) from both stores. Blocking."""
    from modules.telemetry_db import query_device_state_series
    from modules.zigbee_cache import get_vendor_history
    known: Dict[str, list] = {}
    texts: Dict[str, list] = {}
    for t, name, value, numeric in query_device_state_series(ieee):
        if numeric is not None:
            known.setdefault(name, []).append((float(t), float(numeric)))
        elif value is not None:
            texts.setdefault(name, []).append((float(t), str(value)))
    return get_vendor_history(ieee), known, texts


async def _from_history(device, s: "Session", st: Dict[str, Any]) -> Dict[str, Any]:
    """A step nobody acts for: conclude from what the device has already reported."""
    from modules.learning_ops import OPS
    from modules.learning_recipes import yields_allows
    key = st["key"]
    try:
        records, known, texts = await asyncio.to_thread(_history, str(device.ieee))
    except Exception as e:
        logger.warning(f"[{device.ieee}] learning history unavailable: {e}")
        return {"success": False, "error": "the device's history could not be read"}
    ctx = {**_context(device, st), "known": known, "texts": texts}
    proposals = []
    for op in st["infer"]:
        try:
            got = OPS[op["op"]](records, {}, {**st, "inputs": {}}, ctx)
        except Exception as e:
            logger.warning(f"[{device.ieee}] {op['op']} failed: {e}")
            got = []
        proposals += [{**p, "op": op["op"]} for p in got
                      if not p["path"] or yields_allows(op["yields"], p["path"])]
    s.results[key] = {"status": "done", "proposals": proposals, "inputs": {}}
    logger.info(f"[{device.ieee}] Learning step {key} from history: {len(records)} vendor "
                f"values against {len(known)} states; "
                + "; ".join(f"{p['confidence']}: {p['evidence']}" for p in proposals))
    return state(device)


async def finish(device, key: str) -> Dict[str, Any]:
    from modules.learning_ops import OPS
    from modules.learning_recipes import yields_allows
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    if st.get("mode") in BACKGROUND:
        return state(device)        # it ends when the device has answered, not when asked to
    if not s.active or s.active["key"] != key:
        return {"success": False, "error": "that step is not running"}
    a, s.active = s.active, None
    after = await _read(device, _sample_targets(device, st, a["ctx"]))
    window = [f for f in s.frames if f["t"] >= a["t0"]] + after
    baseline: Dict[Any, Any] = {}
    for f in [f for f in s.frames if f["t"] < a["t0"]] + a["baseline"]:
        baseline[(f["ep"], f["cluster"], f["attr"], f.get("mfr"))] = f["value"]
    step = {**st, "inputs": a["inputs"], "t0": a["t0"],
            "watch_ca": (st["watch"].get("cluster"), st["watch"].get("attr"))
            if st["watch"].get("cluster") is not None else None}
    proposals = []
    for op in st["infer"]:
        try:
            got = OPS[op["op"]](window, baseline, step, a["ctx"])
        except Exception as e:
            logger.warning(f"[{device.ieee}] {op['op']} failed: {e}")
            got = []
        for p in got:
            if p["path"] and not yields_allows(op["yields"], p["path"]):
                continue            # outside what the recipe may settle
            proposals.append({**p, "op": op["op"]})
    s.results[key] = {"status": "done", "proposals": proposals, "inputs": a["inputs"]}
    logger.info(f"[{device.ieee}] Learning step {key} finished: {len(after)} read back, "
                f"{len(window) - len(after)} frames seen; "
                + "; ".join(f"{p['confidence']}: {p['evidence']}" for p in proposals))
    if _is_power_test(st):
        from modules.learning_ops import moved_endpoints
        s.results[key].update(target=st["ep"], moved=moved_endpoints(window, baseline))
        _update_map(device, s)
    return state(device)


def decide(device, key: str, accept: List[int]) -> Dict[str, Any]:
    """Record the chosen proposals as learned facts; the rest are dropped."""
    from modules.device_facts import Fact, _j, record
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    res = s.results.get(key)
    if not res or res["status"] != "done":
        return {"success": False, "error": "that step has no results to decide"}
    facts = []
    for i in accept or []:
        if not 0 <= i < len(res["proposals"]) or not res["proposals"][i]["path"]:
            continue
        p = res["proposals"][i]
        path = p["path"]
        if path == "zmm.settings":
            path = f"zmm.settings.{p['value']['id']}"
        ep = int(path.split(".")[1]) if path.startswith("endpoints.") else 0
        facts.append(Fact(ep, f"learned:{path}", "learned",
                          _j({"value": p["value"], "evidence": p["evidence"],
                              "confidence": p["confidence"], "step": key})))
    if facts:
        record(str(device.ieee), facts)
    res["status"] = "accepted" if facts else "skipped"
    logger.info(f"[{device.ieee}] Learning step {key} {res['status']}: "
                f"{', '.join(f.subject for f in facts) or 'nothing kept'} "
                "(not in use until the review is saved)")
    return state(device)


async def end(device) -> Dict[str, Any]:
    s = _sessions.pop(str(device.ieee), None)
    if s is not None and s.active and s.active.get("task"):
        s.active["task"].cancel()
    if s is not None and s.trial:
        await _revert(device, s.trial)
    return {"success": True, "active": False}


# write-and-revert (plan §8 step 8)

TRY_TIMEOUT = 60.0
TOGGLE_TYPES = {"bool": 0x10, "uint8": 0x20, "enum8": 0x30}
# Attributes that start an action rather than hold a setting: never flipped.
NEVER_WRITE = {(0xFCC0, 0x0270)}      # Aqara TRV: start motor calibration


def candidates(device, key: str) -> Dict[str, Any]:
    """Settings ZMM may flip for this step: writable manufacturer toggles
    (bool, or a small integer now 0 or 1). A battery device is offered them
    too: `try_write` reads the value first, so one that is asleep is refused
    before anything is written."""
    import json
    from modules.device_facts import attr_subject, mfr_hex
    from modules.zigbee_cache import get_facts
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    if st.get("mode") != "try_write":
        return {"success": False, "error": "this step does not write"}
    values = {}
    for f in get_facts(str(device.ieee)):
        if f["source"] == "answered" and f["subject"].startswith("attr:"):
            try:
                values[(f["endpoint_id"], f["subject"])] = json.loads(f["value"]).get("value")
            except ValueError:
                pass
    out = []
    for (ep, cid, aid, mfr), type_name in sorted(_context(device, st)["writable"].items(),
                                                 key=lambda kv: (kv[0][:3], kv[0][3] or 0)):
        if type_name not in TOGGLE_TYPES or (cid, aid) in NEVER_WRITE:
            continue
        v = values.get((ep, attr_subject("attr", cid, aid, mfr)))
        if v not in (None, 0, 1, True, False):
            continue
        label = f"EP{ep} 0x{cid:04X}/0x{aid:04X}"
        out.append({"ep": ep, "cluster": f"0x{cid:04X}", "attr": f"0x{aid:04X}", "mfr": mfr_hex(mfr),
                    "type": type_name, "value": v, "ruled_out": label in s.tried.get(key, [])})
    return {"success": True, "candidates": out}


async def _write(device, key, type_name: str, value) -> bool:
    from zigpy.zcl import foundation
    from modules.zmm_settings import _write_ok
    ep, cid, aid, mfr = key
    cluster = (device.zigpy_dev.endpoints[ep].in_clusters or {}).get(cid)
    if cluster is None:
        return False
    from modules.zmm_settings import typed_value
    tv = typed_value(TOGGLE_TYPES[type_name], int(value))
    attr = foundation.Attribute()
    attr.attrid = aid
    attr.value = tv
    try:
        async with asyncio.timeout(READ_TIMEOUT):
            return _write_ok(await cluster.write_attributes_raw([attr], mfr))
    except Exception as e:
        logger.warning(f"[{device.ieee}] learning write failed: {e}")
        return False


async def _revert(device, trial) -> bool:
    """Write the old value back and read it to be sure."""
    ok = await _write(device, trial["key"], trial["type"], trial["old"])
    back = await _read(device, [trial["key"]])
    ok = ok and bool(back) and int(back[-1]["value"]) == int(trial["old"])
    ep, cid, aid, _ = trial["key"]
    (logger.info if ok else logger.error)(
        f"[{device.ieee}] EP{ep} 0x{cid:04X}/0x{aid:04X} put back to {int(trial['old'])}"
        + ("" if ok else ": NOT confirmed, check the device"))
    return ok


async def _auto_revert(device, trial) -> None:
    await asyncio.sleep(trial["timeout"])
    s = _sessions.get(str(device.ieee))
    if s is not None and s.trial is trial:
        s.trial = None
        await _revert(device, trial)
        s.results[trial["step"]] = {"status": "pending", "proposals": [
            {"path": "", "value": None, "confidence": "none",
             "evidence": "no answer in time: the setting was put back"}]}


async def try_write(device, key: str, candidate: Dict[str, Any],
                    inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Flip one candidate while the user watches; they answer, then it is put back."""
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    if s.trial or s.active:
        return {"success": False, "error": "answer the current step first"}
    clean, err = _clean_inputs(st, inputs)
    if err:
        return {"success": False, "error": err}
    if "setting_id" in clean and not SETTING_ID.fullmatch(clean["setting_id"]):
        return {"success": False, "error": SETTING_ID_HELP}
    offered = candidates(device, key)
    if not offered["success"]:
        return offered
    pick = next((c for c in offered["candidates"] if (c["ep"], c["cluster"], c["attr"])
                 == (candidate.get("ep"), candidate.get("cluster"), candidate.get("attr"))), None)
    if pick is None:
        return {"success": False, "error": "not an attribute ZMM may flip"}
    tkey = (pick["ep"], int(pick["cluster"], 16), int(pick["attr"], 16),
            int(pick["mfr"], 16) if pick.get("mfr") else None)
    now = await _read(device, [tkey])
    if not now:
        return {"success": False, "error": "the device did not answer: wake it (press a "
                                           "button on it) and try again"}
    if now[-1]["value"] not in (0, 1, True, False):
        return {"success": False, "error": "its value is no longer 0 or 1: not a toggle"}
    old = int(now[-1]["value"])
    trial = {"step": key, "key": tkey, "type": pick["type"], "old": old, "new": 1 - old,
             "t0": time.time(), "inputs": clean,
             # A behaviour takes longer to show than a light does: a step with
             # a longer window holds its trial that many times longer.
             "timeout": TRY_TIMEOUT * max(1.0, float(st.get("window_s") or 0) / 60)}
    if not await _write(device, tkey, pick["type"], trial["new"]):
        return {"success": False, "error": "the device refused the write"}
    s.trial = trial
    s.results[key] = {"status": "running", "proposals": []}
    asyncio.create_task(_auto_revert(device, trial))
    logger.info(f"[{device.ieee}] Learning: EP{tkey[0]} 0x{tkey[1]:04X}/0x{tkey[2]:04X} "
                f"{old} -> {1 - old} (will be put back)")
    return state(device)


async def answer(device, key: str, changed: bool) -> Dict[str, Any]:
    """The user saw (or did not see) the change: put it back, then conclude."""
    from modules.learning_ops import OPS
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    trial = s.trial
    if not trial or trial["step"] != key:
        return {"success": False, "error": "nothing is being tried for this step"}
    s.trial = None
    restored = await _revert(device, trial)
    ep, cid, aid, _ = trial["key"]
    label = f"EP{ep} 0x{cid:04X}/0x{aid:04X}"
    if not restored:
        s.results[key] = {"status": "pending", "proposals": [
            {"path": "", "value": None, "confidence": "none",
             "evidence": f"{label} could not be confirmed put back to {trial['old']}: check the device"}]}
        return state(device)
    if not changed:
        s.tried.setdefault(key, []).append(label)
        s.results[key] = {"status": "pending", "proposals": [
            {"path": "", "value": None, "confidence": "none",
             "evidence": f"{label} changed nothing visible; put back. Try another."}]}
        return state(device)
    step = {**st, "inputs": trial["inputs"]}
    proposals = [{**p, "op": "confirmed_write"}
                 for p in OPS["confirmed_write"]([], {}, step, {"trial": {**trial, "confirmed": True}})]
    s.results[key] = {"status": "done", "proposals": proposals, "inputs": trial["inputs"]}
    return state(device)


# learned facts -> entry

def set_path(entry: Dict[str, Any], path: str, value: Any) -> None:
    """Write a proposal into an entry dict ("zmm.settings.<id>" upserts by id)."""
    parts = path.split(".")
    if parts[:2] == ["zmm", "settings"]:
        settings = entry.setdefault("zmm", {}).setdefault("settings", [])
        settings[:] = [x for x in settings if x.get("id") != value.get("id")] + [value]
        return
    node = entry
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def apply_learned(device, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Merge this device's learned facts into an entry. Returns what came from them."""
    import json
    from modules.zigbee_cache import get_facts
    used = []
    for f in get_facts(str(device.ieee)):
        if f["source"] != "learned" or not f["subject"].startswith("learned:"):
            continue
        try:
            fact = json.loads(f["value"])
        except ValueError:
            continue
        path = f["subject"][len("learned:"):]
        set_path(entry, path, fact["value"])
        used.append({"path": path, "evidence": fact.get("evidence"),
                     "confidence": fact.get("confidence")})
    return used


# review, save, export, import

EXPORT_FORMAT = "zmm-entry/1"


def _overlay(base: Dict[str, Any], draft: Dict[str, Any]) -> Dict[str, Any]:
    """base, with the draft filling only what base does not say."""
    out = dict(base)
    for k, v in draft.items():
        if k not in out or out[k] in (None, {}, []):
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict):
            out[k] = _overlay(out[k], v)
    return out


def review(device) -> Dict[str, Any]:
    """The entry saving would store: the current entry (a user profile wholly
    replaces a ZMM entry, so its knowledge must be carried), the draft from
    evidence where it says nothing, and learned results on top."""
    import copy
    from modules.device_profiles import profile_for_device
    from modules.quirk_draft import draft_entry
    base = profile_for_device(device)
    draft = draft_entry(device)
    entry = _overlay(copy.deepcopy(base), draft["entry"]) if base else draft["entry"]
    for k in ("ieee_overrides", "schema_version"):
        entry.pop(k, None)
    entry["meta"] = {"source": "user", "author": "zmm learning"}
    learned = apply_learned(device, entry)
    return {"success": True, "entry": entry, "learned": learned, "device": editor_context(device),
            "preview": preview(device, entry), "candidates": draft["candidates"],
            "based_on": {"id": base["id"], "source": (base.get("meta") or {}).get("source")}
            if base else None}


# What the entry editor may offer a scaling for: name -> (cluster, attr).
EDITABLE_MEASUREMENTS = {"active_power": (0x0B04, 0x050B), "rms_voltage": (0x0B04, 0x0505),
                         "rms_current": (0x0B04, 0x0508), "energy": (0x0702, 0x0000)}


def editor_context(device) -> Dict[str, Any]:
    """The device as the entry editor needs it: its EPs with their clusters, and
    the measurements its clusters could carry (with the first EP that has each)."""
    from modules.device_identity import endpoint_label
    eps, measurements = [], {}
    for ep_id in sorted(e for e in (device.zigpy_dev.endpoints or {}) if e):
        ep = device.zigpy_dev.endpoints[ep_id]
        clusters = sorted(getattr(ep, "in_clusters", {}) or {})
        h = (device.handlers or {}).get((ep_id, 0x0006))
        live = h.endpoint_kind() if h is not None and hasattr(h, "endpoint_kind") else None
        eps.append({"ep": ep_id, "clusters": [f"0x{c:04X}" for c in clusters],
                    "kind": live.kind if live else None, "label": endpoint_label(device, ep_id)})
        for name, (cl, at) in EDITABLE_MEASUREMENTS.items():
            if cl in clusters and name not in measurements:
                measurements[name] = {"cluster": f"0x{cl:04X}", "attr": f"0x{at:04X}", "ep": ep_id}
    return {"endpoints": eps, "measurements": measurements, "blob_tags": blob_tags(device),
            "model": str(device.zigpy_dev.model or "")}


def blob_tags(device) -> List[Dict[str, Any]]:
    """Every tag in the device's latest Aqara status blobs with the value it
    last carried, so a person can match them against what they know (a
    battery at 3.2 V, a power-on count) and name them. The global map's name,
    where it has one, is what ZMM falls back on for a tag left unnamed."""
    from handlers.aqara import BLOB_ATTRS_SEEN, XIAOMI_ATTR_MAP, parse_xiaomi_struct
    values, _ = _vendor_values(device)
    out: Dict[int, Dict[str, Any]] = {}
    for (ep, cid, aid, _mfr), raw in sorted(values.items(), key=lambda kv: str(kv[0])):
        if cid != 0xFCC0 or aid not in BLOB_ATTRS_SEEN or not isinstance(raw, str):
            continue
        try:
            tags = parse_xiaomi_struct(bytes.fromhex(raw))
        except ValueError:
            continue
        for tag, v in tags.items():
            if isinstance(v, float):
                v = round(v, 3)
            elif isinstance(v, (bytes, bytearray)):
                v = bytes(v).hex()
            out.setdefault(tag, {"tag": f"0x{tag:02X}", "blob": f"0x{aid:04X}", "value": v,
                                 "map": XIAOMI_ATTR_MAP.get(tag, (None,))[0]})
    return [out[t] for t in sorted(out)]


def preview_entry(device, entry: Dict[str, Any]) -> Dict[str, Any]:
    """An edited entry as saving would store it (malformed parts dropped), and what it changes."""
    from modules.device_profiles import normalise_profile
    if not isinstance(entry, dict):
        return {"success": False, "error": "not an entry"}
    clean = normalise_profile(entry)
    return {"success": True, "entry": clean, "preview": preview(device, clean)}


def preview(device, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What saving `entry` would change, as (what, from, to)."""
    from modules.device_decisions import records
    from modules.device_identity import endpoint_label
    from modules.device_profiles import profile_for_device
    decided = records(str(device.ieee))
    current = profile_for_device(device) or {}
    changes = []

    def diff(what, old, new):
        if new is not None and new != old:
            changes.append({"what": what, "from": old, "to": new})

    for ep_id in sorted(e for e in (device.zigpy_dev.endpoints or {}) if e):
        e = (entry.get("endpoints") or {}).get(str(ep_id)) or {}
        h = (device.handlers or {}).get((ep_id, 0x0006))
        live = h.endpoint_kind() if h is not None and hasattr(h, "endpoint_kind") else None
        diff(f"EP{ep_id} type", live.kind if live else None, e.get("kind"))
        diff(f"EP{ep_id} name", endpoint_label(device, ep_id), e.get("label"))
        diff(f"EP{ep_id} metering", (decided.get((ep_id, "metering")) or (None,))[0], e.get("metering"))
        named = ((current.get("endpoints") or {}).get(str(ep_id)) or {}).get("clusters") or {}
        for cl, spec in sorted((e.get("clusters") or {}).items()):
            for at, m in sorted(((spec or {}).get("attributes") or {}).items()):
                old = ((named.get(cl) or {}).get("attributes") or {}).get(at) or {}
                diff(f"EP{ep_id} {cl}/{at} reads as", old.get("name"), (m or {}).get("name"))
    if entry.get("device_type") not in (None, "generic"):
        diff("device type", (decided.get((0, "device_type")) or (None,))[0], entry["device_type"])
    zmm, cur = entry.get("zmm") or {}, current.get("zmm") or {}
    ap = (zmm.get("measurements") or {}).get("active_power") or {}
    if ap:
        old = next((v[0] for (e, s), v in decided.items() if s == "scaling:active_power"), None)
        diff("power scaling", old, f"x{ap.get('multiplier', 1)}/{ap.get('divisor', 1)}")
    cur_tags = cur.get("struct_tags") or {}
    for tag, spec in sorted((zmm.get("struct_tags") or {}).items()):
        old = (cur_tags[tag] or "dropped") if tag in cur_tags else "global map"
        diff(f"blob tag {tag}", old, spec or "dropped")
    for value, name in sorted((zmm.get("press_names") or {}).items()):
        diff(f"press value {value}", (cur.get("press_names") or {}).get(value), name)
    have = {x.get("id"): x for x in cur.get("settings") or []}
    for st in zmm.get("settings") or []:
        diff(f"setting {st.get('id')}", "present" if st.get("id") in have else None,
             st.get("label") or st.get("id"))
    return changes


def refresh(device) -> None:
    """Drop conclusions cached from the old entry (the caller re-announces)."""
    for h in set((device.handlers or {}).values()):
        if hasattr(h, "_kind"):
            h._kind = None
        if hasattr(h, "_resolve_scaling"):
            h._resolve_scaling()
    if hasattr(device, "capabilities"):
        device.capabilities._detect_capabilities()


def save(device, entry: Dict[str, Any]) -> Dict[str, Any]:
    from modules.device_profiles import get_profile_store
    model = str(device.zigpy_dev.model or "")
    if ((entry.get("match") or {}).get("model") or "") != model:
        return {"success": False, "error": f"this entry is not for {model}"}
    saved = get_profile_store().upsert_profile(entry)
    refresh(device)
    logger.info(f"[{device.ieee}] Learning saved as profile {saved['id']}: settings "
                f"{[s.get('id') for s in (saved.get('zmm') or {}).get('settings') or []]}")
    return {"success": True, "saved_profile": saved["id"]}


def export(device) -> Dict[str, Any]:
    """The device's entry plus what settled it; no IEEE, no file names."""
    import json
    from modules.zigbee_cache import get_facts
    # What saving would store: the entry in use with what was learned on top,
    # so a result not yet saved is not left out of its own export.
    entry = review(device)["entry"]
    entry = {k: v for k, v in entry.items() if k not in ("ieee_overrides",)}
    zmm = dict(entry.get("zmm") or {})
    if "evidence" in zmm:
        zmm["evidence"] = {k: v for k, v in zmm["evidence"].items() if k != "probes"}
    entry["zmm"] = zmm
    evidence = []
    for f in get_facts(str(device.ieee)):
        if f["source"] == "learned":
            fact = json.loads(f["value"])
            evidence.append({"field": f["subject"][len("learned:"):],
                             "evidence": fact.get("evidence"), "confidence": fact.get("confidence")})
    return {"format": EXPORT_FORMAT, "entry": entry, "evidence": evidence,
            "firmware": device.state.get("sw_version")}


def import_entry(device, payload: Dict[str, Any], apply: bool) -> Dict[str, Any]:
    """Validate an exported entry for this device's model; preview, and save if asked."""
    from modules.device_profiles import normalise_profile
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        return {"success": False, "error": "not a ZMM entry export"}
    entry = normalise_profile(payload.get("entry") or {})
    entry["meta"]["source"] = "user"
    model = str(device.zigpy_dev.model or "")
    if entry["match"]["model"] != model:
        return {"success": False, "error": f"this entry is for {entry['match']['model'] or 'no model'}, "
                                           f"not {model}"}
    out = {"success": True, "entry": entry, "preview": preview(device, entry),
           "evidence": payload.get("evidence") or [], "firmware": payload.get("firmware")}
    if apply:
        out.update(save(device, entry))
    return out
