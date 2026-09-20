"""
Differential trim graph: relating models never heard together
(open-zone.md §7.6).

A trim is only ever established by aligning a device against another in the
same zone, so what a listener produces is a relation, not an absolute. Storing
the relation makes alignments compose: align a WiiM against a hub and a Mini
against that hub, and WiiM↔Mini follows without those two sharing a zone.

Models are nodes, observed differences are edges, and absolutes are recovered
by weighted least squares against whatever the network already knows. The
least squares is for the cycles: three models aligned pairwise will not agree
to the millisecond, and the closing error is distributed rather than decided
by whichever edge was walked last.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("modules.media.cast_sync")

# Model names are free text from mDNS and LinkPlay, so the only delimiter that
# cannot collide is one that cannot appear in a name.
SEP = "\x1f"

MAX_SAMPLES = 7

# An edge carries hardware latency *and* the difference in where the two
# speakers stand (a metre is 3 ms). Repeated observations that scatter this far
# are measuring placement, which does not transfer to another device. Matches
# TRIM_MODEL_AGREE_MS, the same test applied to two units of one model.
EDGE_AGREE_MS = 25

# Ratios are the policy: measured here outranks shipped by an order of
# magnitude, an edge sits between, and the ridge only keeps a component with no
# absolute at all from being singular.
W_ANCHOR = 10.0     # trim learned for this model on this network
W_EDGE = 3.0
W_PRIOR = 1.0       # shipped table value
W_RIDGE = 0.01

CONF_UNCORROBORATED = 0.5    # one observation is usable, not yet evidence


def edge_key(model_a: str, model_b: str) -> Tuple[str, str, int]:
    """Canonical (key, first model, sign). The stored delta is always
    ``trim(first) - trim(second)`` sorted, so either direction lands on the
    same edge."""
    if model_a <= model_b:
        return f"{model_a}{SEP}{model_b}", model_a, 1
    return f"{model_b}{SEP}{model_a}", model_b, -1


class TrimGraph:
    """Observed inter-model trim differences, and the absolutes they imply.

    ``read``/``write`` are the engine's atomic JSON pair, so tests can hold the
    graph in memory. Solving is lazy and cached; ``invalidate`` is required
    when an anchor moves, since the graph is then unchanged but its pinning is
    not.
    """

    def __init__(self, read, write):
        self._read = read
        self._write = write
        self._edges: Dict[str, List[int]] = {}
        edges = (read() or {}).get("edges")
        if isinstance(edges, dict):
            for k, v in edges.items():
                samples = v.get("samples") if isinstance(v, dict) else v
                if isinstance(samples, list) and samples:
                    try:
                        self._edges[k] = [int(s) for s in samples][-MAX_SAMPLES:]
                    except (TypeError, ValueError):
                        continue
        self._solved: Optional[Dict[str, int]] = None
        self._solved_for: Optional[tuple] = None

    # Observation
    def observe(self, model_a: str, trim_a: int, model_b: str,
                trim_b: int) -> bool:
        """Record one device of ``model_a`` at ``trim_a`` beside one of
        ``model_b`` at ``trim_b``. True if the graph changed."""
        if not model_a or not model_b or model_a == model_b:
            return False
        key, _, sign = edge_key(model_a, model_b)
        delta = int(round((trim_a - trim_b) * sign))
        samples = self._edges.setdefault(key, [])
        if samples and samples[-1] == delta:
            return False          # same pair, same answer: nothing new
        samples.append(delta)
        del samples[:-MAX_SAMPLES]
        self._solved = None
        self._persist()
        a, b = key.split(SEP)
        spread = max(samples) - min(samples)
        logger.info(
            f"Trim edge {a} → {b}: {delta:+d} ms ({len(samples)} obs"
            + (f", spread {spread} ms — placement, not hardware"
               if spread > EDGE_AGREE_MS else "") + ")")
        return True

    def observe_session(self, trims: List[Tuple[str, int]]) -> None:
        """Record every cross-model pair among one zone's ``(model, trim)``.

        Explicit trims only: an effective trim the graph itself supplied would
        feed its own output back in and harden a guess into evidence.
        """
        rows = [(m, t) for m, t in trims if m]
        for i, (ma, ta) in enumerate(rows):
            for mb, tb in rows[i + 1:]:
                self.observe(ma, ta, mb, tb)

    # Solving
    def _trusted(self) -> Dict[str, Tuple[float, float]]:
        """Edges surviving the agreement test, as key -> (delta, weight)."""
        out = {}
        for key, samples in self._edges.items():
            if len(samples) >= 2 and max(samples) - min(samples) > EDGE_AGREE_MS:
                continue
            conf = CONF_UNCORROBORATED if len(samples) == 1 else 1.0
            out[key] = (float(np.median(samples)), W_EDGE * conf)
        return out

    def solve(self, anchors: Dict[str, int],
              priors: Dict[str, int]) -> Dict[str, int]:
        """Absolute trims implied by the graph, for models that have none.

        Anchors and priors enter as soft rows, not constraints, so a model with
        a weak prior and strong edges is pulled toward the edges — the reason
        for solving rather than walking paths from an anchor.

        Only models reachable from some absolute through trusted edges are
        returned: a component holding no absolute has a shape but no position,
        and answering 0 for it would be a fabricated number.
        """
        # Keyed on the inputs, not just the graph: the same edges pinned to
        # different absolutes are a different answer.
        key = (tuple(sorted(anchors.items())), tuple(sorted(priors.items())))
        if self._solved is not None and self._solved_for == key:
            return self._solved
        self._solved_for = key
        trusted = self._trusted()
        nodes = sorted({m for key in trusted for m in key.split(SEP)}
                       | set(anchors) | set(priors))
        if not trusted or not nodes:
            self._solved = {}
            return self._solved
        idx = {m: i for i, m in enumerate(nodes)}
        rows, rhs, weights = [], [], []

        def add(row, value, weight):
            rows.append(row)
            rhs.append(value)
            weights.append(weight)

        for key, (delta, w) in trusted.items():
            a, b = key.split(SEP)
            row = np.zeros(len(nodes))
            row[idx[a]], row[idx[b]] = 1.0, -1.0
            add(row, delta, w)
        for source, weight in ((anchors, W_ANCHOR), (priors, W_PRIOR)):
            for m, v in source.items():
                row = np.zeros(len(nodes))
                row[idx[m]] = 1.0
                add(row, float(v), weight)
        for m in nodes:
            row = np.zeros(len(nodes))
            row[idx[m]] = 1.0
            add(row, 0.0, W_RIDGE)

        w = np.asarray(weights)[:, None]
        try:
            x, *_ = np.linalg.lstsq(np.vstack(rows) * w,
                                    np.asarray(rhs) * w[:, 0], rcond=None)
        except np.linalg.LinAlgError as e:
            logger.warning(f"Trim graph did not solve: {e}")
            self._solved = {}
            return self._solved

        grounded = self._grounded(trusted, set(anchors) | set(priors))
        self._solved = {m: int(round(x[idx[m]])) for m in nodes
                        if m in grounded and m not in anchors}
        return self._solved

    @staticmethod
    def _grounded(trusted: Dict[str, Tuple[float, float]],
                  absolutes: set) -> set:
        """Models connected to some absolute through trusted edges."""
        adj: Dict[str, List[str]] = {}
        for key in trusted:
            a, b = key.split(SEP)
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        seen, stack = set(absolutes), list(absolutes)
        while stack:
            for nxt in adj.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def invalidate(self) -> None:
        """Only for a change the cache key cannot see; anchors it handles."""
        self._solved = None
        self._solved_for = None

    def models(self) -> set:
        return {m for key in self._edges for m in key.split(SEP)}

    # Inspection / persistence
    def describe(self) -> List[dict]:
        out = []
        for key, samples in sorted(self._edges.items()):
            a, b = key.split(SEP)
            spread = max(samples) - min(samples)
            out.append({
                "from": a, "to": b,
                "delta_ms": int(round(float(np.median(samples)))),
                "observations": len(samples),
                "spread_ms": spread,
                "trusted": not (len(samples) >= 2 and spread > EDGE_AGREE_MS),
            })
        return out

    def _persist(self) -> None:
        self._write({"version": 1,
                     "edges": {k: {"samples": v}
                               for k, v in self._edges.items()}})
