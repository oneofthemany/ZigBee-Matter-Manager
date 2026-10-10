"""Turns per-frame detections into steady presence: on after it's seen twice
running, off after it's been gone a while. See docs/vision.md §Pipeline."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

Box = Tuple[int, int, int, int]


class Presence:
    def __init__(self, groups: List[str], confirm: int = 2, hold_s: float = 12.0, confirm_window_s: float = 4.0):
        self.confirm, self.hold_s, self.window = confirm, hold_s, confirm_window_s
        self.state: Dict[str, Dict[str, Any]] = {
            g: {"present": False, "hits": 0, "seen": 0.0, "score": 0.0, "box": None, "label": None} for g in groups}

    def any_present(self) -> bool:
        return any(s["present"] or s["hits"] for s in self.state.values())

    def boxes(self) -> List[Box]:
        return [s["box"] for s in self.state.values() if (s["present"] or s["hits"]) and s["box"]]

    def looked(self, now: float, found: Dict[str, Tuple[float, Box, str]]) -> List[str]:
        """Record one detector pass (group -> best score, box, label). Returns
        the groups whose presence changed. A miss doesn't clear anything by
        itself: the detector may have been looking elsewhere."""
        changed = []
        for g, s in self.state.items():
            hit = found.get(g)
            if hit is None:
                if not s["present"]:
                    s["hits"] = 0
                continue
            s["hits"] = s["hits"] + 1 if now - s["seen"] <= self.window else 1
            s.update(seen=now, score=round(hit[0], 2), box=hit[1], label=hit[2])
            if not s["present"] and s["hits"] >= self.confirm:
                s["present"] = True
                changed.append(g)
        return changed

    def expire(self, now: float) -> List[str]:
        changed = []
        for g, s in self.state.items():
            if s["present"] and now - s["seen"] > self.hold_s:
                s.update(present=False, hits=0, box=None)
                changed.append(g)
        return changed

    def public(self) -> Dict[str, Optional[Dict[str, Any]]]:
        return {g: {"present": s["present"], "score": s["score"], "label": s["label"], "box": s["box"]}
                for g, s in self.state.items()}
