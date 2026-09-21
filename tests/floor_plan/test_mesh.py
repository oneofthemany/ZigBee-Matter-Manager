"""
Mesh-on-the-plan tests — one link per pair, with both ends' view of it.

    python3 tests/floor_plan/test_mesh.py

A neighbour table lists a link from each end that reports it, with its own
LQI and its own idea of the relationship. The plan draws one line per pair,
coloured by the worse direction, so the merge must not lose either side.
"""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

from modules.mesh_plan import lqi_band, merge_links  # noqa: E402

MESH = {
    "nodes": [
        {"id": "00:0C", "friendly_name": "Coordinator", "role": "Coordinator", "online": True},
        {"id": "00:0b", "friendly_name": "Hall Plug", "role": "Router", "online": True},
        {"id": "00:0a", "friendly_name": "Garage Sensor", "role": "EndDevice", "online": True},
        {"id": "00:0d", "friendly_name": "Dead Bulb", "role": "Router", "online": False},
    ],
    "links": [
        {"source": "00:0C", "target": "00:0b", "lqi": 230, "relationship": 1},
        {"source": "00:0b", "target": "00:0C", "lqi": 160, "relationship": 0},
        {"source": "00:0b", "target": "00:0a", "lqi": 62, "relationship": 1},
        {"source": "00:0b", "target": "00:0d", "lqi": 120, "relationship": 2},
        {"source": "00:0b", "target": "ff:ff", "lqi": 40},            # unknown device
        {"source": "00:0b", "target": "00:0b", "lqi": 255},           # itself
    ],
}


def run() -> Checker:
    c = Checker("test_mesh")
    out = merge_links(MESH)
    links = {(l["a"], l["b"]): l for l in out["links"]}

    c.section("one link per pair")
    c.check("the coordinator and the plug are one link, not two",
            list(links).count(("00:0b", "00:0c")) == 1 and len(links) == 3, list(links))
    both = links[("00:0b", "00:0c")]
    c.check("with each end's LQI kept",
            both["lqi_ab"] == 160 and both["lqi_ba"] == 230, both)
    c.check("coloured by the worse direction", both["lqi"] == 160 and both["band"] == "ok", both)
    c.check("and each end's relationship, named",
            both["rel_ab"] == "parent" and both["rel_ba"] == "child", both)
    one_sided = links[("00:0a", "00:0b")]
    c.check("an end device that reports nothing still has its link",
            one_sided["lqi_ba"] == 62 and one_sided["lqi_ab"] is None and one_sided["lqi"] == 62)
    c.check("62 is bad", one_sided["band"] == "bad")

    c.section("what is left out, and what is marked")
    c.check("a link to a device the hub doesn't know is dropped",
            not any("ff:ff" in k for k in links))
    c.check("a device's link to itself is dropped", ("00:0b", "00:0b") not in links)
    c.check("a link to an offline device is marked offline",
            links[("00:0b", "00:0d")]["online"] is False and both["online"] is True)
    c.check("ieees are lowercased to match the plan", "00:0c" in out["nodes"])
    c.check("nodes carry name, role and whether they are online",
            out["nodes"]["00:0c"] == {"ieee": "00:0c", "name": "Coordinator", "role": "Coordinator",
                                     "online": True, "lqi": None, "rssi": None}, out["nodes"]["00:0c"])
    c.check("nothing in, nothing out", merge_links(None) == {"nodes": {}, "links": []})

    c.section("bands match the Topology graph's colours")
    c.check("200 / 150 / 100 boundaries",
            [lqi_band(v) for v in (255, 200, 199, 150, 149, 100, 99, None)]
            == ["good", "good", "ok", "ok", "weak", "weak", "bad", "unknown"])
    return c


if __name__ == "__main__":
    result = run()
    print(f"\n{result.passed} passed, {len(result.failures)} failed")
    sys.exit(1 if result.failures else 0)
