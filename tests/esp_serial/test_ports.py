"""Only Espressif ports are offered, and never the Zigbee radio's — not even by name."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness import Checker, fake_sysfs

from modules import esp_serial as S


def run() -> Checker:
    c = Checker("ports")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sysfs = fake_sysfs(root, {"ttyACM0": ("303a", "A0:85:E3:E8:8A:58"),
                                  "ttyUSB0": ("10c4", "0001"),            # a CP210x Zigbee stick
                                  "ttyACM1": ("303a", "AA:BB:CC:DD:EE:FF")})  # an ESP32 coordinator
        dev = root / "dev"
        (dev / "serial" / "by-id").mkdir(parents=True)
        for n in ("ttyACM0", "ttyUSB0", "ttyACM1"):
            (dev / n).touch()
        coordinator = dev / "serial" / "by-id" / "usb-radio"
        coordinator.symlink_to(dev / "ttyACM1")
        m = S.EspSerialManager(lambda: str(coordinator), sysfs=str(sysfs), dev_root=str(dev), root=root / "data")

        listed = m.ports()
        names = [p["name"] for p in listed["ports"]]
        c.check("only the Espressif speaker is offered", names == ["ttyACM0"], listed)
        c.check("the others are counted as hidden", listed["hidden"] == 2, listed)
        c.check("an Espressif coordinator named through a by-id link is still recognised", "ttyACM1" not in names)
        c.check("the speaker is keyed by its USB serial", listed["ports"][0]["key"] == "usb:A0:85:E3:E8:8A:58", listed)
        for name in ("ttyUSB0", "ttyACM1", "../ttyACM0"):
            try:
                m.port(name)
                c.check(f"asking for {name} by name is refused", False)
            except LookupError:
                c.check(f"asking for {name} by name is refused", True)
        c.check("the confirm code is the end of the MAC", m.confirm_code(listed["ports"][0]) == "E88A58")

        m2 = S.EspSerialManager(lambda: str(dev / "ttyACM0"), sysfs=str(sysfs), dev_root=str(dev), root=root / "data")
        c.check("a coordinator moved to the speaker's port hides it at once",
                [p["name"] for p in m2.ports()["ports"]] == ["ttyACM1"], m2.ports())
    return c
