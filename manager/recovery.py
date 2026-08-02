"""
Disaster recovery from the manager — replaces the old in-container
recovery_server.py, which could only run while the app container was alive.

Crash records and backups sit in a bind mount the manager also mounts, and app
code is reached through the runtime's archive API, so both work with the app
container dead. "Retry" writes data/.recovery_resume for the launcher's standby.
See docs/upgrades.md.
"""
import io
import json
import logging
import os
import re
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from manager.containers import APP_CONTAINER, detect_socket

logger = logging.getLogger("manager.recovery")

DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
HOST_DATA = Path(DATA_DIR) / "data"
CRASH_FILE = HOST_DATA / "last_crash.json"
ACTIVE_MARKER = HOST_DATA / ".recovery_active"
RESUME_MARKER = HOST_DATA / ".recovery_resume"
PENDING_FILES = (".test_pending", ".boot_failures")
BACKUP_DIR = HOST_DATA / ".editor_backups"

APP_ROOT = "/app"          # path inside the app container
MAX_FILE_BYTES = 4 * 1024 * 1024

# Same write policy as the old recovery server / editor routes. "manager" is
# included so a broken manager module can be repaired too (the manager runs
# the same image — fix the file in the app container, ship it via refresh).
ALLOWED_WRITE_DIRS = [
    "", "core", "device", "handlers", "manager", "modules", "modules/media",
    "modules/media/players", "modules/media/sources", "routes",
    "static", "static/js", "static/js/modal", "static/css", "static/css/api",
    "config", "config/matter_definitions", "data", "scripts",
]
ALLOWED_EXTS = {".py", ".js", ".css", ".html", ".yaml", ".yml",
                ".json", ".md", ".txt", ".conf", ".sh"}

_BAK_RE = re.compile(r"^(.+?)\.(\d{8}_\d{6})(\..+)?\.bak$")


# Path policy

def _clean_rel(rel: str) -> Optional[str]:
    """Normalise a project-relative path; None if it escapes the project."""
    if not rel:
        return None
    clean = rel.replace("\\", "/").lstrip("/")
    if not clean or any(part in ("..", "") for part in clean.split("/")):
        return None
    return clean


def path_is_writable(rel: str) -> bool:
    clean = _clean_rel(rel)
    if clean is None:
        return False
    ok_dir = ("/" not in clean) or any(
        clean.startswith(d + "/") for d in ALLOWED_WRITE_DIRS if d)
    return ok_dir and os.path.splitext(clean)[1].lower() in ALLOWED_EXTS


# Recovery state (host files)

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def is_active(app_ok: Optional[bool] = None) -> bool:
    """Recovery is active when the launcher's marker exists — UNLESS the app
    is verifiably healthy, in which case the marker is stale (e.g. the old
    recovery server was SIGKILLed mid-session and its cleanup never ran).
    A healthy app cannot be in recovery: the standby serves 503 on /api/."""
    if not ACTIVE_MARKER.is_file():
        return False
    return not app_ok


def _test_deploy_detail() -> Optional[Dict[str, Any]]:
    """Parsed view of a pending editor test deploy (data/.test_pending),
    so the dashboard can say WHAT is mid-test instead of just naming the
    marker file. None when no batch is pending."""
    p = HOST_DATA / ".test_pending"
    if not p.is_file():
        return None
    info = _read_json(p)
    files = [f.get("path") for f in info.get("files", [])]
    if not files and info.get("path"):        # legacy single-file schema
        files = [info.get("path")]
    try:
        age = int(time.time() - p.stat().st_mtime)
    except OSError:
        age = None
    return {
        "files": files,
        "action": info.get("action"),
        "deployed_at": info.get("deployed_at"),
        "age_seconds": age,
    }


def state(app_ok: Optional[bool] = None) -> Dict[str, Any]:
    active = is_active(app_ok)
    pending = [p for p in PENDING_FILES if (HOST_DATA / p).is_file()]
    return {
        "active": active,
        "stale_marker": ACTIVE_MARKER.is_file() and not active,
        "marker": _read_json(ACTIVE_MARKER) if active else None,
        "crash": _read_json(CRASH_FILE),
        "pending_markers": pending,
        "test_deploy": _test_deploy_detail(),
    }


