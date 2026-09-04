"""
Swarm Intelligence — virtual devices.

House-scope inputs that are not devices: the weather, the thermal state of the
house, the electricity tariff. Expressed as ordinary device-likes so a pattern
can fill a slot from one without knowing it is different, and so the automation
engine can trigger on one without a new condition type.

Two things live here that nothing else could provide:

**Computed booleans.** The engine compares an attribute to a literal, not to
another attribute. "Is it cooler outside than in?" and "is it time to start
heating for the arrival?" are comparisons between two live values, so they are
computed by the module that owns the domain and published as a plain flag. That
keeps the rule engine simple and puts the arithmetic where the knowledge is.

**Nothing on the event loop.** Every value here comes from a service's already
cached state — `weather.get_current()`, the advisor's in-memory model — and the
arithmetic is a handful of floating-point operations. No query, no file, no
DuckDB. The refresh runs on a timer and publishes a dict; the engine only ever
reads that dict.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.swarm.virtual")

# How often the computed state is recalculated. Weather moves in tens of
# minutes and the thermal model in minutes; a minute is comfortably inside both
# and cheap, since every input is already in memory.
REFRESH_SECONDS = 60

WEATHER_IEEE = "virtual::weather"
HOUSE_IEEE = "virtual::house"
TARIFF_IEEE = "virtual::tariff"

# Walking-pace fallback for turning a straight-line distance into a time.
# Deliberately conservative: an arrival predicted late is a cold house, while
# one predicted early only wastes a little gas.
DEFAULT_SPEED_MS = 11.0          # ~40 km/h, mixed urban driving
MAX_ETA_MINUTES = 240


class _Capabilities:
    """Duck-typed capabilities object, as the other providers present."""

    def __init__(self, caps: List[str]) -> None:
        self._caps = list(caps)

    def has_capability(self, cap: str) -> bool:
        return cap in self._caps

    def get_capabilities(self) -> List[str]:
        return list(self._caps)


class VirtualDevice:
    """A house-scope input, shaped like every other device the engine holds."""

    def __init__(self, ieee: str, name: str, model: str,
                 capabilities: List[str]) -> None:
        self.ieee = ieee
        self.friendly_name = name
        self.manufacturer = "ZMM"
        self.model = model
        self.state: Dict[str, Any] = {"available": True}
        self.last_seen: float = 0.0
        self.capabilities = _Capabilities(capabilities)

    def is_available(self) -> bool:
        return True

    def get_control_commands(self) -> List[Dict[str, Any]]:
        return []          # nothing here can be commanded

    def apply(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Merge new values, returning only what changed.

        A key whose value is None is dropped rather than published: an absent
        reading must not read as zero, which would fire a "below" rule the
        moment a service restarts.
        """
        changed: Dict[str, Any] = {}
        for key, value in values.items():
            if value is None:
                self.state.pop(key, None)
                continue
            if self.state.get(key) != value:
                changed[key] = value
        self.state.update({k: v for k, v in values.items() if v is not None})
        if changed:
            self.last_seen = time.time()
        return changed

    def to_device_list_entry(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee, "friendly_name": self.friendly_name,
            "manufacturer": self.manufacturer, "model": self.model,
            "type": "virtual", "protocol": "virtual", "available": True,
            "state": dict(self.state), "last_seen": self.last_seen,
            "capabilities": self.capabilities.get_capabilities(),
        }


def _f(value: Any) -> Optional[float]:
    """A float, or None. Services return strings and sentinels in places."""
    try:
        if value is None:
            return None
        out = float(value)
        return None if out != out else out          # drop NaN
    except (TypeError, ValueError):
        return None


