"""
What a queue's now-playing says, per player kind.

Two display contracts, and the bug was that one was being answered with the
other's data. A per-track player re-labels itself on every load, so its screens
follow the song. A zone plays the whole queue as one endless stream and its
endpoint displays carry the label that rode in on the load that started it
(docs/open-zone.md §10.7) — which is the one place the head track is the wrong
answer, because it is read for the whole session.

Real PlayerQueue, real MediaController, real ZonePlayerProvider and the real
OpenZone now-playing payload. Stubbed is only what talks to hardware.
"""

from __future__ import annotations

import asyncio
import time

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media.controller import MediaController
from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider
from modules.media.players.zone import ZonePlayerProvider
from modules.media.queue import PlayerQueue


def _tracks(n: int = 3):
    return [MediaItem(url=f"http://x/{i}", title=f"Track {i}",
                      artist=f"Artist {i}", artwork_url=f"http://art/{i}",
                      media_type="tidal", source_id=str(i),
                      duration_ms=180000)
            for i in range(1, n + 1)]


class _PerTrack(PlayerProvider):
    """A speaker the controller drives item by item (Cast, WiiM, Sonos…)."""

    provider = "cast"

    def __init__(self):
        self.loads = []          # every item it was told to play, in order

    async def list_players(self):
        return [await self.get_state("cast:one")]

    async def get_state(self, player_id: str):
        return PlayerState(player_id=player_id, provider=self.provider,
                           name="Hub", state=PlaybackState.PLAYING)

    async def play_url(self, player_id: str, item: MediaItem) -> None:
        self.loads.append(item)

    async def pause(self, player_id): ...
    async def resume(self, player_id): ...
    async def stop_playback(self, player_id): ...
    async def set_volume(self, player_id, level): ...


class _Sync:
    """Stands in for OpenZone at the seam ZonePlayerProvider talks through."""

    def __init__(self):
        self.started = None

    def list_groups(self):
        return {"groups": [{"id": "g1", "name": "Kitchen", "members": []}]}

    async def start_zone(self, gid, media=None, duration_s=None, use_saved=False):
        self.started = media
        return {"success": True}


def _zone_media(loop, collection=None, items=None):
    """The media block a zone is started with for this queue."""
    sync = _Sync()
    prov = ZonePlayerProvider(sync, sync.start_zone)
    loop.run_until_complete(
        prov.play_queue("zone:g1", items if items is not None else _tracks(),
                        0, False, collection))
    return sync.started


