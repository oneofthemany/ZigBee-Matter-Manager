"""The model behind one call: detect(input) -> [(label, score, box)]. Loads on
the Edge TPU when asked to and able, otherwise on the CPU, and says which.
See docs/vision.md §Backends."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np

from . import assets

logger = logging.getLogger("vision.detector")

Detection = Tuple[str, float, Tuple[float, float, float, float]]


class Detector:
    def __init__(self, root: Path, want: str = "cpu", threads: int = 2):
        self.root, self.want, self.threads = root, want, threads
        self.backend: Optional[str] = None
        self.note: Optional[str] = None
        self.size = 320
        self.ms = 0.0                      # smoothed time per inference
        self.count = 0
        self._lock = threading.Lock()      # one interpreter, one accelerator
        self._it: Any = None
        self._labels: List[str] = []

    def load(self) -> None:
        from ai_edge_litert.interpreter import Interpreter, load_delegate
        self.note = None
        self._labels = assets.fetch("coco_labels.txt", self.root).read_text().splitlines()
        if self.want == "coral":
            try:
                lib = assets.edgetpu_library(self.root)
                if lib is None:
                    raise RuntimeError("no Edge TPU runtime for this CPU architecture")
                model = assets.fetch("mobiledet_edgetpu.tflite", self.root)
                self._it = Interpreter(model_path=str(model), experimental_delegates=[load_delegate(str(lib))])
                self.backend = "coral"
            except Exception as e:                        # noqa: BLE001
                # A missing device node and a runtime mismatch look the same from here.
                self.note = f"Coral not usable ({str(e).strip() or type(e).__name__}) — running on the CPU"
                logger.warning(self.note)
        if self._it is None:
            model = assets.fetch("mobiledet_cpu.tflite", self.root)
            self._it = Interpreter(model_path=str(model), num_threads=self.threads)
            self.backend = "cpu"
        self._it.allocate_tensors()
        inp = self._it.get_input_details()[0]
        self._in = inp["index"]
        self.size = int(inp["shape"][1])
        self._out = [o["index"] for o in self._it.get_output_details()]
        self.detect(np.zeros((self.size, self.size, 3), np.uint8))     # first call is slow
        self.ms, self.count = 0.0, 0
        logger.info("detector ready on %s", self.backend)

    @property
    def ready(self) -> bool:
        return self.backend is not None

    def detect(self, rgb: np.ndarray, min_score: float = 0.4) -> List[Detection]:
        with self._lock:
            t = time.perf_counter()
            self._it.set_tensor(self._in, rgb[None])
            self._it.invoke()
            outs = [self._it.get_tensor(i) for i in self._out]
            took = (time.perf_counter() - t) * 1000
        self.ms = took if not self.count else self.ms * 0.9 + took * 0.1
        self.count += 1
        boxes = next(o for o in outs if o.ndim == 3)[0]
        flat = [o[0] for o in outs if o.ndim == 2]
        # Exporters disagree on the order of classes and scores; classes are whole numbers.
        whole = [bool(np.all(o == np.floor(o))) and float(o.max(initial=0)) > 1 for o in flat]
        classes, scores = (flat[0], flat[1]) if whole[0] or not whole[1] else (flat[1], flat[0])
        out = []
        for box, cls, score in zip(boxes, classes, scores):
            if score < min_score:
                continue
            i = int(cls)
            label = self._labels[i] if 0 <= i < len(self._labels) else str(i)
            out.append((label, float(score), tuple(float(v) for v in box)))
        return out