class VirtualDeviceProvider:
    """Owns the virtual devices and keeps their state current.

    Every service is optional and injected as a getter, so a house with no
    tariff integration simply has no tariff device rather than a broken one.
    """

    def __init__(self,
                 weather_getter: Optional[Callable[[], Any]] = None,
                 advisor_getter: Optional[Callable[[], Any]] = None,
                 tariff_getter: Optional[Callable[[], Any]] = None,
                 presence_getter: Optional[Callable[[], Any]] = None,
                 evaluator: Optional[Callable] = None) -> None:
        self._weather = weather_getter
        self._advisor = advisor_getter
        self._tariff = tariff_getter
        self._presence = presence_getter
        self._evaluator = evaluator

        self.devices: Dict[str, VirtualDevice] = {
            WEATHER_IEEE: VirtualDevice(
                WEATHER_IEEE, "Weather", "Outdoor Conditions", ["weather"]),
            HOUSE_IEEE: VirtualDevice(
                HOUSE_IEEE, "House", "Thermal State", ["house"]),
            TARIFF_IEEE: VirtualDevice(
                TARIFF_IEEE, "Electricity Tariff", "Energy Pricing", ["tariff"]),
        }
        self._task: Optional[asyncio.Task] = None

    # Lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("Swarm virtual devices started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def automation_devices(self) -> Dict[str, VirtualDevice]:
        """The registry the automation engine merges in."""
        return dict(self.devices)

    async def _loop(self) -> None:
        # Services come up around the same time this does; a first pass
        # immediately would mostly publish None.
        await asyncio.sleep(5)
        while True:
            try:
                await self.refresh()
                await asyncio.sleep(REFRESH_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:                              # noqa: BLE001
                logger.error(f"Virtual device refresh failed: {e}")
                await asyncio.sleep(REFRESH_SECONDS)

    # Refresh

    async def refresh(self) -> Dict[str, Dict[str, Any]]:
        """Recompute every virtual device, and tell the engine what changed."""
        weather = self._read_weather()
        house = self._read_house(weather.get("temperature"))
        tariff = self._read_tariff()

        changes = {}
        for ieee, values in ((WEATHER_IEEE, weather), (HOUSE_IEEE, house),
                             (TARIFF_IEEE, tariff)):
            changed = self.devices[ieee].apply(values)
            if changed:
                changes[ieee] = changed

        if self._evaluator:
            for ieee, changed in changes.items():
                try:
                    await self._evaluator(ieee, changed)
                except Exception as e:                          # noqa: BLE001
                    logger.warning(f"Evaluating {ieee} failed: {e}")
        return changes

    def _read_weather(self) -> Dict[str, Any]:
        svc = self._weather() if self._weather else None
        if not svc:
            return {}
        try:
            current = svc.get_current() or {}
        except Exception:
            return {}
        return {
            "temperature": _f(current.get("temperature_2m")),
            "humidity": _f(current.get("relative_humidity_2m")),
            "wind_speed": _f(current.get("wind_speed_10m")),
            "solar_wm2": _f(current.get("shortwave_radiation")),
            "is_daylight": _int_flag(_f(current.get("shortwave_radiation")), 1.0),
        }

    def _read_house(self, outdoor: Optional[float]) -> Dict[str, Any]:
        svc = self._advisor() if self._advisor else None
        if not svc:
            return {}
        indoor = preheat = None
        try:
            indoor = _f(svc._get_avg_indoor_temp())
        except Exception:
            pass
        try:
            rec = svc.get_preheat_recommendation() or {}
            preheat = _f(rec.get("preheat_minutes"))
            if outdoor is None:
                outdoor = _f(rec.get("current_outdoor"))
        except Exception:
            pass

        # The two comparisons the rule engine cannot make for itself: it tests an
        # attribute against a literal, never against another live value.
        cooler_outside = None
        if indoor is not None and outdoor is not None:
            cooler_outside = 1 if outdoor < indoor - 1.0 else 0

        return {
            "indoor_avg_temp": round(indoor, 1) if indoor is not None else None,
            "outdoor_temp": round(outdoor, 1) if outdoor is not None else None,
            "preheat_minutes": int(preheat) if preheat is not None else None,
            "outdoor_cooler_than_indoor": cooler_outside,
            "preheat_now_for_arrival": self._preheat_now(preheat),
        }

    def _preheat_now(self, preheat_minutes: Optional[float]) -> Optional[int]:
        """Is somebody due home within the time it takes to warm the house?

        The whole pre-arrival example in one flag. Distance comes from the
        presence users already in memory; no journey history is read, because
        that would mean a database query on the event loop.
        """
        if preheat_minutes is None or not self._presence:
            return None
        try:
            mgr = self._presence()
            devices = getattr(mgr, "devices", {}) if mgr else {}
        except Exception:
            return None
        if not devices:
            return None

        soonest = None
        for dev in devices.values():
            state = getattr(dev, "state", None) or {}
            if state.get("presence") == "home":
                continue                      # already in; nothing to predict
            eta = _eta_minutes(_f(state.get("distance_m")))
            if eta is not None and (soonest is None or eta < soonest):
                soonest = eta
        if soonest is None:
            return 0
        return 1 if soonest <= preheat_minutes else 0

    def _read_tariff(self) -> Dict[str, Any]:
        """The live electricity rate, and whether now is the cheap window.

        `current_unit_rate()` reads the cached half-hourly rates directly;
        `_cheapest_window()` finds the cheapest contiguous block in the next
        24 hours, which is what "off peak" means on an agile tariff — there is
        no fixed night rate to compare against.
        """
        svc = self._tariff() if self._tariff else None
        if not svc:
            return {}
        try:
            rate = _f(svc.current_unit_rate("electricity"))
        except Exception:
            rate = None
        return {
            "unit_rate": round(rate, 2) if rate is not None else None,
            "is_off_peak": self._off_peak_now(svc),
        }

    @staticmethod
    def _off_peak_now(svc: Any) -> Optional[int]:
        """Whether the clock is inside today's cheapest window.

        The window is expressed as local HH:MM and may wrap past midnight, so
        the comparison handles a window whose end is numerically before its
        start rather than silently reading as never.
        """
        try:
            window = svc._cheapest_window("electricity")
        except Exception:
            return None
        if not window:
            return None
        start, end = window.get("off_peak_start"), window.get("off_peak_end")
        if not start or not end:
            return None
        now = time.strftime("%H:%M")
        inside = (start <= now < end) if start <= end else (now >= start or now < end)
        return 1 if inside else 0


def _eta_minutes(distance_m: Optional[float]) -> Optional[float]:
    """Straight-line distance to a rough travel time.

    Deliberately crude. It answers "roughly how long until they are back",
    which is the resolution the heating decision needs — a smarter estimate
    would mean reading journey history, and that is a database query this must
    not make.
    """
    if distance_m is None or distance_m < 0:
        return None
    minutes = (distance_m / DEFAULT_SPEED_MS) / 60.0
    return min(minutes, MAX_ETA_MINUTES)


def _int_flag(value: Optional[float], threshold: float) -> Optional[int]:
    if value is None:
        return None
    return 1 if value > threshold else 0


_provider: Optional[VirtualDeviceProvider] = None


def get_virtual_provider() -> Optional[VirtualDeviceProvider]:
    return _provider


def set_virtual_provider(provider: VirtualDeviceProvider) -> None:
    global _provider
    _provider = provider
