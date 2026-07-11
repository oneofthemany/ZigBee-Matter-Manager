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
GET    /api/ac/units/{unit_id}/timers
POST   /api/ac/units/{unit_id}/timer     {in_minutes, changes} — app-side
                                        timer: applies `changes` after the
                                        delay (neither vendor protocol
                                        exposes its onboard timer usefully)
DELETE /api/ac/timers/{timer_id}

Timers persist in data/ac_timers.json and are rescheduled on startup (the
lifespan in main.py calls app.state.ac_timers_start once the loop runs).
Timers missed by more than 15 min while the app was down are dropped —
firing an hours-stale "turn on" after a long outage is worse than skipping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, Request

logger = logging.getLogger("zbm.ac")

CONFIG_PATH = "./config/config.yaml"
TIMERS_PATH = "./data/ac_timers.json"
TIMER_MISSED_GRACE_SEC = 900     # fire timers missed by ≤15 min on restart
TIMER_MAX_MINUTES = 24 * 60


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
    state: Dict[str, Any] = {"controller": None, "timer_tasks": {}}

    def _persist_key(unit_id: str, key: str, cipher_version: int = 1) -> None:
        """Gree bind() derived a device key — write it (and which cipher
        version the device negotiated) back to config; both are needed to
        re-bind without a fresh key exchange."""
        cfg = _load_config()
        units = ((cfg.get("ac") or {}).get("units")) or []
        for u in units:
            if str(u.get("id")) == str(unit_id):
                u["key"] = key
                u["cipher"] = int(cipher_version)
                break
        cfg.setdefault("ac", {})["units"] = units
        _save_config(cfg)
        logger.info(f"AC: persisted learned gree key for {unit_id} "
                    f"(cipher v{cipher_version})")

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

    # ── device-list integration ────────────────────────────────────
    # /api/devices (device_routes) appends these so AC units show up in
    # the main device list alongside zigbee/matter. Pseudo-ieee = unit id,
    # protocol "wifi"; the frontend routes Manage to the AC modal via
    # ac_unit_id.

    async def _device_list_entries() -> list:
        import time as _time
        async def _one(u):
            # /api/devices is a hot path — don't let one cold/offline unit
            # stall the whole device list (controller caches errors, so the
            # slow probe happens at most once per cache window).
            try:
                return await asyncio.wait_for(
                    ctl.status(str(u.get("id")), max_age_sec=15.0), timeout=4.0)
            except Exception as e:
                return {"online": False, "error": str(e)}

        try:
            ctl = _controller()
            if not ctl.units:
                return []
            statuses = await asyncio.gather(*(_one(u) for u in ctl.units))
        except Exception as e:
            logger.warning(f"AC device-list entries failed: {e}")
            return []
        entries = []
        for u, s in zip(ctl.units, statuses):
            online = bool(s.get("online"))
            entries.append({
                "ieee": str(u.get("id")),
                "ac_unit_id": str(u.get("id")),
                "friendly_name": u.get("name") or u.get("id"),
                "type": "AirConditioner",
                "protocol": "wifi",
                "manufacturer": str(u.get("brand") or "").capitalize(),
                "model": u.get("model") or str(u.get("brand") or "").capitalize(),
                "available": online,
                "ip_addresses": [u.get("host")] if u.get("host") else [],
                "last_seen_ts": int(_time.time() * 1000) if online else None,
                "state": {k: s.get(k) for k in
                          ("power", "mode", "target_c", "current_c", "fan")
                          if s.get(k) is not None},
            })
        return entries

    app.state.ac_device_entries = _device_list_entries

    def _rename_unit(unit_id: str, name: str) -> bool:
        """Rename hook for /api/device/rename (AC pseudo-ieee = unit id)."""
        cfg = _load_config()
        units = ((cfg.get("ac") or {}).get("units")) or []
        for u in units:
            if str(u.get("id")) == str(unit_id):
                u["name"] = name
                cfg.setdefault("ac", {})["units"] = units
                _save_config(cfg)
                state["controller"] = None
                return True
        return False

    app.state.ac_rename_unit = _rename_unit

    # ── app-side timers ────────────────────────────────────────────

    def _load_timers() -> list:
        try:
            with open(TIMERS_PATH, "r") as f:
                return json.load(f) or []
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning(f"AC timers load failed: {e}")
            return []

    def _save_timers(timers: list) -> None:
        os.makedirs(os.path.dirname(TIMERS_PATH), exist_ok=True)
        with open(TIMERS_PATH, "w") as f:
            json.dump(timers, f, indent=2)

    def _schedule_timer(t: Dict[str, Any]) -> None:
        """Create the asyncio task that fires this (already persisted) timer."""
        async def _run():
            try:
                delay = float(t["at"]) - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    await _controller().control(str(t["unit_id"]), t["changes"])
                    logger.info(f"AC timer fired for {t['unit_id']}: "
                                f"{t['changes']}")
                except Exception as e:
                    logger.error(f"AC timer for {t.get('unit_id')} failed: {e}")
                # fired (or consumed by a failure) — drop the persisted entry
                state["timer_tasks"].pop(t["id"], None)
                _save_timers([x for x in _load_timers()
                              if x.get("id") != t["id"]])
            except asyncio.CancelledError:
                # explicit cancel (endpoint already rewrote the file) or app
                # shutdown — keep the persisted entry so a restart restores it
                state["timer_tasks"].pop(t["id"], None)
                raise
        state["timer_tasks"][t["id"]] = asyncio.get_event_loop().create_task(_run())

    def _start_timers() -> None:
        """Reschedule persisted timers — called from the lifespan in main.py
        once the event loop is running (on_event doesn't fire with an
        explicit lifespan)."""
        now = time.time()
        keep = []
        for t in _load_timers():
            if now - float(t.get("at", 0)) > TIMER_MISSED_GRACE_SEC:
                logger.warning(f"AC timer for {t.get('unit_id')} missed while "
                               f"down — dropped ({t.get('changes')})")
                continue
            keep.append(t)
            _schedule_timer(t)
        _save_timers(keep)
        if keep:
            logger.info(f"AC: rescheduled {len(keep)} persisted timer(s)")

    app.state.ac_timers_start = _start_timers

    @app.get("/api/ac/units/{unit_id}/timers")
    async def list_timers(unit_id: str):
        timers = [t for t in _load_timers()
                  if str(t.get("unit_id")) == str(unit_id)]
        return {"success": True,
                "timers": sorted(timers, key=lambda t: t.get("at", 0))}

    @app.post("/api/ac/units/{unit_id}/timer")
    async def create_timer(unit_id: str, req: Request):
        try:
            body = await req.json()
            minutes = float(body.get("in_minutes") or 0)
            changes = body.get("changes")
            if not (0 < minutes <= TIMER_MAX_MINUTES):
                return {"success": False,
                        "error": f"in_minutes must be 1..{TIMER_MAX_MINUTES}"}
            if not isinstance(changes, dict) or not changes:
                return {"success": False,
                        "error": "changes must be a non-empty object "
                                 "(e.g. {\"power\": false})"}
            if not _controller().unit_config(unit_id):
                return {"success": False, "error": f"unknown unit '{unit_id}'"}
            t = {"id": uuid.uuid4().hex[:12], "unit_id": str(unit_id),
                 "at": time.time() + minutes * 60, "changes": changes,
                 "created_at": time.time()}
            _save_timers(_load_timers() + [t])
            _schedule_timer(t)
            return {"success": True, "timer": t}
        except Exception as e:
            logger.error(f"AC timer create failed for {unit_id}: {e}",
                         exc_info=True)
            return {"success": False, "error": str(e)}

    @app.delete("/api/ac/timers/{timer_id}")
    async def cancel_timer(timer_id: str):
        timers = _load_timers()
        if not any(t.get("id") == timer_id for t in timers):
            return {"success": False, "error": f"unknown timer '{timer_id}'"}
        task = state["timer_tasks"].pop(timer_id, None)
        if task is not None:
            task.cancel()
        _save_timers([t for t in timers if t.get("id") != timer_id])
        return {"success": True}

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
            cfg = _load_config()
            ac_cfg = cfg.setdefault("ac", {})
            units = ac_cfg.setdefault("units", [])

            unit_id = body.get("id")
            existing = next((u for u in units if str(u.get("id")) == str(unit_id)), None) \
                if unit_id else None

            # updates by id may be partial — fall back to stored values
            merged = {**(existing or {}), **{k: v for k, v in body.items()
                                             if v is not None}}
            brand = str(merged.get("brand") or "").lower()
            if brand not in ("gree", "midea"):
                return {"success": False, "error": "brand must be gree|midea"}
            if not merged.get("host"):
                return {"success": False, "error": "host is required"}
            if brand == "midea" and not merged.get("device_id"):
                return {"success": False, "error": "device_id is required for midea"}

            if existing is None and not unit_id:
                unit_id = _slug(body.get("name") or brand)
                # avoid collisions
                base, n = unit_id, 2
                while any(str(u.get("id")) == unit_id for u in units):
                    unit_id = f"{base}_{n}"
                    n += 1

            allowed = ("name", "brand", "host", "port", "mac", "key",
                       "cipher", "device_id", "token", "protocol", "model",
                       "subtype", "room_id")
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
                unit.pop("cipher", None)
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
