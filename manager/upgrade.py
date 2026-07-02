"""Upgrade visibility + recovery actions for the manager (CP2b).

The manager owns ROLLBACK and IMAGE RETENTION so they work even when the app
is down (the whole point of the sidecar). It reuses the existing host-watcher
contract — it writes the same ``data/upgrade/trigger`` file the app's
upgrade_manager writes (the manager mounts DATA_DIR), and the host watcher
does the actual work:

  - rollback: ``do_rollback`` already accepts any local image tag via the
    ``previous_image_tag`` payload field, so "roll back to a specific
    version" is just a trigger naming one of the retained images.
  - retention/GC: ``do_gc`` reads ``retention_count`` from
    ``data/state/version.json``; the manager edits that field and can write a
    ``gc`` trigger to apply it immediately.

Actions require a bearer token (``data/state/manager_token``, generated on
first start, 0600). The app's Upgrade tab shows the token to authenticated
users; it is also readable on the host. Reads stay unauthenticated like the
rest of the manager.
"""
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from manager.containers import APP_CONTAINER, detect_socket

logger = logging.getLogger("manager.upgrade")

DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
UPGRADE_DIR = Path(DATA_DIR) / "data" / "upgrade"
TRIGGER_FILE = UPGRADE_DIR / "trigger"
STATUS_FILE = UPGRADE_DIR / "status.json"
LOCK_FILE = UPGRADE_DIR / "lock"
STATE_DIR = Path(DATA_DIR) / "data" / "state"
VERSION_STATE_FILE = STATE_DIR / "version.json"
TOKEN_FILE = STATE_DIR / "manager_token"

IMAGE_NAME = os.environ.get("ZMM_IMAGE_NAME", "zigbee-matter-manager")
ACTIVE_STATES = ("building", "swapping", "rolling_back")
LOCK_STALE_SECS = 3600  # mirrors upgrade_manager.clear_stale_lock's age cutoff

# localhost/zigbee-matter-manager:3.1.9-amd64 → version "3.1.9"
_TAG_RE = re.compile(rf"(?:^|/){re.escape(IMAGE_NAME)}:(?P<ver>\d[\w.]*)-(?P<arch>[a-z0-9_]+)$")


# ── Token auth ────────────────────────────────────────────────────────────────

def get_token() -> str:
    """Read the action token, generating it on first use (0600)."""
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(t + "\n", encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
        logger.info("Generated manager action token at %s", TOKEN_FILE)
    except OSError as e:
        logger.error("Could not persist manager token: %s", e)
    return t


def check_token(authorization: str) -> bool:
    """Validate an ``Authorization: Bearer <token>`` header value."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    presented = authorization[7:].strip()
    return bool(presented) and hmac.compare_digest(presented, get_token())


# ── State / status reads ─────────────────────────────────────────────────────

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def version_state() -> Dict[str, Any]:
    return _read_json(VERSION_STATE_FILE)


def read_status() -> Dict[str, Any]:
    return _read_json(STATUS_FILE)


async def list_images() -> List[Dict[str, Any]]:
    """Version-tagged ZMM images on the host (the rollback candidates),
    newest first: [{tag, version, arch, created, current}]."""
    sock = detect_socket()
    if not sock:
        return []
    state = version_state()
    current = state.get("current_version")
    out: List[Dict[str, Any]] = []
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=10.0) as cx:
            r = await cx.get("/images/json")
            if r.status_code != 200:
                return []
            for img in r.json():
                for ref in (img.get("RepoTags") or []):
                    m = _TAG_RE.search(ref)
                    if not m:
                        continue
                    out.append({
                        "tag": ref,
                        "version": m.group("ver"),
                        "arch": m.group("arch"),
                        "created": img.get("Created") or 0,
                        "current": m.group("ver") == current,
                    })
    except Exception as e:
        logger.warning("list_images failed: %s", e)
        return []
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


# ── Trigger writing (same contract as modules/upgrade_manager.write_trigger) ─

def _lock_blocked() -> Optional[str]:
    """Return a reason string if a LIVE upgrade lock forbids a new trigger.
    Mirrors the app's stale-lock tolerance without its PID check (host PIDs
    aren't visible from this container): a lock only blocks if the watcher
    also reports an active state and the lock isn't ancient."""
    if not LOCK_FILE.exists():
        return None
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
    except OSError:
        return None
    state = (read_status().get("state") or "").lower()
    if state in ACTIVE_STATES and age < LOCK_STALE_SECS:
        return f"Upgrade operation in progress (state={state})"
    return None  # stale — the watcher consumes/overwrites it


def write_trigger(action: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    blocked = _lock_blocked()
    if blocked:
        return False, blocked
    trigger = {
        "action": action,
        "payload": payload,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": "zmm-manager",
    }
    try:
        UPGRADE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TRIGGER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(trigger, indent=2), encoding="utf-8")
        os.replace(tmp, TRIGGER_FILE)
        logger.info("Wrote upgrade trigger: %s %s", action, payload)
        return True, f"{action} requested"
    except OSError as e:
        logger.error("Failed to write trigger: %s", e)
        return False, str(e)


# ── Actions ──────────────────────────────────────────────────────────────────

async def rollback_to(version: str) -> Tuple[bool, str]:
    """Roll back (swap) to a specific retained image version."""
    images = await list_images()
    match = next((i for i in images if i["version"] == version), None)
    if match is None:
        have = ", ".join(sorted({i["version"] for i in images})) or "none"
        return False, f"No local image for v{version} (retained: {have})"
    if match["current"]:
        return False, f"v{version} is already the running version"
    return write_trigger("rollback", {
        "previous_version": match["version"],
        "previous_image_tag": match["tag"],
    })


def run_gc() -> Tuple[bool, str]:
    """Prune images beyond the configured retention (host do_gc)."""
    return write_trigger("gc", {})


def set_retention(count: int) -> Tuple[bool, str]:
    """Persist retention_count in version.json (read by the host's do_gc)."""
    if not isinstance(count, int) or not (1 <= count <= 20):
        return False, "retention_count must be an integer between 1 and 20"
    state = version_state()
    state["retention_count"] = count
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = VERSION_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, VERSION_STATE_FILE)
        return True, f"Retention set to {count}"
    except OSError as e:
        return False, str(e)
