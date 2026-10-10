"""
The recordings folder: segments, clips, playback stretches and retention.
Shared by the recorder sidecar, which writes it, and the app, which reads it
(and deletes clips). Synchronous: call it from a thread. See docs/recordings.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("recorder.store")

ROOT = Path(os.environ.get("ZMM_RECORDINGS_DIR", "./data/recordings"))
SEGMENT_S = 4
# A segment ends at a keyframe, so one can run well past SEGMENT_S.
SEGMENT_SLACK_S = 12
# In "events" mode footage is kept only long enough to cut clips from.
EVENT_BUFFER_S = 15 * 60
MAX_EVENT_S = 5 * 60
MAX_PLAY_S = 10 * 60
PLAY_CACHE_S = 10 * 60
MIN_FREE_BYTES = 2 * 1024 ** 3
DEFAULT_MAX_GB = 20
STALL_S = 40
TICK_S = 5
PRUNE_EVERY_S = 120
MODES = ("off", "events", "continuous")
EVENT_KEYS = ("motion", "person", "vehicle", "animal")
RECORD_DEFAULTS = {"mode": "off", "events": [], "pre_s": 5, "post_s": 10, "clip_days": 14, "hours": 24}
_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)\.ts$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CLIP_RE = re.compile(r"^\d{8}T\d{6}Z$")
STAMP = "%Y%m%dT%H%M%SZ"


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime(STAMP)


def unstamp(s: str) -> float:
    return datetime.strptime(s, STAMP).replace(tzinfo=timezone.utc).timestamp()


def normalise_record(data: Any, current: Optional[Dict[str, Any]], detect_labels: List[str]) -> Dict[str, Any]:
    """A camera's recording settings; raises ValueError."""
    out = {**RECORD_DEFAULTS, **(current or {})}
    d = data or {}
    if "mode" in d:
        if d["mode"] not in MODES:
            raise ValueError(f"Recording mode must be one of {', '.join(MODES)}")
        out["mode"] = d["mode"]
    out.pop("when", None)
    if "events" in d:
        out["events"] = [k for k in EVENT_KEYS if k in (d["events"] or [])]
    for key, lo, hi, what in (("pre_s", 0, 30, "Seconds before"), ("post_s", 0, 60, "Seconds after"),
                              ("clip_days", 1, 90, "Days to keep clips"), ("hours", 1, 168, "Hours to keep")):
        if key in d:
            try:
                v = int(d[key])
            except (TypeError, ValueError):
                raise ValueError(f"{what} must be a whole number")
            if not lo <= v <= hi:
                raise ValueError(f"{what} must be {lo}-{hi}")
            out[key] = v
    # An object a camera isn't detecting can never start a clip.
    out["events"] = [k for k in out["events"] if k == "motion" or k in detect_labels]
    if out["mode"] != "off" and not out["events"]:
        out["events"] = list(detect_labels) or ["motion"]
    return out


def _ffmpeg(args: List[str], timeout: float = 120) -> bool:
    try:
        p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[recordings] ffmpeg: %s", e)
        return False
    if p.returncode != 0:
        logger.warning("[recordings] ffmpeg: %s", p.stderr.decode("utf-8", "replace").strip()[-300:])
    return p.returncode == 0


