"""
Lifecycle of a sidecar that runs from the app's own image (``python -m <module>``):
object detection and the recorder. Like Beekeeper it needs no image of its own
and follows the app through upgrades; like go2rtc it is off until enabled.

Standalone by design: the manager never imports from modules/.
"""
import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from manager import containers

logger = logging.getLogger("manager.app_sidecar")

_SHARE = ("/app/data", "/app/logs")
START_RETRY_S = 60
SERVICE_RETRY_S = 15 * 60


class AppSidecar:
    def __init__(self, key: str, label: str, variant: Optional[Callable[[], Dict[str, Any]]] = None,
                 variant_env: str = ""):
        """`key` names the module, the container suffix and the data folder.
        `variant()` says how this host should run it — {"name", "devices",
        "binds", "rules"} — and a change of name recreates the container."""
        self.key, self.label, self._variant, self.variant_env = key, label, variant, variant_env
        self.container = f"{containers.APP_CONTAINER}-{key}"
        data = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") or "/opt/.zigbee-matter-manager"
        self.dir = os.path.join(data, "data", key)
        # Whether the user wants it running — so the watchdog can tell a stop
        # someone asked for from a container that died.
        self._state = os.path.join(self.dir, "manager.json")
        # Boot-time service, written on the host by scripts/sidecar_service.sh <key>.
        self.svc_trigger = os.path.join(self.dir, "service_action")
        self.svc_status = os.path.join(self.dir, "service_status.json")

    def variant(self) -> Dict[str, Any]:
        v = self._variant() if self._variant else {}
        return {"name": v.get("name"), "devices": v.get("devices") or [], "binds": v.get("binds") or [],
                "rules": v.get("rules") or []}

    def enabled(self) -> bool:
        try:
            with open(self._state) as f:
                return bool(json.load(f).get("enabled"))
        except (OSError, ValueError):
            return False

    def _set_enabled(self, on: bool) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(self._state, "w") as f:
            json.dump({"enabled": on, "at": time.time()}, f)

    def service_status(self) -> Dict[str, Any]:
        """The host helper's last report on the boot-time service (never raises)."""
        try:
            with open(self.svc_status) as f:
                data = json.load(f)
            return {"known": True, **data, "pending": os.path.isfile(self.svc_trigger)}
        except (OSError, ValueError):
            return {"known": False, "installed": False, "pending": os.path.isfile(self.svc_trigger),
                    "detail": "not checked yet — needs the host helper (install_watcher.sh)"}

    def request_service(self, action: str) -> Dict[str, Any]:
        if action not in ("install", "remove", "check"):
            return {"success": False, "error": "action must be install|remove|check"}
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.svc_trigger, "w") as f:
                f.write(action)
            return {"success": True, "message": f"Autostart {action} requested"}
        except OSError as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _client(sock: str):
        import httpx
        return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock),
                                 base_url="http://d", timeout=60.0)

    @staticmethod
    async def _inspect(cx, name: str) -> Optional[Dict[str, Any]]:
        try:
            r = await cx.get(f"/containers/{name}/json")
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            logger.debug("inspect %s failed: %s", name, e)
            return None

    def variant_of(self, info: Dict[str, Any]) -> Optional[str]:
        if not self.variant_env:
            return None
        for e in (info.get("Config") or {}).get("Env") or []:
            if e.startswith(self.variant_env + "="):
                return e.split("=", 1)[1]
        return None

    async def status(self) -> Dict[str, Any]:
        """Container state for the card and /status. Never raises."""
        out: Dict[str, Any] = {"available": False, "installed": False, "running": False,
                               "enabled": self.enabled(), "name": self.container, "state": None,
                               "backend": None, "would_use": self.variant()["name"],
                               "service": self.service_status(), "error": None}
        sock = containers.detect_socket()
        if not sock:
            out["error"] = "no container socket mounted"
            return out
        out["available"] = True
        try:
            async with self._client(sock) as cx:
                info = await self._inspect(cx, self.container)
        except Exception as e:
            out["error"] = str(e)
            return out
        if info:
            st = info.get("State") or {}
            out.update(installed=True, running=bool(st.get("Running")), state=st.get("Status"),
                       backend=self.variant_of(info))
        return out

    def _spec(self, app_info: Dict[str, Any], v: Dict[str, Any]) -> Dict[str, Any]:
        binds = [f"{m['Source']}:{m['Destination']}:rw" for m in app_info.get("Mounts") or []
                 if m.get("Destination") in _SHARE and m.get("Source")]
        if not any(b.split(":")[1] == "/app/data" for b in binds):
            raise RuntimeError(f"'{containers.APP_CONTAINER}' has no /app/data mount to share")
        host = {
            # Loopback only: the app reaches it on 127.0.0.1, nothing else should.
            "NetworkMode": "host",
            "Binds": binds + v["binds"],
            "RestartPolicy": {"Name": "unless-stopped"},
            "SecurityOpt": ["label=disable"],
        }
        if v["devices"]:
            host["Devices"] = v["devices"]
        if v["rules"]:
            host["DeviceCgroupRules"] = v["rules"]
        return {"Image": (app_info.get("Config") or {}).get("Image") or app_info.get("Image"),
                "Cmd": ["python", "-m", self.key],
                "Env": [f"{self.variant_env}={v['name']}"] if self.variant_env else [],
                "HostConfig": host}

    async def enable(self) -> Dict[str, Any]:
        """Create (if needed) and start the sidecar. Recreates it when the
        app's image has moved on or its variant has changed. Idempotent."""
        self._set_enabled(True)
        sock = containers.detect_socket()
        if not sock:
            return {"success": False, "error": "No container socket mounted — cannot manage the sidecar."}
        try:
            async with self._client(sock) as cx:
                app_info = await self._inspect(cx, containers.APP_CONTAINER)
                if not app_info:
                    return {"success": False, "error": f"could not inspect '{containers.APP_CONTAINER}'"}
                v = self.variant()
                existing = await self._inspect(cx, self.container)
                if existing and existing.get("Image") == app_info.get("Image") and self.variant_of(existing) == v["name"]:
                    r = await cx.post(f"/containers/{self.container}/start")
                    if r.status_code not in (204, 304):
                        return {"success": False, "error": f"start failed: {r.status_code} {r.text[:200]}"}
                    self.request_service("install")
                    return {"success": True, "created": False, "message": f"{self.label} started."}
                if existing:
                    self.request_service("remove")       # its unit would restart the old container
                    await cx.post(f"/containers/{self.container}/stop", params={"t": "10"})
                    await cx.delete(f"/containers/{self.container}", params={"force": "true"})
                r = await cx.post("/containers/create", params={"name": self.container}, json=self._spec(app_info, v))
                if r.status_code != 201:
                    return {"success": False, "error": f"create failed: {r.status_code} {r.text[:200]}"}
                r = await cx.post(f"/containers/{self.container}/start")
                if r.status_code not in (204, 304):
                    return {"success": False, "error": f"start failed: {r.status_code} {r.text[:200]}"}
                self.request_service("install")
                return {"success": True, "created": True, "backend": v["name"], "message": f"{self.label} started."}
        except Exception as e:
            logger.error("%s enable failed: %s", self.key, e)
            return {"success": False, "error": str(e)}

    async def disable(self, remove: bool = False) -> Dict[str, Any]:
        self._set_enabled(False)
        sock = containers.detect_socket()
        if not sock:
            return {"success": False, "error": "no container socket mounted"}
        try:
            async with self._client(sock) as cx:
                # Before the stop: the service would restart a stopped container.
                self.request_service("remove")
                if not await self._inspect(cx, self.container):
                    return {"success": True, "message": f"{self.label} is not installed."}
                r = await cx.post(f"/containers/{self.container}/stop", params={"t": "10"})
                if remove:
                    await cx.delete(f"/containers/{self.container}", params={"force": "true"})
                    return {"success": True, "message": f"{self.label} stopped and removed."}
                ok = r.status_code in (204, 304)
                return {"success": ok, "message" if ok else "error":
                        f"{self.label} stopped." if ok else f"stop failed: {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def restart(self) -> Dict[str, Any]:
        ok = await containers.restart_container(self.container)
        return {"success": ok, "message" if ok else "error": f"{self.label} restarted." if ok else "restart failed"}

    async def ensure(self, t: Dict[str, Any]) -> None:
        """Watchdog step for an enabled sidecar: recreate it when the app's
        image or its variant changed (once per combination), ask the host for
        its boot-time service, and start it where no service owns that."""
        if not self.enabled():
            return
        info = await containers.inspect_container(self.container)
        app_info = await containers.inspect_container(containers.APP_CONTAINER)
        if not app_info:
            return
        want = (app_info.get("Image"), self.variant()["name"])
        if info is None or (info.get("Image"), self.variant_of(info)) != want:
            if t.get("tried") != want:
                t["tried"] = want
                res = await self.enable()
                logger.info("%s %s: %s", self.label, "recreated" if info else "created",
                            res.get("message") or res.get("error"))
            return
        svc = self.service_status()
        running = bool((info.get("State") or {}).get("Running"))
        if running and not (svc.get("installed") or svc.get("pending") or svc.get("conflict")
                            or svc.get("backend") == "none") \
                and time.time() - t.get("service_at", 0) > SERVICE_RETRY_S:
            t["service_at"] = time.time()
            self.request_service("install")
        if svc.get("installed"):
            return
        if not running and time.time() - t.get("started_at", 0) > START_RETRY_S:
            t["started_at"] = time.time()
            logger.info("%s is enabled but stopped — starting it", self.label)
            await self.enable()
