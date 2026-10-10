"""
Backups: what goes in one, building and encrypting it off the event loop,
where it is sent, what is kept, and the nightly schedule. See docs/backups.md.

A backup is a zip of the manifest below; with a passphrase it is wrapped in
AES-256-GCM (key from scrypt). Targets are a folder (which is also how an SMB
or NFS share mounted on the host is used), WebDAV, and S3-compatible storage.
Retention keeps the newest backup of each of the last N days and of each of
the last M weeks, and only ever deletes files this module named.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlparse

logger = logging.getLogger("backup")

APP_DIR = os.environ.get("ZMM_APP_DIR", "/app")
SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
SCHEDULE_PATH = Path("./data/backup_schedule.json")
STATUS_PATH = Path("./data/backup_status.json")

# Core files always included
BACKUP_MANIFEST = [
    # Network credentials & config — every integration's enablement lives in
    # config.yaml, so this file alone carries the full enablement state.
    "config/config.yaml",
    # Integration credentials (Blueair, cameras, Shelly, notification channels,
    # go2rtc…). Without it a restore brings every integration back logged out.
    "config/secrets.yaml",

    # Zigpy device database (paired devices, network state).
    # The live path is data/zigbee.db (config_builder database_path); the bare
    # root entry is kept so backups from the legacy layout still restore.
    "data/zigbee.db",
    "zigbee.db",

    # Application data
    "data/names.json",
    "data/device_settings.json",
    "data/polling_config.json",
    "data/device_state_cache.json",
    "data/device_tabs.json",
    "data/automations.json",
    "data/banned_devices.json",
    "data/device_overrides.json",
    "data/zones.yaml",
    "data/zone_device_policy.json",
    "data/auth.yaml",
    "data/floor_plan.json",
    "data/frames.json",
    "data/workers.json",
    "data/places.yaml",

    # Groups — live registry is data/groups.json; the groups/ entry is the
    # legacy in-image location, kept so old backups still restore.
    "data/groups.json",
    "groups/groups.json",

    # Integration enablement & user config (External APIs tab and friends)
    "data/presence_users.yaml",
    "data/remote_access.yaml",
    "data/app_alerts.json",
    "data/ac_timers.json",
    "data/cast_sync_groups.json",
    "data/cast_sync_model.json",
    "data/cast_sync_trims.json",
    "data/cast_sync_model_trims.json",
    "data/media_prefs.json",
    "data/media_sessions.json",
    "data/media_eq.json",
    "data/radio_favourites.json",
    # HomeKit controller keys: without them a restored hub cannot reach a TV
    # that still counts itself paired, and it will not accept a new pairing.
    "data/homekit_pairings.json",

    # Notifications: rules, each user's channels, and the push identity —
    # a new VAPID key would silently unsubscribe every phone.
    "data/notification_rules.json",
    "data/notify_channels.json",
    "data/push_subscriptions.yaml",
    "data/vapid.json",

    # House mode, alarm (zones and PIN hashes), cameras, Wi-Fi devices
    "data/house_mode.json",
    "data/alarm.json",
    "data/cameras.json",
    "data/shelly_devices.json",
    "data/esphome_devices.json",

    "data/backup_schedule.json",
]

# Restored owner-only, matching how the app writes them.
RESTORE_PRIVATE = {"data/auth.yaml", "data/homekit_pairings.json", "config/secrets.yaml",
                   "data/alarm.json", "data/notify_channels.json", "data/push_subscriptions.yaml",
                   "data/vapid.json"}

# Directories included recursively (each contained file is backed up and
# restorable — see entry_allowed()).
BACKUP_DIRS = [
    # One Tidal refresh token per linked user. Plural where it used to be a
    # single file, so a restore now hands back every household member's login.
    "data/media/tidal",
    "data/floor_plans",   # heating floor-plan background images
    "data/coverage",      # saved signal heatmaps, for before/after comparison
    "data/matter",        # Matter fabric / commissioning storage
    "data/certs",         # TLS pair — preserves browser trust across restores
]

# History databases (toggled via include_telemetry). Energy history is the
# hardest to re-fetch (telemetry_database.md).
OPTIONAL_BACKUP_FILES = [
    "data/telemetry.duckdb",
    "data/zigbee_cache.duckdb",
    "data/octopus.duckdb",
]

NAME_RE = re.compile(r"^zmm_backup_(\d{8}_\d{6})_(full|config)\.zip(\.enc)?$")
MAGIC = b"ZMMBAK1\n"
CHUNK = 1024 * 1024


def entry_allowed(name: str) -> bool:
    """Zip-slip guard for restore: no absolute or parent paths, and only
    manifest files or files under a manifest directory."""
    norm = os.path.normpath(name)
    if os.path.isabs(norm) or norm.startswith(".."):
        return False
    if norm in set(BACKUP_MANIFEST) | set(OPTIONAL_BACKUP_FILES):
        return True
    return any(norm.startswith(d.rstrip("/") + "/") for d in BACKUP_DIRS)


def backup_name(include_telemetry: bool, when: Optional[datetime] = None, encrypted: bool = False) -> str:
    ts = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"zmm_backup_{ts}_{'full' if include_telemetry else 'config'}.zip" + (".enc" if encrypted else "")


# Building (blocking — run in a thread)

def build_zip(dest: Path, include_telemetry: bool, device_count: int = 0,
              app_dir: Optional[str] = None) -> Dict[str, Any]:
    """Write the backup zip to `dest`; returns its manifest."""
    app_dir = app_dir or APP_DIR
    files = list(BACKUP_MANIFEST) + (OPTIONAL_BACKUP_FILES if include_telemetry else [])
    meta: Dict[str, Any] = {"created_at": datetime.now().isoformat(), "version": "1.2",
                            "include_telemetry": include_telemetry, "device_count": device_count,
                            "files": []}
    skipped: List[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        def add(full: str, rel: str) -> None:
            zf.write(full, rel)
            meta["files"].append({"path": rel, "size": os.path.getsize(full)})
        for rel in files:
            full = os.path.join(app_dir, rel)
            if os.path.isfile(full):
                add(full, rel)
            else:
                skipped.append(rel)
        for rel_dir in BACKUP_DIRS:
            full_dir = os.path.join(app_dir, rel_dir)
            if not os.path.isdir(full_dir):
                skipped.append(rel_dir + "/")
                continue
            for root, _dirs, names in os.walk(full_dir):
                for fname in names:
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, app_dir)
                    try:
                        add(full, rel)
                    except OSError as e:
                        skipped.append(rel)
                        logger.warning(f"Backup skip (unreadable): {rel}: {e}")
        meta["included"] = len(meta["files"])
        meta["skipped"] = skipped
        zf.writestr("backup_manifest.json", json.dumps(meta, indent=2))
    return meta


# Encryption (blocking)

def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    """MAGIC | salt(16) | nonce(12) | ciphertext | tag(16), streamed."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    salt, nonce = os.urandom(16), os.urandom(12)
    enc = Cipher(algorithms.AES(_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    enc.authenticate_additional_data(MAGIC)
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        out.write(MAGIC + salt + nonce)
        while chunk := inp.read(CHUNK):
            out.write(enc.update(chunk))
        out.write(enc.finalize() + enc.tag)


def is_encrypted(data: bytes) -> bool:
    return data[:len(MAGIC)] == MAGIC


def decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not is_encrypted(data) or len(data) < len(MAGIC) + 44:
        raise ValueError("Not an encrypted ZMM backup")
    o = len(MAGIC)
    salt, nonce, body, tag = data[o:o + 16], data[o + 16:o + 28], data[o + 28:-16], data[-16:]
    dec = Cipher(algorithms.AES(_key(passphrase, salt)), modes.GCM(nonce, tag)).decryptor()
    dec.authenticate_additional_data(MAGIC)
    try:
        return dec.update(body) + dec.finalize()
    except InvalidTag:
        raise ValueError("Wrong passphrase, or the backup is damaged") from None


# Retention

def to_delete(names: List[str], keep_daily: int, keep_weekly: int) -> List[str]:
    """Which of our backups to remove: all but the newest of each of the last
    `keep_daily` days that have one, and of each of the last `keep_weekly`
    ISO weeks that have one. Names that aren't ours are never returned."""
    ours = []
    for n in names:
        m = NAME_RE.match(n)
        if m:
            ours.append((datetime.strptime(m.group(1), "%Y%m%d_%H%M%S"), n))
    ours.sort(reverse=True)
    keep, days, weeks = set(), [], []
    for when, n in ours:
        day, week = when.date(), when.isocalendar()[:2]
        if day not in days and len(days) < max(1, keep_daily):
            days.append(day)
            keep.add(n)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.append(week)
            keep.add(n)
    return [n for _, n in ours if n not in keep]


# Targets

class TargetError(Exception):
    pass


class LocalTarget:
    """A folder: the hub's own disk, or a share mounted on the host."""

    def __init__(self, path: str, app_dir: Optional[str] = None):
        p = Path(path or "data/backups")
        self.dir = p if p.is_absolute() else Path(app_dir or APP_DIR) / p

    def describe(self) -> str:
        return str(self.dir)

    async def upload(self, file: Path, name: str) -> None:
        def _copy():
            import shutil
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / (name + ".part")
            shutil.copyfile(file, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.dir / name)
        try:
            await asyncio.to_thread(_copy)
        except OSError as e:
            raise TargetError(f"can't write to {self.dir}: {e}") from e

    async def list(self) -> List[str]:
        def _ls():
            return [f.name for f in self.dir.iterdir() if f.is_file()] if self.dir.is_dir() else []
        return await asyncio.to_thread(_ls)

    async def delete(self, name: str) -> None:
        await asyncio.to_thread(lambda: (self.dir / name).unlink(missing_ok=True))


async def _file_chunks(path: Path):
    with open(path, "rb") as fh:
        while True:
            chunk = await asyncio.to_thread(fh.read, CHUNK)
            if not chunk:
                return
            yield chunk


class WebDavTarget:
    def __init__(self, url: str, username: str = "", password: str = "", client: Any = None):
        u = urlparse(url or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise TargetError("WebDAV URL must be http(s)://host/folder")
        self.url = url.rstrip("/")
        self.auth = (username, password) if username else None
        self._client = client

    def describe(self) -> str:
        return self.url

    def _cx(self):
        import httpx
        return self._client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=600, write=600))

    async def _request(self, method: str, name: str = "", **kw):
        cx = self._cx()
        try:
            r = await cx.request(method, f"{self.url}/{quote(name)}" if name else self.url + "/",
                                 auth=self.auth, **kw)
        except Exception as e:                            # noqa: BLE001
            raise TargetError(f"WebDAV unreachable ({type(e).__name__})") from e
        finally:
            if self._client is None:
                await cx.aclose()
        if r.status_code == 401:
            raise TargetError("WebDAV refused the username or password")
        if r.status_code >= 400 and not (method == "DELETE" and r.status_code == 404):
            raise TargetError(f"WebDAV answered {r.status_code} to {method}")
        return r

    async def upload(self, file: Path, name: str) -> None:
        await self._request("PUT", name, content=_file_chunks(file),
                            headers={"Content-Length": str(file.stat().st_size)})

    async def list(self) -> List[str]:
        r = await self._request("PROPFIND", headers={"Depth": "1"})
        return [n for n in (os.path.basename(h.rstrip("/")) for h in
                            re.findall(r"<(?:[A-Za-z]+:)?href>([^<]+)</", r.text)) if NAME_RE.match(n)]

    async def delete(self, name: str) -> None:
        await self._request("DELETE", name)


def sigv4(method: str, url: str, headers: Dict[str, str], payload_hash: str, region: str,
          access_key: str, secret_key: str, now: Optional[datetime] = None) -> Dict[str, str]:
    """AWS Signature Version 4 for S3; returns headers including Authorization."""
    u = urlparse(url)
    now = now or datetime.now(timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    h = {**headers, "host": u.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    canon_headers = "".join(f"{k.lower()}:{' '.join(str(v).split())}\n" for k, v in sorted(h.items(), key=lambda kv: kv[0].lower()))
    signed = ";".join(sorted(k.lower() for k in h))
    # The URL arrives encoded as it goes on the wire: the path is used as it
    # is, the query decoded and re-encoded the one way SigV4 specifies.
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
                     for k, v in sorted(parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([method, u.path or "/", query, canon_headers, signed, payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def mac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k = mac(mac(mac(mac(("AWS4" + secret_key).encode(), day), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    h["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    del h["host"]
    return h


class S3Target:
    """S3 and compatibles (MinIO, Backblaze B2, Cloudflare R2), path-style."""

    EMPTY = hashlib.sha256(b"").hexdigest()

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 region: str = "us-east-1", prefix: str = "", client: Any = None):
        u = urlparse(endpoint or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise TargetError("S3 endpoint must be http(s)://host")
        if not re.match(r"^[A-Za-z0-9.\-_]{1,255}$", bucket or ""):
            raise TargetError("Enter the bucket name")
        if not access_key or not secret_key:
            raise TargetError("S3 needs an access key and a secret key")
        self.endpoint, self.bucket, self.region = endpoint.rstrip("/"), bucket, region or "us-east-1"
        self.access_key, self.secret_key = access_key, secret_key
        self.prefix = (prefix or "").strip("/")
        self._client = client

    def describe(self) -> str:
        return f"{self.endpoint}/{self.bucket}/{self.prefix}".rstrip("/")

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    async def _request(self, method: str, path: str, query: str = "", payload_hash: str = "", **kw):
        import httpx
        url = f"{self.endpoint}/{self.bucket}{path}" + (f"?{query}" if query else "")
        headers = sigv4(method, url, kw.pop("headers", {}), payload_hash or self.EMPTY, self.region,
                        self.access_key, self.secret_key)
        cx = self._client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=600, write=600))
        try:
            r = await cx.request(method, url, headers=headers, **kw)
        except Exception as e:                            # noqa: BLE001
            raise TargetError(f"S3 unreachable ({type(e).__name__})") from e
        finally:
            if self._client is None:
                await cx.aclose()
        if r.status_code in (401, 403):
            raise TargetError("S3 refused the keys (or they can't write to that bucket)")
        if r.status_code >= 400 and not (method == "DELETE" and r.status_code == 404):
            raise TargetError(f"S3 answered {r.status_code} to {method}")
        return r

    async def upload(self, file: Path, name: str) -> None:
        def _hash():
            h = hashlib.sha256()
            with open(file, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
            return h.hexdigest()
        digest = await asyncio.to_thread(_hash)
        await self._request("PUT", "/" + quote(self._key(name)), payload_hash=digest,
                            content=_file_chunks(file),
                            headers={"content-length": str(file.stat().st_size)})

    async def list(self) -> List[str]:
        q = "list-type=2" + (f"&prefix={quote(self.prefix + '/', safe='')}" if self.prefix else "")
        r = await self._request("GET", "", query=q)
        return [n for n in (os.path.basename(k) for k in re.findall(r"<Key>([^<]+)</Key>", r.text))
                if NAME_RE.match(n)]

    async def delete(self, name: str) -> None:
        await self._request("DELETE", "/" + quote(self._key(name)))


# Schedule

DEFAULTS: Dict[str, Any] = {
    "enabled": False, "time": "03:30", "keep_daily": 7, "keep_weekly": 4,
    "include_telemetry": False, "encrypt": False,
    "target": {"type": "local", "path": "data/backups", "url": "", "username": "",
               "endpoint": "", "bucket": "", "region": "us-east-1", "prefix": "", "access_key": ""},
}
SECRET_KEYS = ("passphrase", "webdav_password", "s3_secret_key")
STALE_AFTER_S = 2 * 86400


def _read_secrets() -> Dict[str, Any]:
    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
        return {}


def _write_secrets(section: Dict[str, Any]) -> None:
    import yaml
    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_secrets()
    existing["backup"] = section
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)


def normalise_schedule(data: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(current))
    for k in ("enabled", "include_telemetry", "encrypt"):
        if k in data:
            cfg[k] = bool(data[k])
    if "time" in data:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(data["time"])):
            raise ValueError("Time must be HH:MM")
        cfg["time"] = str(data["time"])
    for k, lo, hi in (("keep_daily", 1, 60), ("keep_weekly", 0, 52)):
        if k in data:
            v = int(data[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be {lo}-{hi}")
            cfg[k] = v
    t = data.get("target")
    if isinstance(t, dict):
        tgt = cfg["target"]
        if "type" in t:
            if t["type"] not in ("local", "webdav", "s3"):
                raise ValueError("Target must be local, webdav or s3")
            tgt["type"] = t["type"]
        for k in ("path", "url", "username", "endpoint", "bucket", "region", "prefix", "access_key"):
            if k in t:
                v = str(t[k] or "").strip()
                if "\r" in v or "\n" in v:
                    raise ValueError(f"{k} must be a single line")
                tgt[k] = v
    return cfg


class BackupScheduler:
    def __init__(self, prepare: Optional[Callable[[bool], Awaitable[int]]] = None,
                 schedule_path: Path = SCHEDULE_PATH, status_path: Path = STATUS_PATH,
                 clock: Callable[[], float] = time.time, app_dir: Optional[str] = None,
                 target_factory: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Any]] = None,
                 alert: Optional[Callable[..., Any]] = None, resolve: Optional[Callable[[str], Any]] = None):
        self._prepare = prepare
        self.schedule_path, self.status_path = schedule_path, status_path
        self._clock = clock
        self._app_dir = app_dir
        self._target_factory = target_factory
        self._alert, self._resolve = alert, resolve
        self.config: Dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.status: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self.load()

    def load(self) -> None:
        for path, attr in ((self.schedule_path, "config"), (self.status_path, "status")):
            try:
                raw = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                logger.error("[backup] unreadable %s: %s", path, e)
                continue
            if attr == "config":
                try:
                    self.config = normalise_schedule(raw, self.config)
                except ValueError as e:
                    logger.error("[backup] bad saved schedule: %s", e)
            else:
                self.status = raw if isinstance(raw, dict) else {}

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    def secrets(self) -> Dict[str, str]:
        return {k: str(v) for k, v in (_read_secrets().get("backup") or {}).items() if v}

    def public(self) -> Dict[str, Any]:
        s = self.secrets()
        return {**self.config, **{f"{k}_set": bool(s.get(k)) for k in SECRET_KEYS},
                "status": self.status, "next_run": self.next_run()}

    def update(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cfg = normalise_schedule(data, self.config)
        s = self.secrets()
        for k in SECRET_KEYS:
            if k in data:
                if data[k] is None:
                    s.pop(k, None)
                elif str(data[k]).strip():
                    if "\n" in str(data[k]) or "\r" in str(data[k]):
                        raise ValueError(f"{k} must be a single line")
                    s[k] = str(data[k])
        if cfg["encrypt"] and not s.get("passphrase"):
            raise ValueError("Enter a passphrase to encrypt backups with")
        self.make_target(cfg, s)                     # refuse a target that can't be built
        _write_secrets(s)
        self.config = cfg
        self._write_json(self.schedule_path, cfg)
        return self.public()

    def make_target(self, cfg: Optional[Dict[str, Any]] = None, secrets: Optional[Dict[str, str]] = None):
        cfg, secrets = cfg or self.config, self.secrets() if secrets is None else secrets
        if self._target_factory:
            return self._target_factory(cfg, secrets)
        t = cfg["target"]
        try:
            if t["type"] == "webdav":
                return WebDavTarget(t["url"], t["username"], secrets.get("webdav_password", ""))
            if t["type"] == "s3":
                return S3Target(t["endpoint"], t["bucket"], t["access_key"], secrets.get("s3_secret_key", ""),
                                t["region"], t["prefix"])
            return LocalTarget(t["path"], self._app_dir)
        except TargetError as e:
            raise ValueError(str(e)) from e

    # When
    def _today_run(self, now: float) -> float:
        h, m = (int(x) for x in self.config["time"].split(":"))
        return datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()

    def due(self) -> bool:
        """After today's time, and not yet attempted today — so a hub that
        was off at 03:30 backs up when it comes back."""
        if not self.config["enabled"]:
            return False
        now = self._clock()
        run_at = self._today_run(now)
        return now >= run_at and float(self.status.get("last_attempt") or 0) < run_at

    def next_run(self) -> Optional[float]:
        if not self.config["enabled"]:
            return None
        now = self._clock()
        run_at = self._today_run(now)
        return run_at if float(self.status.get("last_attempt") or 0) < run_at else run_at + 86400

    # Doing it
    async def run(self) -> Dict[str, Any]:
        """One backup: build, encrypt, send, prune. Never raises; the outcome
        is in the status it returns and saves."""
        async with self._lock:
            started = self._clock()
            self.status = {**self.status, "last_attempt": started, "running": True}
            tmpdir = Path(tempfile.mkdtemp(prefix="zmm-backup-"))
            try:
                target = self.make_target()
                include = bool(self.config["include_telemetry"])
                devices = await self._prepare(include) if self._prepare else 0
                encrypted = bool(self.config["encrypt"])
                name = backup_name(include, datetime.fromtimestamp(started), encrypted)
                plain = tmpdir / "backup.zip"
                meta = await asyncio.to_thread(build_zip, plain, include, devices, self._app_dir)
                out = plain
                if encrypted:
                    out = tmpdir / "backup.zip.enc"
                    await asyncio.to_thread(encrypt_file, plain, out, self.secrets()["passphrase"])
                size = out.stat().st_size
                await target.upload(out, name)
                removed = []
                for old in to_delete(await target.list(), self.config["keep_daily"], self.config["keep_weekly"]):
                    if old != name:
                        await target.delete(old)
                        removed.append(old)
                self.status = {"last_attempt": started, "last_success": self._clock(), "last_error": None,
                               "last_file": name, "last_size": size, "files": meta["included"],
                               "target": target.describe(), "pruned": len(removed), "encrypted": encrypted}
                logger.info("[backup] %s (%d files, %.1f MB) -> %s; pruned %d", name, meta["included"],
                            size / 1e6, target.describe(), len(removed))
                if self._resolve:
                    self._resolve("backup:failed")
                    self._resolve("backup:stale")
            except Exception as e:                        # noqa: BLE001
                msg = str(e) or type(e).__name__
                self.status = {**self.status, "last_attempt": started, "last_error": msg, "running": False}
                logger.error("[backup] failed: %s", msg)
                if self._alert:
                    self._alert("error", "backup", "Scheduled backup failed", msg, dedupe_key="backup:failed")
            finally:
                self.status.pop("running", None)
                await asyncio.to_thread(_rmtree, tmpdir)
                try:
                    self._write_json(self.status_path, self.status)
                except OSError as e:
                    logger.warning("[backup] could not save status: %s", e)
            return self.status

    async def test_target(self) -> Dict[str, Any]:
        """Write, list and delete a small file: proves the target before 03:30 does."""
        target = self.make_target()
        tmp = Path(tempfile.mkdtemp(prefix="zmm-backup-")) / "probe"
        name = backup_name(False, datetime(2000, 1, 1))     # ours, and older than anything real
        try:
            tmp.write_bytes(b"ZMM backup target test\n")
            await target.upload(tmp, name)
            listed = name in await target.list()
            await target.delete(name)
            if not listed:
                raise TargetError("the file was written but doesn't show in the folder listing — "
                                  "old backups couldn't be pruned")
            return {"success": True, "target": target.describe()}
        except TargetError as e:
            return {"success": False, "error": str(e)}
        finally:
            await asyncio.to_thread(_rmtree, tmp.parent)

    # Loop
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                if self.due():
                    await self.run()
                self.check_stale()
            except asyncio.CancelledError:
                break
            except Exception as e:                        # noqa: BLE001
                logger.error("[backup] scheduler tick failed: %s", e)

    def check_stale(self) -> None:
        if not self.config["enabled"] or not self._alert:
            return
        last = float(self.status.get("last_success") or 0)
        if last and self._clock() - last > STALE_AFTER_S:
            days = int((self._clock() - last) // 86400)
            self._alert("warning", "backup", "No recent backup",
                        f"The last successful scheduled backup was {days} days ago.",
                        dedupe_key="backup:stale")


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


_scheduler: Optional[BackupScheduler] = None


def get_backup_scheduler() -> Optional[BackupScheduler]:
    return _scheduler


def set_backup_scheduler(s: Optional[BackupScheduler]) -> None:
    global _scheduler
    _scheduler = s
