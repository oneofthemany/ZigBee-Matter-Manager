# modules/device_profile_apply.py
"""
Apply a device profile to a live device — capabilities, actions, reporting and
state transforms.

Read-only on the profile; never mutates it. The apply runner is idempotent, so a
battery device that misses the first reporting push picks it up on a later
interview-complete. See docs/device-profiles.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Any, Dict, List, Optional, Tuple

from modules import device_ops
from modules.device_profiles import (
    get_profile_store, CAPABILITY_TO_HA, _to_int,
)

logger = logging.getLogger("modules.device_profile_apply")


def _get_device_identity(device) -> Dict[str, Any]:
    """Pull model / manufacturer / vendor_id / product_id from a live device.

    Works for both ZigManDevice (Zigbee) and MatterDevice — falls back to
    sensible defaults when fields are missing.
    """
    ident: Dict[str, Any] = {
        "ieee":         str(getattr(device, "ieee", "")),
        "protocol":     getattr(device, "protocol", None) or "zigbee",
        "model":        "",
        "manufacturer": "",
        "vendor_id":    None,
        "product_id":   None,
    }

    # Zigbee path
    zdev = getattr(device, "zigpy_dev", None)
    if zdev is not None:
        ident["protocol"]     = "zigbee"
        ident["model"]        = str(getattr(zdev, "model", "") or "")
        ident["manufacturer"] = str(getattr(zdev, "manufacturer", "") or "")
        return ident

    # Matter path — only enter when there is no zigpy device backing this
    # wrapper, or when the IEEE explicitly carries the matter_ prefix.
    is_matter = (
            str(getattr(device, "ieee", "")).startswith("matter_")
            or getattr(device, "protocol", None) == "matter"
    )
    if is_matter:
        state = getattr(device, "state", None) or {}
        ident["protocol"]     = "matter"
        ident["vendor_id"]    = _to_int(state.get("vendor_id"))
        ident["product_id"]   = state.get("product_id") or state.get("part_number")
        ident["manufacturer"] = state.get("vendor_name") or ident["manufacturer"]
        ident["model"]        = state.get("product_name") or ident["model"]

    return ident


def resolve_profile_for_device(device) -> Optional[Dict[str, Any]]:
    """Return the profile that applies to this device, or None."""
    ident = _get_device_identity(device)
    if not ident["ieee"]:
        return None
    return get_profile_store().get_profile_for_device(**ident)


# Capabilities

def _apply_capabilities(device, profile: Dict[str, Any]) -> List[str]:
    """
    Merge profile capabilities into device.capabilities (set). Returns
    the list of capabilities newly added.
    """
    added: List[str] = []
    caps_obj = getattr(device, "capabilities", None)
    if caps_obj is None:
        # The device hasn't been wrapped with a DeviceCapabilities; fall
        # back to a plain set on the device itself.
        existing = set(getattr(device, "_profile_capabilities", set()) or [])
        new = set(profile.get("capabilities") or [])
        device._profile_capabilities = existing | new
        added = sorted(new - existing)
    else:
        existing = set(getattr(caps_obj, "_capabilities", set()) or set())
        for cap in profile.get("capabilities") or []:
            if cap not in existing:
                try:
                    caps_obj._capabilities.add(cap)
                    added.append(cap)
                except Exception:
                    pass
    if added:
        logger.info(f"[{device.ieee}] Profile added capabilities: {added}")
    return added


# State transforms

def transform_state_with_profile(device, state: Dict[str, Any],
                                 changed: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """
    Add friendly keys to *state* based on the profile + IEEE overrides.

    Pure: returns a new dict (does not mutate ``state``). `changed` names the
    keys this update carries: a friendly key already present is recomputed
    only when its source is among them and it is not itself (a handler that
    writes the name directly wins). Without `changed`, present keys are kept.
    """
    out = dict(state or {})
    changed = set(changed or ())

    def free(name: str, source: str) -> bool:
        return name not in out or (source in changed and name not in changed)

    profile = resolve_profile_for_device(device)
    store   = get_profile_store()
    ieee    = str(getattr(device, "ieee", ""))
    mappings = store.get_ieee_mappings(ieee) or {}

    # Build a quick lookup of (cluster_id, attr_id) -> mapping from the profile
    profile_attrs: Dict[Tuple[int, int], Dict[str, Any]] = {}
    if profile:
        for ep in profile["endpoints"].values():
            for cl_hex, cl in ep.get("clusters", {}).items():
                cid = _to_int(cl_hex)
                if cid is None:
                    continue
                for at_hex, attr in (cl.get("attributes") or {}).items():
                    aid = _to_int(at_hex)
                    if aid is None:
                        continue
                    profile_attrs[(cid, aid)] = attr

    # An entry's setting names the attribute it writes, so the same definition
    # names what the device reports for it: one name for both directions.
    settings: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for st in ((profile or {}).get("zmm") or {}).get("settings") or []:
        cid, aid = _to_int(st.get("cluster")), _to_int(st.get("attr"))
        if cid is not None and aid is not None and st.get("ep") != "each":
            settings[(cid, aid)] = st

    for key, raw_value in list(out.items()):
        # Three spellings of an unnamed attribute: cluster_XXXX_attr_YYYY from the
        # generic handler, attr_XXXX_YYYY from a registered handler's fallback
        # (ClusterHandler.get_attr_name), opple_0xYYYY from the Aqara handler's
        # for 0xFCC0. A profile names any of them.
        parts = key.split("_")
        try:
            if key.startswith("cluster_") and len(parts) >= 4 and parts[2] == "attr":
                cid, aid = int(parts[1], 16), int(parts[3], 16)
            elif key.startswith("attr_") and len(parts) == 3:
                cid, aid = int(parts[1], 16), int(parts[2], 16)
            elif key.startswith("opple_0x") and len(parts) == 2:
                cid, aid = 0xFCC0, int(parts[1], 16)
            else:
                continue
        except ValueError:
            continue

        setting = settings.get((cid, aid))
        if setting is not None:
            from modules.zmm_settings import _from_raw
            # A report is the device's own word: it replaces what a write left.
            # In a live update, a raw key the update does not carry is no report.
            if setting["id"] not in out or not changed or key in changed:
                out[setting["id"]] = _from_raw(setting, int(raw_value) if isinstance(raw_value, bool)
                                               else raw_value)
            continue

        mapping = mappings.get(key) or profile_attrs.get((cid, aid))
        if not mapping or not mapping.get("name"):
            continue

        try:
            value = _apply_attr_transform(raw_value, mapping)
        except Exception as e:
            logger.debug(f"[{ieee}] transform failed for {key}: {e}")
            continue

        if free(mapping["name"], key):
            out[mapping["name"]] = value

    # state-key mappings (learn-by-demonstration)
    # These rename/transform an arbitrary state key by its literal name, so
    # they cover Tuya datapoints (dp_16), Matter attributes and any derived
    # key — not just ZCL cluster attributes. raw_key form: "state:<key>".
    for raw_key, mapping in mappings.items():
        if not isinstance(raw_key, str) or not raw_key.startswith("state:"):
            continue
        src_key = raw_key[len("state:"):]
        if not src_key or src_key not in out:
            continue
        name = mapping.get("name") if isinstance(mapping, dict) else None
        if not name or not free(name, src_key):
            continue
        try:
            out[name] = _apply_attr_transform(out[src_key], mapping)
        except Exception as e:
            logger.debug(f"[{ieee}] state transform failed for {raw_key}: {e}")

    # profile-level state mappings (shared across the model)
    # IEEE mappings above already ran, so per-device overrides win via the
    # "name not in out" guard.
    if profile:
        for src_key, mapping in (profile.get("state_mappings") or {}).items():
            if src_key not in out:
                continue
            name = mapping.get("name") if isinstance(mapping, dict) else None
            if not name or not free(name, src_key):
                continue
            try:
                out[name] = _apply_attr_transform(out[src_key], mapping)
            except Exception as e:
                logger.debug(f"[{ieee}] profile state transform failed for {src_key}: {e}")

    return out


def derived_changes(device, changed: Dict[str, Any]) -> Dict[str, Any]:
    """Friendly keys a live update adds beside its raw ones: each whose value
    moved, and a binary reading this update reports even unmoved. A report of
    one is an event, as a raw `occupancy` is: a clear after a restart's reset
    to False still reaches the log and the engine."""
    state = getattr(device, "state", None) or {}
    merged = transform_state_with_profile(device, {**state, **changed}, changed=changed)
    reported = transform_state_with_profile(device, dict(changed), changed=changed)
    events = _binary_names(device)
    return {k: v for k, v in merged.items()
            if k not in changed and (state.get(k) != v or (k in events and k in reported))}


def _binary_names(device) -> set:
    profile = resolve_profile_for_device(device) or {}
    maps = [a for ep in (profile.get("endpoints") or {}).values()
            for cl in (ep.get("clusters") or {}).values()
            for a in (cl.get("attributes") or {}).values()]
    maps += list((get_profile_store().get_ieee_mappings(str(getattr(device, "ieee", ""))) or {}).values())
    maps += list((profile.get("state_mappings") or {}).values())
    return {m["name"] for m in maps if isinstance(m, dict) and m.get("name")
            and m.get("device_class") in BINARY_CLASSES}


# Home Assistant binary_sensor classes: a reading of one is true or false.
BINARY_CLASSES = frozenset({"occupancy", "motion", "presence", "door", "window", "opening",
                            "garage_door", "moisture", "smoke", "gas", "vibration", "tamper",
                            "problem", "safety", "sound", "lock", "plug", "running"})


def _raw_attr(key: str) -> Optional[Tuple[int, int]]:
    """(cluster, attr) a raw state key stands for, in any of its spellings."""
    parts = key.split("_")
    try:
        if key.startswith("cluster_") and len(parts) >= 4 and parts[2] == "attr":
            return int(parts[1], 16), int(parts[3], 16)
        if key.startswith("attr_") and len(parts) == 3:
            return int(parts[1], 16), int(parts[2], 16)
        if key.startswith("opple_0x") and len(parts) == 2:
            return 0xFCC0, int(parts[1], 16)
    except ValueError:
        pass
    return None


def named_raw_keys(device) -> List[str]:
    """Raw state keys the device's entry names, as a reading or a setting:
    the device's views show the name in their place, not both."""
    profile = resolve_profile_for_device(device) or {}
    named = set()
    for ep in (profile.get("endpoints") or {}).values():
        for cl_hex, cl in (ep.get("clusters") or {}).items():
            for at_hex, m in (cl.get("attributes") or {}).items():
                if (m or {}).get("name") and _to_int(cl_hex) is not None and _to_int(at_hex) is not None:
                    named.add((_to_int(cl_hex), _to_int(at_hex)))
    for st in (profile.get("zmm") or {}).get("settings") or []:
        cid, aid = _to_int(st.get("cluster")), _to_int(st.get("attr"))
        if cid is not None and aid is not None:
            named.add((cid, aid))
    return sorted(k for k in (getattr(device, "state", None) or {}) if _raw_attr(k) in named)


