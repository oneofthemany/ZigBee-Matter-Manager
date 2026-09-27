#!/usr/bin/env python3
"""
Dry run of modules/endpoint_kind.py over a real network. Read-only.

Feed it the JSON from GET /api/devices of the *running* app (live endpoints,
after quirks), saved from a logged-in browser tab:

    python3 scripts/classify_dryrun.py devices.json

Prints every On/Off endpoint with the HA component the old rule gave and the
one the classifier gives now, changes first. Profile overrides are not
applied: the file carries none.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.endpoint_kind import classify  # noqa: E402


def _int(v):
    try:
        return int(v, 16) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        return None


def old_rule(ids: set) -> str:
    """handlers/general.py before the classifier, light/switch step only."""
    level, color, link = 0x0008 in ids, 0x0300 in ids, 0x1000 in ids
    forced = (0x0B04 in ids and level) or 0x0012 in ids or 0xFC11 in ids
    if forced and not (color or link):
        return "switch"
    return "light" if (link or 0xFCC0 in ids or color or level) else "switch"


def main(path: str) -> int:
    devices = json.loads(Path(path).read_text())
    rows = []
    for d in devices:
        for ep in d.get("capabilities") or []:
            ids = {c["id"] for c in ep.get("inputs") or []}
            kind = classify(ids, _int(ep.get("profile_id")), _int(ep.get("device_type")))
            if not kind:
                continue
            old = old_rule(ids)
            rows.append((old != kind.kind, d.get("friendly_name") or d.get("ieee"),
                         d.get("model"), ep["id"], ep.get("device_type"),
                         old, kind.kind, kind.reason,
                         ",".join(f"{c:04X}" for c in sorted(ids))))

    rows.sort(key=lambda r: (not r[0], str(r[1]), r[3]))
    changed = sum(r[0] for r in rows)
    print(f"{len(rows)} On/Off endpoints, {changed} change HA component\n")
    print(f"{'':2}{'device':28} {'model':22} {'EP':>3} {'type':>6}  {'old':6} {'new':6} reason")
    for ch, name, model, ep, dt, old, new, reason, clusters in rows:
        mark = "* " if ch else "  "
        print(f"{mark}{str(name)[:28]:28} {str(model)[:22]:22} {ep:>3} {str(dt):>6}  "
              f"{old:6} {new:6} {reason}")
        if ch:
            print(f"{'':4}in: {clusters}")
    print("\nThe Control tab showed every EP as a switch before; its new value is 'new'.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
