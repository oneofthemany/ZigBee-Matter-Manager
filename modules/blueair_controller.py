"""
Blueair air purifiers and humidifiers, through Blueair's cloud account API via
blueair_api (the client behind the Home Assistant Blueair integration).

Unlike the AC units this is cloud-only: there is no local protocol, so every
read and write goes through Blueair's servers and is subject to their rate
limits. The library is optional and imported lazily; without it the module
reports "library not installed" instead of breaking the app.

Two device generations share one account:
  * current ("aws") — HealthProtect, DustMagic, Blue Pure 311i+/411i+, humidifiers;
    power is `standby`, fan speed is a per-model scale (`fan_speed_count`).
  * classic — Classic 280i/480i/680i, Sense+; no standby, fan speed 0-3 where
    0 is off, brightness 0-4.
Both are normalised to one status shape (`_normalise`) with a `capabilities`
block the control modal renders from, so the UI never branches on generation.

Credentials never live in config.yaml (it is tracked in git): the environment
(ZMM_BLUEAIR_USERNAME / ZMM_BLUEAIR_PASSWORD) wins, then config/secrets.yaml
under `blueair`. config.yaml holds only `blueair: {enabled, region,
poll_interval_seconds}`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("zbm.blueair")

SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
ENV_USERNAME = "ZMM_BLUEAIR_USERNAME"
ENV_PASSWORD = "ZMM_BLUEAIR_PASSWORD"

REGIONS = ("eu", "us", "au", "cn")
DEFAULT_REGION = "eu"
# Blueair's cloud rate-limits aggressively (RateError); a purifier's readings
# move slowly anyway, so reads are cached and polls are floored.
MIN_POLL_SECONDS = 60
DEFAULT_POLL_SECONDS = 120
DEVICE_LIST_MAX_AGE = 3600.0


class BlueairError(Exception):
    """User-visible Blueair failure (no library, no credentials, cloud error)."""


# Credentials

def _read_secrets() -> Dict[str, Any]:
    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            return (yaml.safe_load(fh) or {}).get("blueair") or {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
        return {}


def resolve_credentials() -> tuple[str, str]:
    username = os.environ.get(ENV_USERNAME, "").strip()
    password = os.environ.get(ENV_PASSWORD, "").strip()
    if username and password:
        return username, password
    secrets = _read_secrets()
    return (username or str(secrets.get("username") or "").strip(),
            password or str(secrets.get("password") or "").strip())


def credentials_status() -> Dict[str, Any]:
    """What the settings UI may know. The password is never returned."""
    username, password = resolve_credentials()
    if os.environ.get(ENV_USERNAME) and os.environ.get(ENV_PASSWORD):
        source = "environment"
    elif username or password:
        source = "secrets_file"
    else:
        source = "none"
    return {"configured": bool(username and password), "source": source,
            "username": username}


def save_credentials(username: str, password: str) -> None:
    """Persist to the gitignored secrets file. The file is opened 0600 before
    anything is written, so it is never briefly readable by other users."""
    import yaml

    username, password = (username or "").strip(), (password or "").strip()
    if not username or not password:
        raise ValueError("Both username and password are required")

    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing = yaml.safe_load(path.read_text()) or {}
        except Exception:                                 # noqa: BLE001
            existing = {}
    existing["blueair"] = {"username": username, "password": password}

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)
    logger.info("Blueair credentials saved to %s", path)


# Normalisation

def _val(v: Any) -> Any:
    """The library marks fields a model lacks as NotImplemented, and SenML
    state values arrive as floats even when whole ("brightness": 55.0)."""
    if v is NotImplemented:
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _supported(v: Any) -> bool:
    return v is not NotImplemented and v is not None


def _normalise(dev: Any, generation: str) -> Dict[str, Any]:
    if generation == "aws":
        fan_max = int(getattr(dev, "fan_speed_count", 100) or 100)
        standby = _val(getattr(dev, "standby", NotImplemented))
        fan = _val(getattr(dev, "fan_speed", NotImplemented))
        caps = {
            "power": _supported(getattr(dev, "standby", NotImplemented)),
            "fan_speed": _supported(getattr(dev, "fan_speed", NotImplemented)),
            "auto": _supported(getattr(dev, "fan_auto_mode", NotImplemented)),
            "night_mode": _supported(getattr(dev, "night_mode", NotImplemented)),
            "child_lock": _supported(getattr(dev, "child_lock", NotImplemented)),
            "brightness": _supported(getattr(dev, "brightness", NotImplemented)),
            "germ_shield": _supported(getattr(dev, "germ_shield", NotImplemented)),
            "brightness_max": 100,
        }
        power = None if standby is None else not standby
        model = getattr(dev, "model_name", None) or getattr(dev, "type_name", None)
        filter_pct = _val(getattr(dev, "filter_usage_percentage", NotImplemented))
        filter_expired = filter_pct is not None and filter_pct >= 100
    else:
        fan_max = 3
        fan = _val(getattr(dev, "fan_speed", NotImplemented))
        caps = {
            # Classic units have no standby: fan speed 0 is off.
            "power": _supported(getattr(dev, "fan_speed", NotImplemented)),
            "fan_speed": _supported(getattr(dev, "fan_speed", NotImplemented)),
            "auto": _supported(getattr(dev, "fan_auto_mode", NotImplemented)),
            "night_mode": False,
            "child_lock": _supported(getattr(dev, "child_lock", NotImplemented)),
            "brightness": _supported(getattr(dev, "brightness", NotImplemented)),
            "germ_shield": False,
            "brightness_max": 4,
        }
        power = None if fan is None else fan > 0
        model = getattr(dev, "model", None)
        filter_pct = None
        filter_expired = _val(getattr(dev, "filter_expired", NotImplemented))

    fan_pct = None
    if fan is not None and fan_max:
        fan_pct = max(0, min(100, round(fan / fan_max * 100)))

    def reading(*names):
        for n in names:
            v = _val(getattr(dev, n, NotImplemented))
            if v is not None:
                return v
        return None

    return {
        "id": str(dev.uuid),
        "name": getattr(dev, "name", None) or getattr(dev, "name_api", None) or str(dev.uuid),
        "model": model or "Blueair",
        "mac": getattr(dev, "mac", None),
        "generation": generation,
        # The cloud's own online flag is unreliable (it reports offline for
        # devices that are streaming data), so reaching the cloud counts.
        "online": True,
        "power": power,
        "fan_speed_pct": fan_pct,
        "auto": _val(getattr(dev, "fan_auto_mode", NotImplemented)),
        "night_mode": _val(getattr(dev, "night_mode", NotImplemented)) if generation == "aws" else None,
        "child_lock": _val(getattr(dev, "child_lock", NotImplemented)),
        "brightness": _val(getattr(dev, "brightness", NotImplemented)),
        "germ_shield": _val(getattr(dev, "germ_shield", NotImplemented)) if generation == "aws" else None,
        "filter_usage_pct": filter_pct,
        "filter_expired": filter_expired,
        "pm1": reading("pm1"),
        "pm2_5": reading("pm2_5", "pm25"),
        "pm10": reading("pm10"),
        "voc": reading("voc", "total_voc"),
        "co2": reading("co2"),
        "temperature_c": reading("temperature"),
        "humidity_pct": reading("humidity"),
        "capabilities": caps,
    }


# Controller

class BlueairController:
    """Account session, device registry and a short status cache.

    All library calls are native asyncio (aiohttp), so nothing here blocks the
    event loop. One refresh runs at a time so a burst of UI requests does not
    multiply cloud calls into a rate limit.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._devices: Dict[str, tuple[Any, str]] = {}   # id -> (device, generation)
        self._status: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._apis: List[Any] = []
        self._devices_at = 0.0
        self._session_key: Optional[tuple] = None
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None
        self.reload(config or {})

    def reload(self, config: Dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))
        region = str(config.get("region") or DEFAULT_REGION).lower()
        self.region = region if region in REGIONS else DEFAULT_REGION
        poll = int(config.get("poll_interval_seconds") or DEFAULT_POLL_SECONDS)
        self.poll_seconds = max(MIN_POLL_SECONDS, poll)

    # Session
    async def _close_apis(self) -> None:
        apis, self._apis = self._apis, []
        for api in apis:
            try:
                await api.cleanup_client_session()
            except Exception as e:                        # noqa: BLE001
                logger.debug(f"Blueair session close: {e}")

    async def reset(self) -> None:
        """Drop the session and caches — credentials or region changed."""
        async with self._lock:
            await self._close_apis()
            self._devices.clear()
            self._status.clear()
            self._devices_at = 0.0
            self._session_key = None

    async def _login(self, username: str, password: str) -> None:
        try:
            import blueair_api
        except ImportError as e:
            raise BlueairError("blueair_api library not installed — "
                               "add 'blueair_api' to requirements") from e

        await self._close_apis()
        devices: Dict[str, tuple[Any, str]] = {}
        errors: List[str] = []

        # Newer devices: the one most accounts have.
        try:
            api, aws = await blueair_api.get_aws_devices(
                username=username, password=password, region=self.region)
            self._apis.append(api)
            for d in aws:
                devices[str(d.uuid)] = (d, "aws")
        except blueair_api.LoginError as e:
            # Accounts are per region, so a correct password on the wrong
            # region fails exactly like a wrong password.
            raise BlueairError(f"Blueair login failed ({self.region} region): {e} — "
                               f"check the password and that the region matches "
                               f"where the account was created") from e
        except Exception as e:                            # noqa: BLE001
            errors.append(f"current devices: {e}")

        # Classic devices live on the older API; an account without any
        # answers with an error there, which is not a failure.
        try:
            api, classic = await blueair_api.get_devices(username=username, password=password)
            self._apis.append(api)
            for d in classic:
                devices.setdefault(str(d.uuid), (d, "classic"))
        except Exception as e:                            # noqa: BLE001
            logger.debug(f"Blueair classic device lookup: {e}")

        if not devices and errors:
            raise BlueairError("Blueair: " + "; ".join(errors))
        self._devices = devices
        self._devices_at = time.monotonic()
        self._session_key = (username, self.region)
        logger.info(f"Blueair: {len(devices)} device(s) on the {self.region} account")

    async def _ensure_devices(self, force: bool = False) -> None:
        if not self.enabled:
            raise BlueairError("Blueair integration is disabled")
        username, password = resolve_credentials()
        if not (username and password):
            raise BlueairError("Blueair account not set up — add it in Settings → APIs → Blueair")
        stale = time.monotonic() - self._devices_at > DEVICE_LIST_MAX_AGE
        if force or stale or self._session_key != (username, self.region):
            await self._login(username, password)

    # Public
    async def test_login(self, username: str, password: str, region: str) -> List[Dict[str, Any]]:
        """Log in with the given credentials without touching the live session."""
        probe = BlueairController({"enabled": True, "region": region})
        try:
            async with probe._lock:
                await probe._login(username, password)
            return [{"id": i, "name": getattr(d, "name", None) or getattr(d, "name_api", None) or i,
                     "generation": g} for i, (d, g) in probe._devices.items()]
        finally:
            await probe._close_apis()

    def device_ids(self) -> List[str]:
        return list(self._devices)

    def cached_status(self, device_id: str) -> Optional[tuple[float, Dict[str, Any]]]:
        entry = self._status.get(device_id)
        if entry is None:
            return None
        return time.monotonic() - entry[0], entry[1]

    async def list_devices(self, max_age: Optional[float] = None) -> List[Dict[str, Any]]:
        max_age = self.poll_seconds if max_age is None else max_age
        async with self._lock:
            await self._ensure_devices()
            ids = list(self._devices)
        return [await self.status(i, max_age=max_age) for i in ids]

    async def status(self, device_id: str, max_age: Optional[float] = None) -> Dict[str, Any]:
        max_age = self.poll_seconds if max_age is None else max_age
        cached = self.cached_status(device_id)
        if cached is not None and cached[0] <= max_age:
            return cached[1]
        async with self._lock:
            # Another caller may have refreshed while this one waited.
            cached = self.cached_status(device_id)
            if cached is not None and cached[0] <= max_age:
                return cached[1]
            await self._ensure_devices()
            entry = self._devices.get(device_id)
            if entry is None:
                raise BlueairError(f"Unknown Blueair device {device_id}")
            dev, generation = entry
            try:
                await dev.refresh()
            except Exception as e:                        # noqa: BLE001
                self.last_error = str(e)
                if cached is not None:
                    # Serve the last good reading, flagged, rather than blank
                    # the UI on a transient cloud or rate-limit failure.
                    return {**cached[1], "online": False, "stale": True, "error": str(e)}
                raise BlueairError(f"Blueair refresh failed: {e}") from e
            status = _normalise(dev, generation)
            self._status[device_id] = (time.monotonic(), status)
            self.last_error = None
            return status

    async def control(self, device_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            await self._ensure_devices()
            entry = self._devices.get(device_id)
            if entry is None:
                raise BlueairError(f"Unknown Blueair device {device_id}")
            dev, generation = entry
            caps = _normalise(dev, generation)["capabilities"]
            try:
                await self._apply(dev, generation, caps, changes)
            except BlueairError:
                raise
            except Exception as e:                        # noqa: BLE001
                raise BlueairError(f"Blueair control failed: {e}") from e
            # The library updates the device object as it writes, so the
            # post-write status needs no extra cloud round trip.
            status = _normalise(dev, generation)
            self._status[device_id] = (time.monotonic(), status)
            return status

    @staticmethod
    async def _apply(dev: Any, generation: str, caps: Dict[str, Any],
                     changes: Dict[str, Any]) -> None:
        def need(cap: str) -> None:
            if not caps.get(cap):
                raise BlueairError(f"This device does not support {cap.replace('_', ' ')}")

        if "power" in changes:
            need("power")
            on = bool(changes["power"])
            if generation == "aws":
                await dev.set_standby(not on)
            elif not on:
                await dev.set_fan_speed("0")
            elif not (_val(dev.fan_speed) or 0):
                await dev.set_fan_speed("1")

        if "auto" in changes:
            need("auto")
            await dev.set_fan_auto_mode(bool(changes["auto"]))

        if "fan_speed_pct" in changes:
            need("fan_speed")
            pct = max(0, min(100, int(changes["fan_speed_pct"])))
            if generation == "aws":
                count = int(getattr(dev, "fan_speed_count", 100) or 100)
                # Humidifier-class models take gears 1..count; 0 is not a speed.
                floor = 1 if count <= 4 else 0
                await dev.set_fan_speed(max(floor, round(pct / 100 * count)))
            else:
                await dev.set_fan_speed(str(round(pct / 100 * 3)))

        if "night_mode" in changes:
            need("night_mode")
            await dev.set_night_mode(bool(changes["night_mode"]))

        if "child_lock" in changes:
            need("child_lock")
            await dev.set_child_lock(bool(changes["child_lock"]))

        if "germ_shield" in changes:
            need("germ_shield")
            await dev.set_germ_shield(bool(changes["germ_shield"]))

        if "brightness" in changes:
            need("brightness")
            top = int(caps.get("brightness_max") or 100)
            await dev.set_brightness(max(0, min(top, int(changes["brightness"]))))
