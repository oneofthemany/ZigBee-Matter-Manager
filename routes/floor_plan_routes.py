"""
Floor-plan API — read, save (projecting into circuits), preview and clear the
plan, plus per-level background images.

The plan is read and written only through modules/floor_plan_store; images are
files under data/floor_plans/. 20 MB, PNG/JPEG only — PDFs must be rendered
client-side. Served at /api/floor-plan, and at /api/heating/floor-plan for
existing clients. See docs/floor-plan.md.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import time
from typing import Any, Dict, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse

from modules import floor_plan_store
from modules.auth import scope_matches
from modules import daylight
from modules.mesh_plan import merge_links
from modules.radio_model import analyse
from modules.floor_plan import (
    changed_parts,
    clean_floor_plan,
    daylight_geometry,
    project_floor_plan_to_circuits,
)
from modules.location import home_coords

logger = logging.getLogger("routes.floor_plan")

CONFIG_PATH = "./config/config.yaml"
IMAGE_DIR = "./data/floor_plans"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg"}
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
# The alias keeps existing clients working; only /api/floor-plan is documented.
ALIAS = {"include_in_schema": False}

#: Scope each part of a save needs. docs/floor-plan.md § Who may change what.
PART_SCOPES = {"structure": "device:write", "heating": "heating:write"}


def _scopes(request: Request) -> list:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "Authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    return list(principal.scopes)


def _require(*alternatives: str):
    """Dependency: the caller holds at least one of ``alternatives``."""
    def dep(request: Request) -> None:
        granted = _scopes(request)
        if not any(scope_matches(s, granted) for s in alternatives):
            raise HTTPException(403, f"Needs {' or '.join(alternatives)}")
    return dep


READ = Depends(_require("device:read", "heating:read"))


def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def _safe_level_id(level_id: str) -> Optional[str]:
    if not isinstance(level_id, str):
        return None
    s = level_id.strip().lower()
    return s if _ID_RE.match(s) else None


def _existing_image_path(level_id: str) -> Optional[str]:
    for ext in ALLOWED_IMAGE_EXTS:
        p = os.path.join(IMAGE_DIR, f"{level_id}{ext}")
        if os.path.exists(p):
            return p
    return None


def _hourly_cloud(weather) -> Dict[str, float]:
    """Forecast cloud fraction by local hour ("YYYY-MM-DDTHH:00")."""
    try:
        hourly = weather.get_hourly_solar() if weather else None
    except Exception:
        hourly = None
    if not hourly:
        return {}
    return {str(t)[:13] + ":00": cf for t, cf in zip(hourly.get("times") or [],
                                                      hourly.get("cloud_fraction") or [])}


def register_floor_plan_routes(app: FastAPI, get_controller=None, get_weather=None,
                               get_mesh=None):

    os.makedirs(IMAGE_DIR, exist_ok=True)

    def _resolve_controller():
        if not get_controller:
            return None
        c = get_controller()
        if callable(c):
            try:
                c = c()
            except Exception:
                pass
        return c

    # plan read/write

    @app.get("/api/floor-plan")
    @app.get("/api/heating/floor-plan", **ALIAS)
    async def get_floor_plan(_=READ):
        """The saved plan (or null), and the home's coordinates for the map."""
        coords = home_coords(_load_config())
        return {"success": True, "plan": floor_plan_store.load_plan(),
                "home": {"lat": coords[0], "lon": coords[1]} if coords else None}

    @app.post("/api/floor-plan")
    @app.post("/api/heating/floor-plan", **ALIAS)
    async def post_floor_plan(req: Request):
        """
        Save the floor plan and project it into the controller circuits.

        Each part changed needs its own scope (``PART_SCOPES``), so someone
        without heating:write can place lights but not move a radiator.
        Request body: full floor-plan dict (see modules/floor_plan.py).
        Response: ``{success, plan, warnings, projected_room_ids}``.
        """
        granted = _scopes(req)
        try:
            raw = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        cleaned = clean_floor_plan(raw)
        if cleaned is None:
            return {"success": False, "error": "floor plan is empty or invalid"}

        # Cleaned both sides, so a plan saved before a schema change does not
        # read as changed where only the cleaner's defaults differ.
        parts = changed_parts(clean_floor_plan(floor_plan_store.load_plan()), cleaned)
        missing = sorted({PART_SCOPES[p] for p in parts
                          if not scope_matches(PART_SCOPES[p], granted)})
        if missing:
            what = {"device:write": "walls, rooms or device positions",
                    "heating:write": "radiators, sensors, contacts or circuits"}
            return JSONResponse(status_code=403, content={
                "success": False, "missing_scopes": missing,
                "error": "You can't change " + " or ".join(what[m] for m in missing)
                         + f" (needs {', '.join(missing)})."})

        cfg = _load_config()
        heating = cfg.setdefault("heating", {})
        controller = heating.setdefault("controller", {})

        # floor-plan save rather than being reset to defaults.
        existing_circuits = (
                controller.get("circuits")
                or heating.get("circuits")
                or []
        )

        try:
            updated_circuits, warnings = project_floor_plan_to_circuits(
                cleaned, existing_circuits
            )
            from routes.heating_controller_routes import normalise_circuits
            updated_circuits = normalise_circuits(updated_circuits)
        except Exception as e:
            logger.exception("floor plan projection failed")
            return {"success": False, "error": f"projection failed: {e}"}

        try:
            floor_plan_store.save_plan(cleaned)
        except Exception as e:
            logger.exception("floor plan write failed")
            return {"success": False, "error": f"could not write floor plan: {e}"}

        controller["circuits"] = updated_circuits
        # Ensure the controller knows it is in floor-plan mode
        controller.setdefault("config_mode", "floor_plan")
        floor_plan_store.drop_legacy_key(cfg)

        try:
            _save_config(cfg)
        except Exception as e:
            logger.exception("config write failed")
            return {"success": False, "error": f"could not write config: {e}"}

        ctrl = _resolve_controller()
        if ctrl is not None:
            try:
                if hasattr(ctrl, "apply_config"):
                    # Pass the full heating block so apply_config resolves
                    # circuits with mode-awareness (floor_plan -> controller.circuits)
                    await ctrl.apply_config(heating)
                elif hasattr(ctrl, "circuits"):
                    ctrl.circuits = updated_circuits
            except Exception as e:
                logger.warning(f"controller hot-apply failed: {e}")
                warnings.append(f"controller hot-apply failed: {e}")

        projected_room_ids = [
            r["id"] for c in updated_circuits
            for r in (c.get("rooms") or [])
            if r.get("floor_plan_ref")
        ]

        return {
            "success": True,
            "plan": cleaned,
            "warnings": warnings,
            "projected_room_ids": projected_room_ids,
        }

    @app.get("/api/floor-plan/daylight")
    async def room_daylight(step_minutes: int = 30, _=READ):
        """Each windowed room's estimated daylight across today, for the editor.

        The same model the rooms' daylight devices publish (docs/daylight.md
        §7), on the saved plan: the current weather near now, the hourly cloud
        forecast elsewhere, a clear sky where there is neither.
        """
        weather = get_weather() if get_weather else None
        coords = None
        if weather and weather.latitude not in (None, "") and weather.longitude not in (None, ""):
            coords = (float(weather.latitude), float(weather.longitude))
        coords = coords or home_coords(_load_config())
        if not coords:
            return {"success": False, "error": "Set the home location to estimate daylight."}
        step = max(10, min(120, int(step_minutes))) * 60
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        times = [midnight + i * step for i in range(int(86400 / step) + 1)]
        current = weather.get_current() if weather else None
        clouds = _hourly_cloud(weather)
        rooms = daylight_geometry(floor_plan_store.load_plan())
        skies = [daylight.sky(t, coords, current,
                              clouds.get(time.strftime("%Y-%m-%dT%H:00", time.localtime(t))))
                 for t in times]
        out = []
        for room in rooms:
            lux, sun = [], []
            for sk in skies:
                v, s_in = daylight.room_lux(room, sk["lux"], sk["azimuth"], sk["elevation"],
                                            sk["cloud"]) if sk else (0.0, False)
                lux.append(daylight.round_lux(v))
                sun.append(1 if s_in else 0)
            out.append({"room_id": room["room_id"], "name": room["name"],
                        "level_id": room["level_id"], "lux": lux, "sun": sun})
        return {"success": True, "times": [int(t) for t in times], "now": int(now),
                "outdoor": [daylight.round_lux(sk["lux"]) if sk else 0 for sk in skies],
                "rooms": out}

    @app.get("/api/floor-plan/mesh")
    async def mesh_links(_=Depends(_require("system:read"))):
        """The mesh, one link per pair, for drawing on the plan.

        Positions are the editor's to resolve: it draws the plan being edited,
        not the saved one. system:read, as /api/network is.
        """
        try:
            mesh = get_mesh() if get_mesh else None
        except Exception as e:
            logger.warning(f"mesh unavailable: {e}")
            mesh = None
        if mesh is None:
            return {"success": False, "error": "The Zigbee network isn't available."}
        return {"success": True, **merge_links(mesh)}

    @app.get("/api/floor-plan/coverage")
    async def coverage(step: float = 0.5, _=Depends(_require("system:read"))):
        """Learned attenuation, predicted RSSI per level, and repeater advice.

        Off the loop: it is a grid search over every wall, and this shares its
        loop with the audio engine. Model: docs/signal-coverage.md.
        """
        try:
            mesh = get_mesh() if get_mesh else None
        except Exception as e:
            logger.warning(f"mesh unavailable: {e}")
            mesh = None
        if mesh is None:
            return {"success": False, "error": "The Zigbee network isn't available."}
        plan = floor_plan_store.load_plan()
        if not plan:
            return {"success": False, "error": "Draw the floor plan first."}
        step = max(0.25, min(2.0, float(step)))
        result = await asyncio.to_thread(analyse, plan, mesh, step)
        return {"success": True, **result}

    @app.get("/api/floor-plan/preview")
    @app.get("/api/heating/floor-plan/preview", **ALIAS)
    async def preview_floor_plan(_=Depends(_require("heating:read"))):
        """Dry-run projection: shows what the saved plan would write."""
        cfg = _load_config()
        heating = cfg.get("heating") or {}
        plan = floor_plan_store.load_plan()
        circuits = (heating.get("controller") or {}).get("circuits") or []
        if not plan:
            return {"success": False, "error": "no floor plan saved"}
        try:
            updated, warnings = project_floor_plan_to_circuits(plan, circuits)
        except Exception as e:
            return {"success": False, "error": f"projection failed: {e}"}
        return {"success": True, "circuits": updated, "warnings": warnings}

    @app.delete("/api/floor-plan")
    @app.delete("/api/heating/floor-plan", **ALIAS)
    async def delete_floor_plan(_=Depends(_require("device:write")),
                                __=Depends(_require("heating:write"))):
        """Remove the saved plan (and all level background images)."""
        try:
            floor_plan_store.delete_plan()
        except Exception as e:
            return {"success": False, "error": f"could not delete floor plan: {e}"}
        cfg = _load_config()
        if floor_plan_store.drop_legacy_key(cfg):
            try:
                _save_config(cfg)
            except Exception as e:
                return {"success": False, "error": f"could not write config: {e}"}
        if os.path.isdir(IMAGE_DIR):
            for fn in os.listdir(IMAGE_DIR):
                p = os.path.join(IMAGE_DIR, fn)
                try:
                    os.remove(p)
                except Exception:
                    pass
        return {"success": True}

    # background images

    @app.post("/api/floor-plan/image/{level_id}")
    @app.post("/api/heating/floor-plan/image/{level_id}", **ALIAS)
    async def upload_floor_plan_image(level_id: str, file: UploadFile = File(...),
                                      _=Depends(_require("device:write"))):
        """
        Upload a background image for a level. PNG and JPEG only.

        PDFs are rendered to PNG client-side via pdf.js before upload, so
        the user can drop a PDF in the UI and the first page is captured.

        Body: multipart/form-data with a single ``file`` field.
        Limits: 20 MB.
        """
        lid = _safe_level_id(level_id)
        if not lid:
            return JSONResponse({"success": False, "error": "invalid level_id"}, status_code=400)

        ctype = (file.content_type or "").lower()
        if ctype not in ALLOWED_IMAGE_TYPES:
            return JSONResponse(
                {"success": False, "error": f"unsupported content-type {ctype!r}; "
                                            "allowed: PNG, JPEG"},
                status_code=415,
            )

        ext = ".png" if ctype == "image/png" else ".jpg"
        target = os.path.join(IMAGE_DIR, f"{lid}{ext}")
        os.makedirs(IMAGE_DIR, exist_ok=True)

        for prior_ext in ALLOWED_IMAGE_EXTS:
            prior = os.path.join(IMAGE_DIR, f"{lid}{prior_ext}")
            if prior != target and os.path.exists(prior):
                try:
                    os.remove(prior)
                except Exception:
                    pass

        written = 0
        try:
            with open(target, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 64)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_IMAGE_BYTES:
                        out.close()
                        os.remove(target)
                        return JSONResponse(
                            {"success": False, "error": f"image > {MAX_IMAGE_BYTES} bytes"},
                            status_code=413,
                        )
                    out.write(chunk)
        except Exception as e:
            logger.exception("image upload write failed")
            try:
                os.remove(target)
            except Exception:
                pass
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

        return {
            "success": True,
            "level_id": lid,
            "url": f"/api/floor-plan/image/{lid}",
            "bytes": written,
            "content_type": ctype,
        }

    @app.get("/api/floor-plan/image/{level_id}")
    @app.get("/api/heating/floor-plan/image/{level_id}", **ALIAS)
    async def get_floor_plan_image(level_id: str, _=READ):
        """Return the background image for a level, or 404."""
        lid = _safe_level_id(level_id)
        if not lid:
            return JSONResponse({"success": False, "error": "invalid level_id"}, status_code=400)
        path = _existing_image_path(lid)
        if not path:
            return JSONResponse({"success": False, "error": "no image"}, status_code=404)
        media_type, _ = mimetypes.guess_type(path)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.delete("/api/floor-plan/image/{level_id}")
    @app.delete("/api/heating/floor-plan/image/{level_id}", **ALIAS)
    async def delete_floor_plan_image(level_id: str, _=Depends(_require("device:write"))):
        """Remove the background image for a level."""
        lid = _safe_level_id(level_id)
        if not lid:
            return JSONResponse({"success": False, "error": "invalid level_id"}, status_code=400)
        path = _existing_image_path(lid)
        if path:
            try:
                os.remove(path)
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": True}