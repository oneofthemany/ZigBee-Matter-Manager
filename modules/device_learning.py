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
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.device_learning")

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
    for f in get_facts(str(device.ieee)):
        if f["source"] != "answered" or "@" not in f["subject"] or not f["subject"].startswith("attr:"):
            continue
        try:
            meta = json.loads(f["value"])
        except ValueError:
            continue
        if "W" not in str(meta.get("acl") or ""):
            continue
        ca, mfr = f["subject"][5:].split("@")
        cid, aid = (int(x, 16) for x in ca.split("/"))
        type_name = str(meta.get("type") or "").split("/")[-1]
        writable[(f["endpoint_id"], cid, aid, int(mfr, 16))] = type_name
    ep = step.get("ep")
    switched = ep is not None and 0x0006 in (
        getattr(device.zigpy_dev.endpoints.get(ep), "in_clusters", None) or {})
    return {"writable": writable, "energy_kwh": device.state.get("energy"),
            "max_power_w": SWITCHED_OUTLET_MAX_W if switched else None}


def _sample_targets(device, step: Dict[str, Any], ctx) -> List[Tuple[int, int, int, Optional[int]]]:
    ops = {i["op"] for i in step["infer"]}
    targets = []
    eps = {e: ep for e, ep in (device.zigpy_dev.endpoints or {}).items() if e and ep is not None}
    if ops & {"which_endpoints_moved", "scale_from_known"}:
        targets += [(e, 0x0B04, 0x050B, None) for e, ep in eps.items() if 0x0B04 in (ep.in_clusters or {})]
    if "which_endpoint_changed" in ops:
        targets += [(e, 0x0006, 0x0000, None) for e, ep in eps.items() if 0x0006 in (ep.in_clusters or {})]
    if "attribute_that_toggled" in ops:
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
                    out.append({"t": time.time(), "ep": ep, "cluster": cid,
                                "attr": int(rec.attrid), "mfr": mfr, "value": v})
    return out


# lifecycle

def start(device) -> Dict[str, Any]:
    from modules.learning_recipes import applies, expand, load
    recipes = [r for r in load().values() if applies(r, device)]
    steps = [s for r in recipes for s in expand(r, device)]
    for s in steps:
        s["title"] = next(r["title"] for r in recipes if r["id"] == s["recipe"])
    _sessions[str(device.ieee)] = Session(str(device.ieee), steps)
    logger.info(f"[{device.ieee}] Learning started: {len(steps)} steps from "
                f"{', '.join(r['id'] for r in recipes) or 'no recipes'}")
    return state(device)


