"""
Shared scaffolding for the speaker serial tests — plain scripts, matching tests/wifi_devices.
A pty stands in for the speaker's USB serial port; esptool is a recording fake.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if "ZMM_DATA_DIR" not in os.environ:
    os.environ["ZMM_DATA_DIR"] = tempfile.mkdtemp(prefix="zmm_test_data_")
    atexit.register(shutil.rmtree, os.environ["ZMM_DATA_DIR"], True)

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Checker:
    """Collects pass/fail lines so a module can report as a group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, label: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failures.append(f"{self.name}: {label}")
            print(f"    FAIL {label}  <- {detail!r}"[:400])
        return bool(ok)


BANNER = """esptool v5.1.0
Connected to ESP32-S3 on /dev/ttyACM0:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG
MAC:                02:00:00:aa:bb:cc
"""
FLASH_ID = BANNER + """Manufacturer: 5e
Device: 4018
Detected flash size: 16MB
Flash type set in eFuse: quad (4 data lines)
Flash voltage set by eFuse: 3.3V
"""
SECURITY = BANNER + """Flags: 0x00000000 (0b0)
  BLOCK_KEY0 - USER/EMPTY
Secure Boot: Disabled
Flash Encryption: Disabled
SPI Boot Crypt Count (SPI_BOOT_CRYPT_CNT): 0x0
"""
BOOT_OK = (b"ESP-ROM:esp32s3-20210327\r\nrst:0x15 (USB_UART_CHIP_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
           b"[C][safe_mode:084]: Unsuccessful boot attempts: 0\r\n")


def fake_sysfs(root: Path, devices: dict) -> Path:
    """devices: tty name -> (idVendor, serial). Laid out as Linux does for cdc_acm."""
    tty = root / "sys" / "class" / "tty"
    tty.mkdir(parents=True)
    for i, (name, (vid, serial)) in enumerate(devices.items()):
        usb = root / "sys" / "devices" / f"usb{i}"
        iface = usb / f"{i}-1:1.0"
        iface.mkdir(parents=True)
        (usb / "idVendor").write_text(vid + "\n")
        (usb / "idProduct").write_text("1001\n")
        (usb / "serial").write_text(serial + "\n")
        (usb / "product").write_text("USB JTAG/serial debug unit\n")
        (tty / name).mkdir()
        (tty / name / "device").symlink_to(iface)
    return tty
