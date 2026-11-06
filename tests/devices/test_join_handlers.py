"""
A joining device gets handlers once its clusters are known
(core/lifecycle.py): an endpoint listed before its Simple Descriptor lands
builds nothing, and zigpy's device_initialized, arriving with the clusters,
is not skipped as a duplicate of a run that built nothing.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS

from harness import Checker

from core.lifecycle import DeviceLifecycleMixin

IEEE = "54:ef:44:10:01:65:4d:91"


class _Dev:
    def __init__(self, zdev):
        self.zigpy_dev, self.handlers, self.last_seen = zdev, {}, 0
        self.built = 0

    def _identify_handlers(self):
        self.built += 1
        self.handlers = {(ep_id, cid): object() for ep_id, ep in self.zigpy_dev.endpoints.items()
                         if ep_id for cid in (ep.in_clusters or {})}


class _Svc(DeviceLifecycleMixin):
    def __init__(self, dev):
        self.devices = {IEEE: dev}
        self.pipelines = 0

    async def announce_device(self, ieee):
        pass

    async def _async_device_initialized(self, ieee):
        self.pipelines += 1

    def _emit_sync(self, *a):
        pass

    def _rebuild_name_maps(self):
        pass


def run() -> Checker:
    c = Checker("join_handlers")
    sleep = asyncio.sleep

    async def quick(_s):
        await sleep(0)

    asyncio.sleep = quick
    try:
        c.section("endpoint listed, clusters not yet known")
        ep1 = NS(in_clusters={}, out_clusters={})
        zdev = NS(endpoints={0: NS(), 1: ep1}, is_initialized=False, status=1, model=None)
        dev = _Dev(zdev)
        svc = _Svc(dev)

        async def join():
            task = asyncio.ensure_future(svc._delayed_handler_init(IEEE))
            for _ in range(3):
                await sleep(0)
            c.check("nothing is built from an endpoint with no clusters",
                    dev.built == 0 and svc.pipelines == 0, (dev.built, svc.pipelines))
            ep1.in_clusters = {0xFCC0: object(), 0x0400: object()}     # the Simple Descriptor lands
            await task
        asyncio.run(join())
        c.check("once they are known, handlers are built and setup runs once",
                len(dev.handlers) == 2 and svc.pipelines == 1, (dev.handlers, svc.pipelines))

        c.section("zigpy's initialised call just after a run that built nothing")
        dev2 = _Dev(NS(endpoints={0: NS(), 1: NS(in_clusters={0xFCC0: object()}, out_clusters={})}))
        svc2 = _Svc(dev2)
        svc2._full_init_ts = {IEEE: time.time()}
        svc2.interview_status = NS(emit_for=lambda _i: None)

        asyncio.run(_again(svc2))
        c.check("is not skipped as a duplicate: it builds the handlers and runs setup",
                dev2.built == 1 and len(dev2.handlers) == 1 and svc2.pipelines == 1,
                (dev2.built, dev2.handlers, svc2.pipelines))

        c.section("zigpy's initialised call just after a run that did build")
        svc3 = _Svc(dev2)
        svc3._full_init_ts = {IEEE: time.time()}
        svc3.interview_status = NS(emit_for=lambda _i: None)
        asyncio.run(_again(svc3, same=True))
        c.check("is still skipped, so the device list does not flash",
                dev2.built == 1 and svc3.pipelines == 0, (dev2.built, svc3.pipelines))

        c.section("zigpy's initialised call bringing the quirked device, just after a run")
        raw = NS(ieee=IEEE, model="lumi.magnet.agl02",
                 endpoints={0: NS(), 1: NS(in_clusters={0x0000: object(), 0x0500: object()},
                                           out_clusters={})})
        dev4 = _Dev(raw)
        dev4._identify_handlers()
        svc4 = _Svc(dev4)
        svc4._full_init_ts = {IEEE: time.time()}
        svc4.interview_status = NS(emit_for=lambda _i: None)
        quirked = NS(ieee=IEEE, model="lumi.magnet.agl02",
                     endpoints={0: NS(), 1: NS(in_clusters={0x0000: object(), 0xFCC0: object()},
                                               out_clusters={0x0006: object()})})
        svc4.device_initialized(quirked)
        c.check("rebuilds the handlers from the quirked device",
                dev4.zigpy_dev is quirked and dev4.built == 2
                and set(dev4.handlers) == {(1, 0x0000), (1, 0xFCC0)}, (dev4.built, set(dev4.handlers)))
        c.check("without running setup a second time", svc4.pipelines == 0, svc4.pipelines)
    finally:
        asyncio.sleep = sleep
    return c


async def _again(svc, same=False):
    """zigpy's call, carrying the device with its clusters: the same object
    unless a quirk replaced it."""
    zdev = svc.devices[IEEE].zigpy_dev
    if same:
        zdev.ieee, zdev.model = IEEE, "lumi.sensor_occupy.agl8"
    else:
        zdev = NS(ieee=IEEE, model="lumi.sensor_occupy.agl8", endpoints=zdev.endpoints)
    svc.device_initialized(zdev)
    await asyncio.sleep(0)


if __name__ == "__main__":
    run()
