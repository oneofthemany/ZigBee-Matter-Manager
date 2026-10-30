"""
The engine over a pty: cooldown, the esptool reset sequence, boot capture,
Improv, the log, and the flash guards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from harness import BOOT_OK, FLASH_ID, SECURITY, Checker, fake_sysfs

from modules import esp_esptool as E
from modules import esp_serial as S


class Pty:
    """The speaker's end of a fake USB serial port."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)

    def read(self) -> bytes:
        try:
            return os.read(self.master, 65536)
        except (BlockingIOError, OSError):
            return b""

    def close(self) -> None:
        os.close(self.master)
        os.close(self.slave)


def manager(root: Path, pty: Pty, runner=None) -> S.EspSerialManager:
    sysfs = fake_sysfs(root, {"ttyACM0": ("303a", "A0:85:E3:E8:8A:58")})
    dev = root / "dev"
    dev.mkdir()
    (dev / "ttyACM0").symlink_to(pty.path)
    return S.EspSerialManager(lambda: None, sysfs=str(sysfs), dev_root=str(dev), root=root / "data",
                              runner=runner or (lambda a, t: None))


class FakeEsptool:
    def __init__(self, pty: Pty, fail_on: str = "", boot: bytes = BOOT_OK) -> None:
        self.calls: list = []
        self.pty, self.fail_on, self.boot = pty, fail_on, boot

    async def __call__(self, args, timeout) -> str:
        self.calls.append(list(args))
        if self.fail_on and self.fail_on in args:
            raise E.EsptoolError("The chip stopped responding.")
        if "default-reset" in args:
            os.write(self.pty.master, self.boot)
            return "MAC: 02:00:00:aa:bb:cc\nHard resetting via RTS pin..."
        if "flash-id" in args:
            return FLASH_ID
        if "get-security-info" in args:
            return SECURITY
        if "read-flash" in args:
            Path(args[-1]).write_bytes(b"\xff" * int(args[-2], 0))
            return "Read done"
        if "write-flash" in args:
            return "Wrote ...\nHash of data verified."
        return ""


