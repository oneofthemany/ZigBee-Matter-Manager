"""A time series of how the detector is doing: chip temperature, throttling
and load, sampled twice a minute into a small file the app charts on the
System page. A file, not the telemetry database: that has one writer, and it
isn't this container. See docs/vision.md §Metrics."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("vision.metrics")

SAMPLE_S = 30
KEEP_S = 7 * 86400
# Compacted when it has grown about a day past what is kept.
MAX_BYTES = 3 * 1024 * 1024
APEX = Path(os.environ.get("ZMM_SYSFS_ROOT", "/sys")) / "class/apex/apex_0"


def _millic(path: Path) -> Optional[float]:
    try:
        return round(int(path.read_text().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def chip() -> Dict[str, Any]:
    """An M.2 Coral's temperature and how far its clock has been stepped down
    (0-3). Nothing for a USB Coral or the CPU: they report neither."""
    temp = _millic(APEX / "temp")
    if temp is None:
        return {}
    trips = [_millic(APEX / f"trip_point{i}_temp") for i in range(3)]
    return {"temp": temp, "throttle": sum(1 for t in trips if t is not None and temp >= t)}


class Sampler:
    def __init__(self, hub: Any, path: Path, clock: Callable[[], float] = time.time,
                 chip_fn: Callable[[], Dict[str, Any]] = chip):
        self.hub, self.path, self._clock, self._chip = hub, path, clock, chip_fn
        self._count = 0

    def sample(self) -> Dict[str, Any]:
        d = self.hub.detector
        workers = list(self.hub.workers.values())
        row = {"t": round(self._clock()), "backend": d.backend,
               # Looks since the last sample; the count restarts when the model reloads.
               "n": d.count - self._count if d.count >= self._count else d.count,
               "ms": round(d.ms, 1) if d.count else None,
               "cams": sum(1 for w in workers if w.online), **self._chip()}
        self._count = d.count
        return row

    def write(self, row: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        if self.path.stat().st_size > MAX_BYTES:
            self.compact()

    def compact(self) -> None:
        cutoff = self._clock() - KEEP_S
        keep = [ln for ln in self.path.read_text().splitlines() if _t(ln) >= cutoff]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + ("\n" if keep else ""))
        os.replace(tmp, self.path)

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(SAMPLE_S):
            try:
                if self.hub.detector.ready:
                    self.write(self.sample())
            except Exception as e:                        # noqa: BLE001
                logger.warning("metrics sample failed: %s", e)


def _t(line: str) -> float:
    try:
        return float(json.loads(line)["t"])
    except (ValueError, KeyError, TypeError):
        return 0.0


def read(path: Path, hours: float, bucket_minutes: int, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The series for the last `hours`, one row per bucket: temperature and
    time per look averaged, throttling at its worst, looks as a rate per minute."""
    now = time.time() if now is None else now
    start, width = now - hours * 3600, bucket_minutes * 60
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                    t = float(row["t"])
                except (ValueError, KeyError, TypeError):
                    continue
                if t >= start:
                    buckets.setdefault(int(t // width), []).append(row)
    except OSError:
        return []

    def avg(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else None
    out = []
    for b in sorted(buckets):
        rows = buckets[b]
        span = max(len(rows) * SAMPLE_S, SAMPLE_S) / 60
        out.append({"ts": b * width, "backend": rows[-1].get("backend"),
                    "temp": avg(rows, "temp"), "ms": avg(rows, "ms"),
                    "throttle": max((r.get("throttle") or 0) for r in rows),
                    "looks_per_min": round(sum(r.get("n") or 0 for r in rows) / span, 1),
                    "cams": max((r.get("cams") or 0) for r in rows)})
    return out
