"""
The raw report dispatcher hands a handler the attributes zigpy has no
definition for, whichever header the frame carries.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from harness import Checker

from core.service import ZigbeeService

IEEE = "54:ef:44:10:01:65:58:b3"


def _dispatch(cluster, frame: bytes):
    seen = []
    handler = NS(attribute_updated=lambda attr, value: seen.append((attr, value)))
    dev = NS(handlers={(1, cluster): handler}, update_last_seen=lambda: None)
    svc = NS(devices={IEEE: dev}, fast_path=NS(process_frame=lambda *a: False),
             _parse_zcl_value=ZigbeeService._parse_zcl_value)
    ZigbeeService.handle_message(svc, NS(ieee=IEEE, nwk=0x062A), 0x0104, cluster, 1, 1, frame)
    return seen


def run() -> Checker:
    c = Checker("report_dispatch")

    c.section("attribute reports reach the handler by their own id")
    got = _dispatch(0x0402, bytes.fromhex("18210a0000297708"))
    c.check("a standard report: temperature 0x0000 = 2167", got == [(0x0000, 2167)], got)
    got = _dispatch(0xFCC0, bytes.fromhex("1c5f11180a42012001"))
    c.check("a maker's report is read past its manufacturer code: 0x0142 = 1, "
            "not the sequence number as an attribute", got == [(0x0142, 1)], got)
    got = _dispatch(0xFCC0, bytes.fromhex("1c5f11190a420120005201 2000".replace(" ", "")))
    c.check("and every attribute in it", got == [(0x0142, 0), (0x0152, 0)], got)
    got = _dispatch(0xFCC0, bytes.fromhex("1c5f111a0142012001"))
    c.check("a frame that is not a report is left to zigpy", got == [], got)
    return c


if __name__ == "__main__":
    run()