class Store:
    """The recordings folder. Synchronous: call it from a thread.

        live/<cam>/            what ffmpeg is writing now
        segments/<cam>/<day>/  closed segments, a folder per UTC day
        clips/<cam>/           <id>.mp4, .jpg, .json
        cache/                 stretches joined for playback
    """

    def __init__(self, root: Path = ROOT, ffmpeg: Callable[..., bool] = _ffmpeg,
                 disk_free: Optional[Callable[[], int]] = None):
        self.root, self._ffmpeg = root, ffmpeg
        self._disk_free = disk_free or (lambda: shutil.disk_usage(self.root).free)

    def live_dir(self, cam: str) -> Path:
        d = self.root / "live" / cam
        d.mkdir(parents=True, exist_ok=True)
        return d

    # Settings
    def settings(self) -> Dict[str, Any]:
        try:
            raw = json.loads((self.root / "settings.json").read_text())
        except (OSError, ValueError):
            raw = {}
        try:
            gb = float(raw.get("max_gb", DEFAULT_MAX_GB))
        except (TypeError, ValueError):
            gb = DEFAULT_MAX_GB
        # On: cameras record only while the house mode is away or holiday.
        return {"max_gb": min(max(gb, 1), 100000), "away_only": raw.get("away_only", True) is not False}

    def save_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """A partial update: what isn't mentioned is kept."""
        out = self.settings()
        if "max_gb" in changes:
            try:
                gb = float(changes.get("max_gb"))
            except (TypeError, ValueError):
                raise ValueError("The space limit must be a number of GB")
            if not 1 <= gb <= 100000:
                raise ValueError("The space limit must be at least 1 GB")
            out["max_gb"] = gb
        if "away_only" in changes:
            out["away_only"] = bool(changes["away_only"])
        if not {"max_gb", "away_only"} & set(changes):
            raise ValueError("Nothing to change")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "settings.tmp"
        tmp.write_text(json.dumps(out))
        os.replace(tmp, self.root / "settings.json")
        return out

    # What to record, as the app last sent it. Kept so recording resumes
    # after a restart with the app down. It holds go2rtc's API password, as
    # go2rtc.yaml on the same disk already does.
    def save_config(self, cameras: List[Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "config.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"cameras": cameras}, f)
        os.replace(tmp, self.root / "config.json")

    def load_config(self) -> List[Dict[str, Any]]:
        try:
            return list(json.loads((self.root / "config.json").read_text()).get("cameras") or [])
        except (OSError, ValueError, AttributeError):
            return []

    # Segments
    def adopt(self, cam: str, writing: bool) -> Optional[float]:
        """Move closed segments out of live/ into their day folders. ffmpeg
        writes one file at a time, so all but the newest are closed — and that
        one too when it isn't running. Returns when the newest was last written."""
        live = self.live_dir(cam)
        names = sorted(n for n in os.listdir(live) if _NAME_RE.match(n))
        newest = None
        if names:
            try:
                newest = (live / names[-1]).stat().st_mtime
            except OSError:
                pass
        for n in names[:-1] if writing else names:
            day = self.root / "segments" / cam / n[:8]
            day.mkdir(parents=True, exist_ok=True)
            try:
                if (live / n).stat().st_size == 0:
                    (live / n).unlink()
                else:
                    os.replace(live / n, day / n)
            except OSError as e:
                logger.warning("[recordings] couldn't file %s: %s", n, e)
        return newest

    def _days(self, cam: str) -> List[Path]:
        base = self.root / "segments" / cam
        try:
            return sorted(p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}", p.name))
        except OSError:
            return []

    def segments(self, cam: str, t0: float, t1: float) -> List[Tuple[float, Path]]:
        """Closed segments that overlap [t0, t1], oldest first."""
        out: List[Tuple[float, Path]] = []
        lo, hi = stamp(t0 - 86400)[:8], stamp(t1)[:8]
        for day in self._days(cam):
            if not lo <= day.name <= hi:
                continue
            for n in sorted(os.listdir(day)):
                m = _NAME_RE.match(n)
                if m:
                    out.append((unstamp(m.group(1)), day / n))
        # One that starts before t0 may still run into it; its end is the next one's start.
        keep = [i for i, (s, _p) in enumerate(out)
                if s < t1 and (out[i + 1][0] if i + 1 < len(out) else s + SEGMENT_SLACK_S) > t0
                and s > t0 - SEGMENT_SLACK_S]
        return [out[i] for i in keep]

    def coverage(self, cam: str, t0: float, t1: float) -> List[List[float]]:
        """Stretches of [t0, t1] there is footage for, as [start, end] pairs."""
        runs: List[List[float]] = []
        for s, _p in self.segments(cam, t0, t1):
            if runs and s - runs[-1][1] <= SEGMENT_SLACK_S:
                runs[-1][1] = s + SEGMENT_S
            else:
                runs.append([s, s + SEGMENT_S])
        return [[max(a, t0), min(b, t1)] for a, b in runs if b > t0 and a < t1]

    def _join(self, parts: List[Path], out: Path) -> bool:
        out.parent.mkdir(parents=True, exist_ok=True)
        listing = out.with_suffix(".txt")
        listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
        tmp = out.with_suffix(".part.mp4")
        try:
            ok = self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                               "-movflags", "+faststart", str(tmp)])
            if ok and tmp.exists():
                os.replace(tmp, out)
            return ok and out.exists()
        finally:
            listing.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)

    # Clips
    def build_clip(self, cam: str, start: float, end: float, pre: float, post: float,
                   labels: List[str], thumb: Optional[bytes] = None) -> Optional[Dict[str, Any]]:
        parts = self.segments(cam, start - pre, end + post)
        if not parts:
            return None
        cid = stamp(start)
        d = self.root / "clips" / cam
        mp4 = d / f"{cid}.mp4"
        if not self._join([p for _s, p in parts], mp4):
            return None
        jpg = d / f"{cid}.jpg"
        if thumb:
            jpg.write_bytes(thumb)
        else:
            self._ffmpeg(["-ss", str(max(start - parts[0][0], 0)), "-i", str(mp4), "-frames:v", "1",
                          "-vf", "scale=640:-2", "-q:v", "5", str(jpg)], timeout=30)
        meta = {"id": cid, "camera": cam, "start": round(start, 1), "end": round(end, 1),
                "from": parts[0][0], "labels": sorted(labels), "size": mp4.stat().st_size,
                "thumb": jpg.exists()}
        (d / f"{cid}.json").write_text(json.dumps(meta))
        return meta

    def clips(self) -> List[Dict[str, Any]]:
        out = []
        base = self.root / "clips"
        try:
            cams = [p for p in base.iterdir() if p.is_dir()]
        except OSError:
            return []
        for d in cams:
            for f in d.glob("*.json"):
                try:
                    meta = json.loads(f.read_text())
                    if (d / f"{meta['id']}.mp4").exists():
                        out.append(meta)
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(out, key=lambda m: m["start"], reverse=True)

    def clip_file(self, cam: str, cid: str, ext: str) -> Optional[Path]:
        if not _ID_RE.match(cam) or not _CLIP_RE.match(cid) or ext not in ("mp4", "jpg"):
            return None
        p = self.root / "clips" / cam / f"{cid}.{ext}"
        return p if p.is_file() else None

    def delete_clip(self, cam: str, cid: str) -> bool:
        if not _ID_RE.match(cam) or not _CLIP_RE.match(cid):
            return False
        found = False
        for ext in ("mp4", "jpg", "json"):
            p = self.root / "clips" / cam / f"{cid}.{ext}"
            if p.exists():
                p.unlink()
                found = True
        return found

    # Playback of continuous footage
    def play(self, cam: str, start: float, seconds: float) -> Optional[Path]:
        seconds = min(max(seconds, SEGMENT_S), MAX_PLAY_S)
        out = self.root / "cache" / f"{cam}_{int(start)}_{int(seconds)}.mp4"
        if out.exists():
            os.utime(out)
            return out
        parts = self.segments(cam, start, start + seconds)
        if not parts or not self._join([p for _s, p in parts], out):
            return None
        return out

    # Space
    def usage(self) -> Dict[str, Any]:
        seg = clip = 0
        for sub in ("segments", "live"):
            for _dir, _dirs, files in os.walk(self.root / sub):
                for f in files:
                    try:
                        seg += os.stat(os.path.join(_dir, f)).st_size
                    except OSError:
                        pass
        for _dir, _dirs, files in os.walk(self.root / "clips"):
            for f in files:
                try:
                    clip += os.stat(os.path.join(_dir, f)).st_size
                except OSError:
                    pass
        try:
            free = self._disk_free()
        except OSError:
            free = None
        return {"footage_bytes": seg, "clip_bytes": clip, "free_bytes": free,
                "max_bytes": int(self.settings()["max_gb"] * 1024 ** 3)}

    def prune(self, keep: Dict[str, Dict[str, float]], now: float) -> Dict[str, int]:
        """Apply each camera's retention (`keep[cam] = {footage_s, clip_s}`;
        a camera not listed keeps no footage and its clips for the default
        time), then free space, oldest footage first and clips last, until
        under the limit with room left on the disk."""
        removed = {"segments": 0, "clips": 0}
        default_clip_s = RECORD_DEFAULTS["clip_days"] * 86400
        segs: List[Tuple[float, int, Path]] = []
        try:
            cams = [p.name for p in (self.root / "segments").iterdir() if p.is_dir()]
        except OSError:
            cams = []
        for cam in cams:
            limit = now - keep.get(cam, {}).get("footage_s", 0)
            for day in self._days(cam):
                for e in os.scandir(day):
                    m = _NAME_RE.match(e.name)
                    if not m:
                        continue
                    s = unstamp(m.group(1))
                    if s < limit:
                        os.unlink(e.path)
                        removed["segments"] += 1
                    else:
                        segs.append((s, e.stat().st_size, Path(e.path)))
                try:
                    day.rmdir()                           # only if it emptied
                except OSError:
                    pass
        clips = self.clips()
        for meta in list(clips):
            if meta["start"] < now - keep.get(meta["camera"], {}).get("clip_s", default_clip_s):
                self.delete_clip(meta["camera"], meta["id"])
                clips.remove(meta)
                removed["clips"] += 1

        max_bytes = int(self.settings()["max_gb"] * 1024 ** 3)
        used = sum(s for _t, s, _p in segs) + sum(m.get("size", 0) for m in clips)
        try:
            short = max(MIN_FREE_BYTES - self._disk_free(), 0)
        except OSError:
            short = 0
        over = max(used - max_bytes, short)
        for _t, size, path in sorted(segs):
            if over <= 0:
                break
            try:
                os.unlink(path)
                removed["segments"] += 1
                over -= size
            except OSError:
                pass
        for meta in reversed(clips):
            if over <= 0:
                break
            self.delete_clip(meta["camera"], meta["id"])
            removed["clips"] += 1
            over -= meta.get("size", 0)

        cache = self.root / "cache"
        if cache.is_dir():
            for e in os.scandir(cache):
                if e.stat().st_mtime < now - PLAY_CACHE_S:
                    os.unlink(e.path)
        return removed
