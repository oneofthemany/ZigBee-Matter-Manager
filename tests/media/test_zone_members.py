"""
What a speaker says while a zone is feeding it.

A member's own display carries the session label (docs/open-zone.md §10.7) and
its own queue is whatever it last played alone. Neither describes what it is
playing now — the zone does — and a restart that resumed the member's queue
would start a stale item under the zone's own resume.

Real MediaController and ZonePlayerProvider; stubbed is only what talks to
hardware.
"""

from __future__ import annotations

import asyncio

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media.controller import MediaController
from modules.media.models import MediaItem, PlayerState, PlaybackState
from modules.media.players.base import PlayerProvider
from modules.media.players.zone import ZonePlayerProvider

MEMBERS = ["cast:a", "cast:b", "cast:arc"]


class _Casts(PlayerProvider):
    """Three speakers showing the label their zone load carried; a fourth
    playing on its own."""

    provider = "cast"

    async def list_players(self):
        return [PlayerState(player_id=pid, provider="cast", name=pid,
                            state=PlaybackState.PLAYING, title="Rainy Day",
                            artist="40 tracks", artwork_url="http://art/mix")
                for pid in MEMBERS + ["cast:solo"]]

    async def get_state(self, player_id): ...
    async def play_url(self, player_id, item): ...
    async def pause(self, player_id): ...
    async def resume(self, player_id): ...
    async def stop_playback(self, player_id): ...
    async def set_volume(self, player_id, level): ...


class _Sync:
    """OpenZone at the seam the zone provider talks through. ``cast:arc`` is a
    member the session has yielded."""

    cast = None

    def __init__(self):
        self.active_group = "g1"
        self.item = {"title": "Track 7", "artist": "Artist 7",
                     "artwork_url": "http://art/7", "media_type": "tidal",
                     "source_id": "7", "duration_ms": 180000,
                     "position_ms": 175000}

    def list_groups(self):
        return {"groups": [{"id": "g1", "name": "Kitchen",
                            "active": bool(self.active_group),
                            "members": [{"player_id": m} for m in MEMBERS]}]}

    def now_playing(self):
        return dict(self.item)

    def fed_players(self):
        return ["cast:a", "cast:b"]

    last = None

    def last_media(self, gid):
        return self.last

    async def stop_session(self):
        self.active_group = ""

    async def start_zone(self, gid, media=None, **k):
        self.started = media
        return {"success": True}


def run() -> Checker:
    c = Checker("zone_members")
    loop = asyncio.new_event_loop()
    sync = _Sync()
    ctl = MediaController()
    ctl.add_player_provider(_Casts())
    ctl.add_player_provider(ZonePlayerProvider(sync, sync.start_zone))
    stale = MediaItem(url="http://x/old", title="Old Song", artist="Old",
                      artwork_url="http://art/old", media_type="tidal",
                      source_id="old", duration_ms=1000)
    for pid in MEMBERS + ["cast:solo"]:
        loop.run_until_complete(ctl.play_items(pid, [stale]))
    by = {s.player_id: s for s in loop.run_until_complete(ctl.refresh())}

    c.section("a fed member shows the zone's current item")
    a = by["cast:a"]
    c.check("title is the item's, not the session label",
            a.title == "Track 7", a.title)
    c.check("artwork is the item's", a.artwork_url == "http://art/7",
            a.artwork_url)
    c.check("the source id follows, so lyrics open on the right track",
            a.now_playing_id == "7" and a.media_type == "tidal",
            (a.now_playing_id, a.media_type))
    c.check("no finite length is painted on — that would arm end-detection "
            "against the member's own queue when the zone stops",
            a.duration_ms == 0 and a.position_ms == 0,
            (a.position_ms, a.duration_ms))

    c.section("a speaker the zone is not feeding is left alone")
    c.check("a yielded member keeps what it reports",
            by["cast:arc"].title == "Rainy Day", by["cast:arc"].title)
    c.check("a speaker outside the zone keeps what it reports",
            by["cast:solo"].artwork_url == "http://art/mix",
            by["cast:solo"].artwork_url)

    c.section("a restart resumes the zone, not its members' old queues")
    rec = ctl.sessions_snapshot()["playback"]
    c.check("no member is recorded as playing its own queue",
            not (set(rec) & set(MEMBERS)), sorted(rec))
    c.check("a speaker playing alone still is", "cast:solo" in rec, sorted(rec))

    sync.active_group = ""
    loop.run_until_complete(ctl.refresh())
    rec = ctl.sessions_snapshot()["playback"]
    c.check("with the zone stopped its members are their own again",
            set(MEMBERS) <= set(rec), sorted(rec))
    c.section("playing a stopped zone carries on with what it last played")
    prov = ZonePlayerProvider(sync, sync.start_zone)
    sync.active_group, sync.last = "g1", {"station_uuid": "radio-x"}
    loop.run_until_complete(prov.stop_playback("zone:g1"))
    loop.run_until_complete(prov.resume("zone:g1"))
    c.check("stop then play is the same station, not the zone's saved source",
            sync.started == {"station_uuid": "radio-x"}, sync.started)
    sync.last = None
    loop.run_until_complete(prov.resume("zone:g1"))
    c.check("a zone that has never played falls back to its saved source",
            sync.started is None, sync.started)

    c.section("the engine keeps what a zone was on when it stopped")
    import tempfile
    from test_zone_yield import _fake_cast, _stream
    from test_zone_lock import _FakeCastProvider
    from modules.media.cast_sync import OpenZone
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {k: f"{tmp}/{k}.json" for k in
               ("trims_file", "model_trims_file", "groups_file", "model_file",
                "policy_file", "last_file")}
        z = OpenZone(_FakeCastProvider(_fake_cast()), cfg)
        z.running, z._active_group = True, "g1"
        _stream(z)
        z._queue_pos = 4
        z._session_media = {"media_type": "tidal", "items": [{"source_id": "1"}],
                            "start_index": 0}
        z._remember_last()
        c.check("a queue is kept at the item it had reached",
                (z.last_media("g1") or {}).get("start_index") == 4,
                z.last_media("g1"))
        z._session_media = {"station_uuid": "radio-x", "url": "http://old/x",
                            "title": "Radio X"}
        z._remember_last()
        c.check("a station is kept by id — its URL is resolved afresh",
                z.last_media("g1") == {"station_uuid": "radio-x",
                                       "title": "Radio X"}, z.last_media("g1"))
        c.check("and survives a restart",
                OpenZone(_FakeCastProvider(_fake_cast()), cfg).last_media("g1")
                == z.last_media("g1"))
    loop.close()
    return c


if __name__ == "__main__":
    raise SystemExit(1 if run().failures else 0)
