"""
docs/mesh-health.md §1-2: every trust-center join verdict is on record,
refusals included; the coordinator keeps a silent sleeping child for days,
not 256 minutes; rotated logs stay readable.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

from modules import join_audit

PARENT = NS(ieee="00:12:4b:00:aa:bb:cc:dd")


class _App:
    """The bellows application surface the audit touches."""

    def __init__(self):
        self.seen = []

    def _handle_tc_join_handler(self, nwk, ieee, device_update_status, decision, parent_nwk):
        self.seen.append((nwk, str(ieee), device_update_status, decision, parent_nwk))

    def get_device(self, nwk=None):
        if nwk == 0xA1FA:
            return PARENT
        raise KeyError(nwk)


def _service():
    out = []
    return NS(friendly_names={"54:ef:44:10:01:65:58:b3": "Motion - Hallway",
                              str(PARENT.ieee): "Socket - Hallway"},
              _emit_sync=lambda kind, data: out.append((kind, data)), out=out)


def _bellows(c: Checker):
    c.section("against bellows itself")
    try:
        from bellows.zigbee.application import ControllerApplication
        from bellows.ezsp.v8.config import EZSP_SCHEMA
        import bellows.types as t
    except ImportError:
        return None
    params = list(inspect.signature(ControllerApplication._handle_tc_join_handler).parameters)
    c.check("the callback the audit wraps takes the arguments it passes on",
            params == ["self", "nwk", "ieee", "device_update_status", "decision", "parent_nwk"], params)
    src = inspect.getsource(ControllerApplication)
    c.check("bellows calls it through the instance, so the wrap sees every event",
            "self._handle_tc_join_handler(" in src)
    c.check("and drops a denied join itself, which is why it is audited first",
            "DENY_JOIN" in inspect.getsource(ControllerApplication._handle_tc_join_handler))
    import voluptuous as vol
    ok = vol.Schema(EZSP_SCHEMA)({"CONFIG_END_DEVICE_POLL_TIMEOUT": 14})
    c.check("the radio schema takes the longest child timeout", ok["CONFIG_END_DEVICE_POLL_TIMEOUT"] == 14, ok)
    return t


def run() -> Checker:
    c = Checker("join_audit")
    t = _bellows(c)
    U = t.EmberDeviceUpdate if t else NS(STANDARD_SECURITY_UNSECURED_REJOIN="STANDARD_SECURITY_UNSECURED_REJOIN",
                                         STANDARD_SECURITY_SECURED_REJOIN="STANDARD_SECURITY_SECURED_REJOIN",
                                         DEVICE_LEFT="DEVICE_LEFT")
    D = t.EmberJoinDecision if t else NS(DENY_JOIN="DENY_JOIN", USE_PRECONFIGURED_KEY="USE_PRECONFIGURED_KEY")

    c.section("h3: every join verdict is logged, refusals included")
    app, svc = _App(), _service()
    c.check("installed once", join_audit.install(app, svc) and not join_audit.install(app, svc))
    app._handle_tc_join_handler(0xB1A7, "54:ef:44:10:01:65:58:b3", U.STANDARD_SECURITY_UNSECURED_REJOIN,
                                D.DENY_JOIN, 0xA1FA)
    kind, data = svc.out[-1]
    c.check("a refused rejoin reaches the logbook as a warning, naming the device and its parent",
            kind == "log" and data["level"] == "WARNING" and data["category"] == "connection"
            and "Motion - Hallway" in data["message"] and "Socket - Hallway" in data["message"]
            and "REFUSED" in data["message"] and data["join"]["kind"] == "unsecured rejoin", data)
    c.check("and bellows still gets the event, once", len(app.seen) == 1, app.seen)

    app._handle_tc_join_handler(0xB1A7, "54:ef:44:10:01:65:58:b3", U.STANDARD_SECURITY_SECURED_REJOIN,
                                D.USE_PRECONFIGURED_KEY, 0xA1FA)
    data = svc.out[-1][1]
    c.check("a rejoin through a new parent is recorded as accepted",
            data["level"] == "INFO" and data["join"]["kind"] == "secured rejoin" and not data["join"]["denied"]
            and "accepted" in data["message"], data)
    app._handle_tc_join_handler(0xB1A7, "54:ef:44:10:01:65:58:b3", U.DEVICE_LEFT, D.USE_PRECONFIGURED_KEY, 0x0000)
    data = svc.out[-1][1]
    c.check("a leave says so, with no verdict, and an unknown parent by its address",
            data["join"]["kind"] == "leave" and data["join"]["decision"] is None
            and "via 0x0000" in data["message"], data)

    broken = NS(friendly_names={}, _emit_sync=lambda *a: (_ for _ in ()).throw(RuntimeError("bus down")))
    app2 = _App()
    join_audit.install(app2, broken)
    app2._handle_tc_join_handler(1, "aa:bb", U.DEVICE_LEFT, D.DENY_JOIN, 0)
    c.check("a failing logbook never stops the event reaching bellows", len(app2.seen) == 1)
    c.check("a radio library without the callback is left alone", join_audit.install(NS(), svc) is False)

    c.section("the coordinator keeps a silent sleeping child for days")
    from core.config_builder import ConfigBuilderMixin
    stub = NS(port="socket://radio:6638", _config={})
    stub._is_socket_path = lambda: True
    stub._build_network_conf = lambda key: {}
    conf = ConfigBuilderMixin._build_ezsp_config(stub, {}, None)
    c.check("the child timeout is the radio's longest, not bellows' 256 min",
            conf["ezsp_config"].get("CONFIG_END_DEVICE_POLL_TIMEOUT") == 14, conf["ezsp_config"])
    conf = ConfigBuilderMixin._build_ezsp_config(stub, {"CONFIG_END_DEVICE_POLL_TIMEOUT": 10}, None)
    c.check("a value set in config still wins", conf["ezsp_config"]["CONFIG_END_DEVICE_POLL_TIMEOUT"] == 10)

    c.section("h4: rotated logs stay readable from ZMM Manager")
    try:
        from manager import logs
    except ImportError:
        return c
    tmp = Path(tempfile.mkdtemp(prefix="zmm_logs_"))
    for n in ("zigbee.log", "zigbee.log.1", "zigbee.log.5", "notes.txt", "zigbee.log.bak"):
        (tmp / n).write_text("x")
    keep, logs.LOG_DIR = logs.LOG_DIR, tmp
    try:
        names = [f["name"] for f in logs.list_log_files()]
        c.check("the log and its rotations are listed, nothing else",
                names == ["zigbee.log", "zigbee.log.1", "zigbee.log.5"], names)
        c.check("a rotation can be streamed", logs.resolve_log_file("zigbee.log.1") == tmp / "zigbee.log.1")
        c.check("a path or another file cannot",
                logs.resolve_log_file("../zigbee.log.1") is None and logs.resolve_log_file("zigbee.log.bak") is None)
    finally:
        logs.LOG_DIR = keep
    return c


if __name__ == "__main__":
    raise SystemExit(1 if run().failures else 0)
