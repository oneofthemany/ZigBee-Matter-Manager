"""Which cameras record, how an event becomes a clip, and recovering when
ffmpeg stops. See docs/recordings.md §How it works."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .store import (EVENT_BUFFER_S, MAX_EVENT_S, MODES, PRUNE_EVERY_S, RECORD_DEFAULTS, SEGMENT_S,
                    SEGMENT_SLACK_S, STALL_S, TICK_S, Store, stamp)

logger = logging.getLogger("recorder")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_THUMB_BYTES = 2 * 1024 * 1024


def record_cmd(url: str, live: Path) -> List[str]:
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-i", url,
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_S), "-segment_format", "mpegts",
            "-reset_timestamps", "1", "-strftime", "1", str(live / "%Y%m%dT%H%M%SZ.ts")]


def clean_config(cameras: Any) -> List[Dict[str, Any]]:
    """The app's camera list, checked; raises ValueError."""
    if not isinstance(cameras, list) or len(cameras) > 64:
        raise ValueError("cameras must be a list of at most 64")
    out = []
    for c in cameras:
        cid, url = str((c or {}).get("id") or ""), str(c.get("url") or "")
        if not _ID_RE.match(cid) or not url.startswith(("http://", "https://", "rtsp://", "rtsps://")) \
                or any(ch in url for ch in "\r\n "):
            raise ValueError(f"bad camera entry '{cid}'")
        rec = {**RECORD_DEFAULTS, **{k: v for k, v in (c.get("record") or {}).items() if k in RECORD_DEFAULTS}}
        if rec["mode"] not in MODES or not isinstance(rec["events"], list):
            raise ValueError(f"bad recording settings for '{cid}'")
        for k in ("pre_s", "post_s", "clip_days", "hours"):
            rec[k] = int(rec[k])
        out.append({"id": cid, "url": url, "record": rec})
    return sorted(out, key=lambda c: c["id"])


