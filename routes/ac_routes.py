"""
ac_routes.py
============
API for local-LAN air-conditioner control (Gree / Midea units — EcoAir,
Comfee, and other clones). Thin HTTP layer over modules/ac_controller.py;
unit definitions persist in config.yaml under `ac.units`.

Endpoints
---------
GET    /api/ac/units                 — configured units with live status
POST   /api/ac/discover              — LAN scan for both protocols
POST   /api/ac/units                 — add or update a unit
DELETE /api/ac/units/{unit_id}       — remove a unit
GET    /api/ac/units/{unit_id}/status
POST   /api/ac/units/{unit_id}/control   {power, mode, target_c, fan, ...}
POST   /api/ac/units/{unit_id}/bind  — midea: fetch token/key (preset cloud
                                        account by default); gree: force a
                                        fresh key bind
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, Request

logger = logging.getLogger("zbm.ac")

CONFIG_PATH = "./config/config.yaml"


def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "ac").lower()).strip("_")
    return f"ac_{s or 'unit'}"


def register_ac_routes(app: FastAPI):
    from modules.ac_controller import ACController, ACError

    # Lazily (re)built so config edits take effect without a restart.
    state: Dict[str, Any] = {"controller": None}

    def _persist_key(unit_id: str, key: str) -> None:
        """Gree bind() derived a device key — write it back to config."""
        cfg = _load_config()
        units = ((cfg.get("ac") or {}).get("units")) or []
        for u in units:
            if str(u.get("id")) == str(unit_id):
                u["key"] = key
                break
        cfg.setdefault("ac", {})["units"] = units
        _save_config(cfg)
        logger.info(f"AC: persisted learned gree key for {unit_id}")

    def _controller() -> "ACController":
        cfg = _load_config()
        ac_cfg = cfg.get("ac") or {}
        ctl = state["controller"]
        if ctl is None:
            ctl = ACController(ac_cfg, on_key_learned=_persist_key)
            state["controller"] = ctl
        else:
            ctl.reload(ac_cfg)
        return ctl

    # ── listing / discovery ─────────────────────────────────────────

    @app.get("/api/ac/units")
    async def list_units():
        try:
            ctl = _controller()
            statuses = await asyncio.gather(
                *(ctl.status(str(u.get("id"))) for u in ctl.units))
            return {"success": True, "units": list(statuses)}
        except Exception as e:
            logger.error(f"AC list failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/ac/discover")
    async def discover(wait_for: int = 4):
        try:
            ctl = _controller()
            found = await ctl.discover(wait_for=wait_for)
            configured_hosts = {u.get("host") for u in ctl.units}
            for lst in (found.get("gree") or []), (found.get("midea") or []):
                for cand in lst:
                    cand["already_configured"] = cand.get("host") in configured_hosts

            # Midea units are identified by device_id, not IP. If a configured
            # unit shows up at a different address (DHCP moved it), heal the
            # stored host — a stale host is exactly how control "stops working".
            by_devid = {str(u.get("device_id")): u for u in ctl.units
                        if u.get("device_id") is not None}
            moved = []
            for cand in (found.get("midea") or []):
                unit = by_devid.get(str(cand.get("device_id")))
                if not unit:
                    continue
                cand["already_configured"] = True
                cand["configured_id"] = unit.get("id")
                if cand.get("host") and unit.get("host") != cand["host"]:
                    moved.append((unit.get("id"), unit.get("host"), cand["host"]))
            if moved:
                cfg = _load_config()
                units = ((cfg.get("ac") or {}).get("units")) or []
                for uid, old, new in moved:
                    for u in units:
                        if str(u.get("id")) == str(uid):
                            u["host"] = new
                            logger.info(f"AC: unit {uid} moved {old} → {new}; "
                                        f"updated stored host")
                cfg.setdefault("ac", {})["units"] = units
                _save_config(cfg)
                state["controller"] = None
                found["host_updates"] = [
                    {"id": uid, "old_host": old, "new_host": new}
                    for uid, old, new in moved]
            return {"success": True, "found": found}
        except Exception as e:
            logger.error(f"AC discovery failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ── unit CRUD ──────────────────────────────────────────────────

    @app.post("/api/ac/units")
    async def upsert_unit(req: Request):
        try:
            body = await req.json()
            brand = str(body.get("brand") or "").lower()
            if brand not in ("gree", "midea"):
                return {"success": False, "error": "brand must be gree|midea"}
            if not body.get("host"):
                return {"success": False, "error": "host is required"}
            if brand == "midea" and not body.get("device_id"):
                return {"success": False, "error": "device_id is required for midea"}

            cfg = _load_config()
            ac_cfg = cfg.setdefault("ac", {})
            units = ac_cfg.setdefault("units", [])

            unit_id = body.get("id")
            existing = next((u for u in units if str(u.get("id")) == str(unit_id)), None) \
                if unit_id else None
            if existing is None and not unit_id:
                unit_id = _slug(body.get("name") or brand)
                # avoid collisions
                base, n = unit_id, 2
                while any(str(u.get("id")) == unit_id for u in units):
                    unit_id = f"{base}_{n}"
                    n += 1

            allowed = ("name", "brand", "host", "port", "mac", "key",
                       "device_id", "token", "protocol", "model", "subtype",
                       "room_id")
            record = {k: body[k] for k in allowed if body.get(k) is not None}
            record["id"] = unit_id

            if existing is not None:
                existing.update(record)
            else:
                units.append(record)
            _save_config(cfg)
            state["controller"] = None   # rebuild adapters on next use
            return {"success": True, "unit": record}
        except Exception as e:
            logger.error(f"AC upsert failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.delete("/api/ac/units/{unit_id}")
    async def delete_unit(unit_id: str):
        try:
            cfg = _load_config()
            units = ((cfg.get("ac") or {}).get("units")) or []
            kept = [u for u in units if str(u.get("id")) != str(unit_id)]
            if len(kept) == len(units):
                return {"success": False, "error": f"unknown unit '{unit_id}'"}
            cfg.setdefault("ac", {})["units"] = kept
            _save_config(cfg)
            state["controller"] = None
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── status / control ───────────────────────────────────────────

    @app.get("/api/ac/units/{unit_id}/status")
    async def unit_status(unit_id: str):
        try:
            status = await _controller().status(unit_id, max_age_sec=0)
            return {"success": True, "status": status}
        except ACError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"AC status failed for {unit_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/ac/units/{unit_id}/control")
    async def unit_control(unit_id: str, req: Request):
        try:
            changes = await req.json()
            if not isinstance(changes, dict) or not changes:
                return {"success": False, "error": "body must be a JSON object "
                        "with power/mode/target_c/fan/…"}
            status = await _controller().control(unit_id, changes)
            return {"success": True, "status": status}
        except ACError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"AC control failed for {unit_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.post("/api/ac/units/{unit_id}/bind")
    async def unit_bind(unit_id: str, req: Request):
        """
        gree  → forget the cached key and re-bind (key is re-persisted).
        midea → fetch token/key from the Midea cloud (preset anonymous
                account unless {account, password, cloud_name} supplied)
                and persist them.
        """
        try:
            try:
                body = await req.json()
            except Exception:
                body = {}
            cfg = _load_config()
            units = ((cfg.get("ac") or {}).get("units")) or []
            unit = next((u for u in units if str(u.get("id")) == str(unit_id)), None)
            if not unit:
                return {"success": False, "error": f"unknown unit '{unit_id}'"}

            brand = str(unit.get("brand") or "").lower()
            ctl = _controller()
            if brand == "gree":
                unit.pop("key", None)
                cfg.setdefault("ac", {})["units"] = units
                _save_config(cfg)
                state["controller"] = None
                status = await _controller().status(unit_id, max_age_sec=0)
                return {"success": bool(status.get("online")), "status": status}

            if brand == "midea":
                keys = await ctl.fetch_midea_keys(
                    device_id=int(unit.get("device_id")),
                    account=body.get("account"),
                    password=body.get("password"),
                    cloud_name=body.get("cloud_name"),
                )
                unit["token"] = keys["token"]
                unit["key"] = keys["key"]
                cfg.setdefault("ac", {})["units"] = units
                _save_config(cfg)
                state["controller"] = None
                status = await _controller().status(unit_id, max_age_sec=0)
                return {"success": True, "status": status,
                        "available_protocols": keys.get("available_protocols")}

            return {"success": False, "error": f"unknown brand '{brand}'"}
        except ACError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"AC bind failed for {unit_id}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    logger.info("AC routes registered (gree + midea local control)")
