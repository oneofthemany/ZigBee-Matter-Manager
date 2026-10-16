"""
A maintenance action, observed (modules/device_ops, docs/device-ops.md): the
verdict comes from what the device answered to each step, and its own log
lines stream while the action runs.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS

from harness import Checker

from modules import device_ops
from modules import device_profiles_apply as apply
from modules.device_profiles import normalise_profile

IEEE = "54:ef:44:10:01:65:4d:91"
OTHER = "54:ef:44:10:01:65:58:b3"

PROFILE = normalise_profile({
    "id": "x", "match": {"model": "x"},
    "reporting": [{"ep": 1, "cluster": "0xFCC0", "attr": a, "min": 0, "max": 3600, "delta": 1,
                   "mfr": "0x115F", "type": "uint8"} for a in ("0x0142", "0x014D")],
})


class _Status(int):
    name = "INSUFFICIENT_SPACE"


class _Refusing:
    """A maker's cluster that binds but refuses every reporting request, as the FP300 does."""
    cluster_id = 0xFCC0

    def find_attribute(self, aid):
        raise KeyError(aid)

    async def bind(self):
        return [0]

    async def configure_reporting_multiple(self, config):
        return [NS(status=_Status(0x89), attrid=int(a.id)) for a in config]


class _Asleep:
    """A cluster on a device that never answers."""
    cluster_id = 0x0405
    endpoint = NS(endpoint_id=1)

    def add_listener(self, _l):
        pass

    async def bind(self):
        await asyncio.sleep(3600)


def _run(work):
    events = []
    rep = asyncio.run(device_ops.run(IEEE, "reconfigure", work, lambda e, d: events.append((e, d))))
    return rep, events


def run() -> Checker:
    c = Checker("device_ops")
    log = logging.getLogger("tests.device_ops")
    # An earlier module may leave logging disabled; the tap is what is under test.
    disabled, level = logging.root.manager.disable, logging.getLogger().level
    logging.disable(logging.NOTSET)
    logging.getLogger().setLevel(logging.INFO)
    try:
        return _checks(c, log)
    finally:
        logging.disable(disabled)
        logging.getLogger().setLevel(level)


def _checks(c: Checker, log: logging.Logger) -> Checker:

    c.section("the verdict")
    async def fine():
        device_ops.step("Bind 0x0402", True)
        device_ops.step("Reporting 0x0402/measured_value", True)
    rep, _ = _run(fine)
    c.check("every step taken reads as confirmed", rep["verdict"] == "ok" and rep["passed"] == 2, rep)

    async def mixed():
        device_ops.step("Bind 0x0402", True)
        device_ops.step("Bind 0x0405", False, "timeout")
    rep, _ = _run(mixed)
    c.check("one step the device did not take makes it partial, not a success",
            rep["verdict"] == "partial" and rep["failed"] == 1, rep)
    c.check("and says what to do: wake it",
            [a["reason"] for a in rep["advice"]] == ["timeout"] and "Wake it" in rep["advice"][0]["text"], rep)

    async def silent():
        return {"success": True}
    rep, _ = _run(silent)
    c.check("an action with no answers is not called a success", rep["verdict"] == "unchecked", rep)

    async def broken():
        raise RuntimeError("NCP down")
    rep, _ = _run(broken)
    c.check("an action that raised has failed, with the reason",
            rep["verdict"] == "failed" and not rep["success"] and "NCP down" in rep["error"], rep)

    c.section("the live log")
    async def chatty():
        log.info(f"[{IEEE}] Configuring 0x0402...")
        log.info(f"[{OTHER}] battery=100")
        log.debug(f"[{IEEE}] raw frame")
    rep, events = _run(chatty)
    lines = [d["line"]["message"] for e, d in events if d.get("phase") == "line"]
    c.check("the device's own lines stream, without its address", lines == ["Configuring 0x0402..."], lines)
    c.check("and the run is bracketed by a start and a done carrying the verdict",
            [d["phase"] for _, d in events][0] == "start" and events[-1][1]["phase"] == "done"
            and events[-1][1]["verdict"] == rep["verdict"], events)
    c.check("the tap is gone afterwards", not any(isinstance(h, device_ops._LineTap)
                                                  for h in logging.getLogger().handlers))
    device_ops.step("outside any action", False, "timeout")     # must be a no-op

    c.section("an FP300 refusing its presence reporting")
    dev = NS(ieee=IEEE, zigpy_dev=NS(endpoints={1: NS(in_clusters={0xFCC0: _Refusing()})}))
    rep, _ = _run(lambda: apply.apply_reporting(dev, PROFILE))
    bad = [s for s in rep["steps"] if not s["ok"]]
    c.check("the bind it took is confirmed, the two refusals are not hidden",
            rep["verdict"] == "partial" and [s["what"] for s in bad]
            == ["Reporting 0xFCC0/0x0142", "Reporting 0xFCC0/0x014D"], rep["steps"])
    c.check("each refusal names the device's status",
            {s["reason"] for s in bad} == {"INSUFFICIENT_SPACE"}, bad)
    text = " ".join(a["text"] for a in rep["advice"])
    c.check("the advice reads the refusal as an answer to our request and says how to check, never 're-pair'",
            "would not store this reporting setup" in text and "arrives on its own" in text
            and "pair" not in text.lower(), rep["advice"])

    # The kitchen's own presence report: Aqara's code, Report Attributes, 0x0142 = 1.
    from modules import device_observer
    device_observer.observe(IEEE, 0x0104, 0xFCC0, 1, bytes.fromhex("1c5f11010a42012001"))
    rep, _ = _run(lambda: apply.apply_reporting(dev, PROFILE))
    by = {s["what"]: s for s in rep["steps"]}
    c.check("a refusal of a reading the device reports anyway is declined, not failed",
            by["Reporting 0xFCC0/0x0142"]["outcome"] == "declined"
            and "s ago" in by["Reporting 0xFCC0/0x0142"]["reports_anyway"], by)
    c.check("one it has never reported still fails, so the action is partial",
            by["Reporting 0xFCC0/0x014D"]["outcome"] == "failed" and rep["verdict"] == "partial", rep)
    device_observer.observe(IEEE, 0x0104, 0xFCC0, 1, bytes.fromhex("1c5f11020a4d012001"))
    rep, _ = _run(lambda: apply.apply_reporting(dev, PROFILE))
    c.check("once both report on their own, setup is fine as it stands",
            rep["verdict"] == "ok" and rep["declined"] == 2 and rep["failed"] == 0, rep)
    device_observer.forget(IEEE)

    c.section("a sleeping device's cluster")
    from handlers.base import ClusterHandler
    h = ClusterHandler(NS(ieee=IEEE), _Asleep())
    rep, _ = _run(h.configure)
    c.check("a bind with no answer is a timed-out step, so the action is not green",
            rep["verdict"] == "failed" and rep["steps"][0]["reason"] == "timeout", rep)
    return c


if __name__ == "__main__":
    run()
