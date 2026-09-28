"""
Rewrite button reporting that re-sends the last value on a timer.

A device keeps the reporting configuration it was given at join. Button EPs
configured with a periodic max interval re-report their last value every
period, and each re-report reads as a press (MultistateInputHandler), so an
automation on the button fires on a timer. Once per start, mains devices'
button EPs are asked for their configuration and rewritten only where
periodic reporting is still on. Sleepy devices are left alone.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("modules.reporting_heal")

HEAL_DELAY = 180.0      # after start, behind the probe-lite backfill
STEP_TIMEOUT = 6.0


async def periodic_max_interval(cluster, attr_id: int):
    """The max interval the device holds for attr_id, or None if unknown."""
    from zigpy.zcl import foundation
    rsp = await cluster.general_command(
        foundation.GeneralCommand.Read_Reporting_Configuration,
        [foundation.ReadReportingConfigRecord(direction=0, attrid=attr_id)])
    for rec in getattr(rsp, "attribute_configs", None) or []:
        if int(rec.status) == 0 and int(rec.config.attrid) == attr_id:
            return int(rec.config.max_interval)
    return None


async def heal_device(device) -> int:
    """Rewrite this device's periodic button reporting. Returns EPs rewritten."""
    from handlers.aqara import MultistateInputHandler
    from modules.probe_lite import _lock, _mains

    if getattr(device, "is_coordinator", False) or not _mains(device.zigpy_dev):
        return 0
    healed = 0
    seen = set()
    for key, h in list((getattr(device, "handlers", None) or {}).items()):
        if not isinstance(key, tuple) or not isinstance(h, MultistateInputHandler) or h in seen:
            continue
        seen.add(h)
        attr, lo, hi, change = ("present_value",) + MultistateInputHandler.REPORT_CONFIG[0][1:]
        async with _lock:
            try:
                async with asyncio.timeout(STEP_TIMEOUT):
                    held = await periodic_max_interval(h.cluster, MultistateInputHandler.ATTR_PRESENT_VALUE)
                if held in (None, hi):
                    continue
                async with asyncio.timeout(STEP_TIMEOUT):
                    await h.cluster.configure_reporting(attr, lo, hi, change)
            except Exception as e:
                logger.debug(f"[{device.ieee}] EP{key[0]} button reporting not checked: {e}")
                continue
        healed += 1
        logger.info(f"[{device.ieee}] EP{key[0]} button reported every {held}s; "
                    f"periodic reporting turned off")
    return healed


async def heal(devices, delay: float = HEAL_DELAY) -> int:
    """One pass over every device after start. Returns EPs rewritten."""
    await asyncio.sleep(delay)
    total = 0
    for device in list(devices):
        try:
            total += await heal_device(device)
        except Exception as e:
            logger.debug(f"[{getattr(device, 'ieee', '?')}] reporting heal failed: {e}")
    if total:
        logger.info(f"Button reporting rewritten on {total} endpoints")
    return total
