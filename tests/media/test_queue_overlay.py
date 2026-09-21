"""
The media tab shows what a speaker is playing, not what ZMM last queued on it.

The controller fills a player's now-playing from its queue item, because many
devices report nothing useful for a raw URL. That must not paint a stale Tidal
track over a speaker that has since been put on the radio from elsewhere.
"""

from __future__ import annotations

from harness import Checker, REPO  # noqa: F401  (REPO sets sys.path)

from modules.media.controller import MediaController
from modules.media.models import MediaItem, PlayerState, PlaybackState

TIDAL_ART = "https://resources.tidal.com/images/album/1280x1280.jpg"
RADIO_ART = "https://cdn.example/radiox.png"


def _state(pid: str, title: str = "", art: str = "",
           state=PlaybackState.PLAYING) -> PlayerState:
    return PlayerState(player_id=pid, provider=pid.split(":")[0], name=pid,
                       available=True, state=state, title=title,
                       artwork_url=art)


def _controller_with_tidal(pid: str) -> MediaController:
    mc = MediaController()
    mc._queue.get(pid, create=True).load([MediaItem(
        title="Everlong", artist="Foo Fighters", artwork_url=TIDAL_ART,
        media_type="tidal", source_id="123", url="http://x/manifest")])
    return mc


def run() -> Checker:
    c = Checker("queue_overlay")

    c.section("a speaker playing something started elsewhere keeps its own art")
    pid = "cast:master-display"
    mc = _controller_with_tidal(pid)
    s = _state(pid, title="Radio X", art=RADIO_ART)
    mc._attach_queue(s)
    c.check("the radio art is not replaced by the queued Tidal art",
            s.artwork_url == RADIO_ART, s.artwork_url)
    c.check("nor is the title", s.title == "Radio X", s.title)
    c.check("the queue is still attached for the queue panel", bool(s.queue))

    c.section("ZMM's own playback is still enriched from the queue")
    mc = _controller_with_tidal(pid)
    s = _state(pid, title="Everlong", art=TIDAL_ART)
    mc._attach_queue(s)
    c.check("a matching title takes the queue item's metadata",
            s.artist == "Foo Fighters" and s.now_playing_id == "123",
            (s.artist, s.now_playing_id))
    s = _state(pid, title="EVERLONG ", art="")
    mc._attach_queue(s)
    c.check("case and whitespace do not count as a different track",
            s.artwork_url == TIDAL_ART, s.artwork_url)

    c.section("a device that reports no metadata is filled in")
    pid = "wiim:10.0.0.5"
    mc = _controller_with_tidal(pid)
    s = _state(pid)
    mc._attach_queue(s)
    c.check("no title, no art: the queue supplies both",
            s.title == "Everlong" and s.artwork_url == TIDAL_ART,
            (s.title, s.artwork_url))
    # WiiM reads tag titles from the stream, which drift from ZMM's, and never
    # reports art — so the queue must still supply the picture.
    s = _state(pid, title="Everlong (Remastered)")
    mc._attach_queue(s)
    c.check("a drifting tag title without device art still gets queue art",
            s.artwork_url == TIDAL_ART, s.artwork_url)

    c.section("an idle speaker is left alone")
    pid = "cast:elena"
    mc = _controller_with_tidal(pid)
    s = _state(pid, state=PlaybackState.IDLE)
    mc._attach_queue(s)
    c.check("idle state is not given the queue's art", s.artwork_url == "")
    return c


if __name__ == "__main__":
    run()