def _apply_attr_transform(value: Any, mapping: Dict[str, Any]) -> Any:
    """Apply scale / value_map / invert to a raw value."""
    if value is None:
        return None

    # value_map (e.g. {"0": "closed", "1": "open"}) — match by str(value)
    vmap = mapping.get("value_map")
    if isinstance(vmap, dict) and vmap:
        key = str(value)
        if key not in vmap and isinstance(value, bool):
            key = str(int(value))       # a map keyed "0"/"1" names a Bool too
        if key in vmap:
            return vmap[key]

    # invert (boolean attributes)
    if mapping.get("invert"):
        if isinstance(value, bool):
            value = not value
        elif isinstance(value, (int, float)):
            value = 1 - value if value in (0, 1) else value

    # A mapping that names the class but not the two values still yields a
    # truth value: consumers test `is True`, and 1 is not.
    if mapping.get("device_class") in BINARY_CLASSES and not isinstance(value, bool) \
            and isinstance(value, int) and value in (0, 1):
        return bool(value)

    # scale
    scale = mapping.get("scale")
    if scale and isinstance(value, (int, float)) and scale not in (0, 1):
        try:
            value = value / float(scale)
        except Exception:
            pass

    return value


# Reporting configuration

def _reporting_def(device, cluster, ep_id: int, cid: int, aid: int, row: Dict[str, Any]):
    """The attribute definition a configure request needs. zigpy's own where
    it has one and the row names no maker code; otherwise built here, typed
    from the row or from what the device answered (a maker's attribute zigpy
    has never heard of). None when the type is not known."""
    from zigpy.zcl import foundation
    from modules.device_profiles import SETTING_TYPES
    mfr = _to_int(row.get("mfr"))
    if not mfr:
        try:
            return cluster.find_attribute(aid)
        except (KeyError, AttributeError):
            pass
    type_name = row.get("type") or _answered_type(device, ep_id, cid, aid, mfr)
    if type_name not in SETTING_TYPES:
        return None
    py_type = foundation.DataType.from_type_id(SETTING_TYPES[type_name]).python_type
    return foundation.ZCLAttributeDef(id=aid, name=f"attr_{aid:04x}", type=py_type,
                                      manufacturer_code=mfr or None)


