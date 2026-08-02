"""
Signal Inspector routes — snapshot, start/stop live streaming (which emits
signal_inspector_update over the websocket), and clear for a fresh baseline
before a learn-by-demonstration capture.

Backed by modules.signal_inspector. See docs/debugging.md.
"""
import logging
from typing import Any, Dict

from fastapi import FastAPI

from modules.signal_inspector import get_signal_inspector

logger = logging.getLogger("routes.signals")


def register_signal_routes(app: FastAPI, get_zigbee_service):
    """Register signal-inspector routes."""

    def _exists(ieee: str) -> bool:
        # Matter devices flow through update_state too, so accept any device
        # the service knows about; fall back to "always allow" if the service
        # can't be queried (the inspector simply returns an empty snapshot).
        try:
            svc = get_zigbee_service()
            if svc is not None and hasattr(svc, "devices"):
                return ieee in svc.devices
        except Exception:
            pass
        return True

    @app.get("/api/signals/{ieee}")
    async def get_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            return {
                "success": True,
                "ieee": "all",
                "active": inspector.is_watching_all(),
                "signals": inspector.snapshot_all(),
            }
        return {
            "success": True,
            "ieee": ieee,
            "active": inspector.is_active(ieee),
            "signals": inspector.snapshot(ieee),
        }

    @app.post("/api/signals/{ieee}/start")
    async def start_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            inspector.start_all()
            logger.info("Signal inspection started (all devices)")
            return {
                "success": True, "ieee": "all", "active": True,
                "signals": inspector.snapshot_all(),
            }
        if not _exists(ieee):
            return {"success": False, "error": "Device not found"}
        inspector.start(ieee)
        logger.info(f"[{ieee}] Signal inspection started")
        return {
            "success": True,
            "ieee": ieee,
            "active": True,
            "signals": inspector.snapshot(ieee),
        }

    @app.post("/api/signals/{ieee}/stop")
    async def stop_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            inspector.stop_all()
            logger.info("Signal inspection stopped (all devices)")
            return {"success": True, "ieee": "all", "active": False}
        inspector.stop(ieee)
        logger.info(f"[{ieee}] Signal inspection stopped")
        return {"success": True, "ieee": ieee, "active": False}

    @app.post("/api/signals/{ieee}/clear")
    async def clear_signals(ieee: str):
        inspector = get_signal_inspector()
        if ieee == "all":
            # Clear every device's recorded signals.
            for _ieee in [s.get("ieee") for s in inspector.snapshot_all(limit=100000)]:
                if _ieee:
                    inspector.clear(_ieee)
            return {"success": True, "ieee": "all", "signals": []}
        inspector.clear(ieee)
        return {"success": True, "ieee": ieee, "signals": []}

    # learn-by-demonstration

    def _device(ieee: str):
        try:
            svc = get_zigbee_service()
            if svc is not None and hasattr(svc, "devices"):
                return svc.devices.get(ieee)
        except Exception:
            pass
        return None

    @app.post("/api/signals/{ieee}/learn/baseline")
    async def learn_baseline(ieee: str):
        """Snapshot current signal values, then the user operates the device."""
        if ieee == "all":
            return {"success": False, "error": "Pick a specific device to learn"}
        if not _exists(ieee):
            return {"success": False, "error": "Device not found"}
        inspector = get_signal_inspector()
        inspector.start(ieee)                 # ensure we're capturing
        n = inspector.mark_baseline(ieee)
        logger.info(f"[{ieee}] Learn baseline set ({n} signals)")
        return {"success": True, "ieee": ieee, "baseline_signals": n}

    @app.get("/api/signals/{ieee}/learn/diff")
    async def learn_diff(ieee: str):
        """Return the signals that moved since the baseline, ranked."""
        inspector = get_signal_inspector()
        if not inspector.has_baseline(ieee):
            return {"success": False, "error": "No baseline — start learning first"}
        return {"success": True, "ieee": ieee, "changes": inspector.diff(ieee)}

    @app.post("/api/signals/{ieee}/learn/map")
    async def learn_map(ieee: str, data: Dict[str, Any] = None):
        """Persist a friendly mapping for a demonstrated signal.

        Body: {state_key, friendly_name, scale?, unit?, device_class?, invert?}
        Mappings are keyed by the literal state key (``state:<key>``), which
        uniformly covers ZCL attributes, Tuya datapoints and derived keys.
        """
        data = data or {}
        friendly = (data.get("friendly_name") or "").strip()

        from modules.device_profiles import get_profile_store, _to_int

        # command -> action mapping
        # A demonstrated button command becomes a named `action` (the z2m/ZHA
        # convention automations trigger on). Keyed by ep/cluster/command.
        if data.get("command"):
            if not friendly:
                return {"success": False, "error": "action name required"}
            ep = _to_int(data.get("endpoint"))
            cl = _to_int(data.get("cluster"))
            cmd = _to_int(data.get("item"))
            if ep is None or cl is None or cmd is None:
                return {"success": False, "error": "endpoint, cluster and item required for a command"}
            base = f"cmd:{ep}/{cl}/{cmd}"
            # When match_args is set, bind the action to this exact payload
            # (e.g. single vs double press on the same command id).
            disc = (data.get("arg_disc") or "").strip()
            raw_key = f"{base}/{disc}" if (data.get("match_args") and disc) else base
            get_profile_store().set_ieee_mapping(ieee, raw_key, friendly)
            logger.info(f"[{ieee}] Learned action {raw_key} -> {friendly!r}")
            return {"success": True, "ieee": ieee, "raw_key": raw_key,
                    "friendly_name": friendly, "kind": "action",
                    "match_args": bool(data.get("match_args") and disc)}

        state_key = (data.get("state_key") or "").strip()
        if not state_key:
            return {"success": False, "error": "state_key required (map a value signal, not a command)"}
        if not friendly:
            return {"success": False, "error": "friendly_name required"}

        try:
            scale = float(data.get("scale") or 1)
        except (TypeError, ValueError):
            scale = 1.0
        unit = str(data.get("unit") or "")
        device_class = str(data.get("device_class") or "")
        invert = bool(data.get("invert", False))

        raw_key = f"state:{state_key}"
        get_profile_store().set_ieee_mapping(
            ieee, raw_key, friendly,
            scale=scale, unit=unit, device_class=device_class, invert=invert,
        )
        logger.info(f"[{ieee}] Learned mapping {state_key!r} -> {friendly!r}")

        # Surface the friendly key immediately by re-transforming current state.
        dev = _device(ieee)
        applied = None
        if dev is not None:
            try:
                from modules.device_profiles_apply import transform_state_with_profile
                new = transform_state_with_profile(dev, dev.state)
                added = {k: v for k, v in new.items() if k not in dev.state}
                if added and hasattr(dev, "update_state"):
                    dev.update_state(added)
                applied = new.get(friendly)
            except Exception as e:
                logger.debug(f"[{ieee}] immediate re-transform failed: {e}")

        return {
            "success": True, "ieee": ieee,
            "raw_key": raw_key, "friendly_name": friendly, "value": applied,
        }

    @app.post("/api/signals/{ieee}/learn/unmap")
    async def learn_unmap(ieee: str, data: Dict[str, Any] = None):
        data = data or {}
        from modules.device_profiles import get_profile_store
        store = get_profile_store()
        raw_key = (data.get("raw_key") or "").strip()
        if not raw_key:
            state_key = (data.get("state_key") or "").strip()
            if not state_key:
                return {"success": False, "error": "raw_key or state_key required"}
            raw_key = f"state:{state_key}"
        # Remember the friendly key so we can strip it from live state.
        m = store.get_ieee_mapping(ieee, raw_key)
        ok = store.remove_ieee_mapping(ieee, raw_key)
        if ok and m and m.get("name"):
            dev = _device(ieee)
            if dev is not None and isinstance(getattr(dev, "state", None), dict):
                dev.state.pop(m["name"], None)
        return {"success": ok, "ieee": ieee, "raw_key": raw_key}

    # mapped-signals management

    def _safe(v):
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        try:
            return str(v)
        except Exception:
            return None

    def _describe_mapping(raw_key: str, m: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(m, dict):
            m = {"name": str(m)}
        e = {
            "raw_key":      raw_key,
            "friendly_name": m.get("name", ""),
            "scale":        m.get("scale", 1),
            "unit":         m.get("unit", ""),
            "device_class": m.get("device_class", ""),
            "invert":       bool(m.get("invert", False)),
        }
        if raw_key.startswith("state:"):
            src = raw_key[len("state:"):]
            e.update(kind="value", source_key=src, label=src,
                     current=_safe(state.get(m.get("name"))))
        elif raw_key.startswith("cmd:"):
            parts = raw_key[len("cmd:"):].split("/")
            e["kind"] = "action"
            if len(parts) >= 3:
                try:
                    ep, cl, cmd = int(parts[0]), int(parts[1]), int(parts[2])
                    disc = "/".join(parts[3:]) if len(parts) > 3 else ""
                    e.update(endpoint=ep,
                             cluster=f"0x{cl:04X}", command=f"0x{cmd:02X}",
                             payload=disc,
                             label=(f"EP{ep} · 0x{cl:04X} cmd 0x{cmd:02X}"
                                    + (f" · {disc}" if disc else " · any press")))
                except ValueError:
                    e["label"] = raw_key
            else:
                e["label"] = raw_key
        else:
            # Legacy cluster_XXXX_attr_XXXX profile mapping
            e.update(kind="attribute", label=raw_key)
        return e

    @app.post("/api/signals/{ieee}/promote")
    async def promote_mappings(ieee: str, data: Dict[str, Any] = None):
        """Promote this device's learned mappings into a shareable model
        profile so every device of the same model inherits them."""
        import copy
        import re
        dev = _device(ieee)
        if dev is None:
            return {"success": False, "error": "Device not found"}
        from modules.device_profiles import get_profile_store
        from modules.device_profiles_apply import _get_device_identity, apply_profile

        ident = _get_device_identity(dev)
        model = ident.get("model") or ""
        manuf = ident.get("manufacturer") or ""
        if not model:
            return {"success": False,
                    "error": "Device has no model identifier — cannot build a shareable profile"}

        store = get_profile_store()
        mappings = store.get_ieee_mappings(ieee)
        if not mappings:
            return {"success": False, "error": "No learned mappings to promote"}

        # Merge into an existing profile for this model, if any.
        existing = store.get_profile_for_device(
            protocol="zigbee", model=model, manufacturer=manuf) or {}
        state_mappings = dict(existing.get("state_mappings") or {})
        command_actions = dict(existing.get("command_actions") or {})
        endpoints = copy.deepcopy(existing.get("endpoints") or {})

        n_state = n_cmd = n_attr = 0
        for raw_key, m in mappings.items():
            if not isinstance(m, dict):
                m = {"name": str(m)}
            if raw_key.startswith("state:"):
                state_mappings[raw_key[len("state:"):]] = m
                n_state += 1
            elif raw_key.startswith("cmd:"):
                command_actions[raw_key[len("cmd:"):]] = {"name": m.get("name")}
                n_cmd += 1
            elif raw_key.startswith("cluster_"):
                mm = re.match(r"cluster_([0-9a-fA-F]+)_attr_([0-9a-fA-F]+)", raw_key)
                if mm:
                    clhex = f"0x{int(mm.group(1), 16):04X}"
                    athex = f"0x{int(mm.group(2), 16):04X}"
                    ep = endpoints.setdefault("1", {"role": "primary", "clusters": {}})
                    cl = ep.setdefault("clusters", {}).setdefault(clhex, {"attributes": {}})
                    cl.setdefault("attributes", {})[athex] = m
                    n_attr += 1

        profile_in = {
            "id":            existing.get("id") or model,
            "protocol":      "zigbee",
            "match":         existing.get("match") or {"model": model, "manufacturer": manuf},
            "device_type":   existing.get("device_type") or "generic",
            "capabilities":  existing.get("capabilities") or [],
            "endpoints":     endpoints,
            "actions":       existing.get("actions") or [],
            "reporting":     existing.get("reporting") or [],
            "state_mappings":  state_mappings,
            "command_actions": command_actions,
            "meta":          {"source": "user"},
        }
        saved = store.upsert_profile(profile_in)

        # Apply to every currently-loaded device of the same model so it takes
        # effect immediately (esp. the command->action wiring).
        applied = 0
        try:
            svc = get_zigbee_service()
            for d in list(getattr(svc, "devices", {}).values()):
                di = _get_device_identity(d)
                if di.get("model") == model and (not manuf or di.get("manufacturer") == manuf):
                    try:
                        await apply_profile(d)
                        applied += 1
                    except Exception:
                        pass
        except Exception:
            pass

        logger.info(f"[{ieee}] Promoted mappings to profile {saved['id']!r} "
                    f"(values={n_state}, actions={n_cmd}, attrs={n_attr}; applied to {applied})")
        return {
            "success": True, "profile_id": saved["id"], "model": model,
            "promoted": {"values": n_state, "actions": n_cmd, "attributes": n_attr},
            "applied_to_devices": applied,
        }

    @app.get("/api/signals/{ieee}/mappings")
    async def list_mappings(ieee: str):
        from modules.device_profiles import get_profile_store
        raw = get_profile_store().get_ieee_mappings(ieee)
        dev = _device(ieee)
        state = getattr(dev, "state", {}) if dev is not None else {}
        out = [_describe_mapping(k, m, state) for k, m in raw.items()]
        out.sort(key=lambda e: (e.get("kind", ""), e.get("friendly_name", "")))
        return {"success": True, "ieee": ieee, "count": len(out), "mappings": out}

    @app.post("/api/signals/{ieee}/mappings/update")
    async def update_mapping(ieee: str, data: Dict[str, Any] = None):
        data = data or {}
        from modules.device_profiles import get_profile_store
        store = get_profile_store()
        raw_key = (data.get("raw_key") or "").strip()
        name = (data.get("friendly_name") or "").strip()
        if not raw_key or not name:
            return {"success": False, "error": "raw_key and friendly_name required"}
        existing = store.get_ieee_mapping(ieee, raw_key)
        if not existing:
            return {"success": False, "error": "mapping not found"}

        try:
            scale = float(data.get("scale") or 1)
        except (TypeError, ValueError):
            scale = 1.0
        unit = str(data.get("unit") or "")
        device_class = str(data.get("device_class") or "")
        invert = bool(data.get("invert", False))

        store.set_ieee_mapping(ieee, raw_key, name, scale=scale, unit=unit,
                               device_class=device_class, invert=invert)

        # For value mappings, drop the old friendly key (if renamed) and
        # re-surface the new one immediately.
        dev = _device(ieee)
        if dev is not None and raw_key.startswith("state:"):
            old_name = existing.get("name")
            if isinstance(getattr(dev, "state", None), dict) and old_name and old_name != name:
                dev.state.pop(old_name, None)
            try:
                from modules.device_profiles_apply import transform_state_with_profile
                new = transform_state_with_profile(dev, dev.state)
                added = {k: v for k, v in new.items() if k not in dev.state}
                if added and hasattr(dev, "update_state"):
                    dev.update_state(added)
            except Exception:
                pass
        return {"success": True, "ieee": ieee, "raw_key": raw_key}
