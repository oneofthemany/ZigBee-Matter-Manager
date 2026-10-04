"""
OpenZone: a screen follows the track, without a re-LOAD (open-zone.md §10.7).

The default receiver takes one artwork URL at load and never asks again, so
the URL it is given is an image that never ends, and each cover is a new part
of it. Real OpenZone; stubbed is the provider that names a device's model and
the HTTP fetch of a cover.
"""

from __future__ import annotations

import asyncio
import tempfile
import time

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media import cast_sync as cs
from modules.media.cast_sync import OpenZone, _Stream

MODELS = {"cast:hub": "Google Nest Hub", "cast:tab": "Pixel Tablet",
          "cast:wiim": "WiiM Ultra", "cast:audio": "Google Nest Audio"}


class _Prov:
    def model_key(self, pid):
        return MODELS[pid]


class _Source:
    delay_s = 0.05

    def item_position_s(self):
        return 0.0


class _Http:
    def __init__(self):
        self.fetched = []

    async def get(self, url):
        self.fetched.append(url)

        class R:
            headers = {"content-type": "image/jpeg"}
            content = url.encode()

            def raise_for_status(self): ...
        return R()


def _zone(tmp: str) -> OpenZone:
    z = OpenZone(None, {k: f"{tmp}/{k}.json" for k in
                        ("trims_file", "model_trims_file", "groups_file",
                         "model_file", "policy_file", "last_file")})
    z.set_provider_resolver(lambda pid: _Prov())
    z.running = True
    z._source = _Source()
    z._epoch = time.monotonic()
    z._queue = [{"title": f"T{i}", "artwork_url": f"http://art/{i}"}
                for i in range(3)]
    return z


def run() -> Checker:
    c = Checker("zone_live_art")

    async def go(tmp):
        z = _zone(tmp)
        base = "http://hub:8010"

        c.section("which devices are given a live image")
        c.check("none while the zone has no artwork to follow",
                z._live_art_url("cast:tab", "s1", base) == "")
        z._art_task = asyncio.create_task(asyncio.sleep(60))
        c.check("a Pixel Tablet is",
                z._live_art_url("cast:tab", "s1", base)
                == "http://hub:8010/sync/art/s1.mjpg")
        c.check("a Nest Hub is", bool(z._live_art_url("cast:hub", "s2", base)))
        c.check("a WiiM keeps the fixed cover — its display does not redraw",
                z._live_art_url("cast:wiim", "s3", base) == "")
        c.check("a speaker with no screen is not handed a stream to hold open",
                z._live_art_url("cast:audio", "s4", base) == "")
        z._art_task.cancel()

        c.section("the image a device holds open")
        st = _Stream("s1", "cast:tab", "Pixel Tablet")
        z._streams["s1"] = st
        z._art_now = ((0, "a"), "image/jpeg", b"COVER-0")
        gen = z._art_stream(st)
        first, again = await gen.__anext__(), await gen.__anext__()
        c.check("opens on the current cover",
                first.startswith(b"--zmmart\r\nContent-Type: image/jpeg")
                and b"COVER-0" in first, first[:60])
        c.check("sent twice, so the receiver draws it without waiting for "
                "the next track", again == first)
        nxt = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        c.check("nothing more is sent while the track is unchanged",
                not nxt.done())
        z._art_now = ((1, "b"), "image/jpeg", b"COVER-1")
        part = await asyncio.wait_for(nxt, cs.LIVE_ART_POLL_S + 1)
        c.check("a new track is a new part of the same response",
                b"COVER-1" in part, part[:60])
        await gen.aclose()

        c.section("the cover follows what is heard, not what is decoded")
        http = _Http()
        z._art_now = None
        got = await z._fetch_art(http, "http://art/0")
        c.check("a cover is fetched as the image it is",
                got == ("image/jpeg", b"http://art/0"), got)
        z._target_lag = 0.6
        orig = cs.httpx.AsyncClient

        class _Client:
            def __init__(self, **k): ...
            async def __aenter__(self): return http
            async def __aexit__(self, *a): ...
        cs.httpx.AsyncClient = _Client
        try:
            task = asyncio.create_task(z._art_watch())
            await asyncio.sleep(0.1)
            c.check("the first cover is shown at once",
                    z._art_now and z._art_now[2] == b"http://art/0", z._art_now)
            z._queue_pos = 1                    # the decoder has moved on
            await asyncio.sleep(cs.LIVE_ART_POLL_S + 0.2)
            c.check("the screen holds the old cover while the old track is "
                    "still in the speakers", z._art_now[2] == b"http://art/0")
            await asyncio.sleep(0.8)
            c.check("and changes once the zone's lag has passed",
                    z._art_now[2] == b"http://art/1", z._art_now)
            task.cancel()
        finally:
            cs.httpx.AsyncClient = orig

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(go(tmp))
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
