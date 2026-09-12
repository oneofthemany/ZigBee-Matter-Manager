"""
Ownership travelling with the playback.

A Tidal stream URL is re-resolved long after the request that queued it — on
auto-advance, on a restart resume, by the zone engine, by the Cast lyrics
receiver — and none of those have a principal to ask. So the owning user rides
on the item, and every resolution names it. Step 2 of
docs/plans/tidal-per-user-auth.md.

The zone engine itself is not exercised here: modules.media.cast_sync imports
FastAPI at module scope, which is not installed on a dev box. What feeds it is
covered instead — the rows carry an owner, and the media block is stamped with
one server-side.
"""

from __future__ import annotations

import asyncio

from harness import Checker, TempSessions

from modules.media.controller import MediaController
from modules.media.models import MediaItem
from modules.media.queue import PlayerQueue
from modules.media.sources import tidal as T
from modules.media.sources.tidal import TidalAccount, TidalSource, UNASSIGNED


def _source(**kw) -> TidalSource:
    src = TidalSource(enabled=True, **kw)
    src._available = True
    return src


class FakeTrack:
    """The handful of attributes _track_to_item reads off a tidalapi Track."""
    id, name, duration = 42, "Song", 180
    artist = album = None


def _item(c: Checker) -> None:
    c.section("the item carries the owner")
    c.check("owner defaults to empty", MediaItem(url="x").owner == "")

    with TempSessions(T):
        acct = TidalAccount("alice")
        item = acct._track_to_item(FakeTrack())
        c.check("an account stamps its own name on what it builds",
                item.owner == "alice", item.owner)

    q = PlayerQueue("cast:1")
    q.add([MediaItem(url="", source_id="42", media_type="tidal", owner="alice")])
    back = PlayerQueue.from_dict("cast:1", q.to_dict())
    c.check("owner survives a queue snapshot",
            back.current().item.owner == "alice", back.current().item.owner)

    # Queues written before this field existed still load — MediaItem(**d).
    old = q.to_dict()
    for entry in old.get("items", []):
        entry["item"].pop("owner", None)
    legacy = PlayerQueue.from_dict("cast:1", old)
    c.check("a queue saved before the field still restores",
            legacy.current().item.owner == "", legacy.current().item.owner)


def _fail_closed(c: Checker) -> None:
    c.section("resolution fails closed")
    with TempSessions(T):
        src = _source()
        src._ensure("alice")
        src._ensure("bob")

        c.check("a named, linked owner resolves to that account",
                src._for("alice").username == "alice")
        c.check("a named owner with no account resolves to nothing",
                src._for("carol") is None)
        c.check("and does not fall back to a linked one",
                src._for("carol") is None and len(src._accounts) == 2)
        c.check("an empty owner takes the default",
                src._for("").username == UNASSIGNED)

        # Nothing linked for carol: every entry point answers empty rather than
        # playing her queue on alice's subscription.
        c.check("resolve_url gives nothing for an unlinked owner",
                asyncio.run(src.resolve_url("42", "cast", "carol")) is None)
        c.check("track_radio gives nothing for an unlinked owner",
                asyncio.run(src.track_radio("42", "carol")) == [])
        c.check("track_lyrics gives nothing for an unlinked owner",
                asyncio.run(src.track_lyrics("42", "carol")) is None)

        # One linked account is the default, so pre-upgrade items still play —
        # on the account that played them before the split.
        solo = _source()
        solo._ensure("alice")
        c.check("an empty owner with one account linked reaches it",
                solo._for("").username == "alice")


def _controller(c: Checker) -> None:
    c.section("the controller names the owner")
    seen = []

    async def spy(source_id, provider=None, owner=""):
        seen.append((source_id, provider, owner))
        return {"url": f"https://x/{source_id}"}

    ctl = MediaController()
    ctl.register_resolver("tidal", spy)

    item = MediaItem(url="", source_id="42", media_type="tidal", owner="alice")
    asyncio.run(ctl._resolve(item, "cast:abc"))
    c.check("a queued item resolves on its own owner",
            seen[-1] == ("42", "cast", "alice"), seen[-1])
    c.check("and the fresh URL is applied", item.url == "https://x/42", item.url)

    asyncio.run(ctl.resolve_source_url("tidal", "43", provider="zone",
                                       owner="bob"))
    c.check("the zone path passes the owner it was given",
            seen[-1] == ("43", "zone", "bob"), seen[-1])

    asyncio.run(ctl.resolve_source_url("tidal", "44", provider="zone"))
    c.check("and an unnamed one stays empty rather than guessing",
            seen[-1] == ("44", "zone", ""), seen[-1])

    # An item saved before the field existed resolves with an empty owner,
    # which the source reads as "the default account".
    asyncio.run(ctl._resolve(MediaItem(url="", source_id="45",
                                       media_type="tidal"), "wiim:1"))
    c.check("a pre-upgrade item resolves with no owner, not a wrong one",
            seen[-1] == ("45", "wiim", ""), seen[-1])

    extended = []

    async def extender(seed, owner=""):
        extended.append((seed, owner))
        return []

    ctl.register_extender(extender)
    q = PlayerQueue("cast:abc")
    q.add([MediaItem(url="", source_id="42", media_type="tidal", owner="alice")])
    asyncio.run(ctl._extend_queue("cast:abc", q))
    c.check("radio tops up from the library the seed came from",
            extended[-1] == ("42", "alice"), extended[-1])


def _service(c: Checker) -> None:
    c.section("the service stamps the owner")
    from modules.media.service import MediaService

    svc = MediaService(config={"tidal": {"enabled": False}})

    async def fake_items(kind, container_id, mode="play", username=""):
        return [MediaItem(url="", source_id="42", media_type="tidal",
                          owner=username or "alice", title="Song")]

    svc.tidal_items = fake_items
    rows = asyncio.run(svc.sync_queue_items("tidal", "album", "7", "bob"))
    c.check("a zone row says whose account it plays on",
            rows and rows[0].get("owner") == "bob", rows)

    media = {"source_id": "7", "media_type": "tidal", "kind": "album"}
    ok, err = asyncio.run(svc.resolve_zone_media(media, "bob"))
    c.check("a zone media block is stamped server-side",
            ok and media["owner"] == "bob", (ok, err, media))

    # The body is client-supplied; naming an owner is naming an account to
    # stream on, so the caller's own name must win over anything in it.
    hostile = {"source_id": "7", "media_type": "tidal", "kind": "album",
               "owner": "alice"}
    asyncio.run(svc.resolve_zone_media(hostile, "bob"))
    c.check("a client-supplied owner cannot name someone else's account",
            hostile["owner"] == "bob", hostile)

    saved = {"source_id": "7", "media_type": "tidal", "kind": "album",
             "owner": "alice"}
    asyncio.run(svc.resolve_zone_media(saved))
    c.check("a saved zone keeps the owner it was stored with",
            saved["owner"] == "alice", saved)


def run() -> Checker:
    c = Checker("tidal-owner")
    for part in (_item, _fail_closed, _controller, _service):
        part(c)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
