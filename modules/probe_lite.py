"""
Probe-lite: attribute discovery at join, recorded as `answered` facts
(docs/plans/zmm-quirks.md §9 step 4).

Discovery only: each input cluster's attribute list with access flags, plus
manufacturer-scoped attributes on vendor clusters (0xFC00+). No reads, no
writes, a few frames per cluster. Mains devices only, one device at a time
network-wide, so a mass rejoin queues instead of flooding the radio. The
full probe (modules/device_probe.py) remains the deep, on-demand tool.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

logger = logging.getLogger("modules.probe_lite")

_lock = asyncio.Lock()

BACKFILL_DELAY = 120.0     # after start, so the mesh has settled
BACKFILL_SPACING = 5.0     # between devices


def acl_letters(acl) -> str:
    """Extended-discovery access bitmap as the full probe writes it ('RWP')."""
    if acl is None:
        return ""
    return "".join(ch for bit, ch in ((0x01, "R"), (0x02, "W"), (0x04, "P")) if acl & bit)


def _mains(zigpy_dev) -> bool:
    nd = getattr(zigpy_dev, "node_desc", None)
    return bool(nd is not None and getattr(nd, "is_mains_powered", False))


def has_answered_facts(ieee: str) -> bool:
    from modules.zigbee_cache import get_facts
    return any(f["source"] == "answered" for f in get_facts(ieee))


async def probe_lite(device, force: bool = False) -> int:
    """Discover and record one device's attributes. Returns facts written."""
    from handlers.base import discover_attribute_info
    from modules.device_facts import Fact, _j, attr_subject, record
    from modules.zcl_decode import type_name

    ieee = str(device.ieee)
    zdev = device.zigpy_dev
    if getattr(device, "is_coordinator", False) or not _mains(zdev):
        return 0
    async with _lock:
        if not force and has_answered_facts(ieee):
            return 0
        mfr = getattr(getattr(zdev, "node_desc", None), "manufacturer_code", None)
        facts: List[Fact] = []
        for ep_id, ep in (zdev.endpoints or {}).items():
            if ep_id == 0 or ep is None:
                continue
            for cid, cluster in (getattr(ep, "in_clusters", None) or {}).items():
                scopes = [None] + ([mfr] if mfr and int(cid) >= 0xFC00 else [])
                for code in scopes:
                    info = await discover_attribute_info(cluster, manufacturer=code) or {}
                    for aid, meta in info.items():
                        facts.append(Fact(ep_id, attr_subject("attr", int(cid), aid, code),
                                          "answered",
                                          _j({"type": type_name(meta["type"]),
                                              "acl": acl_letters(meta["acl"])})))
        n = record(ieee, facts) if facts else 0
        logger.info(f"[{ieee}] Probe-lite recorded {n} attributes")
        return n


async def backfill(devices, delay: float = BACKFILL_DELAY,
                   spacing: float = BACKFILL_SPACING) -> int:
    """One pass over devices that have never been probed. Returns devices done."""
    await asyncio.sleep(delay)
    done = 0
    for device in list(devices):
        try:
            if await probe_lite(device):
                done += 1
                await asyncio.sleep(spacing)
        except Exception as e:
            logger.debug(f"[{getattr(device, 'ieee', '?')}] probe-lite failed: {e}")
    if done:
        logger.info(f"Probe-lite backfill covered {done} devices")
    return done