def state(device) -> Dict[str, Any]:
    s = _sessions.get(str(device.ieee))
    if s is None:
        return {"success": True, "active": False}
    steps = []
    for st in s.steps:
        res = s.results.get(st["key"]) or {}
        steps.append({"key": st["key"], "title": st["title"], "label": st["label"],
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
                 "expires_in": max(0, round(t["t0"] + TRY_TIMEOUT - time.time()))}
    return {"success": True, "active": True, "running": s.active["key"] if s.active else None,
            "trial": trial, "steps": steps}


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
                return None, f"{spec['label']} must be a number"
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
    clean, err = _clean_inputs(st, inputs)
    if err:
        return {"success": False, "error": err}
    ctx = _context(device, st)
    baseline = await _read(device, _sample_targets(device, st, ctx))
    s.active = {"key": key, "t0": time.time(), "inputs": clean, "baseline": baseline, "ctx": ctx}
    s.results[key] = {"status": "running", "proposals": []}
    return state(device)


async def finish(device, key: str) -> Dict[str, Any]:
    from modules.learning_ops import OPS
    from modules.learning_recipes import yields_allows
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    if not s.active or s.active["key"] != key:
        return {"success": False, "error": "that step is not running"}
    a, s.active = s.active, None
    after = await _read(device, _sample_targets(device, st, a["ctx"]))
    window = [f for f in s.frames if f["t"] >= a["t0"]] + after
    baseline: Dict[Any, Any] = {}
    for f in [f for f in s.frames if f["t"] < a["t0"]] + a["baseline"]:
        baseline[(f["ep"], f["cluster"], f["attr"], f.get("mfr"))] = f["value"]
    step = {**st, "inputs": a["inputs"],
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
    return state(device)


async def end(device) -> Dict[str, Any]:
    s = _sessions.pop(str(device.ieee), None)
    if s is not None and s.trial:
        await _revert(device, s.trial)
    return {"success": True, "active": False}


# write-and-revert (plan §8 step 8)

TRY_TIMEOUT = 60.0
TOGGLE_TYPES = {"bool": 0x10, "uint8": 0x20, "enum8": 0x30}
# Attributes that start an action rather than hold a setting: never flipped.
NEVER_WRITE = {(0xFCC0, 0x0270)}      # Aqara TRV: start motor calibration


def _mains(device) -> bool:
    nd = getattr(device.zigpy_dev, "node_desc", None)
    return bool(nd is not None and getattr(nd, "is_mains_powered", False))


def candidates(device, key: str) -> Dict[str, Any]:
    """Settings ZMM may flip for this step: writable manufacturer toggles
    (bool, or a small integer now 0 or 1), mains devices only."""
    import json
    from modules.zigbee_cache import get_facts
    s, st, err = _session_step(device, key)
    if err:
        return {"success": False, "error": err}
    if st.get("mode") != "try_write":
        return {"success": False, "error": "this step does not write"}
    if not _mains(device):
        return {"success": False, "error": "only mains-powered devices: a sleeping one "
                                           "might not be put back"}
    values = {}
    for f in get_facts(str(device.ieee)):
        if f["source"] == "answered" and f["subject"].startswith("attr:") and "@" in f["subject"]:
            try:
                values[(f["endpoint_id"], f["subject"])] = json.loads(f["value"]).get("value")
            except ValueError:
                pass
    out = []
    for (ep, cid, aid, mfr), type_name in sorted(_context(device, st)["writable"].items()):
        if type_name not in TOGGLE_TYPES or (cid, aid) in NEVER_WRITE:
            continue
        v = values.get((ep, f"attr:0x{cid:04X}/0x{aid:04X}@0x{mfr:04X}"))
        if v not in (None, 0, 1, True, False):
            continue
        label = f"EP{ep} 0x{cid:04X}/0x{aid:04X}"
        out.append({"ep": ep, "cluster": f"0x{cid:04X}", "attr": f"0x{aid:04X}", "mfr": f"0x{mfr:04X}",
                    "type": type_name, "value": v, "ruled_out": label in s.tried.get(key, [])})
    return {"success": True, "candidates": out}


async def _write(device, key, type_name: str, value) -> bool:
    from zigpy.zcl import foundation
    from modules.zmm_settings import _write_ok
    ep, cid, aid, mfr = key
    cluster = (device.zigpy_dev.endpoints[ep].in_clusters or {}).get(cid)
    if cluster is None:
        return False
    tv = foundation.TypeValue()
    tv.type = TOGGLE_TYPES[type_name]
    tv.value = bool(value) if type_name == "bool" else int(value)
    attr = foundation.Attribute()
    attr.attrid = aid
    attr.value = tv
    try:
        async with asyncio.timeout(READ_TIMEOUT):
            return _write_ok(await cluster.write_attributes_raw([attr], manufacturer=mfr))
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
    await asyncio.sleep(TRY_TIMEOUT)
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
    offered = candidates(device, key)
    if not offered["success"]:
        return offered
    pick = next((c for c in offered["candidates"] if (c["ep"], c["cluster"], c["attr"])
                 == (candidate.get("ep"), candidate.get("cluster"), candidate.get("attr"))), None)
    if pick is None:
        return {"success": False, "error": "not an attribute ZMM may flip"}
    tkey = (pick["ep"], int(pick["cluster"], 16), int(pick["attr"], 16), int(pick["mfr"], 16))
    now = await _read(device, [tkey])
    if not now or now[-1]["value"] not in (0, 1, True, False):
        return {"success": False, "error": "could not read its current value"}
    old = int(now[-1]["value"])
    trial = {"step": key, "key": tkey, "type": pick["type"], "old": old, "new": 1 - old,
             "t0": time.time(), "inputs": clean}
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
    return {"success": True, "entry": entry, "learned": learned,
            "preview": preview(device, entry), "candidates": draft["candidates"],
            "based_on": {"id": base["id"], "source": (base.get("meta") or {}).get("source")}
            if base else None}


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
    return {"success": True, "saved_profile": saved["id"]}


def export(device) -> Dict[str, Any]:
    """The device's entry plus what settled it; no IEEE, no file names."""
    import json
    from modules.device_profiles import profile_for_device
    from modules.zigbee_cache import get_facts
    entry = profile_for_device(device) or review(device)["entry"]
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
