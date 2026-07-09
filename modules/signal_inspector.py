"""
signal_inspector.py — universal, device-agnostic signal capture.
================================================================

The onboarding pain across ALL IoT devices (Zigbee or Matter, standard ZCL
or vendor-proprietary) is the same: you cannot map what you cannot see. This
module gives you one live view of *every raw signal a device emits*, no matter
which handler produced it.

The trick is that everything a device says already converges on a small number
of choke points:

  * ``ClusterHandler.attribute_updated`` — every ZCL / manufacturer attribute
    report, with its raw address (endpoint, cluster, attribute). Inherited by
    every handler, so this is universal for Zigbee.
  * ``ClusterHandler.cluster_command``  — every cluster command received
    (button presses, Tuya DP reports, scene recalls, …).
  * ``device.update_state``             — the catch-all: Tuya datapoints
    (``dp_16``), Matter attributes, and any friendly/derived key a handler
    computes all pass through here.

Each of those calls :func:`record`. We never depend on knowing the device
*type* — a signal is just ``(source, address, value)``. That is what makes the
inspector work for a device nobody has ever written a handler for, and what
lets the future data-driven layer gradually replace the hard-coded handlers:
you can see the raw address a handler is deriving from and map it yourself.

Recording is always on (it is cheap — a dict update per report). Live
streaming to the frontend only happens for devices the user is actively
inspecting (``start(ieee)`` / ``stop(ieee)``), so idle devices cost nothing on
the wire.

This module is intentionally free of any device-class knowledge. It never
raises into the handler path — every public entry point swallows its own
errors.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from threading import RLock
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger("modules.signal_inspector")

# How many recent timestamps to keep per signal for the updates/min rate.
_RATE_WINDOW_S = 60.0
_MAX_SAMPLES = 128

# Safety cap: distinct signals we track per device. Raw addresses are bounded
# (a handful of endpoints × clusters × attributes), so this only ever trips on
# a misbehaving device spraying unique keys.
_MAX_SIGNALS_PER_DEVICE = 1024


# Canonical source identifiers. New protocols slot in here without any other
# code change — the inspector treats them all identically.
SOURCE_ZCL_ATTR = "zcl_attr"     # ZCL / manufacturer attribute report
SOURCE_ZCL_CMD = "zcl_cmd"       # ZCL cluster command received
SOURCE_STATE = "state"           # anything landing in device.update_state
SOURCE_DP = "dp"                 # explicit Tuya-style datapoint (optional)
SOURCE_MATTER_ATTR = "matter_attr"


def _jsonify(val: Any) -> Any:
    """Best-effort JSON-safe rendering of a signal value."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    if isinstance(val, (list, tuple)):
        return [_jsonify(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _jsonify(v) for k, v in val.items()}
    # zigpy wrapped types often carry a .value
    inner = getattr(val, "value", None)
    if inner is not None and inner is not val:
        return _jsonify(inner)
    try:
        return str(val)
    except Exception:
        return repr(val)


def _value_type(val: Any) -> str:
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "string"
    if isinstance(val, (bytes, bytearray)):
        return "bytes"
    if isinstance(val, (list, tuple)):
        return "list"
    if isinstance(val, dict):
        return "map"
    return type(val).__name__


class _Signal:
    """One tracked signal (a single raw address) for one device."""

    __slots__ = (
        "key", "source", "endpoint", "cluster", "item", "name",
        "value", "value_type", "first_seen", "last_seen", "count",
        "last_changed", "_samples",
    )

    def __init__(self, key: str, source: str, endpoint: Optional[int],
                 cluster: Optional[int], item: Optional[int], name: str):
        now = time.time()
        self.key = key
        self.source = source
        self.endpoint = endpoint
        self.cluster = cluster
        self.item = item
        self.name = name
        self.value: Any = None
        self.value_type = "unknown"
        self.first_seen = now
        self.last_seen = now
        self.count = 0
        self.last_changed = now
        self._samples: Deque[float] = deque(maxlen=_MAX_SAMPLES)

    def observe(self, value: Any) -> bool:
        """Record one observation. Returns True if the value changed."""
        now = time.time()
        jv = _jsonify(value)
        changed = (self.count > 0 and jv != self.value)
        if changed or self.count == 0:
            self.last_changed = now
        self.value = jv
        self.value_type = _value_type(value)
        self.last_seen = now
        self.count += 1
        self._samples.append(now)
        return changed

    def rate_per_min(self) -> float:
        """Updates observed in the trailing rate window, scaled to per-minute."""
        if not self._samples:
            return 0.0
        cutoff = time.time() - _RATE_WINDOW_S
        recent = sum(1 for t in self._samples if t >= cutoff)
        return round(recent * (60.0 / _RATE_WINDOW_S), 2)

    def _address_label(self) -> str:
        if self.source == SOURCE_ZCL_ATTR:
            return f"EP{self.endpoint} · 0x{(self.cluster or 0):04X}/0x{(self.item or 0):04X}"
        if self.source == SOURCE_ZCL_CMD:
            return f"EP{self.endpoint} · 0x{(self.cluster or 0):04X} cmd 0x{(self.item or 0):02X}"
        if self.source == SOURCE_DP:
            return f"DP {self.item}"
        if self.source == SOURCE_MATTER_ATTR:
            return f"EP{self.endpoint} · 0x{(self.cluster or 0):04X}/0x{(self.item or 0):04X}"
        return "state"

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now or time.time()
        return {
            "key": self.key,
            "source": self.source,
            "endpoint": self.endpoint,
            "cluster": f"0x{self.cluster:04X}" if self.cluster is not None else None,
            "item": self.item,
            "name": self.name,
            "address": self._address_label(),
            "value": self.value,
            "value_type": self.value_type,
            "count": self.count,
            "rate_per_min": self.rate_per_min(),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "age_s": round(now - self.last_seen, 1),
            "since_change_s": round(now - self.last_changed, 1),
        }


class SignalInspector:
    """Process-wide singleton collecting signals for every device."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._signals: Dict[str, Dict[str, _Signal]] = {}
        self._active: set[str] = set()
        self._watch_all = False
        self._emitter: Optional[Callable[[str, dict], None]] = None

    # ---- wiring -------------------------------------------------------

    def set_emitter(self, emitter: Callable[[str, dict], None]) -> None:
        """Install a sync emit function (e.g. ``service._emit_sync``)."""
        self._emitter = emitter

    # ---- capture (called from the handler / state choke points) -------

    def record(
            self,
            ieee: Any,
            source: str,
            *,
            endpoint: Optional[int] = None,
            cluster: Optional[int] = None,
            item: Optional[int] = None,
            name: Optional[str] = None,
            value: Any = None,
    ) -> None:
        """Record one signal observation. Never raises."""
        try:
            ieee = str(ieee)
            key = self._make_key(source, endpoint, cluster, item, name)
            with self._lock:
                dev = self._signals.get(ieee)
                if dev is None:
                    dev = {}
                    self._signals[ieee] = dev
                sig = dev.get(key)
                if sig is None:
                    if len(dev) >= _MAX_SIGNALS_PER_DEVICE:
                        return
                    sig = _Signal(key, source, endpoint, cluster, item,
                                  name or self._default_name(source, cluster, item))
                    dev[key] = sig
                sig.observe(value)
                is_active = self._watch_all or ieee in self._active
                payload = sig.to_dict() if is_active else None
            if is_active and payload is not None:
                self._emit(ieee, payload)
        except Exception:
            # The capture path must never break a handler.
            pass

    @staticmethod
    def _default_name(source: str, cluster: Optional[int], item: Optional[int]) -> str:
        if source == SOURCE_DP and item is not None:
            return f"dp_{item}"
        if item is not None:
            return f"0x{item:04X}"
        return source

    @staticmethod
    def _make_key(source: str, endpoint: Optional[int], cluster: Optional[int],
                  item: Optional[int], name: Optional[str]) -> str:
        if source == SOURCE_STATE:
            return f"state:{name}"
        if source in (SOURCE_ZCL_CMD,):
            return f"{source}:{endpoint}/{cluster}/{item}"
        if source == SOURCE_DP:
            return f"dp:{item}"
        return f"{source}:{endpoint}/{cluster}/{item}"

    # ---- inspection control -------------------------------------------

    def start(self, ieee: Any) -> None:
        with self._lock:
            self._active.add(str(ieee))

    def stop(self, ieee: Any) -> None:
        with self._lock:
            self._active.discard(str(ieee))

    def is_active(self, ieee: Any) -> bool:
        with self._lock:
            return self._watch_all or str(ieee) in self._active

    def start_all(self) -> None:
        """Stream signals from every device (firehose)."""
        with self._lock:
            self._watch_all = True

    def stop_all(self) -> None:
        with self._lock:
            self._watch_all = False

    def is_watching_all(self) -> bool:
        with self._lock:
            return self._watch_all

    def clear(self, ieee: Any) -> None:
        """Drop all recorded signals for a device (fresh learn baseline)."""
        with self._lock:
            self._signals.pop(str(ieee), None)

    # ---- read ---------------------------------------------------------

    def snapshot(self, ieee: Any) -> List[Dict[str, Any]]:
        """Return all signals for a device, newest activity first."""
        ieee = str(ieee)
        now = time.time()
        with self._lock:
            dev = self._signals.get(ieee, {})
            out = [s.to_dict(now) for s in dev.values()]
        out.sort(key=lambda s: s["last_seen"], reverse=True)
        return out

    def snapshot_all(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return signals across every device, newest first, each tagged with
        its ``ieee``. Capped so a busy network can't flood the client."""
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for ieee, dev in self._signals.items():
                for s in dev.values():
                    d = s.to_dict(now)
                    d["ieee"] = ieee
                    out.append(d)
        out.sort(key=lambda s: s["last_seen"], reverse=True)
        return out[:limit]

    # ---- internals ----------------------------------------------------

    def _emit(self, ieee: str, signal: Dict[str, Any]) -> None:
        if self._emitter is None:
            return
        try:
            self._emitter("signal_inspector_update", {"ieee": ieee, "signal": signal})
        except Exception as e:
            logger.debug(f"signal emit failed: {e}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_inspector: Optional[SignalInspector] = None


def get_signal_inspector() -> SignalInspector:
    global _inspector
    if _inspector is None:
        _inspector = SignalInspector()
    return _inspector