def list_backups(path_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    if not BACKUP_DIR.is_dir():
        return []
    prefix = None
    if path_filter:
        prefix = path_filter.replace("/", "_").replace("\\", "_") + "."
    items = []
    for p in BACKUP_DIR.iterdir():
        if not p.name.endswith(".bak") or (prefix and not p.name.startswith(prefix)):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        m = _BAK_RE.match(p.name)
        items.append({
            "name": p.name,
            "size": st.st_size,
            "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "mtime": int(st.st_mtime),
            "probable_original": m.group(1) if m else p.name,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def backup_content(name: str) -> Tuple[bool, str]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False, "Invalid backup name"
    p = BACKUP_DIR / name
    if not p.is_file():
        return False, "Backup not found"
    try:
        return True, p.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
    except OSError as e:
        return False, str(e)


def clear_pending() -> List[str]:
    removed = []
    for fname in PENDING_FILES:
        p = HOST_DATA / fname
        if p.is_file():
            try:
                p.unlink()
                removed.append(fname)
            except OSError as e:
                logger.error("Failed to remove %s: %s", p, e)
    return removed


# App-container file access (archive API — works on stopped containers)

async def read_app_file(rel: str) -> Tuple[bool, str]:
    """Read /app/<rel> from the app container via GET .../archive."""
    clean = _clean_rel(rel)
    if clean is None:
        return False, "Invalid path"
    sock = detect_socket()
    if not sock:
        return False, "No container socket mounted"
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=30.0) as cx:
            r = await cx.get(f"/containers/{APP_CONTAINER}/archive",
                             params={"path": f"{APP_ROOT}/{clean}"})
            if r.status_code == 404:
                return False, "File not found in app container"
            if r.status_code != 200:
                return False, f"archive returned {r.status_code}"
            with tarfile.open(fileobj=io.BytesIO(r.content)) as tf:
                for member in tf.getmembers():
                    if member.isfile():
                        fh = tf.extractfile(member)
                        data = fh.read(MAX_FILE_BYTES) if fh else b""
                        return True, data.decode("utf-8", errors="replace")
            return False, "Path is not a regular file"
    except Exception as e:
        logger.warning("read_app_file(%s): %s", rel, e)
        return False, str(e)


async def write_app_file(rel: str, data: bytes) -> Tuple[bool, str]:
    """Write /app/<rel> in the app container via PUT .../archive.
    The existing file (if any) is snapshotted to the backup dir first."""
    clean = _clean_rel(rel)
    if clean is None or not path_is_writable(clean):
        return False, f"Path not writable: {rel}"
    if len(data) > MAX_FILE_BYTES:
        return False, "File too large"
    sock = detect_socket()
    if not sock:
        return False, "No container socket mounted"

    # Pre-write snapshot (same convention as the old recovery server).
    ok, current = await read_app_file(clean)
    if ok:
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            safe = clean.replace("/", "_")
            ts = time.strftime("%Y%m%d_%H%M%S")
            (BACKUP_DIR / f"{safe}.{ts}.pre_recovery.bak").write_text(
                current, encoding="utf-8")
        except OSError as e:
            logger.warning("Pre-recovery snapshot failed for %s: %s", clean, e)

    parent = os.path.dirname(clean)
    fname = os.path.basename(clean)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=fname)
        info.size = len(data)
        info.mtime = int(time.time())
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=60.0) as cx:
            dest = f"{APP_ROOT}/{parent}" if parent else APP_ROOT
            r = await cx.put(f"/containers/{APP_CONTAINER}/archive",
                             params={"path": dest}, content=buf.read())
            if r.status_code not in (200, 204):
                return False, f"archive PUT returned {r.status_code}: {r.text[:200]}"
            logger.warning("Recovery write applied: %s (%d bytes)", clean, len(data))
            return True, clean
    except Exception as e:
        logger.error("write_app_file(%s): %s", rel, e)
        return False, str(e)


async def restore_backup(name: str, rel: str) -> Tuple[bool, str]:
    ok, content = backup_content(name)
    if not ok:
        return False, content
    return await write_app_file(rel, content.encode("utf-8"))


# Resume

def request_resume() -> Tuple[bool, str]:
    """Ask the launcher's recovery standby to retry main.py."""
    try:
        RESUME_MARKER.write_text(json.dumps({
            "ts": time.time(),
            "iso": datetime.now(timezone.utc).isoformat(),
            "requested_by": "zmm-manager",
        }), encoding="utf-8")
        return True, "Resume requested — the launcher will retry main.py"
    except OSError as e:
        return False, str(e)
