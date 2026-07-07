"""
Managed remote access via Cloudflare Tunnel.

Runs `cloudflared` as a supervised subprocess (same pattern as
MatterServerManager) so users get remote access to the web UI without
port-forwarding — the tunnel dials OUT, so NAT/CGNAT doesn't matter.

Two modes:

- **token** (recommended for permanent use): the user creates a tunnel in
  the Cloudflare Zero Trust dashboard, points its public hostname at
  ``http://localhost:<web.port>``, and pastes the connector token here.
  The token is passed via the ``TUNNEL_TOKEN`` environment variable, never
  on the command line (argv is world-readable in /proc).

- **quick** (testing only): ``cloudflared tunnel --url ...`` gives an
  ephemeral ``*.trycloudflare.com`` URL with no account needed. The URL
  changes on every start and carries no access controls beyond ZMM's own
  login, so the UI labels it clearly as a trial mode.

Settings persist in ``data/remote_access.yaml`` (0600 — it holds the
tunnel token), managed through the Settings → Security → Remote Access UI.

When the tunnel starts we flip ``cloudflare_tunnel_enabled`` on the live
NetworkResolver so `CF-Connecting-IP` from the local cloudflared is
trusted and remote clients are classified correctly (i.e. NOT as LAN).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import yaml

from modules.auth_network import get_network_resolver

logger = logging.getLogger("modules.remote_access")

SETTINGS_PATH = Path("./data/remote_access.yaml")


def detect_environment() -> dict:
    """
    Where is ZMM running? The UI uses this to show the right cloudflared
    install instructions — a host-installed binary is invisible when ZMM
    runs in a container, which is the usual deployment (build.sh).
    """
    os_release = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os_release[k] = v.strip('"')
    except OSError:
        pass
    return {
        "in_container": (
            os.path.exists("/run/.containerenv")     # podman
            or os.path.exists("/.dockerenv")         # docker
        ),
        "os_id": os_release.get("ID", ""),           # e.g. "debian", "fedora"
        "os_like": os_release.get("ID_LIKE", ""),    # e.g. "rhel fedora"
        "os_pretty": os_release.get("PRETTY_NAME", ""),
        "arch": platform.machine(),                  # e.g. "x86_64", "aarch64"
    }


_ENVIRONMENT = detect_environment()

_QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# cloudflared logs one of these per edge connection (4 when healthy)
_CONN_UP_RE = re.compile(r"Registered tunnel connection|Connection .* registered")
_CONN_DOWN_RE = re.compile(r"Unregistered tunnel connection|connection .* lost")


@dataclass
class RemoteAccessSettings:
    enabled: bool = False
    mode: str = "token"                 # "token" | "quick"
    tunnel_token: str = ""              # Cloudflare connector token (secret)
    hostname: str = ""                  # public hostname (informational, UI link)
    cloudflared_path: str = ""          # override; empty = search PATH

    def to_dict(self) -> dict:
        return asdict(self)

    def public_view(self) -> dict:
        """Settings safe to hand to the UI — token never leaves the server."""
        d = self.to_dict()
        d.pop("tunnel_token", None)
        d["token_set"] = bool(self.tunnel_token)
        return d


class RemoteAccessManager:
    """Supervises the cloudflared subprocess and persists its settings."""

    def __init__(self, origin_port: int = 8000, origin_https: bool = False,
                 settings_path: Path = SETTINGS_PATH):
        self.settings_path = Path(settings_path)
        self.origin_port = origin_port
        self.origin_https = origin_https
        self.settings = RemoteAccessSettings()

        self._process: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._shutdown = False
        self._restart_count = 0
        self._max_restarts = 5
        self._started_at: Optional[float] = None
        self._quick_url: Optional[str] = None
        self._connections = 0
        self._last_error: Optional[str] = None
        self._lock = asyncio.Lock()

    # ---- settings persistence -------------------------------------------

    def load(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            with open(self.settings_path) as f:
                raw = yaml.safe_load(f) or {}
            self.settings = RemoteAccessSettings(
                enabled=bool(raw.get("enabled", False)),
                mode=str(raw.get("mode", "token")),
                tunnel_token=str(raw.get("tunnel_token", "") or ""),
                hostname=str(raw.get("hostname", "") or ""),
                cloudflared_path=str(raw.get("cloudflared_path", "") or ""),
            )
            logger.info(
                f"Remote access settings loaded "
                f"(enabled={self.settings.enabled}, mode={self.settings.mode})"
            )
        except Exception as e:
            logger.error(f"Failed to load {self.settings_path}: {e}")

    def save(self) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(self.settings.to_dict(), f,
                               default_flow_style=False, sort_keys=False)
            os.replace(tmp, self.settings_path)
            try:
                os.chmod(self.settings_path, 0o600)   # holds the tunnel token
            except OSError:
                pass
        except Exception as e:
            logger.error(f"Failed to save {self.settings_path}: {e}")

    # ---- binary discovery ------------------------------------------------

    def cloudflared_path(self) -> Optional[str]:
        if self.settings.cloudflared_path:
            p = self.settings.cloudflared_path
            return p if os.access(p, os.X_OK) else None
        found = shutil.which("cloudflared")
        if found:
            return found
        for candidate in ("/usr/local/bin/cloudflared",
                          "/usr/bin/cloudflared",
                          "/opt/cloudflared/cloudflared"):
            if os.access(candidate, os.X_OK):
                return candidate
        return None

    async def cloudflared_version(self) -> Optional[str]:
        path = self.cloudflared_path()
        if not path:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return out.decode("utf-8", errors="replace").strip().splitlines()[0]
        except Exception as e:
            logger.warning(f"cloudflared --version failed: {e}")
            return None

    # ---- lifecycle ---------------------------------------------------------

    def _origin_url(self) -> str:
        scheme = "https" if self.origin_https else "http"
        return f"{scheme}://127.0.0.1:{self.origin_port}"

    def _build_command(self, path: str) -> list:
        base = [path, "tunnel", "--no-autoupdate"]
        if self.settings.mode == "quick":
            cmd = base + ["--url", self._origin_url()]
        else:
            cmd = base + ["run"]
        if self.origin_https:
            # Local self-signed cert on the origin
            cmd.insert(2, "--no-tls-verify")
        return cmd

    async def start(self) -> bool:
        async with self._lock:
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        if self._running:
            return True
        self._shutdown = False
        self._last_error = None

        path = self.cloudflared_path()
        if not path:
            self._last_error = (
                "cloudflared binary not found. Install it from "
                "https://developers.cloudflare.com/cloudflare-one/connections/"
                "connect-networks/downloads/ or set an explicit path."
            )
            logger.error(f"[remote-access] {self._last_error}")
            return False

        if self.settings.mode == "token" and not self.settings.tunnel_token:
            self._last_error = "No tunnel token configured."
            logger.error(f"[remote-access] {self._last_error}")
            return False

        return await self._spawn(path)

    async def _spawn(self, path: str) -> bool:
        cmd = self._build_command(path)
        env = dict(os.environ)
        if self.settings.mode == "token":
            env["TUNNEL_TOKEN"] = self.settings.tunnel_token
        logger.info(f"[remote-access] starting: {' '.join(cmd)}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                preexec_fn=os.setpgrp,
            )
        except Exception as e:
            self._last_error = f"Failed to start cloudflared: {e}"
            logger.error(f"[remote-access] {self._last_error}")
            return False

        self._running = True
        self._started_at = time.time()
        self._quick_url = None
        self._connections = 0
        self._monitor_task = asyncio.create_task(self._monitor())

        # Make sure the live resolver trusts CF-Connecting-IP arriving via
        # the local cloudflared, so remote clients aren't classified as LAN.
        resolver = get_network_resolver()
        if resolver and not resolver.cloudflare_tunnel_enabled:
            resolver.cloudflare_tunnel_enabled = True
            logger.info("[remote-access] enabled Cloudflare header trust "
                        "on network resolver")

        await asyncio.sleep(2)
        if self._process.returncode is not None:
            self._last_error = (
                f"cloudflared exited immediately "
                f"(code {self._process.returncode}) — check the token"
            )
            logger.error(f"[remote-access] {self._last_error}")
            self._running = False
            return False
        return True

    async def _monitor(self):
        """Stream cloudflared output, track state, restart on crash."""
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue

                m = _QUICK_URL_RE.search(text)
                if m:
                    self._quick_url = m.group(0)
                    logger.info(f"[remote-access] quick tunnel URL: "
                                f"{self._quick_url}")
                if _CONN_UP_RE.search(text):
                    self._connections += 1
                elif _CONN_DOWN_RE.search(text):
                    self._connections = max(0, self._connections - 1)

                if " ERR " in text or "error=" in text:
                    logger.warning(f"[cloudflared] {text}")
                else:
                    logger.debug(f"[cloudflared] {text}")

            if self._process:
                returncode = await self._process.wait()
                if not self._shutdown:
                    logger.warning(
                        f"[remote-access] cloudflared exited "
                        f"with code {returncode}"
                    )

            self._running = False
            self._connections = 0

            if not self._shutdown and self._restart_count < self._max_restarts:
                self._restart_count += 1
                delay = min(5 * self._restart_count, 60)
                logger.info(
                    f"[remote-access] restarting in {delay}s "
                    f"(attempt {self._restart_count}/{self._max_restarts})"
                )
                await asyncio.sleep(delay)
                if not self._shutdown:
                    path = self.cloudflared_path()
                    if path:
                        await self._spawn(path)
            elif not self._shutdown:
                self._last_error = (
                    f"cloudflared exceeded {self._max_restarts} restarts; "
                    "giving up. Fix the configuration and start it again."
                )
                logger.error(f"[remote-access] {self._last_error}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[remote-access] monitor error: {e}")
            self._running = False

    async def stop(self):
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self):
        self._shutdown = True
        self._running = False
        self._restart_count = 0

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        if self._process and self._process.returncode is None:
            logger.info(f"[remote-access] stopping cloudflared "
                        f"(PID {self._process.pid})")
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error(f"[remote-access] error stopping cloudflared: {e}")

        self._process = None
        self._quick_url = None
        self._connections = 0

    async def apply_settings(self, **changes) -> RemoteAccessSettings:
        """Update settings, persist, and restart the tunnel if needed."""
        async with self._lock:
            for key in ("enabled", "mode", "hostname", "cloudflared_path"):
                if key in changes and changes[key] is not None:
                    setattr(self.settings, key, changes[key])
            # Token: None = keep current; "" = clear; else replace
            if changes.get("tunnel_token") is not None:
                self.settings.tunnel_token = changes["tunnel_token"]
            if self.settings.mode not in ("token", "quick"):
                self.settings.mode = "token"
            self.save()

            was_running = self._running
            if was_running:
                await self._stop_locked()
            if self.settings.enabled:
                self._restart_count = 0
                await self._start_locked()
            return self.settings

    # ---- status ------------------------------------------------------------

    def get_status(self) -> dict:
        pid = None
        if self._process and self._process.returncode is None:
            pid = self._process.pid
        url = None
        if self.settings.mode == "quick":
            url = self._quick_url
        elif self.settings.hostname:
            url = f"https://{self.settings.hostname}"
        return {
            **self.settings.public_view(),
            "running": self._running,
            "pid": pid,
            "url": url,
            "connections": self._connections,
            "uptime_s": (time.time() - self._started_at)
                        if (self._running and self._started_at) else None,
            "restart_count": self._restart_count,
            "last_error": self._last_error,
            "binary_path": self.cloudflared_path(),
            "origin_url": self._origin_url(),
            "environment": _ENVIRONMENT,
        }


# --- module singleton --------------------------------------------------------

_manager: Optional[RemoteAccessManager] = None


def get_remote_access_manager() -> Optional[RemoteAccessManager]:
    return _manager


def set_remote_access_manager(mgr: RemoteAccessManager) -> None:
    global _manager
    _manager = mgr
