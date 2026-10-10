"""Where in a frame something moved. The detector only looks there, which
keeps a small object large in its input and leaves the accelerator idle (and
cool) on a still scene. See docs/vision.md §Pipeline."""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

CELL = 8                    # px per side of a motion cell
Box = Tuple[int, int, int, int]            # x0, y0, x1, y1 in frame pixels


class MotionDetector:
    """Frame-to-frame change, not difference from a learned background:
    something that stops moving stops being motion at once, and presence
    (tracker.py) is what remembers it."""

    def __init__(self, threshold: float = 14.0, min_cells: int = 3, flood: float = 0.5):
        self.threshold, self.min_cells, self.flood = threshold, min_cells, flood
        self._prev: Optional[np.ndarray] = None

    def update(self, frame: np.ndarray) -> Optional[Box]:
        """The box around what changed since the last frame, or None."""
        h, w = frame.shape[0] // CELL * CELL, frame.shape[1] // CELL * CELL
        grey = frame[:h, :w].reshape(h // CELL, CELL, w // CELL, CELL, 3).mean(axis=(1, 3, 4), dtype=np.float32)
        prev, self._prev = self._prev, grey
        if prev is None or prev.shape != grey.shape:
            return None
        changed = np.abs(grey - prev) > self.threshold
        n = int(changed.sum())
        # The whole picture moving is exposure, the IR cut or a light switch.
        if n < self.min_cells or n > changed.size * self.flood:
            return None
        ys, xs = np.nonzero(changed)
        return (int(xs.min()) * CELL, int(ys.min()) * CELL,
                (int(xs.max()) + 1) * CELL, (int(ys.max()) + 1) * CELL)


def region_for(box: Box, width: int, height: int, min_side: int = 160, pad: float = 0.25) -> Box:
    """A square around `box` to hand the detector, or the whole frame when the
    box is too wide for one."""
    x0, y0, x1, y1 = box
    side = int(max(x1 - x0, y1 - y0) * (1 + 2 * pad))
    side = max(side, min_side)
    if side >= min(width, height):
        return (0, 0, width, height)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    left = min(max(cx - side // 2, 0), width - side)
    top = min(max(cy - side // 2, 0), height - side)
    return (left, top, left + side, top + side)


def to_input(frame: np.ndarray, region: Box, size: int) -> Tuple[np.ndarray, float, int, int]:
    """`region` of the frame as a size x size model input, letterboxed.
    Returns it with the scale and offsets that map its pixels back."""
    x0, y0, x1, y1 = region
    crop = frame[y0:y1, x0:x1]
    ch, cw = crop.shape[:2]
    scale = size / max(ch, cw)
    nh, nw = max(1, round(ch * scale)), max(1, round(cw * scale))
    # Nearest neighbour: the detector is trained on far worse.
    ys = np.minimum((np.arange(nh) / scale).astype(np.intp), ch - 1)
    xs = np.minimum((np.arange(nw) / scale).astype(np.intp), cw - 1)
    out = np.zeros((size, size, 3), dtype=np.uint8)
    oy, ox = (size - nh) // 2, (size - nw) // 2
    out[oy:oy + nh, ox:ox + nw] = crop[ys][:, xs]
    return out, scale, ox, oy


def from_input(box: Tuple[float, float, float, float], region: Box, size: int,
               scale: float, ox: int, oy: int) -> Box:
    """A detector box (ymin, xmin, ymax, xmax, 0-1 of the input) in frame pixels."""
    ymin, xmin, ymax, xmax = box
    rx0, ry0, rx1, ry1 = region
    fx0 = rx0 + (xmin * size - ox) / scale
    fy0 = ry0 + (ymin * size - oy) / scale
    fx1 = rx0 + (xmax * size - ox) / scale
    fy1 = ry0 + (ymax * size - oy) / scale
    clamp = lambda v, lo, hi: int(min(max(v, lo), hi))    # noqa: E731
    return (clamp(fx0, rx0, rx1), clamp(fy0, ry0, ry1), clamp(fx1, rx0, rx1), clamp(fy1, ry0, ry1))
