"""The device modal's reporting max interval (device-profiles.md §4,
"Reporting interval"): sent to every handler that measures, stored in
device_settings only once the device takes it, and held for a sleeping device
until it wakes. `DEFAULT` clears it and puts the normal reporting back."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List

logger = logging.getLogger("modules.reporting_interval")

NAME = "reporting_max"
DEFAULT = "default"
SETTINGS_PATH = "./data/device_settings.json"
_SEND_TIMEOUT_S = 10.0


def _handlers(device) -> List[Any]:
    return [h for h in dict.fromkeys((getattr(device, "handlers", None) or {}).values())
            if callable(getattr(h, "set_reporting_max", None))]


def stored(device) -> Any:
    service = getattr(device, "service", None)
    saved = (getattr(service, "device_settings", None) or {}).get(str(device.ieee)) or {}
    return (saved.get("updates") or {}).get(NAME)


def _store(device, value: Any) -> None:
    """Record what the device took; DEFAULT forgets it."""
    service = getattr(device, "service", None)
    settings = getattr(service, "device_settings", None)
    if settings is None:
        return
    updates = settings.setdefault(str(device.ieee), {}).setdefault("updates", {})
    if value == DEFAULT:
        updates.pop(NAME, None)
    else:
        updates[NAME] = int(value)
    save = getattr(service, "_save_json", None)
    if callable(save):
        save(SETTINGS_PATH, settings)


async def _send(device, value: int) -> str:
    """sent (any handler accepted), refused, undelivered or unsupported."""
    from modules.zmm_settings import _undelivered
    handlers = _handlers(device)
    if not handlers:
        return "unsupported"
    accepted = undelivered = False
    for h in handlers:
        try:
            async with asyncio.timeout(_SEND_TIMEOUT_S):
                accepted |= bool(await h.set_reporting_max(value))
        except Exception as e:
            undelivered |= _undelivered(e)
            logger.warning(f"[{device.ieee}] reporting max on 0x{getattr(h, 'CLUSTER_ID', 0):04X} "
                           f"not set: {type(e).__name__}: {e}")
    return "sent" if accepted else ("undelivered" if undelivered else "refused")


async def _restore(device) -> str:
    """The device's normal reporting setup: the entry's rows when it owns
    reporting, else each measuring handler's own configure."""
    from modules.zmm_settings import _undelivered
    try:
        from modules.device_profiles_apply import entry_owns_reporting, apply_reporting, \
            resolve_profile_for_device
        if entry_owns_reporting(device):
            res = await apply_reporting(device, resolve_profile_for_device(device) or {})
            return "sent" if res.get("success") else "undelivered"
        for h in _handlers(device):
            async with asyncio.timeout(_SEND_TIMEOUT_S):
                await h.configure()
        return "sent"
    except Exception as e:
        logger.warning(f"[{device.ieee}] restoring default reporting failed: {type(e).__name__}: {e}")
        return "undelivered" if _undelivered(e) else "refused"


async def apply(device, value: Any) -> bool:
    """Set (or with DEFAULT, clear) the reporting max. True when the device
    took it; a sleeping device's change waits in the zmm_settings queue."""
    from modules import zmm_settings
    clear = value in (DEFAULT, "", None)
    if clear:
        value = DEFAULT
        outcome = await _restore(device)
    else:
        value = int(float(value))
        outcome = await _send(device, value)
    zmm_settings._settle(device, NAME, outcome, value)
    if outcome == "sent":
        _store(device, value)
        logger.info(f"[{device.ieee}] reporting max = {value}")
    return outcome == "sent"
