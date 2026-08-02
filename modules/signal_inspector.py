"""
Universal, device-agnostic signal capture.

Records every raw signal a device emits — (source, address, value) — by tapping
the three choke points everything converges on, with no device-class knowledge
and no raising into the handler path. See docs/debugging.md.
"""
from __future__ import annotations

import hashlib
import json
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


def arg_discriminator(value: Any) -> str:
    """Stable, compact key for a command's args/payload.

    Computed identically wherever it's needed (the capture tap, the handler's
    action lookup) so that "this exact button press" can be matched. Empty
    string means "no distinguishing payload" (a plain command).

    Long payloads are hashed so the key stays bounded; short ones are kept
    verbatim so they remain human-inspectable.
    """
    try:
        j = _jsonify(value)
    except Exception:
        j = None
    if j in (None, "", [], {}, ()):
        return ""
    try:
        s = json.dumps(j, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        s = str(j)
    if not s or s in ("null", "[]", "{}"):
        return ""
    if len(s) > 48:
        return "h" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]
    return s


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
        "last_changed", "_samples", "arg_disc",
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
        self.arg_disc = ""       # payload discriminator (commands only)

    def observe(self, value: Any) -> bool:
        """Record one observation. Returns True if the value changed."""
        now = time.time()
        jv = _jsonify(value)
        changed = (self.count > 0 and jv != self.value)
        if changed or self.count == 0:
            self.last_changed = now
        self.value = jv
        self.value_type = _value_type(value)
        if self.source == SOURCE_ZCL_CMD:
            # Compute from the raw value so it matches the handler's lookup.
            self.arg_disc = arg_discriminator(value)
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
        d = {
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
        if self.source == SOURCE_ZCL_CMD:
            d["arg_disc"] = self.arg_disc
            # Human-readable payload summary for the mapping UI.
            summ = "" if self.value in (None, [], {}) else str(self.value)
            d["arg_summary"] = summ[:60]
        return d


class SignalInspector:
    """Process-wide singleton collecting signals for every device."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._signals: Dict[str, Dict[str, _Signal]] = {}
        self._active: set[str] = set()
        self._watch_all = False
        self._emitter: Optional[Callable[[str, dict], None]] = None
        # Learn-by-demonstration baselines: ieee -> {key: (value, count)} + ts
        self._baselines: Dict[str, Dict[str, tuple]] = {}
        self._baseline_ts: Dict[str, float] = {}

    # wiring

    def set_emitter(self, emitter: Callable[[str, dict], None]) -> None:
        """Install a sync emit function (e.g. ``service._emit_sync``)."""
        self._emitter = emitter

    # capture (called from the handler / state choke points)

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

    # inspection control

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
        ieee = str(ieee)
        with self._lock:
            self._signals.pop(ieee, None)
            self._baselines.pop(ieee, None)
            self._baseline_ts.pop(ieee, None)

    # learn-by-demonstration

    def mark_baseline(self, ieee: Any) -> int:
        """Stamp the current (value, count) of every signal as the baseline.

        The user then physically operates the device; :meth:`diff` reports
        which signals moved. Returns the number of signals baselined.
        """
        ieee = str(ieee)
        with self._lock:
            dev = self._signals.get(ieee, {})
            self._baselines[ieee] = {
                k: (s.value, s.count) for k, s in dev.items()
            }
            self._baseline_ts[ieee] = time.time()
            return len(self._baselines[ieee])

    def has_baseline(self, ieee: Any) -> bool:
        with self._lock:
            return str(ieee) in self._baselines

    def diff(self, ieee: Any) -> List[Dict[str, Any]]:
        """Return signals that moved since the baseline, ranked by relevance.

        change kinds:
          * ``changed``  — value differs from baseline (strongest signal)
          * ``new``      — signal first seen after the baseline was taken
          * ``repeated`` — same value but reported again (e.g. a button that
            always emits the same command; count increased)
        """
        ieee = str(ieee)
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self._lock:
            base = self._baselines.get(ieee)
            if base is None:
                return []
            base_ts = self._baseline_ts.get(ieee, now)
            dev = self._signals.get(ieee, {})
            for key, s in dev.items():
                prev = base.get(key)
                if prev is None:
                    # Appeared only after the baseline.
                    if s.first_seen >= base_ts:
                        entry = s.to_dict(now)
                        entry.update(change="new", baseline_value=None, delta_count=s.count)
                        out.append(entry)
                    continue
                prev_val, prev_count = prev
                if s.count <= prev_count:
                    continue  # no new observations
                entry = s.to_dict(now)
                if s.value != prev_val:
                    entry.update(change="changed")
                else:
                    entry.update(change="repeated")
                entry.update(baseline_value=prev_val, delta_count=s.count - prev_count)
                out.append(entry)

        rank = {"changed": 0, "new": 1, "repeated": 2}
        out.sort(key=lambda e: (rank.get(e["change"], 3), -e.get("delta_count", 0)))
        return out

    # read

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

    # internals

    def _emit(self, ieee: str, signal: Dict[str, Any]) -> None:
        if self._emitter is None:
            return
        try:
            self._emitter("signal_inspector_update", {"ieee": ieee, "signal": signal})
        except Exception as e:
            logger.debug(f"signal emit failed: {e}")


_inspector: Optional[SignalInspector] = None


def get_signal_inspector() -> SignalInspector:
    global _inspector
    if _inspector is None:
        _inspector = SignalInspector()
    return _inspector
