"""
The classifier against every endpoint zhaquirks describes.

Quirk replacement signatures are the largest curated record of real devices
we have. Named models pin the cases that decided the rule order
(docs/endpoint-classification.md); the sweeps hold the invariants across all
of them, so a new rule that misfiles a family fails here first.
"""

from __future__ import annotations

import io
import logging
from contextlib import redirect_stderr

from harness import Checker

from modules.endpoint_kind import LIGHT, SWITCH, classify

# model -> expected kind for its On/Off EPs
KNOWN = {
    "DM2500ZB":              LIGHT,   # Sinopé wall dimmer: level + metering, 0x0101
    "VZM31-SN":              LIGHT,   # Inovelli dimmer: level + metering, 0x0101
    "lumi.light.acn014":     LIGHT,   # Aqara T1 bulb: colour + metering + multistate
    "SP 240":                SWITCH,  # Innr plug: level + metering, 0x010A
    "SP 120":                SWITCH,  # Innr plug: ZLL plug-in unit
    "TS0001":                SWITCH,  # Tuya relay: on/off only, reports On/Off Light
    "lumi.relay.c2acn01":    SWITCH,  # Aqara dual relay: on/off only, reports Dimmable Light
    "lumi.switch.n1aeu1":    SWITCH,  # Aqara H1 wall switch
    "TRADFRI control outlet": SWITCH,
}


def _quirk_endpoints():
    logging.disable(logging.WARNING)
    with redirect_stderr(io.StringIO()):
        import zhaquirks
        zhaquirks.setup()
        import zigpy.quirks as zq
    for models in zq.DEVICE_REGISTRY.registry_v1.values():
        for model, quirks in models.items():
            for q in quirks:
                rep = getattr(q, "replacement", None) or {}
                for ep in (rep.get("endpoints") or {}).values():
                    ids = {c if isinstance(c, int) else c.cluster_id
                           for c in ep.get("input_clusters") or []}
                    yield str(model), ids, ep.get("profile_id"), ep.get("device_type")


def run() -> Checker:
    c = Checker("quirk_corpus")
    eps = list(_quirk_endpoints())
    c.check("the corpus loaded", len(eps) > 500, len(eps))

    c.section("the devices that decided the rule order")
    for model, want in KNOWN.items():
        got = {classify(ids, prof, dt).kind for m, ids, prof, dt in eps
               if m == model and classify(ids, prof, dt)}
        c.check(f"{model} is a {want}", got == {want}, got)

    c.section("invariants across every quirk")
    bad = sorted({m for m, ids, prof, dt in eps
                  if 0x0006 in ids and ids.isdisjoint({0x0008, 0x0300, 0x1000})
                  and classify(ids, prof, dt).kind == LIGHT})
    c.check("nothing without level, colour or touchlink is a light", not bad, bad[:10])
    bad = sorted({m for m, ids, prof, dt in eps
                  if 0x0006 in ids and 0x0300 in ids
                  and classify(ids, prof, dt).kind != LIGHT})
    c.check("everything with colour control is a light", not bad, bad[:10])
    return c


if __name__ == "__main__":
    run()
