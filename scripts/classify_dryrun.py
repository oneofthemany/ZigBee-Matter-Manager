#!/usr/bin/env python3
"""
Dry run of modules/endpoint_kind.py over a real network. Read-only.

Runs inside the app container, reading live endpoints (after quirks) from the
running app's /api/devices. Needs an API token (Settings -> tokens):

    podman exec -e ZMM_TOKEN=<token> zigbee-matter-manager \
        python3 scripts/classify_dryrun.py

Against an image that predates the classifier, copy both files in first and
run the copy (it loads endpoint_kind.py from beside itself):

    podman cp modules/endpoint_kind.py zigbee-matter-manager:/tmp/
    podman cp scripts/classify_dryrun.py zigbee-matter-manager:/tmp/
    podman exec -e ZMM_TOKEN=<token> zigbee-matter-manager python3 /tmp/classify_dryrun.py

A saved /api/devices JSON can be passed as the only argument instead.
Profile overrides are not applied: the API does not carry them.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    sys.path.insert(0, str(HERE.parent))
    from modules.endpoint_kind import classify
except ImportError:
    sys.path.insert(0, str(HERE))
    from endpoint_kind import classify

DEFAULT_URL = "https://127.0.0.1:8000/api/devices"


def _fetch(url: str, token: str):
    # The app's certificate is self-signed; this only ever talks to localhost.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)


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


def main(argv) -> int:
    if len(argv) > 1:
        devices = json.loads(Path(argv[1]).read_text())
    else:
        token = os.environ.get("ZMM_TOKEN")
        if not token:
            sys.exit("Set ZMM_TOKEN to an API token, or pass a saved /api/devices JSON.")
        devices = _fetch(os.environ.get("ZMM_URL", DEFAULT_URL), token)
    rows = []
    for d in devices:
        for ep in d.get("capabilities") or []:
            if not isinstance(ep, dict) or "inputs" not in ep:
                continue    # Matter / Wi-Fi entries carry no Zigbee endpoints
            ids = {c["id"] for c in ep["inputs"] or []}
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
    if len(sys.argv) > 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv))
