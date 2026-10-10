"""One camera: ffmpeg decodes it to small raw frames, motion picks where to
look, the detector looks, and presence is what's reported. See
docs/vision.md §Pipeline."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from . import GROUPS, LABEL_GROUP
from .motion import MotionDetector, from_input, region_for, to_input
from .tracker import Presence
from . import zones as Z

logger = logging.getLogger("vision.worker")

WIDTH, HEIGHT = 640, 360
# With something in view and nothing moving, look again this often: a person
# standing still stays present without the detector running flat out.
STILL_RECHECK_S = 4.0
RESTART_MAX_S = 60


def ffmpeg_cmd(url: str, fps: float) -> List[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if url.startswith(("rtsp://", "rtsps://")):
        cmd += ["-rtsp_transport", "tcp", "-timeout", "15000000"]
    elif "://" not in url:
        cmd += ["-re", "-stream_loop", "-1"]             # a file (tests): pace it like a live source
    cmd += ["-fflags", "nobuffer", "-flags", "low_delay", "-threads", "1", "-i", url, "-an",
            "-vf", f"fps={fps},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                   f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    return cmd


def union(boxes: List[Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


class Analyser:
    """Frame in, presence changes out. No I/O, so it can be driven by tests."""

    def __init__(self, detector: Any, groups: List[str], threshold: float = 0.5,
                 clock: Callable[[], float] = time.monotonic,
                 zones: Any = None, zones_only: bool = False):
        self.detector, self.threshold, self._clock = detector, threshold, clock
        self.groups = [g for g in groups if g in GROUPS]
        self.motion = MotionDetector()
        self.zones = Z.prepare(zones or [], WIDTH, HEIGHT, self.groups)
        # With nothing drawn there is nothing to be "only" inside.
        self.zones_only = bool(zones_only and self.zones)
        self._area = Z.bounds(self.zones, WIDTH, HEIGHT) if self.zones_only else None
        # A group in a zone is its own presence, "person:drive".
        self.presence = Presence(self.groups + [f"{g}:{z['id']}" for z in self.zones for g in z["labels"]])
        self.latest: Optional[np.ndarray] = None         # what the zone editor draws on
        self.frames = self.looks = 0
        self._looked_at = 0.0
        self.last: Optional[Dict[str, Any]] = None       # frame and boxes of the latest hit

    def frame(self, frame: np.ndarray) -> List[str]:
        now = self._clock()
        self.frames += 1
        h, w = frame.shape[:2]
        self.latest = frame
        moved = self.motion.update(frame)
        if not getattr(self.detector, "ready", True):
            return []
        if moved is not None and self._area and not Z.touches(moved, self._area):
            moved = None                                 # the street, not the drive
        held = self.presence.boxes()
        if moved is not None:
            target = union([moved] + held)
        elif held and now - self._looked_at >= STILL_RECHECK_S:
            target = union(held)
        else:
            return self.presence.expire(now)
        region = region_for(target, w, h)
        size = self.detector.size
        inp, scale, ox, oy = to_input(frame, region, size)
        self.looks += 1
        self._looked_at = now
        found: Dict[str, Tuple[float, Tuple[int, int, int, int], str]] = {}
        for label, score, box in self.detector.detect(inp, min_score=self.threshold):
            group = LABEL_GROUP.get(label)
            if group not in self.groups:
                continue
            fbox = from_input(box, region, size, scale, ox, oy)
            at = Z.foot(fbox)
            keys = [f"{group}:{z['id']}" for z in self.zones if group in z["labels"] and Z.inside(at, z["poly"])]
            if keys or not self.zones_only:
                keys.append(group)
            for key in keys:
                if score > found.get(key, (0,))[0]:
                    found[key] = (score, fbox, label)
        changed = self.presence.looked(now, found)
        if found:
            self.last = {"frame": frame.copy(), "at": time.time(),
                         "boxes": [(g, *v) for g, v in found.items() if ":" not in g]}
        return changed + self.presence.expire(now)


class CameraWorker(threading.Thread):
    def __init__(self, cfg: Dict[str, Any], detector: Any, on_change: Callable[[], None]):
        super().__init__(name=f"cam-{cfg['id']}", daemon=True)
        self.cfg, self.detector, self._on_change = cfg, detector, on_change
        self.analyser = Analyser(detector, list(cfg.get("labels") or GROUPS), float(cfg.get("threshold") or 0.5),
                                 zones=cfg.get("zones"), zones_only=bool(cfg.get("zones_only")))
        self.online = False
        self.error: Optional[str] = None
        self._halt = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        self._halt.set()
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    def run(self) -> None:
        backoff = 2
        size = WIDTH * HEIGHT * 3
        while not self._halt.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(ffmpeg_cmd(self.cfg["url"], float(self.cfg.get("fps") or 2)),
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                while not self._halt.is_set():
                    buf = self._read(size)
                    if buf is None:
                        break
                    if not self.online:
                        self.online, self.error = True, None
                        self._on_change()
                    if self.analyser.frame(np.frombuffer(buf, np.uint8).reshape(HEIGHT, WIDTH, 3)):
                        self._on_change()
                err = (self._proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
                # ffmpeg quotes the URL it failed on, credentials included.
                self.error = (err[-1].replace(self.cfg["url"], "the stream") if err else "stream ended")[:200]
            except Exception as e:                        # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"[:200]
                logger.exception("camera %s failed", self.cfg["id"])
            finally:
                if self._proc and self._proc.poll() is None:
                    self._proc.kill()
            if self._halt.is_set():
                break
            if self.online:
                self.online = False
                self._on_change()
            backoff = 2 if time.monotonic() - started > 60 else min(backoff * 2, RESTART_MAX_S)
            logger.info("camera %s: %s (retry in %ss)", self.cfg["id"], self.error, backoff)
            self._halt.wait(backoff)

    def _read(self, n: int) -> Optional[bytes]:
        chunks, got = [], 0
        while got < n:
            chunk = self._proc.stdout.read(n - got)
            if not chunk:
                return None
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def public(self) -> Dict[str, Any]:
        a = self.analyser
        return {"online": self.online, "error": self.error, "frames": a.frames, "looks": a.looks,
                "objects": a.presence.public(), "last_at": a.last["at"] if a.last else None}

    def snapshot_rgb(self) -> Optional[np.ndarray]:
        """The frame of the latest detection with its boxes drawn."""
        last = self.analyser.last
        if not last:
            return None
        img = last["frame"].copy()
        colours = {"person": (255, 80, 80), "vehicle": (80, 160, 255), "animal": (90, 220, 120)}
        for group, _score, (x0, y0, x1, y1), _label in last["boxes"]:
            c = colours.get(group, (255, 255, 0))
            x1, y1 = max(x1, x0 + 3), max(y1, y0 + 3)
            img[y0:y0 + 2, x0:x1] = c
            img[y1 - 2:y1, x0:x1] = c
            img[y0:y1, x0:x0 + 2] = c
            img[y0:y1, x1 - 2:x1] = c
        return self._outline(img)

    def frame_rgb(self) -> Optional[np.ndarray]:
        """The latest frame as the detector sees it, zones outlined."""
        latest = self.analyser.latest
        return None if latest is None else self._outline(latest.copy())

    def _outline(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        for z in self.analyser.zones:
            poly = z["poly"]
            for (xa, ya), (xb, yb) in zip(poly, poly[1:] + poly[:1]):
                n = int(max(abs(xb - xa), abs(yb - ya))) + 1
                xs = np.clip(np.linspace(xa, xb, n).astype(np.intp), 0, w - 1)
                ys = np.clip(np.linspace(ya, yb, n).astype(np.intp), 0, h - 1)
                img[ys, xs] = (255, 220, 0)
        return img


def encode_jpeg(rgb: np.ndarray) -> bytes:
    h, w = rgb.shape[:2]
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", f"{w}x{h}", "-i", "-", "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", "-"],
                       input=rgb.tobytes(), capture_output=True, timeout=10)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("could not encode the snapshot")
    return p.stdout
