"""manager/images.py against a stand-in runtime: inventory, clean-up plan, deletion, container detail."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
import time
from pathlib import Path

from harness import Checker, FakeRuntime, container, image

DAY = 86400
ZMM = "localhost/zigbee-matter-manager:{}-amd64"


def run() -> Checker:
    c = Checker("images")
    with tempfile.TemporaryDirectory() as data:
        rt = FakeRuntime()
        os.environ["ZMM_CONTAINER_SOCK"] = rt.sock
        os.environ["ZMM_DATA_DIR"] = data
        import manager.containers, manager.upgrade, manager.images
        importlib.reload(manager.containers)
        upgrade = importlib.reload(manager.upgrade)
        images = importlib.reload(manager.images)
        state_dir = Path(upgrade.VERSION_STATE_FILE).parent
        state_dir.mkdir(parents=True, exist_ok=True)
        Path(upgrade.VERSION_STATE_FILE).write_text(json.dumps({
            "current_version": "04.10.2026", "retention_count": 2,
            "previous_image_tag": ZMM.format("28.09.2026")}))
        now = int(time.time())
        rt.images = [
            image("a1", [ZMM.format("04.10.2026")], now - 1 * DAY),       # running
            image("a2", [ZMM.format("09.2026")], now - 3 * DAY),          # newest-2 kept by retention
            image("a3", [ZMM.format("28.09.2026")], now - 6 * DAY),       # rollback image
            image("a4", [ZMM.format("27.09.2026")], now - 7 * DAY),       # beyond retention -> offered
            image("a5", [ZMM.format("24.01.07.2026")], now - 90 * DAY),   # used by beekeeper -> kept
            image("d1", [], now - 10 * DAY, size=500_000_000),            # old untagged -> offered
            image("d2", [], now - 3600),                                  # young untagged -> kept
            image("o1", ["docker.io/ollama/ollama:latest"], now - 20 * DAY),             # in use
            image("o2", ["docker.io/library/alpine:3.20", "alpine:latest"], now - 30 * DAY),  # unused other
        ]
        rt.containers = [container("zigbee-matter-manager", "a1"),
                         container("zigbee-matter-manager-manager", "a1"),
                         container("zigbee-matter-manager-beekeeper", "a5"),
                         container("ollama", "o1")]
        run_ = lambda coro: asyncio.run(coro)

        c.section("inventory")
        inv = run_(images.inventory())
        by = {i["short_id"][:2]: i for i in inv["images"]}
        c.check("lists every image, newest first", [i["short_id"][:2] for i in inv["images"]][:2] == ["d2", "a1"], list(by))
        c.check("tells ZMM versions, untagged and other images apart",
                (by["a1"]["kind"], by["d1"]["kind"], by["o2"]["kind"]) == ("zmm", "dangling", "other"))
        c.check("marks the running version", by["a1"]["current"] and not by["a2"]["current"])
        c.check("says which containers use each image",
                by["a1"]["used_by"] == ["zigbee-matter-manager", "zigbee-matter-manager-manager"]
                and by["a5"]["used_by"] == ["zigbee-matter-manager-beekeeper"], by["a1"]["used_by"])

        c.section("clean-up plan")
        plan = run_(images.cleanup_plan())
        offered = {x["short_id"][:2]: x for x in plan["candidates"]}
        kept = {x["short_id"][:2]: x["reason"] for x in plan["kept"]}
        c.check("offers the ZMM version beyond retention and the old untagged leftover, ticked",
                offered.get("a4", {}).get("default") and offered.get("d1", {}).get("default"), offered)
        c.check("offers the unused other image unticked", offered.get("o2", {}).get("default") is False, offered.get("o2"))
        c.check("keeps the running version, the newest versions and the rollback image",
                all(k in kept for k in ("a1", "a2", "a3")), kept)
        c.check("keeps an old version a sidecar still uses (do_gc would only fail on it)", "used by" in kept.get("a5", ""), kept.get("a5"))
        c.check("keeps an untagged layer under 48h old", "48h" in kept.get("d2", ""), kept.get("d2"))
        c.check("keeps anything a container uses", "o1" in kept)
        c.check("the ticked total is the ticked images' sizes", plan["default_bytes"] == 100_000_000 + 500_000_000, plan["default_bytes"])

        c.section("deletion")
        res = run_(images.cleanup([offered["a4"]["id"], offered["o2"]["id"], by["a1"]["id"], "sha256:made-up"]))
        c.check("removes what the plan offers", res["removed"] == 2, res)
        c.check("an image with two names is removed name by name, never forced",
                rt.deleted == [ZMM.format("27.09.2026"), "docker.io/library/alpine:3.20", "alpine:latest"], rt.deleted)
        c.check("refuses the running image and an unknown id, removing nothing for them",
                [r["error"] for r in res["results"] if not r["ok"]] == ["not in the current clean-up plan"] * 2, res["results"])
        rt.refuse_delete[offered["d1"]["id"]] = 409
        res = run_(images.cleanup([offered["d1"]["id"]]))
        c.check("an image the runtime won't remove is reported, not hidden",
                res["failed"] == 1 and "409" in res["results"][0]["error"], res)

        c.section("container detail")
        rt.inspect["zigbee-matter-manager"] = {
            "Image": "sha256:" + "a1".ljust(64, "0"), "Created": "2026-10-04T10:00:00Z", "RestartCount": 0,
            "Config": {"Image": ZMM.format("04.10.2026"), "Cmd": ["python", "main.py"],
                       "Env": ["TZ=Europe/London", "ZMM_API_TOKEN=s3cret", "MQTT_PASSWORD=hunter2"]},
            "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "always"},
                           "Devices": [{"PathOnHost": "/dev/ttyACM0", "PathInContainer": "/dev/ttyACM0"}]},
            "State": {"Status": "running", "Running": True, "StartedAt": "2026-10-04T10:00:05Z",
                      "Health": {"Status": "healthy"}},
            "Mounts": [{"Source": "/opt/.zmm/data", "Destination": "/app/data", "RW": True, "Type": "bind"}]}
        rt.stats["zigbee-matter-manager"] = {
            "cpu_stats": {"cpu_usage": {"total_usage": 3_000}, "system_cpu_usage": 100_000, "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 60_000},
            "memory_stats": {"usage": 300_000_000, "limit": 8_000_000_000}}
        d = run_(images.container_detail("zigbee-matter-manager"))
        env = {e["name"]: e["value"] for e in d["env"]}
        c.check("secrets in the environment are masked",
                env["ZMM_API_TOKEN"] != "s3cret" and env["MQTT_PASSWORD"] != "hunter2" and env["TZ"] == "Europe/London", env)
        c.check("shows config: image, health, restart policy, network, device, mount",
                (d["health"], d["restart_policy"], d["network_mode"]) == ("healthy", "always", "host")
                and d["devices"] == ["/dev/ttyACM0 → /dev/ttyACM0"] and d["mounts"][0]["destination"] == "/app/data", d)
        c.check("CPU % from the stats deltas (2000/40000 × 4 CPUs)", d["cpu_percent"] == 20.0, d["cpu_percent"])
        c.check("memory use and limit", d["memory_bytes"] == 300_000_000 and d["memory_limit_bytes"] == 8_000_000_000)
        c.check("a container outside this deployment isn't shown", run_(images.container_detail("someone-elses-db")) is None)
        rt.close()
    return c
