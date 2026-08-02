"""
Per-user home/away tracking from the companion app.

Each user is exposed to the rest of ZMM as a virtual device (presence,
distance_m, accuracy_m, source, last_update), merged into the automation
engine's device registry so rules, AI automations and MQTT discovery work
unmodified. Live coordinates stay in memory and are never persisted; see
docs/presence_detection.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger("modules.presence_users")


PRESENCE_HOME = "home"
PRESENCE_AWAY = "away"
PRESENCE_UNKNOWN = "unknown"

DEFAULT_RADIUS_M = 100.0          # 100 m geofence radius
DEFAULT_HYSTERESIS_M = 30.0       # extra buffer to leave home (radius + this)
DEFAULT_STALE_AFTER_S = 30 * 60   # mark unknown after 30 min of silence
DEFAULT_MIN_ACCURACY_M = 250.0    # ignore fixes worse than this

# Every mode carries a heartbeat, and stale_after_s is derived from it: a stale
# window shorter than the heartbeat would guarantee false "unknown". 900 s is
# Android's floor for periodic background work. responsiveness_ms is a hint the
# OS may ignore — read it as "no faster than". See docs/presence_detection.md.
PRESENCE_MODES: Dict[str, Dict[str, Any]] = {
    "battery": {
        "label": "Battery saver",
        "heartbeat_s": 3600,           # 1 h
        "responsiveness_ms": 300_000,  # ~5 min crossing lag
        "priority": "low",
    },
    "balanced": {
        "label": "Balanced",
        "heartbeat_s": 1800,           # 30 min
        "responsiveness_ms": 120_000,  # ~2 min
        "priority": "balanced",
    },
    "responsive": {
        "label": "Responsive",
        "heartbeat_s": 900,            # 15 min — the platform floor
        "responsiveness_ms": 30_000,   # ~30 s
        "priority": "high",
    },
}

DEFAULT_PRESENCE_MODE = "balanced"

# Tolerated silence, as a multiple of the heartbeat: two missed beats plus slack,
# since one missed report is routine (doze, no signal, a dead spot).
STALE_HEARTBEAT_FACTOR = 2.5


def mode_params(mode: Optional[str]) -> Dict[str, Any]:
    """
    Resolved parameters for a mode, falling back to the default for anything
    unrecognised — a bad value in config must not stop presence working.
    """
    m = PRESENCE_MODES.get(mode or "", PRESENCE_MODES[DEFAULT_PRESENCE_MODE])
    return {
        **m,
        "mode": mode if mode in PRESENCE_MODES else DEFAULT_PRESENCE_MODE,
        "stale_after_s": round(m["heartbeat_s"] * STALE_HEARTBEAT_FACTOR),
    }


CONFIG_PATH = Path("./data/presence_users.yaml")
STATE_PATH = Path("./data/presence_state.json")

# IEEE-style identifier prefix for virtual users so they slot into the
# existing 16-hex-char namespace cleanly (collision-proof).
USER_IEEE_PREFIX = "user::"


def _user_ieee(user_id: str) -> str:
    """Stable virtual-IEEE for a user_id."""
    return f"{USER_IEEE_PREFIX}{user_id.lower()}"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    R = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# Data model

# Distinguishes "key absent" from "key present and null". d.get("account")
# returns None for both and would silently re-link every standalone record.
_MISSING = object()


@dataclass
class UserConfig:
    user_id: str                          # short stable id, e.g. "sean"
    display_name: str                     # "Sean"
    # Login account, or None for a standalone tracker. Explicit rather than the
    # old user_id == username convention, which broke in both directions.
    # See docs/presence_detection.md.
    account: Optional[str] = None
    # See PRESENCE_MODES. The phone fetches the resolved parameters, so changing
    # this retunes the device without reinstalling it.
    presence_mode: str = DEFAULT_PRESENCE_MODE
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    radius_m: float = DEFAULT_RADIUS_M
    hysteresis_m: float = DEFAULT_HYSTERESIS_M
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    min_accuracy_m: float = DEFAULT_MIN_ACCURACY_M
    enabled: bool = True
    # Opt-in: persists movement history, which the rest of this module
    # deliberately does not.
    journeys_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "UserConfig":
        # Records written before `account` existed relied on user_id == username,
        # so adopt user_id as the link — that is what they already meant. Whether
        # the account still exists is answered by `orphaned` in the API.
        account = d.get("account", _MISSING)
        if account is _MISSING:
            account = str(d["user_id"])

        mode = str(d.get("presence_mode") or DEFAULT_PRESENCE_MODE)
        if mode not in PRESENCE_MODES:
            mode = DEFAULT_PRESENCE_MODE

        return UserConfig(
            user_id=str(d["user_id"]),
            display_name=str(d.get("display_name") or d["user_id"]),
            account=account,
            presence_mode=mode,
            home_lat=d.get("home_lat"),
            home_lon=d.get("home_lon"),
            radius_m=float(d.get("radius_m", DEFAULT_RADIUS_M)),
            hysteresis_m=float(d.get("hysteresis_m", DEFAULT_HYSTERESIS_M)),
            # Derived from the mode, never stored independently: a shorter value
            # would mark the user "unknown" between two healthy reports.
            stale_after_s=float(mode_params(mode)["stale_after_s"]),
            min_accuracy_m=float(d.get("min_accuracy_m", DEFAULT_MIN_ACCURACY_M)),
            enabled=bool(d.get("enabled", True)),
            journeys_enabled=bool(d.get("journeys_enabled") or False),
        )


# Virtual device shim

class _Capabilities:
    """Minimal capabilities object so the automation engine treats us as
    a sensor (no actuator capabilities → never appears in target lists)."""
    def has_capability(self, _name: str) -> bool:
        return False


class HouseholdDevice:
    """
    Aggregate of every presence user, as a virtual device.

    Automations can already test one person ("is Sean away?") because
    prerequisites accept any ieee. What they cannot express is a question about
    the household as a whole — "is anybody in?" — without one condition per
    person and a rule that silently goes wrong the day someone is added.

    Exposing counts as attributes keeps that logic in the rule where the user
    can see it:

        home_count  <  1     nobody in           -> lock the door
        home_count  >= 1     somebody in
        anyone_home ==  0    same thing, boolean

    `unknown` deliberately counts as neither home nor away. A phone that has
    not reported is not evidence that its owner left, and a lock rule keyed on
    a flat battery is worse than one that does nothing.
    """

    IEEE = f"{USER_IEEE_PREFIX}_household"

    def __init__(self) -> None:
        self.ieee = self.IEEE
        self.friendly_name = "Household"
        self.manufacturer = "ZMM"
        self.model = "Presence Aggregate"
        self.last_seen: float = 0.0
        self.state: Dict[str, Any] = {
            "home_count": 0,
            "away_count": 0,
            "unknown_count": 0,
            "total": 0,
            # Ints, not bools: the rule builder's operators are numeric, and
            # "anyone_home < 1" reads the same way as "home_count < 1".
            "anyone_home": 0,
            "everyone_home": 0,
            "available": True,
            "last_update": None,
        }
        self.capabilities = _Capabilities()

    def recompute(self, devices: Dict[str, "PresenceUserDevice"]) -> bool:
        """Recalculate from the current users. True if anything changed."""
        home = away = unknown = 0
        for d in devices.values():
            if d.ieee == self.ieee or not getattr(d, "cfg", None):
                continue
            if not d.cfg.enabled:
                continue
            p = d.state.get("presence")
            if p == PRESENCE_HOME:
                home += 1
            elif p == PRESENCE_AWAY:
                away += 1
            else:
                unknown += 1

        total = home + away + unknown
        new = {
            "home_count": home,
            "away_count": away,
            "unknown_count": unknown,
            "total": total,
            "anyone_home": 1 if home > 0 else 0,
            # False for an empty household: "everyone is home" should not be
            # vacuously true when there is nobody to be home.
            "everyone_home": 1 if (total > 0 and home == total) else 0,
        }
        changed = any(self.state.get(k) != v for k, v in new.items())
        if changed:
            self.state.update(new)
            self.state["last_update"] = time.time()
            self.last_seen = time.time()
        return changed

    def is_available(self) -> bool:
        return True

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return []

    def get_device_discovery_configs(self) -> List[Dict[str, Any]]:
        return [
            {
                "component": "sensor",
                "object_id": "home_count",
                "node_id": "presence_household",
                "config": {
                    "name": "People home",
                    "state_topic": "zigbee/presence/_household/state",
                    "value_template": "{{ value_json.home_count }}",
                    "unique_id": "zmm_presence_household_home_count",
                    "icon": "mdi:home-account",
                },
            },
        ]


class PresenceUserDevice:
    """
    Quack-types as a Zigbee/Matter device for the automation engine and
    UI listing. Only the attributes/methods the rest of the code reads
    are implemented.
    """
    def __init__(self, cfg: UserConfig):
        self.cfg = cfg
        self.ieee = _user_ieee(cfg.user_id)
        self.friendly_name = cfg.display_name
        self.manufacturer = "ZMM"
        self.model = "Presence User"
        self.last_seen: float = 0.0
        self.state: Dict[str, Any] = {
            "presence": PRESENCE_UNKNOWN,
            # Named place: "home", "away", a place id, or "unknown" before
            # the first fix. Automations test this for "at the shops".
            "place": PRESENCE_UNKNOWN,
            "available": True,
            "distance_m": None,
            "accuracy_m": None,
            "source": None,
            "last_update": None,
        }
        # In-memory only — never persisted
        self._last_lat: Optional[float] = None
        self._last_lon: Optional[float] = None
        self.capabilities = _Capabilities()

    # The automation engine calls these
    def is_available(self) -> bool:
        return True

    def get_control_commands(self) -> List[Dict[str, Any]]:
        # Not actuatable from automations
        return []

    def get_device_discovery_configs(self) -> List[Dict[str, Any]]:
        """HA MQTT Discovery payload for this user."""
        node_id = f"presence_{self.cfg.user_id}"
        return [
            {
                "component": "device_tracker",
                "object_id": "presence",
                "config": {
                    "name": f"{self.cfg.display_name} Presence",
                    "unique_id": f"{node_id}_presence",
                    "state_topic": f"zigbee/presence/{self.cfg.user_id}/state",
                    "value_template": "{{ value_json.presence }}",
                    "payload_home": "home",
                    "payload_not_home": "away",
                    "source_type": "gps",
                    "device": {
                        "identifiers": [node_id],
                        "name": self.cfg.display_name,
                        "model": "Presence User",
                        "manufacturer": "ZMM",
                    },
                },
            },
        ]

    def to_device_list_entry(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee,
            "friendly_name": self.friendly_name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "type": "presence_user",
            "available": True,
            "state": dict(self.state),
            "last_seen": self.last_seen,
        }


# Manager

class PresenceUserManager:
    """
    Owns user configs, the virtual device dict, and ingest paths from
    the companion app over HTTP.
    """

    def __init__(
            self,
            mqtt_handler: Optional[Any] = None,
            event_emitter: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
            automation_evaluator: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
            config_path: Path = CONFIG_PATH,
            state_path: Path = STATE_PATH,
    ):
        self.mqtt_handler = mqtt_handler
        self.event_emitter = event_emitter
        self.automation_evaluator = automation_evaluator
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)

        # Presence users only — the household aggregate has no .cfg, and every
        # loop here assumes one. Exposed via automation_devices() instead.
        self.devices: Dict[str, PresenceUserDevice] = {}
        self.household = HouseholdDevice()
        self._stale_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._load_config()
        self._load_state()
        for dev in self.devices.values():
            await self._publish_discovery(dev)
            await self._publish_state(dev)
        self._stale_task = asyncio.create_task(self._stale_watcher())
        logger.info(f"Presence users started ({len(self.devices)} configured)")

    async def stop(self) -> None:
        if self._stale_task:
            self._stale_task.cancel()
            try:
                await self._stale_task
            except (asyncio.CancelledError, Exception):
                pass

    # Config persistence
    def _load_config(self) -> None:
        if not self.config_path.exists():
            return
        try:
            with open(self.config_path) as f:
                raw = yaml.safe_load(f) or {}
            users = raw.get("users", [])
            for u in users:
                try:
                    cfg = UserConfig.from_dict(u)
                    self.devices[_user_ieee(cfg.user_id)] = PresenceUserDevice(cfg)
                except Exception as e:
                    logger.warning(f"Skipping bad presence user entry: {e}")
            logger.info(f"Loaded {len(self.devices)} presence users")
        except Exception as e:
            logger.error(f"Failed to load presence users config: {e}")

    def _save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"users": [d.cfg.to_dict() for d in self.devices.values()]}
            with open(self.config_path, "w") as f:
                yaml.dump(payload, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.error(f"Failed to save presence users config: {e}")

    # Where we last believed each user was, so a restart does not flip every badge
    # to "unknown" until the next heartbeat (up to an hour in battery mode). Holds
    # no coordinates: presence/place/distance are what the UI already shows.

    _STATE_KEYS = ("presence", "place", "distance_m", "accuracy_m",
                   "source", "last_update")

    def _save_state(self) -> None:
        try:
            payload = {
                dev.cfg.user_id: {
                    **{k: dev.state.get(k) for k in self._STATE_KEYS},
                    "last_seen": dev.last_seen,
                }
                for dev in self.devices.values()
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: this file is rewritten on every fix, and a crash
            # mid-write must not leave half a JSON to choke the next startup.
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.state_path)
        except Exception as e:
            logger.debug(f"Failed to save presence state: {e}")

    def _load_state(self) -> None:
        """Restore last known state, demoting anything now stale to unknown."""
        if not self.state_path.exists():
            return
        try:
            saved = json.loads(self.state_path.read_text())
        except Exception as e:
            logger.warning(f"Ignoring unreadable presence state file: {e}")
            return
        if not isinstance(saved, dict):
            return

        now = time.time()
        restored = 0
        for user_id, snap in saved.items():
            dev = self.get_user(user_id)
            if not dev or not isinstance(snap, dict):
                continue
            last_seen = float(snap.get("last_seen") or 0.0)
            dev.last_seen = last_seen
            for k in self._STATE_KEYS:
                if k in snap:
                    dev.state[k] = snap[k]
            # Same staleness rule as the live watcher: a snapshot older than the
            # stale window is not evidence, and restoring it would show a
            # confidently wrong badge. last_seen survives either way.
            if not last_seen or now - last_seen > dev.cfg.stale_after_s:
                dev.state["presence"] = PRESENCE_UNKNOWN
                dev.state["place"] = PRESENCE_UNKNOWN
                dev.state["source"] = "stale"
            else:
                restored += 1
        if restored:
            logger.info(f"Restored presence state for {restored} user(s)")

    # CRUD
    def automation_devices(self) -> Dict[str, Any]:
        """
        Presence users plus the household aggregate, for the automation engine.

        The aggregate is a device to automations and a nuisance to everything
        else, so it is merged here rather than living in `devices`.
        """
        return {**self.devices, self.household.ieee: self.household}

    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {
                **d.cfg.to_dict(),
                "ieee": d.ieee,
                "state": dict(d.state),
                "last_seen": d.last_seen,
            }
            for d in self.devices.values()
        ]

    def get_user(self, user_id: str) -> Optional[PresenceUserDevice]:
        return self.devices.get(_user_ieee(user_id))

    async def upsert_user(self, data: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            try:
                cfg = UserConfig.from_dict(data)
            except Exception as e:
                return {"success": False, "error": f"Bad user payload: {e}"}

            if not cfg.user_id or not cfg.user_id.replace("_", "").isalnum():
                return {"success": False, "error": "user_id must be alphanumeric/underscore"}

            ieee = _user_ieee(cfg.user_id)
            existing = self.devices.get(ieee)
            if existing:
                # Preserve runtime state across config edits
                state_snapshot = dict(existing.state)
                last_seen = existing.last_seen
                last_lat, last_lon = existing._last_lat, existing._last_lon
                dev = PresenceUserDevice(cfg)
                dev.state.update(state_snapshot)
                dev.last_seen = last_seen
                dev._last_lat = last_lat
                dev._last_lon = last_lon
                self.devices[ieee] = dev
            else:
                self.devices[ieee] = PresenceUserDevice(cfg)

            self._save_config()
            await self._publish_discovery(self.devices[ieee])
            await self._publish_state(self.devices[ieee])
            return {"success": True, "user": self.list_users()}

    async def delete_user(self, user_id: str) -> Dict[str, Any]:
        async with self._lock:
            ieee = _user_ieee(user_id)
            dev = self.devices.pop(ieee, None)
            if not dev:
                return {"success": False, "error": "User not found"}
            self._save_config()
            self._save_state()
            await self._remove_discovery(dev)
            return {"success": True}

    # Ingest paths
    async def report_pwa_fix(
            self,
            user_id: str,
            lat: float,
            lon: float,
            accuracy: Optional[float] = None,
            timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Called from POST /api/presence/user/{user_id}."""
        return await self._ingest(
            user_id=user_id,
            lat=lat,
            lon=lon,
            accuracy=accuracy,
            timestamp=timestamp,
            source="pwa",
        )

    async def _ingest(
            self,
            user_id: str,
            lat: float,
            lon: float,
            accuracy: Optional[float],
            timestamp: Optional[float],
            source: str,
    ) -> Dict[str, Any]:
        dev = self.get_user(user_id)
        if not dev:
            return {"success": False, "error": "User not found"}
        if not dev.cfg.enabled:
            return {"success": False, "error": "User disabled"}

        if dev.cfg.home_lat is None or dev.cfg.home_lon is None:
            return {"success": False, "error": "User has no home location set"}

        # Drop low-accuracy fixes
        if accuracy is not None and accuracy > dev.cfg.min_accuracy_m:
            logger.debug(
                f"[{user_id}] dropping low-accuracy fix ({accuracy:.0f} m > "
                f"{dev.cfg.min_accuracy_m:.0f} m) from {source}"
            )
            return {"success": False, "error": "accuracy too low", "ignored": True}

        ts = timestamp or time.time()
        # Reject obviously stale fixes
        if ts < time.time() - 6 * 3600:
            return {"success": False, "error": "fix too old", "ignored": True}

        distance = _haversine_m(lat, lon, dev.cfg.home_lat, dev.cfg.home_lon)

        # Hysteresis: stay 'home' until we exceed radius+hysteresis
        prev = dev.state.get("presence", PRESENCE_UNKNOWN)
        radius = dev.cfg.radius_m
        leave_threshold = radius + dev.cfg.hysteresis_m

        if prev == PRESENCE_HOME:
            new_state = PRESENCE_HOME if distance <= leave_threshold else PRESENCE_AWAY
        else:
            new_state = PRESENCE_HOME if distance <= radius else PRESENCE_AWAY

        async with self._lock:
            dev._last_lat = lat
            dev._last_lon = lon
            dev.last_seen = ts

            # Resolved here, not on the phone, so one implementation decides and a
            # new or widened place applies immediately. "home" wins over any place
            # covering the same spot.
            new_place = PRESENCE_HOME if new_state == PRESENCE_HOME else "away"
            if new_state != PRESENCE_HOME:
                try:
                    from modules.places import get_place_manager
                    pm = get_place_manager()
                    hit = pm.resolve(lat, lon) if pm else None
                    if hit:
                        new_place = hit.id
                except Exception as e:                     # noqa: BLE001
                    # Place resolution must never break presence reporting.
                    logger.warning("[presence] place resolve failed: %s", e)

            changed: Dict[str, Any] = {}
            if dev.state.get("presence") != new_state:
                changed["presence"] = new_state
            if dev.state.get("source") != source:
                changed["source"] = source
            if dev.state.get("place") != new_place:
                changed["place"] = new_place

            dev.state["presence"] = new_state
            dev.state["place"] = new_place
            dev.state["distance_m"] = round(distance, 1)
            dev.state["accuracy_m"] = round(accuracy, 1) if accuracy else None
            dev.state["source"] = source
            dev.state["last_update"] = ts

        if changed:
            logger.info(
                f"[presence:{user_id}] {prev} → {new_state} "
                f"(distance {distance:.0f} m, source={source})"
            )
            await self._fire_state_change(dev, changed)
        else:
            # Still publish periodically so HA + WS see distance updates
            await self._publish_state(dev)
            await self._broadcast_event(dev)

        return {
            "success": True,
            "user_id": user_id,
            "presence": new_state,
            "distance_m": round(distance, 1),
        }

    async def manual_set(self, user_id: str, presence: str) -> Dict[str, Any]:
        """Manual override (e.g. for testing or when GPS isn't available)."""
        if presence not in (PRESENCE_HOME, PRESENCE_AWAY, PRESENCE_UNKNOWN):
            return {"success": False, "error": "Bad presence value"}
        dev = self.get_user(user_id)
        if not dev:
            return {"success": False, "error": "User not found"}

        prev = dev.state.get("presence")
        async with self._lock:
            dev.state["presence"] = presence
            dev.state["source"] = "manual"
            dev.state["last_update"] = time.time()
            dev.last_seen = time.time()

        if prev != presence:
            await self._fire_state_change(dev, {"presence": presence, "source": "manual"})
        return {"success": True, "user_id": user_id, "presence": presence}

    # State change pipeline
    async def _fire_state_change(
            self,
            dev: PresenceUserDevice,
            changed: Dict[str, Any],
    ) -> None:
        # 1. Trigger automation engine immediately
        if self.automation_evaluator:
            try:
                await self.automation_evaluator(dev.ieee, changed)
            except Exception as e:
                logger.error(f"[presence:{dev.cfg.user_id}] automation eval failed: {e}")

        # 2. Publish state to MQTT
        await self._publish_state(dev)

        # 3. Broadcast to UI websocket
        await self._broadcast_event(dev)

    async def _broadcast_event(self, dev: PresenceUserDevice) -> None:
        if not self.event_emitter:
            return
        try:
            await self.event_emitter("presence_user_updated", {
                "ieee": dev.ieee,
                "user_id": dev.cfg.user_id,
                "state": dict(dev.state),
                "last_seen": dev.last_seen,
            })
        except Exception as e:
            logger.debug(f"presence broadcast failed: {e}")

    # MQTT
    def _refresh_household(self) -> None:
        """
        Recalculate the aggregate after any change to a user's presence.

        Called from the publish path because that is the one point every
        state change already funnels through — computing it anywhere else
        would mean remembering to, and forgetting once leaves automations
        acting on a stale count.
        """
        try:
            self.household.recompute(self.devices)
        except Exception as e:                        # noqa: BLE001
            logger.warning("[presence] household recompute failed: %s", e)

    async def _publish_state(self, dev: PresenceUserDevice) -> None:
        self._refresh_household()
        # Every state change funnels through here (same reasoning as the
        # household recompute), so this is the one save point that cannot be
        # forgotten. Must run before the MQTT early-return: state should
        # survive a restart whether or not a broker is configured.
        self._save_state()
        if not self.mqtt_handler:
            return
        # The household aggregate has no cfg and its own topic; skip it here.
        if getattr(dev, "cfg", None) is None:
            return
        topic = f"zigbee/presence/{dev.cfg.user_id}/state"
        payload = {
            "presence": dev.state.get("presence"),
            "distance_m": dev.state.get("distance_m"),
            "accuracy_m": dev.state.get("accuracy_m"),
            "source": dev.state.get("source"),
            "last_update": dev.state.get("last_update"),
            "available": True,
        }
        try:
            await self.mqtt_handler.publish(
                topic, json.dumps(payload), retain=True, qos=1
            )
        except Exception as e:
            logger.debug(f"MQTT presence state publish failed: {e}")

    async def _publish_discovery(self, dev: PresenceUserDevice) -> None:
        if not self.mqtt_handler:
            return
        node_id = f"presence_{dev.cfg.user_id}"
        for entity in dev.get_device_discovery_configs():
            topic = (
                f"homeassistant/{entity['component']}/"
                f"{node_id}/{entity['object_id']}/config"
            )
            try:
                await self.mqtt_handler.publish(
                    topic, json.dumps(entity["config"]), retain=True, qos=1
                )
            except Exception as e:
                logger.debug(f"MQTT presence discovery failed: {e}")

    async def _remove_discovery(self, dev: PresenceUserDevice) -> None:
        if not self.mqtt_handler:
            return
        node_id = f"presence_{dev.cfg.user_id}"
        for entity in dev.get_device_discovery_configs():
            topic = (
                f"homeassistant/{entity['component']}/"
                f"{node_id}/{entity['object_id']}/config"
            )
            try:
                await self.mqtt_handler.publish(topic, "", retain=True, qos=1)
            except Exception as e:
                logger.debug(f"MQTT presence discovery removal failed: {e}")

    # Stale-fix watchdog
    async def _stale_watcher(self) -> None:
        """Mark users as 'unknown' if no fix has been received for too long."""
        try:
            while True:
                await asyncio.sleep(60)
                now = time.time()
                for dev in list(self.devices.values()):
                    if not dev.cfg.enabled:
                        continue
                    if not dev.last_seen:
                        continue
                    if dev.state.get("presence") == PRESENCE_UNKNOWN:
                        continue
                    if now - dev.last_seen > dev.cfg.stale_after_s:
                        prev = dev.state.get("presence")
                        async with self._lock:
                            dev.state["presence"] = PRESENCE_UNKNOWN
                            dev.state["source"] = "stale"
                        if prev != PRESENCE_UNKNOWN:
                            logger.info(
                                f"[presence:{dev.cfg.user_id}] {prev} → unknown "
                                f"(no fix in {now - dev.last_seen:.0f}s)"
                            )
                            await self._fire_state_change(
                                dev, {"presence": PRESENCE_UNKNOWN, "source": "stale"}
                            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Stale watcher crashed: {e}")


_manager: Optional[PresenceUserManager] = None


def get_presence_manager() -> Optional[PresenceUserManager]:
    return _manager


def set_presence_manager(mgr: PresenceUserManager) -> None:
    global _manager
    _manager = mgr