async def _scenarios(c: Checker) -> None:
    S.BOOT_LISTEN_S = 0.4

    c.section("cooldown")
    with tempfile.TemporaryDirectory() as tmp:
        ledger = S.RestartLedger(Path(tmp) / "r.json")
        c.check("a first restart is admitted", ledger.admit("usb:x", False) is None)
        try:
            S.RestartLedger(Path(tmp) / "r.json").admit("usb:x", False)
            c.check("a second inside 2 minutes is refused, after an app restart too", False)
        except S.CooldownError as e:
            c.check("a second inside 2 minutes is refused, after an app restart too", 100 < e.remaining <= 120, e.remaining)
        c.check("an explicit override goes through", ledger.admit("usb:x", True) is not None)
        c.check("other speakers are not held back", ledger.remaining("usb:y") == 0)

    c.section("an info session")
    with tempfile.TemporaryDirectory() as tmp:
        pty = Pty()
        fake = FakeEsptool(pty)
        m = manager(Path(tmp), pty, fake)
        try:
            m.precheck("ttyACM0", None, False)
            c.check("a restart without acknowledgement is refused", False)
        except ValueError as e:
            c.check("a restart without acknowledgement is refused", "safe mode" in str(e), e)
        m.precheck("ttyACM0", True, False)
        info = await m.info("ttyACM0")
        first, steps, last = fake.calls[0], fake.calls[1:-1], fake.calls[-1]
        c.check("it connects once and keeps the stub", "no-reset-stub" in first and "flash-id" in first, first)
        c.check("steps reuse the stub without a reset",
                all("no-reset-no-sync" in s and "no-reset-stub" in s for s in steps), steps)
        c.check("it leaves through a fresh default reset, which boots the firmware",
                last[last.index("--before") + 1] == "default-reset" and last[last.index("--after") + 1] == "hard-reset", last)
        r = info["restart"]
        c.check("the boot after it is read: count and flash boot",
                r["boot_attempts"] == 0 and r["booted_from_flash"] is True and r["boot_log_bytes"] > 0, r)
        c.check("security facts come back", info["security"]["secure_boot_enabled"] is False, info["security"])
        try:
            m.precheck("ttyACM0", True, False)
            c.check("the next restart waits out the cooldown", False)
        except S.CooldownError:
            c.check("the next restart waits out the cooldown", True)
        pty.close()

    c.section("a failing session still resets")
    with tempfile.TemporaryDirectory() as tmp:
        pty = Pty()
        fake = FakeEsptool(pty, fail_on="get-security-info")
        m = manager(Path(tmp), pty, fake)
        try:
            await m.info("ttyACM0")
            c.check("the step's error is reported", False)
        except E.EsptoolError as e:
            c.check("the step's error is reported", "stopped responding" in str(e), e)
        c.check("and the chip is still reset out of the stub", "default-reset" in fake.calls[-1], fake.calls[-1])
        c.check("and the restart still counts", m.ledger.remaining("usb:A0:85:E3:E8:8A:58") > 0)
        pty.close()

    c.section("download-mode boots are reported")
    with tempfile.TemporaryDirectory() as tmp:
        pty = Pty()
        m = manager(Path(tmp), pty, FakeEsptool(pty, boot=b"rst:0x15 (USB_UART_CHIP_RESET),boot:0x0 (DOWNLOAD(USB/UART0))\r\n"))
        info = await m.info("ttyACM0")
        c.check("booted_from_flash is false", info["restart"]["booted_from_flash"] is False, info["restart"])
        pty.close()

    c.section("backup and flash guards")
    with tempfile.TemporaryDirectory() as tmp:
        pty = Pty()
        fake = FakeEsptool(pty)
        m = manager(Path(tmp), pty, fake)
        try:
            m.save_image(b"\x00not an image", "x.bin")
            c.check("an image without the bootloader header is refused", False)
        except ValueError:
            c.check("an image without the bootloader header is refused", True)
        img = m.save_image(b"\xe9" + b"\x00" * 4095, "factory.bin")
        try:
            m.flash_precheck("ttyACM0", f"image:{img['id']}", "E88A58", True, False)
            c.check("flashing a speaker with no backup is refused", False)
        except ValueError as e:
            c.check("flashing a speaker with no backup is refused", "Back up" in str(e), e)
        b = await m.backup("ttyACM0")
        c.check("a backup is the whole flash, digested", b["bytes"] == 16 * 1024 ** 2 and len(b["sha256"]) == 64, b)
        c.check("the backup lands in the library",
                any(x["id"] == b["id"] for x in m.library()["backups"]))
        try:
            m.flash_precheck("ttyACM0", f"image:{img['id']}", "WRONG", True, True)
            c.check("the wrong confirm code is refused", False)
        except ValueError as e:
            c.check("the wrong confirm code is refused", "E88A58" in str(e), e)
        m.flash_precheck("ttyACM0", f"image:{img['id']}", "e88a58", True, True)
        out = await m.flash("ttyACM0", f"image:{img['id']}", override=True)
        writes = [call for call in fake.calls if "write-flash" in call]
        c.check("the image is written from offset 0 and verified", writes and writes[0][writes[0].index("write-flash") + 1] == "0x0" and out["verified"], writes)
        try:
            m.source("image:../../etc")
            c.check("a source outside the library is refused", False)
        except LookupError:
            c.check("a source outside the library is refused", True)
        pty.close()

    c.section("Improv and the log over the port")
    with tempfile.TemporaryDirectory() as tmp:
        pty = Pty()
        m = manager(Path(tmp), pty)
        records: list = []
        handler = logging.Handler()
        handler.emit = lambda rec: records.append(rec.getMessage())
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.DEBUG)

        async def speaker():
            buf = b""
            while True:
                buf += pty.read()
                packets, buf = S.parse_improv(buf)
                for kind, data in packets:
                    if kind == 0x03 and data[0] == 0x02:
                        os.write(pty.master, b"[D][wifi]: noise\r\n" + S.improv_packet(0x01, b"\x04"))
                    elif kind == 0x03 and data[0] == 0x03:
                        s = [b"Nabu Casa.Voice", b"25.11.0", b"ESP32-S3", b"home-assistant-voice-090899"]
                        body = b"".join(bytes([len(x)]) + x for x in s)
                        os.write(pty.master, S.improv_packet(0x04, bytes([0x03, len(body)]) + body))
                    elif kind == 0x03 and data[0] == 0x01:
                        url = b"http://192.168.1.170"
                        os.write(pty.master, S.improv_packet(0x04, bytes([0x01, len(url) + 1, len(url)]) + url))
                await asyncio.sleep(0.02)

        dev = asyncio.create_task(speaker())
        status = await m.improv_status("ttyACM0")
        c.check("the Wi-Fi state and device name come back", status["state"] == "provisioned"
                and status["device_name"] == "home-assistant-voice-090899", status)
        res = await m.improv_wifi("ttyACM0", "HomeNet", "s3cret-pass")
        c.check("Wi-Fi settings are accepted with the speaker's address", res["urls"] == ["http://192.168.1.170"], res)
        c.check("the password reaches no log record", not any("s3cret-pass" in r for r in records), records[-5:])
        dev.cancel()
        try:
            await m.improv_wifi("ttyACM0", "", "x")
            c.check("an empty network name is refused before the port is touched", False)
        except ValueError:
            c.check("an empty network name is refused before the port is touched", True)
        logging.getLogger().removeHandler(handler)

        pty.read()
        first = m.log("ttyACM0", 0)
        await asyncio.sleep(0.2)
        os.write(pty.master, b"\x1b[0;32m[I][app:073]: Running\x1b[0m\r\n[D][wifi]: up\r\npart")
        await asyncio.sleep(0.4)
        got = m.log("ttyACM0", first["seq"])
        c.check("log lines arrive numbered, escapes stripped, a partial line held back",
                [line["text"] for line in got["lines"]] == ["[I][app:073]: Running", "[D][wifi]: up"], got)
        c.check("the log echoes nothing back into the speaker", pty.read() == b"")
        await m.stop_log("ttyACM0")
        pty.close()


def run() -> Checker:
    c = Checker("manager")
    asyncio.run(_scenarios(c))
    return c