def _answered_type(device, ep_id: int, cid: int, aid: int, mfr: Optional[int]) -> Optional[str]:
    import json
    from modules.device_facts import attr_subject
    try:
        from modules.zigbee_cache import get_facts
        subject = attr_subject("attr", cid, aid, mfr)
        for f in get_facts(str(device.ieee)):
            if f["endpoint_id"] == ep_id and f["subject"] == subject and f["source"] == "answered":
                return str((json.loads(f["value"]) or {}).get("type") or "").split("/")[-1] or None
    except Exception as e:
        logger.debug(f"[{device.ieee}] attribute type unavailable: {e}")
    return None


async def apply_reporting(device, profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Push the profile's reporting rows to the device. Idempotent.

    Returns a result dict with per-row success/failure for the API to
    surface in the UI.
    """
    rows = profile.get("reporting") or []
    if not rows:
        return {"success": True, "configured": 0, "results": []}

    zdev = getattr(device, "zigpy_dev", None)
    if zdev is None:
        return {"success": False, "error": "Reporting requires Zigbee device", "results": []}

    results: List[Dict[str, Any]] = []
    ok = 0
    for r in rows:
        ep_id = r["ep"]
        cid   = _to_int(r["cluster"])
        aid   = _to_int(r["attr"])
        if ep_id is None or cid is None or aid is None:
            results.append({**r, "ok": False, "error": "bad row"})
            continue
        ep = zdev.endpoints.get(ep_id)
        if not ep:
            results.append({**r, "ok": False, "error": f"endpoint {ep_id} missing"})
            continue
        cluster = (ep.in_clusters or {}).get(cid)
        if not cluster:
            results.append({**r, "ok": False, "error": f"cluster 0x{cid:04X} missing"})
            continue
        try:
            attr_def = _reporting_def(device, cluster, ep_id, cid, aid, r)
            if attr_def is None:
                results.append({**r, "ok": False, "error": "type unknown: run Find everything first"})
                continue
            try:
                async with asyncio.timeout(8.0):
                    bound = await cluster.bind()
                device_ops.step_status(f"Bind 0x{cid:04X}", next(iter(bound or []), 0))
            except Exception as e:
                # Without the bind a maker's cluster sends no reports at all.
                logger.warning(f"[{device.ieee}] Bind 0x{cid:04X} failed: {device_ops.reason_of(e)}")
                device_ops.step(f"Bind 0x{cid:04X}", False, device_ops.reason_of(e))
            from zigpy.zcl.helpers import ReportingConfig
            async with asyncio.timeout(8.0):
                rsp = await cluster.configure_reporting_multiple({attr_def: ReportingConfig(
                    min_interval=int(r["min"] if r.get("min") is not None else 30),
                    max_interval=int(r["max"] if r.get("max") is not None else 600),
                    reportable_change=r.get("delta", 1))})
            refused = [rec for rec in (rsp or []) if int(getattr(rec, "status", 0)) != 0]
            if refused:
                results.append({**r, "ok": False, "error": f"refused: {refused[0].status}",
                                "reason": device_ops.reason_of(refused[0].status)})
                continue
            ok += 1
            results.append({**r, "ok": True})
        except asyncio.TimeoutError:
            results.append({**r, "ok": False, "error": "timeout", "reason": "timeout"})
        except Exception as e:
            results.append({**r, "ok": False, "error": f"{type(e).__name__}: {e}"})

    logger.info(f"[{device.ieee}] Reporting applied: {ok}/{len(rows)} OK")
    # A save applies this in the background: the log is the only place a reason shows.
    for res in results:
        device_ops.step(f"Reporting {res.get('cluster')}/{res.get('attr')}", bool(res.get("ok")),
                        res.get("reason") or res.get("error"),
                        cluster=_to_int(res.get("cluster")), attr=_to_int(res.get("attr")))
        if not res.get("ok"):
            logger.warning(f"[{device.ieee}] Reporting {res.get('cluster')}/{res.get('attr')} "
                           f"not set: {res.get('error') or 'no reason given'}")
    return {"success": ok == len(rows), "configured": ok, "results": results}


async def apply_reads(device, profile: Dict[str, Any]) -> int:
    """Read the attributes the profile lists and hand each value to the
    cluster's handler, as a report would. Returns values read."""
    zdev = getattr(device, "zigpy_dev", None)
    groups: Dict[Tuple[int, int, Optional[int]], List[int]] = {}
    for r in profile.get("reads") or []:
        groups.setdefault((r["ep"], _to_int(r["cluster"]), _to_int(r.get("mfr"))), []).append(_to_int(r["attr"]))
    got = 0
    for (ep_id, cid, mfr), attrs in groups.items():
        ep = (zdev.endpoints or {}).get(ep_id) if zdev else None
        cluster = (ep.in_clusters or {}).get(cid) if ep else None
        handler = (getattr(device, "handlers", None) or {}).get((ep_id, cid))
        if cluster is None:
            continue
        try:
            async with asyncio.timeout(8.0):
                rsp = await cluster.read_attributes_raw(attrs, manufacturer=mfr)
        except Exception as e:
            logger.debug(f"[{device.ieee}] profile read of 0x{cid:04X} failed: {e}")
            continue
        for rec in getattr(rsp, "status_records", None) or []:
            if int(rec.status) != 0 or rec.value is None:
                continue
            got += 1
            if handler is not None:
                try:
                    handler.attribute_updated(int(rec.attrid), rec.value.value)
                except Exception as e:
                    logger.debug(f"[{device.ieee}] read 0x{int(rec.attrid):04X} not handled: {e}")
    if got:
        logger.info(f"[{device.ieee}] Profile reads: {got} values")
    return got


# Action runner

async def run_action(device, action_id: str, args: Optional[List[Any]] = None) -> Dict[str, Any]:
    """
    Execute a profile-defined action on a device.

    Two action shapes are supported:

    * ``command`` action — single ZCL command to a cluster (optionally with args).
    * ``writes`` action — one or more attribute writes. If ``atomic`` is set
      and there's more than one write, they're batched into a single
      ``write_attributes`` call against the *same* cluster, matching the
      Hive SLR1c protocol pattern.
    """
    profile = resolve_profile_for_device(device)
    if not profile:
        return {"success": False, "error": "No profile applied to this device"}

    action = next((a for a in profile.get("actions") or [] if a.get("id") == action_id), None)
    if not action:
        return {"success": False, "error": f"Action {action_id!r} not defined in profile"}

    zdev = getattr(device, "zigpy_dev", None)
    if zdev is None:
        return {"success": False, "error": "Profile actions require a Zigbee device"}

    ep_id = action["ep"]
    cl_id = _to_int(action["cluster"])
    ep = zdev.endpoints.get(ep_id) if ep_id is not None else None
    if not ep:
        return {"success": False, "error": f"Endpoint {ep_id} missing"}
    cluster = (ep.in_clusters or {}).get(cl_id) or (ep.out_clusters or {}).get(cl_id)
    if not cluster:
        return {"success": False, "error": f"Cluster 0x{cl_id:04X} missing on EP{ep_id}"}

    # Path A — writes (preferred for atomic multi-write actions)
    writes = action.get("writes") or []
    raw = [w for w in writes if w.get("mfr") or w.get("type")]
    if raw:
        # A maker's attribute, typed and coded by the entry: zigpy's
        # write_attributes would refuse an id it has no definition for.
        from zigpy.zcl import foundation
        from modules.device_profiles import SETTING_TYPES
        from modules.zmm_settings import _write_ok, typed_value
        try:
            for w in raw:
                target_ep = zdev.endpoints.get(w.get("ep", ep_id))
                target_cl = ((target_ep.in_clusters or {}) if target_ep else {}).get(_to_int(w.get("cluster")))
                if target_cl is None or w.get("type") not in SETTING_TYPES:
                    return {"success": False, "error": f"cannot write {w.get('cluster')}/{w.get('attr')}"}
                attr = foundation.Attribute()
                attr.attrid = _to_int(w.get("attr"))
                attr.value = typed_value(SETTING_TYPES[w["type"]], int(w.get("value") or 0))
                async with asyncio.timeout(10.0):
                    ok = _write_ok(await target_cl.write_attributes_raw([attr], _to_int(w.get("mfr"))))
                if not ok:
                    return {"success": False, "error": "the device refused the write"}
            writes = [w for w in writes if w not in raw]
            if not writes:
                return {"success": True, "action": action_id, "writes": len(raw)}
        except asyncio.TimeoutError:
            return {"success": False, "error": "write timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    if writes:
        try:
            # Group writes by (ep, cluster) — usually all on one cluster
            grouped: Dict[Tuple[int, int], Dict[int, Any]] = {}
            for w in writes:
                w_ep = w.get("ep", ep_id)
                w_cl = _to_int(w.get("cluster")) if w.get("cluster") else cl_id
                w_at = _to_int(w.get("attr"))
                if w_at is None:
                    continue
                grouped.setdefault((w_ep, w_cl), {})[w_at] = w.get("value")
            for (w_ep, w_cl), attrs in grouped.items():
                target_ep = zdev.endpoints.get(w_ep)
                if not target_ep:
                    return {"success": False, "error": f"Endpoint {w_ep} missing"}
                target_cl = (target_ep.in_clusters or {}).get(w_cl)
                if not target_cl:
                    return {"success": False, "error": f"Cluster 0x{w_cl:04X} missing"}
                async with asyncio.timeout(10.0):
                    await target_cl.write_attributes(attrs)
            return {"success": True, "action": action_id, "writes": len(writes)}
        except asyncio.TimeoutError:
            return {"success": False, "error": "write timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Path B — single command
    cmd_id = _to_int(action.get("command"))
    if cmd_id is None:
        return {"success": False, "error": "Action has neither writes nor command"}

    cmd_args = list(args or []) or list(action.get("args") or [])
    try:
        async with asyncio.timeout(10.0):
            await cluster.command(cmd_id, *cmd_args)
        return {"success": True, "action": action_id, "command": f"0x{cmd_id:02X}"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# Top-level apply

async def apply_profile(
        device,
        *,
        push_reporting: bool = True,
) -> Dict[str, Any]:
    """
    Apply the matching profile to a device. Called from the device
    lifecycle when an interview completes, and also on demand from the API.

    Idempotent — re-applying never breaks anything.
    """
    profile = resolve_profile_for_device(device)
    if not profile:
        return {"success": True, "applied": False, "reason": "No matching profile"}

    ieee = str(getattr(device, "ieee", ""))
    summary: Dict[str, Any] = {
        "success":      True,
        "applied":      True,
        "profile_id":   profile["id"],
        "device_type":  profile["device_type"],
        "capabilities": [],
        "actions":      [],
        "reporting":    None,
    }

    # 1. Capabilities
    summary["capabilities"] = _apply_capabilities(device, profile)

    # 2. Actions — record them on the device for the Control tab to show
    actions = profile.get("actions") or []
    try:
        device.profile_actions = actions
    except Exception:
        # Some device wrappers may forbid attribute assignment
        setattr(device, "_profile_actions", actions)
    summary["actions"] = [a["id"] for a in actions]

    # 2b. Command actions + live-transform hint — read by the base cluster
    #     handler (command -> action) and by the live-update gate so promoted
    #     state mappings transform live without any per-device override.
    try:
        setattr(device, "_cmd_actions", dict(profile.get("command_actions") or {}))
        has_transforms = bool(profile.get("state_mappings")) or any(
            cl.get("attributes")
            for ep in profile.get("endpoints", {}).values()
            for cl in ep.get("clusters", {}).values()
        )
        setattr(device, "_profile_live_transform", has_transforms)
    except Exception:
        pass

    # 3. Reporting — opt-out via push_reporting=False
    if push_reporting and (profile.get("reporting") or profile.get("reads")):
        try:
            summary["reporting"] = await apply_reporting(device, profile)
            summary["reads"] = await apply_reads(device, profile)
        except Exception as e:
            logger.exception(f"[{ieee}] apply_reporting failed: {e}")
            summary["reporting"] = {"success": False, "error": str(e)}

    # 4. State transform — re-process current state so friendly keys appear
    try:
        if hasattr(device, "state") and isinstance(device.state, dict):
            new_state = transform_state_with_profile(device, device.state)
            # Update via the device's own update path so listeners see it
            if hasattr(device, "update_state"):
                added = {k: v for k, v in new_state.items() if k not in device.state}
                if added:
                    device.update_state(added)
            else:
                device.state.update(new_state)
    except Exception as e:
        logger.debug(f"[{ieee}] state retransform skipped: {e}")

    logger.info(
        f"[{ieee}] Profile applied: {profile['id']} "
        f"(type={profile['device_type']}, caps={summary['capabilities']}, "
        f"actions={len(summary['actions'])})"
    )
    return summary


# MQTT discovery generation for a profile

def build_discovery_configs(device) -> List[Dict[str, Any]]:
    """
    Generate Home Assistant MQTT discovery configs for the friendly keys
    the profile exposes. Only fills *gaps* — if a built-in handler has
    already produced a discovery config for the same object_id, the
    caller is responsible for not duplicating it (de-duped at MQTT layer
    by ``unique_id``).
    """
    profile = resolve_profile_for_device(device)
    if not profile:
        return []

    configs: List[Dict[str, Any]] = []
    # Collect friendly names + their attribute metadata
    for ep in profile["endpoints"].values():
        for cl in ep.get("clusters", {}).values():
            for attr in (cl.get("attributes") or {}).values():
                name = attr.get("name")
                if not name:
                    continue
                cap = attr.get("capability") or _infer_capability(attr, profile)
                if not cap or cap not in CAPABILITY_TO_HA:
                    continue
                component = CAPABILITY_TO_HA[cap]["component"]
                cfg: Dict[str, Any] = {
                    "component": component,
                    "object_id": name,
                    "config": {
                        "name": _humanise(name),
                        "value_template": f"{{{{ value_json.{name} }}}}",
                    },
                }
                if attr.get("device_class"):
                    cfg["config"]["device_class"] = attr["device_class"]
                if attr.get("unit"):
                    cfg["config"]["unit_of_measurement"] = attr["unit"]
                if component == "binary_sensor":
                    vmap = attr.get("value_map") or {}
                    on_v  = next((k for k, v in vmap.items() if v in ("open", "wet", "motion", "on")), None)
                    off_v = next((k for k, v in vmap.items() if v in ("closed", "dry", "no_motion", "off")), None)
                    if on_v and off_v:
                        cfg["config"]["payload_on"]  = on_v
                        cfg["config"]["payload_off"] = off_v
                    else:
                        # No named pair: the state is a truth value, and HA
                        # matches its default ON / OFF payloads, not "True".
                        cfg["config"]["value_template"] = \
                            f"{{{{ 'ON' if value_json.{name} else 'OFF' }}}}"
                if component == "sensor":
                    cfg["config"]["state_class"] = "measurement"
                configs.append(cfg)

    return configs


def _infer_capability(attr: Dict[str, Any], profile: Dict[str, Any]) -> Optional[str]:
    """Best-effort capability inference from attribute mapping fields."""
    name = (attr.get("name") or "").lower()
    if "temp" in name:        return "temperature"
    if "humid" in name:       return "humidity"
    if "battery" in name:     return "battery"
    if "press" in name:       return "pressure"
    if "lux" in name or "illum" in name: return "illuminance"
    if "power" in name:       return "power"
    if "voltage" in name:     return "voltage"
    if "current" in name:     return "current"
    if name == "contact":     return "contact"
    if name in ("motion", "occupancy"): return "motion"
    if name in ("leak", "water_leak"):  return "water_leak"
    if name == "smoke":       return "smoke"
    if name == "vibration":   return "vibration"
    if name in ("state", "switch", "on_off"): return "on_off"
    # Fall back to first capability of the profile if any single one is implied
    dt = profile.get("device_type")
    if dt == "contact_sensor":     return "contact"
    if dt == "motion_sensor":      return "motion"
    if dt == "temperature_sensor": return "temperature"
    return None


def _humanise(name: str) -> str:
    return name.replace("_", " ").strip().title()