class Recorder:
    def __init__(self, store: Optional[Store] = None, spawn: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.time):
        self.store, self._clock = store or Store(), clock
        self._spawn = spawn or self._spawn_ffmpeg
        self.cams: Dict[str, Dict[str, Any]] = {}
        self.config_hash = ""
        self._procs: Dict[str, Any] = {}                 # cid -> process
        self._retry: Dict[str, Tuple[float, float]] = {}  # cid -> (not before, backoff)
        self._state: Dict[str, Dict[str, Any]] = {}      # cid -> {recording, error, last_write}
        self._events: Dict[str, Dict[str, Any]] = {}     # cid -> the event under way
        self._pending: List[Dict[str, Any]] = []         # events waiting for their last segment
        self._pruned_at = 0.0
        self.clips_made = 0

    @staticmethod
    async def _spawn_ffmpeg(cmd: List[str]):
        # Segment names are wall-clock; UTC keeps them unambiguous across DST.
        return await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env={**os.environ, "TZ": "UTC"})

    def configure(self, cameras: Any, persist: bool = True) -> str:
        clean = clean_config(cameras)
        for c in clean:
            old = self.cams.get(c["id"])
            if old and old["url"] != c["url"]:
                self._stop(c["id"])                      # restarted on the new address by the next tick
        self.cams = {c["id"]: c for c in clean}
        self.config_hash = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()[:16]
        if persist:
            self.store.save_config(clean)
        return self.config_hash

    def _cfg(self, cid: str) -> Dict[str, Any]:
        cam = self.cams.get(cid)
        return cam["record"] if cam else {**RECORD_DEFAULTS}

    # Events
    def signal(self, cid: str, state: Dict[str, Any]) -> bool:
        """A camera's signals changed. Starts, extends or ends its event.
        True when this started one that a detection frame would illustrate."""
        rec = self._cfg(cid)
        now = self._clock()
        on = [k for k in rec["events"] if state.get(k)] if rec["mode"] != "off" else []
        ev = self._events.get(cid)
        started = False
        if on:
            if ev is None:
                ev = self._events[cid] = {"start": now, "labels": set(), "thumb": None}
                started = bool(set(on) - {"motion"})
            ev["labels"].update(on)
        elif ev is not None:
            self._finish(cid, now)
        return started

    def set_thumb(self, cid: str, jpeg: bytes) -> bool:
        ev = self._events.get(cid)
        if ev is None or ev["thumb"] or not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_THUMB_BYTES:
            return False
        ev["thumb"] = jpeg
        return True

    def _finish(self, cid: str, end: float) -> None:
        ev = self._events.pop(cid, None)
        if ev is None:
            return
        rec = self._cfg(cid)
        self._pending.append({"camera": cid, "start": ev["start"], "end": end, "labels": sorted(ev["labels"]),
                              "thumb": ev["thumb"], "pre": rec["pre_s"], "post": rec["post_s"],
                              # The segment holding the end has to close first.
                              "ready": end + rec["post_s"] + SEGMENT_S + SEGMENT_SLACK_S})

    # The loop
    async def tick(self) -> None:
        now = self._clock()
        wanted = {cid for cid in self.cams if self._cfg(cid)["mode"] != "off"}
        for cid in list(self._procs):
            p = self._procs[cid]
            if cid not in wanted:
                self._stop(cid)
            elif p.returncode is not None:
                err = b""
                try:
                    err = await asyncio.wait_for(p.stderr.read(), 1) if p.stderr else b""
                except Exception:                         # noqa: BLE001
                    pass
                line = (err.decode("utf-8", "replace").strip().splitlines() or ["the stream ended"])[-1]
                # ffmpeg quotes the URL it failed on, password included.
                line = re.sub(r"(https?|rtsps?)://\S+", "the stream", line)[:200]
                backoff = min(self._retry.get(cid, (0, 2.5))[1] * 2, 60)
                self._retry[cid] = (now + backoff, backoff)
                self._state[cid] = {**self._state.get(cid, {}), "recording": False, "error": line}
                del self._procs[cid]
                logger.info("%s stopped: %s (retry in %ss)", cid, line, int(backoff))
        for cid in wanted:
            if cid not in self._procs and now >= self._retry.get(cid, (0, 0))[0]:
                await self._start(cid)
        for cid in set(self._state) | wanted:
            running = cid in self._procs
            newest = await asyncio.to_thread(self.store.adopt, cid, running)
            st = self._state.setdefault(cid, {"recording": False, "error": None})
            st["last_write"] = newest
            if running:
                if newest and now - newest < STALL_S:
                    st.update(recording=True, error=None)
                    self._retry.pop(cid, None)
                elif now - st.get("started", now) > STALL_S:
                    st.update(recording=False, error="no video arriving")
                    self._procs[cid].kill()
            if cid not in wanted and not running:
                self._state.pop(cid, None)

        for cid, ev in list(self._events.items()):
            if cid not in wanted:
                self._events.pop(cid)
            elif now - ev["start"] > MAX_EVENT_S:
                # Long events become back-to-back clips rather than one huge file.
                labels = set(ev["labels"])
                self._finish(cid, now)
                self._events[cid] = {"start": now, "labels": labels, "thumb": None}
        for job in [j for j in self._pending if now >= j["ready"]]:
            self._pending.remove(job)
            meta = await asyncio.to_thread(self.store.build_clip, job["camera"], job["start"], job["end"],
                                           job["pre"], job["post"], job["labels"], job["thumb"])
            if meta:
                self.clips_made += 1
                logger.info("clip %s/%s (%s)", meta["camera"], meta["id"], ", ".join(meta["labels"]))
            else:
                logger.info("no footage for the %s event at %s", job["camera"], stamp(job["start"]))

        if now - self._pruned_at >= PRUNE_EVERY_S:
            self._pruned_at = now
            await asyncio.to_thread(self.store.prune, self.retention(), now)

    def retention(self) -> Dict[str, Dict[str, float]]:
        out = {}
        for cid in self.cams:
            rec = self._cfg(cid)
            footage = {"off": 0, "events": EVENT_BUFFER_S, "continuous": rec["hours"] * 3600}[rec["mode"]]
            out[cid] = {"footage_s": footage, "clip_s": rec["clip_days"] * 86400}
        return out

    async def _start(self, cid: str) -> None:
        try:
            live = await asyncio.to_thread(self.store.live_dir, cid)
            self._procs[cid] = await self._spawn(record_cmd(self.cams[cid]["url"], live))
            self._state[cid] = {"recording": False, "error": None, "started": self._clock()}
        except Exception as e:                            # noqa: BLE001
            self._state[cid] = {"recording": False, "error": f"couldn't start ffmpeg: {e}"}
            self._retry[cid] = (self._clock() + 60, 60)

    def _stop(self, cid: str) -> None:
        p = self._procs.pop(cid, None)
        if p is not None and p.returncode is None:
            try:
                p.terminate()                             # lets ffmpeg close the segment it is on
            except ProcessLookupError:
                pass
        self._retry.pop(cid, None)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # noqa: BLE001
                logger.error("tick failed: %s", e)
            await asyncio.sleep(TICK_S)

    def stop(self) -> None:
        for cid in list(self._procs):
            self._stop(cid)

    def status(self) -> Dict[str, Any]:
        return {"config": self.config_hash, "clips_made": self.clips_made,
                "cameras": {cid: {"mode": self._cfg(cid)["mode"], "recording": bool(st.get("recording")),
                                  "error": st.get("error"), "event": cid in self._events}
                            for cid, st in self._state.items()}}
