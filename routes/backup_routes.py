"""
Backup & Restore routes for full network migration.
Creates a downloadable zip containing all configuration, device database,
automations, groups, zones, and state — everything needed to rebuild
the network on a new container.
"""
import io
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile

from modules.auth_middleware import require_scope
from modules.backup import (APP_DIR, BACKUP_DIRS, BACKUP_MANIFEST, OPTIONAL_BACKUP_FILES,
                            RESTORE_PRIVATE, backup_name, build_zip, decrypt_bytes, entry_allowed,
                            get_backup_scheduler, is_encrypted)

logger = logging.getLogger("routes.backup")


def register_backup_routes(app: FastAPI, get_zigbee_service):
    """Register backup & restore API routes."""

    async def prepare(include_telemetry: bool) -> int:
        """Flush what is only in memory so the files on disk are current.
        Returns the device count for the manifest. Shared by the download
        and the nightly schedule (app.state.backup_prepare)."""
        svc = get_zigbee_service()
        if svc and hasattr(svc, '_cache_dirty') and svc._cache_dirty:
            svc._save_state_cache()
            svc._cache_dirty = False
        if svc and hasattr(svc, 'zone_manager') and svc.zone_manager:
            try:
                import yaml
                configs = svc.zone_manager.save_config()
                with open(os.path.join(APP_DIR, "data/zones.yaml"), "w") as f:
                    yaml.dump({"zones": configs}, f)
            except Exception as e:
                logger.warning(f"Could not flush zones before backup: {e}")
        if include_telemetry:
            # Drain appender buffers and merge each WAL, so the copied file is
            # whole. A WAL merge can take seconds: worker thread.
            import asyncio
            from modules import telemetry_db
            for label, flush in (("telemetry", lambda: (telemetry_db.flush_appender(),
                                                        telemetry_db._get_db().cursor().execute("CHECKPOINT"))),
                                 ("octopus", lambda: telemetry_db._get_octopus_db().cursor().execute("CHECKPOINT"))):
                try:
                    await asyncio.to_thread(flush)
                except Exception as e:
                    logger.warning(f"Could not flush {label} DB before backup: {e}")
        return len(svc.devices) if svc else 0

    app.state.backup_prepare = prepare

    @app.get("/api/backup/create")
    async def create_backup(include_telemetry: bool = True):
        """
        Create a full network backup as a downloadable .zip file — everything
        in modules/backup.py's manifest, and optionally the history databases.
        Built in a worker thread, to a temp file: a large telemetry DB would
        otherwise stall the event loop and sit in memory.
        """
        import asyncio
        import tempfile
        from starlette.background import BackgroundTask
        from fastapi.responses import FileResponse
        tmpdir = tempfile.mkdtemp(prefix="zmm-backup-")
        try:
            devices = await prepare(include_telemetry)
            path = Path(tmpdir) / "backup.zip"
            meta = await asyncio.to_thread(build_zip, path, include_telemetry, devices)
            filename = backup_name(include_telemetry)
            logger.info(f"Backup created: {filename} ({meta['included']} files)")
            return FileResponse(path, media_type="application/zip", filename=filename,
                                background=BackgroundTask(shutil.rmtree, tmpdir, True))
        except Exception as e:
            shutil.rmtree(tmpdir, True)
            logger.error(f"Backup creation failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}


    @app.post("/api/backup/restore")
    async def restore_backup(
            file: Optional[UploadFile] = File(None),
            url: Optional[str] = Form(None),
            passphrase: Optional[str] = Form(None),
    ):
        """
        Restore a full network backup from either:
          - a directly uploaded .zip file (multipart form)
          - a remote URL (the server fetches the zip itself)
        Overwrites config, data files, groups, and zigbee.db.
        A restart is required after restore to apply the new database.
        """
        # Acquire the zip bytes from whichever source was provided
        if file is not None:
            if not file.filename.endswith((".zip", ".zip.enc")):
                return {"success": False, "error": "File must be a .zip (or encrypted .zip.enc) backup"}
            contents = await file.read()

        elif url is not None:
            logger.info(f"Fetching backup from remote URL: {url}")
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    contents = resp.content
            except httpx.HTTPStatusError as e:
                return {"success": False, "error": f"Remote fetch failed (HTTP {e.response.status_code}): {url}"}
            except Exception as e:
                return {"success": False, "error": f"Failed to fetch backup from URL: {e}"}

        else:
            return {"success": False, "error": "Provide either a file upload or a 'url' field"}

        # An encrypted backup (docs/backups.md §Encryption) is a zip inside.
        if is_encrypted(contents):
            if not passphrase:
                return {"success": False, "needs_passphrase": True,
                        "error": "This backup is encrypted — enter its passphrase"}
            import asyncio
            try:
                contents = await asyncio.to_thread(decrypt_bytes, contents, passphrase)
            except ValueError as e:
                return {"success": False, "needs_passphrase": True, "error": str(e)}

        # Shared restore logic
        try:
            buffer = io.BytesIO(contents)

            with zipfile.ZipFile(buffer, "r") as zf:
                names = zf.namelist()
                if "backup_manifest.json" not in names:
                    return {
                        "success": False,
                        "error": "Invalid backup: missing backup_manifest.json",
                    }

                manifest = json.loads(zf.read("backup_manifest.json"))
                logger.info(
                    f"Restoring backup from {manifest.get('created_at', 'unknown')} "
                    f"({manifest.get('included', '?')} files, "
                    f"zip entries: {[n for n in names if n != 'backup_manifest.json']})"
                )

                _entry_allowed = entry_allowed

                restored = []
                errors = []

                for entry in names:
                    if entry == "backup_manifest.json" or entry.endswith("/"):
                        continue
                    if not _entry_allowed(entry):
                        logger.warning(f"Skipping unknown file in backup: {entry}")
                        continue

                    target = os.path.join(APP_DIR, os.path.normpath(entry))
                    try:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        data = zf.read(entry)
                        mode = 0o600 if os.path.normpath(entry) in RESTORE_PRIVATE else 0o666
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
                        with os.fdopen(fd, "wb") as f:
                            f.write(data)
                        if mode == 0o600:
                            os.chmod(target, mode)
                        restored.append(entry)
                        logger.info(f"Restored: {entry} ({len(data)} bytes)")
                    except Exception as e:
                        errors.append({"file": entry, "error": str(e)})
                        logger.error(f"Failed to restore {entry}: {e}")

            # Stale WAL files reference page offsets from the pre-restore main
            # file, so DuckDB would replay them against the new one — dropping the
            # restored data at best, corrupting it at worst.
            duckdb_restored = [e for e in restored if e.endswith(".duckdb")]
            for entry in duckdb_restored:
                wal_path = os.path.join(APP_DIR, entry + ".wal")
                if os.path.isfile(wal_path):
                    try:
                        os.remove(wal_path)
                        logger.info(f"Removed stale WAL: {entry}.wal")
                    except Exception as e:
                        logger.warning(f"Could not remove stale WAL {wal_path}: {e}")


            # A backup from before the plan left config.yaml carries it there and
            # no floor_plan.json; drop the live file so the restored plan is the
            # one migrated on the next start, not the plan being replaced.
            if "config/config.yaml" in restored and "data/floor_plan.json" not in restored:
                stale_plan = os.path.join(APP_DIR, "data/floor_plan.json")
                if os.path.isfile(stale_plan):
                    try:
                        os.remove(stale_plan)
                        logger.info("Removed data/floor_plan.json; the restored config's plan applies")
                    except Exception as e:
                        logger.warning(f"Could not remove {stale_plan}: {e}")

            # After extracting all files, fix up config.yaml if needed
            config_target = os.path.join(APP_DIR, "config/config.yaml")
            config_warnings = []
            if os.path.isfile(config_target):
                try:
                    import yaml as _yaml
                    with open(config_target, "r") as f:
                        cfg = _yaml.safe_load(f) or {}
                    cfg_dirty = False

                    # MQTT enabled inference
                    mqtt = cfg.setdefault("mqtt", {})
                    if "enabled" not in mqtt:
                        mqtt["enabled"] = bool(mqtt.get("broker_host", ""))
                        cfg_dirty = True
                        logger.info("Patched mqtt.enabled into restored config.yaml")

                    # SSL enabled in the restored config but the cert/key missing on
                    # disk would stop the server starting. Force-disable so it comes
                    # back up, and report the change in the response.
                    server = cfg.get("server", {}) or {}
                    ssl_cfg = server.get("ssl", {}) or {}
                    if ssl_cfg.get("enabled"):
                        cert_rel = ssl_cfg.get("cert_file", "./data/certs/cert.pem")
                        key_rel  = ssl_cfg.get("key_file",  "./data/certs/key.pem")
                        # Resolve relative to APP_DIR (matches main.py uvicorn launch CWD)
                        cert_abs = (cert_rel if os.path.isabs(cert_rel)
                                    else os.path.join(APP_DIR, cert_rel.lstrip("./")))
                        key_abs  = (key_rel  if os.path.isabs(key_rel)
                                    else os.path.join(APP_DIR, key_rel.lstrip("./")))
                        missing = []
                        if not os.path.isfile(cert_abs):
                            missing.append(cert_rel)
                        if not os.path.isfile(key_abs):
                            missing.append(key_rel)

                        if missing:
                            cfg["server"]["ssl"]["enabled"] = False
                            cfg_dirty = True
                            warning = (
                                f"SSL was enabled in the restored config but cert files are missing "
                                f"({', '.join(missing)}). SSL has been disabled so the server can start. "
                                f"Regenerate certificates and re-enable SSL in config.yaml."
                            )
                            config_warnings.append(warning)
                            logger.warning(warning)

                    if cfg_dirty:
                        with open(config_target, "w") as f:
                            _yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
                except Exception as e:
                    logger.warning(f"Could not patch config.yaml after restore: {e}")
                    config_warnings.append(f"Config post-processing error: {e}")


            result = {
                "success": len(errors) == 0,
                "restored": restored,
                "restored_count": len(restored),
                "errors": errors,
                "warnings": config_warnings,
                "manifest": manifest,
                "message": (
                    f"Restored {len(restored)} files. Restart the service to apply."
                    if not errors
                    else f"Restored {len(restored)} files with {len(errors)} errors."
                ),
            }

            logger.info(f"Restore complete: {len(restored)} OK, {len(errors)} errors")
            return result

        except zipfile.BadZipFile:
            return {"success": False, "error": "Corrupt or invalid zip file"}
        except Exception as e:
            logger.error(f"Restore failed: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    @app.get("/api/backup/info")
    async def backup_info():
        """
        Return what would be included in a backup and file sizes.
        Useful for the frontend to show backup status.
        """
        files = []
        total_size = 0
        telemetry_size = 0

        for rel_path in BACKUP_MANIFEST:
            full = os.path.join(APP_DIR, rel_path)
            exists = os.path.isfile(full)
            size = os.path.getsize(full) if exists else 0
            total_size += size
            files.append({"path": rel_path, "exists": exists, "size": size, "optional": False})

        for rel_dir in BACKUP_DIRS:
            full_dir = os.path.join(APP_DIR, rel_dir)
            dir_size = 0
            count = 0
            if os.path.isdir(full_dir):
                for root, _dirs, fnames in os.walk(full_dir):
                    for fname in fnames:
                        try:
                            dir_size += os.path.getsize(os.path.join(root, fname))
                            count += 1
                        except OSError:
                            pass
            total_size += dir_size
            files.append({
                "path": rel_dir + "/",
                "exists": count > 0,
                "size": dir_size,
                "optional": False,
                "file_count": count,
            })

        for rel_path in OPTIONAL_BACKUP_FILES:
            full = os.path.join(APP_DIR, rel_path)
            exists = os.path.isfile(full)
            size = os.path.getsize(full) if exists else 0
            telemetry_size += size
            files.append({"path": rel_path, "exists": exists, "size": size, "optional": True})

        return {
            "success": True,
            "files": files,
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "telemetry_size": telemetry_size,
            "telemetry_size_mb": round(telemetry_size / (1024 * 1024), 2),
            "total_with_telemetry_mb": round((total_size + telemetry_size) / (1024 * 1024), 2),
        }
    # Scheduled backups (docs/backups.md). Reading the status is system:read
    # by the path table; changing where backups go, and with what keys, is admin.

    def _sched():
        s = get_backup_scheduler()
        if s is None:
            raise HTTPException(503, "Backup scheduler not initialised")
        return s

    @app.get("/api/backup/schedule")
    async def get_schedule(_=Depends(require_scope("admin"))):
        return _sched().public()

    @app.put("/api/backup/schedule")
    async def put_schedule(body: Dict[str, Any], _=Depends(require_scope("admin"))):
        try:
            return _sched().update(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, f"Could not save: {e}")

    @app.post("/api/backup/schedule/test")
    async def test_schedule_target(_=Depends(require_scope("admin"))):
        try:
            return await _sched().test_target()
        except ValueError as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/backup/schedule/run")
    async def run_schedule_now(_=Depends(require_scope("admin"))):
        status = await _sched().run()
        return {"success": not status.get("last_error"), "status": status}

    @app.get("/api/backup/status")
    async def backup_status():
        s = _sched()
        return {"enabled": s.config["enabled"], "next_run": s.next_run(), **s.status}
