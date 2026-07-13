"""
ac_controller.py
================
Local-LAN air-conditioner control for Gree-protocol units (EcoAir and other
Gree clones) and Midea-protocol units (Comfee and other Midea clones). No
Home Assistant bridge — both protocols are spoken directly on the LAN using
the same libraries the popular HA components wrap:

  • gree  — `greeclimate` (async UDP, port 7000). The per-device AES key is
            derived once by bind() and persisted to config.
  • midea — `midea-local` (TCP, port 6444). V3 devices need a token/key pair
            fetched once from the Midea cloud (the library ships a preset
            anonymous account, so no personal credentials are required);
            after that everything is local.

Both libraries are optional dependencies: the module degrades to reporting
"library not installed" rather than breaking the app.

Config shape (config.yaml)
--------------------------
ac:
  units:
    - id: ac_living          # stable id, generated on add
      name: Living Room AC
      brand: gree | midea
      host: 192.168.1.60
      port: 7000             # gree default 7000, midea default 6444
      mac: "ab12cd34ef56"    # gree only
      key: "..."             # gree bind key / midea key
      device_id: 12345       # midea only (appliance id)
      token: "..."           # midea only
      protocol: 3            # midea only (1/2/3)
      model: ""              # midea only, optional
      subtype: 0             # midea only, optional
      room_id: room_abc      # optional heating/floor-plan room binding

Normalised state
----------------
Every adapter reports/accepts the same shape:
  { power: bool, mode: auto|cool|dry|fan|heat,
    target_c: float, current_c: float|None, fan: auto|low|medium|high|turbo,
    swing_v: bool, swing_h: bool,
    extras: {toggle_name: bool, ...} }        # only supported toggles
plus a `capabilities` block describing what the unit supports:
  { modes: [...], fan: [...], swing_v: bool, swing_h: bool,
    extras: [toggle names accepted by control()],
    min_c: float|None, max_c: float|None,
    source: b5|probed|assumed }
Midea capabilities come from the protocol's B5 capability frames (decoded by
midea-local during every refresh). Gree has no capability query, so support
is inferred from which props the unit echoes back in its status response
(`raw_properties`); an empty echo falls back to the standard Gree set.

Normalised toggle names (control() keys, mapped per brand):
  turbo, quiet, xfan, light, sleep, anion, eco, display, indirect_wind
Vendor-specific keys (gree horizontal_swing ints, midea swing_vertical, …)
still pass straight through for callers that want raw control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("zbm.ac")

GREE_DEFAULT_PORT = 7000
MIDEA_DEFAULT_PORT = 6444
STATUS_CACHE_SEC = 5.0
# Last good status per unit (including the capabilities block that drives
# the control UI) persisted across restarts/controller rebuilds, so the UI
# can render controls instantly instead of re-probing every unit.
STATUS_STORE_PATH = "./data/ac_status_cache.json"
STATUS_STORE_MIN_WRITE_SEC = 30.0
# Midea dongles lock up and refuse TCP for a while if they see rapid
# connect churn (verified against a Comfee 00000Q1D: ~3 reconnects in a few
# seconds and port 6444 goes dead). After a failed connect, wait this long
# before trying again rather than hammering it back into lockup.
MIDEA_CONNECT_BACKOFF_SEC = 20.0

# Midea AC mode values as used by midealocal.devices.ac
_MIDEA_MODES = {1: "auto", 2: "cool", 3: "dry", 4: "heat", 5: "fan"}
_MIDEA_MODES_REV = {v: k for k, v in _MIDEA_MODES.items()}
# Midea fan_speed: 1..100 percent, 102 = auto
_MIDEA_FAN = {"auto": 102, "silent": 20, "low": 40, "medium": 60,
              "high": 80, "turbo": 100}
# Normalised toggle → midealocal attribute name
_MIDEA_TOGGLES = {"eco": "eco_mode", "sleep": "sleep_mode",
                  "turbo": "boost_mode", "display": "screen_display",
                  "anion": "anion", "indirect_wind": "indirect_wind"}
# B5 capability flag(s) that gate each toggle; None = no flag, assume yes
_MIDEA_TOGGLE_FLAGS = {"eco": ("eco",), "sleep": None,
                       "turbo": ("turbo_cool", "turbo_heat"),
                       "display": ("display_control",), "anion": ("anion",),
                       "indirect_wind": ("fn_no_wind_sense",)}

# Normalised toggle → gree Device attribute (all bool-ish setters)
_GREE_TOGGLES = {"turbo": "turbo", "quiet": "quiet", "xfan": "xfan",
                 "light": "light", "sleep": "sleep", "anion": "anion",
                 "eco": "power_save"}
# Normalised toggle → gree wire prop, for the raw_properties support probe
_GREE_TOGGLE_PROPS = {"turbo": "Tur", "quiet": "Quiet", "xfan": "Blo",
                      "light": "Lig", "sleep": "SwhSlp", "anion": "Health",
                      "eco": "SvSt"}


def _midea_fan_name(speed: Optional[int]) -> str:
    if speed is None:
        return "auto"
    if speed == 102:
        return "auto"
    if speed <= 25:
        return "silent"
    if speed <= 45:
        return "low"
    if speed <= 65:
        return "medium"
    if speed <= 85:
        return "high"
    return "turbo"


class ACError(Exception):
    """Raised for user-visible AC failures (bad config, offline, no lib)."""


# ────────────────────────── Gree adapter ──────────────────────────

class GreeAdapter:
    """One Gree-protocol unit (EcoAir). All calls are async."""

    def __init__(self, cfg: Dict[str, Any], on_key_learned=None):
        self.cfg = cfg
        self._device = None
        self._on_key_learned = on_key_learned

    def close(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            try:
                device.close()          # closes the UDP transport if open
            except Exception as e:
                logger.debug(f"gree close: {e}")

    async def _ensure(self):
        if self._device is not None:
            return self._device
        try:
            from greeclimate.device import Device, DeviceInfo
        except ImportError as e:
            raise ACError("greeclimate library not installed — "
                          "add 'greeclimate' to requirements") from e
        from greeclimate.cipher import CipherV1, CipherV2
        info = DeviceInfo(
            ip=self.cfg["host"],
            port=int(self.cfg.get("port") or GREE_DEFAULT_PORT),
            mac=self.cfg.get("mac") or "",
            name=self.cfg.get("name") or self.cfg.get("id"),
        )
        device = Device(info)
        key = self.cfg.get("key")
        bound = False
        if key:
            # greeclimate quirks on a keyed bind: it raises "cipher must be
            # provided when key is provided" unless the cipher negotiated on
            # first contact is passed back in (persisted as cfg["cipher"]),
            # and it skips opening the UDP transport entirely — replicate
            # the endpoint setup its negotiation path performs.
            try:
                cipher_ver = int(self.cfg.get("cipher") or 1)
                cipher = CipherV2() if cipher_ver == 2 else CipherV1()
                await device.bind(key=key, cipher=cipher)
                if getattr(device, "_transport", None) is None:
                    loop = asyncio.get_event_loop()
                    device._transport, _ = await loop.create_datagram_endpoint(
                        lambda: device, remote_addr=(info.ip, info.port))
                bound = True
            except Exception as e:
                logger.warning(f"gree keyed bind failed ({e}) — "
                               f"renegotiating a fresh key")
        if not bound:
            await device.bind()
            if device.device_key and self._on_key_learned:
                # First contact — persist key + cipher version so future
                # binds are instant and use the right encryption.
                learned_ver = 2 if isinstance(device.device_cipher, CipherV2) else 1
                try:
                    self._on_key_learned(self.cfg.get("id"),
                                         device.device_key, learned_ver)
                except Exception as e:
                    logger.warning(f"gree key persist failed: {e}")
        self._device = device
        return device

    async def status(self) -> Dict[str, Any]:
        from greeclimate.device import Mode, FanSpeed
        d = await self._ensure()
        await d.update_state()
        mode_name = None
        try:
            mode_name = Mode(d.mode).name.lower() if d.mode is not None else None
        except ValueError:
            pass
        fan_name = None
        try:
            fan_name = FanSpeed(d.fan_speed).name.lower() if d.fan_speed is not None else None
        except ValueError:
            pass

        # Gree has no capability query — treat "prop echoed back in the
        # status response" as supported. An empty echo (shouldn't happen on
        # a live unit) falls back to assuming the standard Gree set.
        raw = d.raw_properties or {}
        probed = bool(raw)

        def _has(prop: str) -> bool:
            return prop in raw if probed else True

        supported = [n for n, p in _GREE_TOGGLE_PROPS.items() if _has(p)]
        capabilities = {
            "modes": ["auto", "cool", "dry", "fan", "heat"],
            "fan": ["auto", "low", "medium", "high", "turbo"],
            "swing_v": _has("SwUpDn"),
            "swing_h": _has("SwingLfRig"),
            "extras": supported,
            "min_c": 16.0, "max_c": 30.0,
            "source": "probed" if probed else "assumed",
        }
        extras = {n: bool(getattr(d, _GREE_TOGGLES[n], None)) for n in supported}
        return {
            "power": bool(d.power),
            "mode": mode_name,
            "target_c": d.target_temperature,
            "current_c": d.current_temperature,
            "fan": {"mediumlow": "low", "mediumhigh": "high"}.get(fan_name, fan_name),
            # any non-Default position (fixed or full) counts as swing "on"
            "swing_v": bool(d.vertical_swing),
            "swing_h": bool(d.horizontal_swing),
            "extras": extras,
            "capabilities": capabilities,
        }

    async def control(self, changes: Dict[str, Any]) -> None:
        from greeclimate.device import (Mode, FanSpeed, HorizontalSwing,
                                        VerticalSwing)
        d = await self._ensure()
        await d.update_state()
        if "power" in changes:
            d.power = bool(changes["power"])
        if changes.get("mode"):
            name = str(changes["mode"]).capitalize()
            try:
                d.mode = Mode[name].value
            except KeyError:
                raise ACError(f"unknown mode '{changes['mode']}' "
                              f"(use auto/cool/dry/fan/heat)")
        if changes.get("target_c") is not None:
            d.target_temperature = int(round(float(changes["target_c"])))
        if changes.get("fan"):
            fan_map = {"auto": "Auto", "silent": "Low", "low": "Low",
                       "medium": "Medium", "high": "High", "turbo": "High"}
            fan_req = str(changes["fan"]).lower()
            name = fan_map.get(fan_req)
            if not name:
                raise ACError(f"unknown fan '{changes['fan']}'")
            d.fan_speed = FanSpeed[name].value
            # turbo is a flag on top of the speed — set it for "turbo",
            # clear it when any plain speed is picked so the unit actually
            # drops out of turbo.
            d.turbo = fan_req == "turbo"
            if fan_req == "silent":
                d.quiet = True
        # normalised swing toggles: on = full swing, off = default position
        if "swing_v" in changes:
            d.vertical_swing = (VerticalSwing.FullSwing.value
                                if changes["swing_v"] else
                                VerticalSwing.Default.value)
        if "swing_h" in changes:
            d.horizontal_swing = (HorizontalSwing.FullSwing.value
                                  if changes["swing_h"] else
                                  HorizontalSwing.Default.value)
        # normalised toggles (light/quiet/xfan/turbo/sleep/anion/eco)
        for name, attr in _GREE_TOGGLES.items():
            if name in changes:
                setattr(d, attr, bool(changes[name]))
        # raw vendor passthrough (positional swing ints, gree-only flags)
        for extra in ("horizontal_swing", "vertical_swing",
                      "power_save", "steady_heat"):
            if extra in changes:
                setattr(d, extra, changes[extra])
        await d.push_state_update()


# ────────────────────────── Midea adapter ──────────────────────────

class MideaAdapter:
    """
    One Midea-protocol unit (Comfee). midea-local is thread/socket based,
    so blocking calls run in the default executor.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._device = None
        self._lock = threading.Lock()      # one protocol exchange at a time
        self._last_connect_fail = 0.0

    def close(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            try:
                device.close_socket()
            except Exception as e:
                logger.debug(f"midea close_socket: {e}")

    def _ensure_sync(self):
        if self._device is not None:
            return self._device
        try:
            from midealocal.devices.ac import MideaACDevice
            from midealocal.const import ProtocolVersion
        except ImportError as e:
            raise ACError("midea-local library not installed — "
                          "add 'midea-local' to requirements") from e
        token = self.cfg.get("token") or ""
        key = self.cfg.get("key") or ""
        proto = int(self.cfg.get("protocol") or 3)
        if proto >= 3 and (not token or not key):
            raise ACError("Midea V3 unit needs token+key — run "
                          "POST /api/ac/units/{id}/bind first (fetches them "
                          "via the library's preset cloud account)")
        remaining = MIDEA_CONNECT_BACKOFF_SEC - (time.monotonic() - self._last_connect_fail)
        if remaining > 0:
            raise ACError(f"Midea unit refused a connection recently — "
                          f"retrying in {remaining:.0f}s")
        device = MideaACDevice(
            name=self.cfg.get("name") or self.cfg.get("id") or "midea_ac",
            device_id=int(self.cfg["device_id"]),
            ip_address=self.cfg["host"],
            port=int(self.cfg.get("port") or MIDEA_DEFAULT_PORT),
            token=token,
            key=key,
            device_protocol=ProtocolVersion(proto),
            model=str(self.cfg.get("model") or ""),
            subtype=int(self.cfg.get("subtype") or 0),
            customize="",
        )
        if not device.connect():
            self._last_connect_fail = time.monotonic()
            raise ACError(f"could not connect to Midea unit at "
                          f"{self.cfg['host']}:{self.cfg.get('port') or MIDEA_DEFAULT_PORT}")
        self._last_connect_fail = 0.0
        self._device = device
        return device

    def _with_reconnect(self, op):
        """Run op(device); on a socket-level failure, reconnect once and retry."""
        with self._lock:
            try:
                return op(self._ensure_sync())
            except ACError:
                raise
            except Exception as e:
                logger.info(f"midea op failed ({e}) — reconnecting once")
                self.close()
                return op(self._ensure_sync())

    def _status_sync(self) -> Dict[str, Any]:
        def _refresh(d):
            # check_protocol=True is load-bearing: without it midea-local
            # only SENDS the queries — responses are consumed by the
            # library's background run() loop, which we don't run, so
            # attributes would stay at their defaults forever.
            d.refresh_status(True)
            return d
        d = self._with_reconnect(_refresh)
        attrs = dict(d.attributes or {})
        mode_val = attrs.get("mode")
        power = bool(attrs.get("power"))
        capabilities = self._capabilities(d, attrs)
        extras = {n: bool(attrs.get(_MIDEA_TOGGLES[n]))
                  for n in capabilities["extras"]}
        return {
            "power": power,
            "mode": _MIDEA_MODES.get(mode_val, "auto") if power else _MIDEA_MODES.get(mode_val),
            "target_c": attrs.get("target_temperature"),
            "current_c": attrs.get("indoor_temperature"),
            "outdoor_c": attrs.get("outdoor_temperature"),
            "fan": _midea_fan_name(attrs.get("fan_speed")),
            "swing_v": bool(attrs.get("swing_vertical")),
            "swing_h": bool(attrs.get("swing_horizontal")),
            "extras": extras,
            "capabilities": capabilities,
        }

    @staticmethod
    def _capabilities(d, attrs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalised capability block from the B5 capability flags midea-local
        decodes during refresh (device.capabilities). Units that never answer
        the B5 query get the assumed full set.
        """
        b5 = dict(getattr(d, "capabilities", None) or {})
        if b5:
            modes = [m for m, flag in (("auto", "auto_mode"),
                                       ("cool", "cool_mode"),
                                       ("dry", "dry_mode"),
                                       ("heat", "heat_mode"))
                     if b5.get(flag)]
            modes.append("fan")            # fan-only is always available
            fan_flags = [f for f in ("fan_auto", "fan_silent", "fan_low",
                                     "fan_medium", "fan_high") if f in b5]
            if fan_flags:
                fans = [f.removeprefix("fan_") for f in fan_flags if b5[f]]
                fans.append("turbo")       # fan_speed=100% always accepted
            else:
                fans = ["auto", "low", "medium", "high", "turbo"]
            extras = [n for n, flags in _MIDEA_TOGGLE_FLAGS.items()
                      if flags is None or any(b5.get(f) for f in flags)]
            swing_v = bool(b5.get("swing_vertical"))
            swing_h = bool(b5.get("swing_horizontal"))
            source = "b5"
        else:
            modes = ["auto", "cool", "dry", "fan", "heat"]
            fans = ["auto", "low", "medium", "high", "turbo"]
            extras = ["eco", "sleep", "turbo", "display"]
            swing_v = swing_h = True
            source = "assumed"
        return {
            "modes": [m for m in ("auto", "cool", "dry", "fan", "heat")
                      if m in modes],
            "fan": [f for f in ("auto", "silent", "low", "medium",
                                "high", "turbo") if f in fans],
            "swing_v": swing_v, "swing_h": swing_h,
            "extras": extras,
            "min_c": attrs.get("min_temperature"),
            "max_c": attrs.get("max_temperature"),
            "source": source,
        }

    def _control_sync(self, changes: Dict[str, Any]) -> None:
        # Validate before touching the device so bad input can't leave a
        # half-applied change after a reconnect retry.
        mode = None
        if changes.get("mode"):
            mode = _MIDEA_MODES_REV.get(str(changes["mode"]).lower())
            if mode is None:
                raise ACError(f"unknown mode '{changes['mode']}' "
                              f"(use auto/cool/dry/fan/heat)")
        speed = None
        if changes.get("fan"):
            speed = _MIDEA_FAN.get(str(changes["fan"]).lower())
            if speed is None:
                raise ACError(f"unknown fan '{changes['fan']}'")

        def _apply(d):
            # Sync the library's view of the unit first — set messages are
            # built from cached attributes, and stale power/mode makes the
            # unit silently ignore them. check_protocol=True is required to
            # actually read the responses (see _status_sync).
            d.refresh_status(True)
            attrs = dict(d.attributes or {})
            target = changes.get("target_c")
            # A mode change implies power-on (that's also what the midea
            # protocol message does), otherwise honour an explicit power
            # change, otherwise keep the current state.
            if "power" in changes:
                want_power = bool(changes["power"])
            else:
                want_power = True if mode is not None else bool(attrs.get("power"))

            if target is not None:
                # The unit only honours a setpoint sent together with
                # power+mode — send all three in one message.
                eff_mode = mode if mode is not None else attrs.get("mode")
                if want_power and eff_mode in _MIDEA_MODES:
                    d.set_target_temperature(float(target), eff_mode)
                else:
                    # off (or no usable mode): best-effort bare setpoint
                    d.set_target_temperature(float(target), None)
                if "power" in changes and not changes["power"]:
                    d.set_attribute("power", False)
            else:
                if "power" in changes:
                    d.set_attribute("power", bool(changes["power"]))
                if mode is not None:
                    d.set_attribute("mode", mode)
            if speed is not None:
                d.set_attribute("fan_speed", speed)
            # normalised swing + toggles
            if "swing_v" in changes:
                d.set_attribute("swing_vertical", bool(changes["swing_v"]))
            if "swing_h" in changes:
                d.set_attribute("swing_horizontal", bool(changes["swing_h"]))
            for name, attr in _MIDEA_TOGGLES.items():
                if name in changes:
                    d.set_attribute(attr, bool(changes[name]))
            # raw vendor passthrough
            for extra in ("eco_mode", "sleep_mode", "swing_vertical",
                          "swing_horizontal", "screen_display",
                          "boost_mode", "indirect_wind", "anion"):
                if extra in changes:
                    d.set_attribute(extra, changes[extra])
        self._with_reconnect(_apply)

    async def status(self) -> Dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(None, self._status_sync)

    async def control(self, changes: Dict[str, Any]) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None, self._control_sync, changes)


# ────────────────────────── controller ──────────────────────────

class ACController:
    """
    Registry of configured units + discovery. Config persistence is the
    caller's job (routes read/write config.yaml); the controller is handed
    the current `ac` config block and an optional key-persist callback.
    """

    def __init__(self, ac_config: Optional[Dict[str, Any]] = None,
                 on_key_learned=None):
        self._on_key_learned = on_key_learned
        self._adapters: Dict[str, Any] = {}
        self._status_cache: Dict[str, tuple] = {}   # id → (ts, status)
        self._last_persist = 0.0
        self._load_status_store()
        self.reload(ac_config or {})

    def reload(self, ac_config: Dict[str, Any]) -> None:
        """
        Apply (possibly unchanged) config. Adapters for units whose config
        is identical are KEPT — routes call this on every request, and
        rebuilding adapters each time meant a fresh TCP connection per
        request, which Midea dongles punish by refusing connections.
        """
        new_units: List[Dict[str, Any]] = list(ac_config.get("units") or [])
        old_by_id = {str(u.get("id")): u for u in getattr(self, "units", [])}
        keep = {}
        for u in new_units:
            uid = str(u.get("id"))
            if uid in self._adapters and old_by_id.get(uid) == u:
                keep[uid] = self._adapters[uid]
        for uid, adapter in self._adapters.items():
            if uid not in keep:
                try:
                    adapter.close()
                except Exception as e:
                    logger.debug(f"adapter close for {uid}: {e}")
        self._adapters = keep
        # Drop cache only for units no longer configured — keeping it for
        # not-yet-connected units is what lets disk-loaded status survive
        # until the first live probe replaces it.
        new_ids = {str(u.get("id")) for u in new_units}
        self._status_cache = {uid: v for uid, v in self._status_cache.items()
                              if uid in new_ids}
        self.units = new_units

    # ── status persistence ───────────────────────────────────────

    def _load_status_store(self) -> None:
        try:
            with open(STATUS_STORE_PATH, "r") as f:
                raw = json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        now_wall, now_mono = time.time(), time.monotonic()
        for uid, ent in raw.items():
            st = ent.get("status")
            if not isinstance(st, dict):
                continue
            age = max(0.0, now_wall - float(ent.get("ts") or 0))
            # Mark so callers can tell "restored, possibly stale" from live
            self._status_cache[uid] = (now_mono - age, {**st, "cached": True})

    def _persist_status_store(self) -> None:
        now = time.monotonic()
        if now - self._last_persist < STATUS_STORE_MIN_WRITE_SEC:
            return
        self._last_persist = now
        data = {}
        for uid, (mts, st) in self._status_cache.items():
            if st.get("online"):
                data[uid] = {
                    "ts": time.time() - (now - mts),
                    "status": {k: v for k, v in st.items() if k != "cached"},
                }
        if not data:
            return
        try:
            os.makedirs(os.path.dirname(STATUS_STORE_PATH), exist_ok=True)
            tmp = STATUS_STORE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATUS_STORE_PATH)
        except OSError as e:
            logger.debug(f"AC status store write failed: {e}")

    def unit_config(self, unit_id: str) -> Optional[Dict[str, Any]]:
        return next((u for u in self.units if str(u.get("id")) == str(unit_id)), None)

    def _adapter(self, unit_id: str):
        if unit_id in self._adapters:
            return self._adapters[unit_id]
        cfg = self.unit_config(unit_id)
        if not cfg:
            raise ACError(f"unknown AC unit '{unit_id}'")
        brand = str(cfg.get("brand") or "").lower()
        if brand == "gree":
            adapter = GreeAdapter(cfg, on_key_learned=self._on_key_learned)
        elif brand == "midea":
            adapter = MideaAdapter(cfg)
        else:
            raise ACError(f"unit '{unit_id}' has unknown brand "
                          f"'{cfg.get('brand')}' (use gree|midea)")
        self._adapters[unit_id] = adapter
        return adapter

    def cached_status(self, unit_id: str) -> Optional[Tuple[float, Dict[str, Any]]]:
        """(age_sec, status) of the last probe result, or None if never
        probed. Never triggers a probe — the /api/devices hot path uses this
        to stay non-blocking while an offline unit is timing out."""
        cached = self._status_cache.get(unit_id)
        if not cached:
            return None
        return time.monotonic() - cached[0], cached[1]

    async def status(self, unit_id: str, max_age_sec: float = STATUS_CACHE_SEC) -> Dict[str, Any]:
        cached = self._status_cache.get(unit_id)
        if cached and (time.monotonic() - cached[0]) < max_age_sec:
            return cached[1]
        cfg = self.unit_config(unit_id) or {}
        base = {"id": unit_id, "name": cfg.get("name"),
                "brand": cfg.get("brand"), "room_id": cfg.get("room_id")}
        try:
            state = await asyncio.wait_for(self._adapter(unit_id).status(), timeout=10)
            result = {**base, "online": True, **state}
        except (ACError, Exception) as e:            # noqa: BLE001 — degrade per unit
            result = {**base, "online": False, "error": str(e)}
            # Carry last-known capabilities forward so the control UI can
            # still render (disabled) controls for an offline unit.
            prev = cached or self._status_cache.get(unit_id)
            prev_caps = (prev[1] if prev else {}).get("capabilities")
            if prev_caps:
                result["capabilities"] = prev_caps
        self._status_cache[unit_id] = (time.monotonic(), result)
        if result.get("online"):
            self._persist_status_store()
        return result

    async def control(self, unit_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        adapter = self._adapter(unit_id)
        await asyncio.wait_for(adapter.control(changes), timeout=10)
        self._status_cache.pop(unit_id, None)
        return await self.status(unit_id, max_age_sec=0)

    # ── discovery ────────────────────────────────────────────────

    async def discover(self, wait_for: int = 4) -> Dict[str, List[Dict[str, Any]]]:
        """Scan the LAN for both protocols; returns candidates, not config."""
        found: Dict[str, List[Dict[str, Any]]] = {"gree": [], "midea": []}

        try:
            from greeclimate.discovery import Discovery
            infos = await Discovery().scan(wait_for=wait_for)
            for di in infos:
                found["gree"].append({
                    "brand": "gree", "host": di.ip, "port": di.port,
                    "mac": di.mac, "name": di.name,
                    "model": getattr(di, "model", None),
                })
        except ImportError:
            found["gree_error"] = ["greeclimate not installed"]
        except Exception as e:
            logger.warning(f"gree discovery failed: {e}")
            found["gree_error"] = [str(e)]

        try:
            from midealocal.discover import discover as midea_discover
            raw = await asyncio.get_event_loop().run_in_executor(None, midea_discover)
            for dev_id, info in (raw or {}).items():
                found["midea"].append({
                    "brand": "midea", "device_id": dev_id,
                    "host": info.get("ip_address"), "port": info.get("port"),
                    "protocol": int(info.get("protocol") or 3),
                    "type": info.get("type"), "sn": info.get("sn"),
                    "model": info.get("model"),
                })
        except ImportError:
            found["midea_error"] = ["midea-local not installed"]
        except Exception as e:
            logger.warning(f"midea discovery failed: {e}")
            found["midea_error"] = [str(e)]

        return found

    # ── midea cloud token fetch (one-time, then fully local) ─────

    async def fetch_midea_keys(self, device_id: int,
                               account: Optional[str] = None,
                               password: Optional[str] = None,
                               cloud_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch token/key for a Midea V3 unit. Uses the library's preset
        anonymous account unless the caller supplies their own.
        """
        try:
            import aiohttp
            from midealocal.cloud import get_midea_cloud, get_preset_account_cloud
        except ImportError as e:
            raise ACError("midea-local library not installed") from e

        if not account or not password:
            preset = get_preset_account_cloud()
            account = preset["username"]
            password = preset["password"]
            cloud_name = cloud_name or preset["cloud_name"]

        async with aiohttp.ClientSession() as session:
            cloud = get_midea_cloud(cloud_name or "SmartHome", session, account, password)
            if not await cloud.login():
                raise ACError("Midea cloud login failed")
            keys = await cloud.get_cloud_keys(int(device_id))
            if not keys:
                raise ACError(f"Midea cloud returned no keys for device {device_id}")
            # keys: {protocol_int: {"token": ..., "key": ...}}
            best = keys.get(3) or next(iter(keys.values()))
            return {"token": best.get("token"), "key": best.get("key"),
                    "available_protocols": list(keys.keys())}