def run() -> Checker:
    c = Checker("zone_now_playing")
    loop = asyncio.new_event_loop()

    # --- the per-track contract is unchanged -----------------------------
    c.section("a per-track player is re-labelled on every item")
    prov = _PerTrack()
    ctl = MediaController()
    ctl.add_player_provider(prov)
    items = _tracks()
    loop.run_until_complete(ctl.play_items("cast:one", items))
    c.check("first item loaded", [i.title for i in prov.loads] == ["Track 1"],
            prov.loads)

    # Ending each track the way Cast reports it (idle_reason FINISHED), with
    # the playing poll in between that releases the controller's advance latch.
    for _ in range(2):
        loop.run_until_complete(ctl.refresh())
        loop.run_until_complete(ctl.tick())
        st = ctl.snapshot()[0]
        st.ended, st.state = True, PlaybackState.IDLE
        ctl._cache[st.player_id] = st
        loop.run_until_complete(ctl.tick())

    c.check("every item was loaded in turn",
            [i.title for i in prov.loads] == ["Track 1", "Track 2", "Track 3"],
            [i.title for i in prov.loads])
    c.check("each load carried that item's own artwork",
            [i.artwork_url for i in prov.loads]
            == ["http://art/1", "http://art/2", "http://art/3"],
            [i.artwork_url for i in prov.loads])
    c.check("each load carried that item's own artist",
            [i.artist for i in prov.loads]
            == ["Artist 1", "Artist 2", "Artist 3"],
            [i.artist for i in prov.loads])

    # --- the zone contract -----------------------------------------------
    c.section("a zone is labelled with the set, not its first track")
    media = _zone_media(loop, {"title": "Rainy Day", "artist": "Tidal Mix",
                               "artwork_url": "http://art/mix"})
    c.check("title is the set's", media.get("title") == "Rainy Day", media)
    c.check("artist is the set's", media.get("artist") == "Tidal Mix", media)
    c.check("artwork is the set's",
            media.get("artwork_url") == "http://art/mix", media)
    c.check("the queue still travels in full",
            [r["title"] for r in media["items"]]
            == ["Track 1", "Track 2", "Track 3"], media.get("items"))
    c.check("the first item's stream is still what opens",
            media.get("source_id") == "1", media)

    c.section("an unnamed set says how many tracks it holds")
    media = _zone_media(loop, None)
    c.check("artist counts the queue", media.get("artist") == "3 tracks", media)
    c.check("no track's artist is passed off as the session's",
            media.get("artist") != "Artist 1", media)

    c.section("a single track is still itself")
    media = _zone_media(loop, None, _tracks(1))
    c.check("title is the track's", media.get("title") == "Track 1", media)
    c.check("artist is the track's", media.get("artist") == "Artist 1", media)

    c.section("a named set survives shuffle")
    ctl = MediaController()
    zsync = _Sync()
    ctl.add_player_provider(ZonePlayerProvider(zsync, zsync.start_zone))
    ctl.set_shuffle("zone:g1", True)
    loop.run_until_complete(ctl.play_items(
        "zone:g1", _tracks(), collection={"title": "Rainy Day",
                                          "artist": "Tidal Mix",
                                          "artwork_url": "http://art/mix"}))
    c.check("shuffling re-orders without losing the label",
            zsync.started.get("title") == "Rainy Day", zsync.started)

    c.section("the collection survives a restart")
    q = PlayerQueue("zone:g1")
    q.load(_tracks(), 0, {"title": "Rainy Day", "artist": "Tidal Mix",
                          "artwork_url": "http://art/mix"})
    back = PlayerQueue.from_dict("zone:g1", q.to_dict())
    c.check("restored from its own snapshot",
            back.collection == q.collection, back.collection)
    c.check("cleared with the queue",
            (q.clear(), q.collection)[1] is None, q.collection)

    # --- what the custom receiver is told ---------------------------------
    c.section("the receiver is told each item, timed to the seam")
    from modules.media.cast_sync import OpenZone, LEAD_SECONDS

    class _Source:
        kind = "media"
        delay_s = 4.0
        origin = 0.0

        def item_position_s(self):
            return 1.0

        def item_origin_s(self):
            return self.origin

    z = OpenZone(None, {})
    z.running = True
    z._epoch = time.monotonic() - 50.0
    z._source = _Source()
    z._queue = [{"title": t.title, "artist": t.artist,
                 "artwork_url": t.artwork_url, "source_id": t.source_id,
                 "duration_ms": t.duration_ms} for t in _tracks()]
    z._queue_pos = 0

    first = z._now_payload()
    c.check("the first item is what it names", first["title"] == "Track 1", first)
    c.check("stamped with when the item is heard, not decoded",
            abs(first["at"] - (z._epoch + LEAD_SECONDS)) < 1e-6, first)
    c.check("its place in the set travels with it",
            (first["index"], first["count"]) == (0, 3), first)

    z._queue_pos = 1
    z._source.origin = 180.0
    second = z._now_payload()
    c.check("it follows the queue", second["title"] == "Track 2", second)
    c.check("and its artwork with it",
            second["artwork"] == "http://art/2", second)
    c.check("the second seam is a track later",
            abs(second["at"] - (first["at"] + 180.0)) < 1e-6,
            (first["at"], second["at"]))

    c.section("it is pushed once per item, not once per chunk")
    sent = []

    class _WS:
        async def send_json(self, payload):
            sent.append(payload)

    class _R:
        sid = "s1"
        ws = _WS()

    z._receivers = {"s1": _R()}
    z._now_key = None
    for _ in range(4):
        loop.run_until_complete(z._push_now_if_changed())
    c.check("a settled item is sent once", len(sent) == 1, sent)
    z._queue_pos = 2
    z._source.origin = 360.0
    for _ in range(4):
        loop.run_until_complete(z._push_now_if_changed())
    c.check("a seam is sent once more", len(sent) == 2, sent)
    c.check("and it is the new item",
            sent[-1]["title"] == "Track 3", sent[-1])

    c.section("a receiver with no item to show says so safely")
    z._queue, z._queue_pos = [], 0
    z._source.origin = None
    blank = z._now_payload()
    c.check("no title rather than a stale one", blank["title"] == "", blank)
    c.check("and no seam to wait for", blank["at"] <= time.monotonic(), blank)

    loop.close()
    return c


if __name__ == "__main__":
    run()
