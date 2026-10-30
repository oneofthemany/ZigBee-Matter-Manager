"""
esptool sessions and the facts parsed from them. One session costs the
speaker one counted restart: connect once, keep the flasher stub between
steps, leave through a fresh standard reset. docs/esp-serial.md §Resets.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import struct
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
QUICK_S = 60.0


class EsptoolError(RuntimeError):
    pass


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def _fields(out: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for raw in strip_ansi(out).splitlines():
        m = re.match(r"^([A-Za-z][A-Za-z0-9 ()_/-]*?):\s+(\S.*)$", raw.strip())
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def parse_chip(out: str) -> Dict[str, Any]:
    f = _fields(out)
    line, mac = f.get("Chip type"), f.get("MAC")
    if not line or not mac:
        raise EsptoolError("esptool did not report the chip type and MAC address")
    rev = re.search(r"\(revision ([^)]+)\)", line)
    rest = re.sub(r"\s*\(revision [^)]+\)", "", line)
    pkg = re.search(r"\(([^)]+)\)\s*$", rest)
    return {
        "type": re.sub(r"\s*\([^)]+\)\s*$", "", rest).strip(),
        "package": pkg.group(1) if pkg else None,
        "revision": rev.group(1) if rev else None,
        "features": [x.strip() for x in f.get("Features", "").split(",") if x.strip()],
        "crystal": f.get("Crystal frequency"),
        "usb_mode": f.get("USB mode"),
        "mac": mac.lower(),
    }


def parse_flash(out: str) -> Dict[str, Optional[str]]:
    f = _fields(out)
    return {
        "manufacturer_id": f.get("Manufacturer"),
        "device_id": f.get("Device"),
        "detected_size": f.get("Detected flash size"),
        "efuse_flash_type": f.get("Flash type set in eFuse"),
        "efuse_flash_voltage": f.get("Flash voltage set by eFuse"),
    }


def _enabled(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    if re.match(r"enabled\b", v, re.I):
        return True
    if re.match(r"disabled\b", v, re.I):
        return False
    return None


def parse_security(out: str) -> Dict[str, Any]:
    """Unreported state stays None: never assume disabled."""
    f = _fields(out)
    blocks = [{"block": m.group(1), "purpose": m.group(2).strip()}
              for m in re.finditer(r"^\s*(BLOCK_KEY\d+)\s+-\s+(\S.*)$", strip_ansi(out), re.M)]
    return {
        "secure_boot_enabled": _enabled(f.get("Secure Boot")),
        "flash_encryption_enabled": _enabled(f.get("Flash Encryption")),
        "spi_boot_crypt_count": f.get("SPI Boot Crypt Count (SPI_BOOT_CRYPT_CNT)"),
        "key_blocks": blocks,
    }


def flash_size_bytes(size: Optional[str]) -> Optional[int]:
    m = re.match(r"^(\d+)(KB|MB)$", size or "")
    if not m:
        return None
    return int(m.group(1)) * (1024 ** 2 if m.group(2) == "MB" else 1024)


_APP = {0x00: "factory", 0x20: "test"}
_DATA = {0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump", 0x04: "nvs_keys",
         0x05: "efuse", 0x80: "esphttpd", 0x81: "fat", 0x82: "spiffs", 0x83: "littlefs"}


def parse_partition_table(data: bytes, offset: int = 0x8000) -> Optional[Dict[str, Any]]:
    """ESP-IDF table: 32-byte entries (AA 50), then an optional MD5 entry (EB EB)."""
    entries: List[Dict[str, Any]] = []
    md5 = "absent"
    for at in range(0, len(data) - 31, 32):
        magic = struct.unpack_from("<H", data, at)[0]
        if magic == 0xEBEB:
            md5 = "valid" if hashlib.md5(data[:at]).digest() == data[at + 16:at + 32] else "mismatch"
            break
        if magic != 0x50AA:
            break
        typ, sub, off, size = struct.unpack_from("<BBII", data, at + 2)
        flags = struct.unpack_from("<I", data, at + 28)[0]
        label = data[at + 12:at + 28].split(b"\0")[0].decode("utf-8", "replace")
        if typ == 0:
            subname = f"ota_{sub - 0x10}" if 0x10 <= sub <= 0x1F else _APP.get(sub, "unknown")
        else:
            subname = _DATA.get(sub, "unknown") if typ == 1 else "unknown"
        entries.append({"label": label, "type": {0: "app", 1: "data"}.get(typ, "custom"),
                        "subtype": subname, "offset": off, "size": size,
                        "encrypted": bool(flags & 1), "readonly": bool(flags & 2)})
    return {"offset": offset, "entries": entries, "md5": md5} if entries else None


def parse_boot(text: str) -> Dict[str, Any]:
    """ESPHome safe_mode count and the ROM boot mode; None where the log is silent."""
    text = strip_ansi(text)
    attempts = re.search(r"Unsuccessful boot attempts: (\d+)", text)
    safe = re.search(r"SAFE MODE IS ACTIVE|Boot loop detected", text, re.I)
    mode = re.search(r"boot:0x[0-9a-f]+ \(([A-Z0-9_()/]+)", text, re.I)
    return {
        "boot_attempts": int(attempts.group(1)) if attempts else None,
        "safe_mode_active": True if safe else (False if attempts else None),
        "booted_from_flash": (None if not mode else
                              ("DOWNLOAD" not in mode.group(1).upper() and "FLASH" in mode.group(1).upper())),
    }


Runner = Callable[[Sequence[str], float], Awaitable[str]]


async def run_esptool(args: Sequence[str], timeout: float) -> str:
    """esptool as a child of this interpreter, so the pinned module is the one run."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "esptool", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "NO_COLOR": "1"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise EsptoolError(f"esptool took longer than {int(timeout)} s")
    text = strip_ansi(out.decode("utf-8", "replace"))
    if proc.returncode != 0:
        tail = [ln for ln in text.splitlines() if ln.strip()][-3:]
        raise EsptoolError(" ".join(tail) or f"esptool exited {proc.returncode}")
    return text


def chip_arg(chip_type: str) -> str:
    return re.sub(r"[^a-z0-9]", "", chip_type.lower())


class StubSession:
    """
    Steps share one stub; the exit is '--before default-reset --after
    hard-reset' in a fresh connection. A hard reset from the kept stub pulses
    RTS while pyserial holds DTR high, which on USB-Serial/JTAG restarts into
    download mode instead of the firmware.
    """

    def __init__(self, port: str, run: Runner = run_esptool) -> None:
        self.port = port
        self._run = run
        self.chip: Optional[str] = None
        self.entered = False

    async def connect(self) -> str:
        self.entered = True
        out = await self._run(["-p", self.port, "--after", "no-reset-stub", "flash-id"], QUICK_S)
        self.chip = chip_arg(parse_chip(out)["type"])
        return out

    async def step(self, args: Sequence[str], timeout: float = QUICK_S) -> str:
        return await self._run(["-c", self.chip or "auto", "-p", self.port, "--before", "no-reset-no-sync",
                                "--after", "no-reset-stub", *args], timeout)

    async def leave(self) -> None:
        if self.entered:
            await self._run([*(["-c", self.chip] if self.chip else []), "-p", self.port,
                             "--before", "default-reset", "--after", "hard-reset", "read-mac"], QUICK_S)
