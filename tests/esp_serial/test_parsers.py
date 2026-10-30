"""esptool output, partition tables, boot logs and Improv packets, parsed by their owners."""

from __future__ import annotations

import hashlib
import struct

from harness import BANNER, BOOT_OK, FLASH_ID, SECURITY, Checker

from modules import esp_esptool as E
from modules import esp_serial as S


def _table(md5: str) -> bytes:
    rows = [("otadata", 1, 0x00, 0x9000, 0x2000, 0), ("app0", 0, 0x10, 0x10000, 0x7C0000, 0),
            ("app1", 0, 0x11, 0x7D0000, 0x7C0000, 0), ("nvs", 1, 0x02, 0xF90000, 0x6D000, 2)]
    body = b"".join(struct.pack("<HBBII16sI", 0x50AA, t, s, o, n, lab.encode(), f) for lab, t, s, o, n, f in rows)
    digest = hashlib.md5(body).digest()
    if md5 == "corrupt":
        digest = bytes([digest[0] ^ 0xFF]) + digest[1:]
    tail = struct.pack("<H14s16s", 0xEBEB, b"\xff" * 14, digest) if md5 != "none" else b"\xff" * 32
    return body + tail + b"\xff" * (0xC00 - len(body) - 32)


def run() -> Checker:
    c = Checker("parsers")
    c.section("esptool")
    chip = E.parse_chip(FLASH_ID)
    c.check("the chip banner gives type, package, revision and MAC",
            (chip["type"], chip["package"], chip["revision"], chip["mac"]) == ("ESP32-S3", "QFN56", "v0.2", "02:00:00:aa:bb:cc"), chip)
    c.check("flash size is read and converted", E.flash_size_bytes(E.parse_flash(FLASH_ID)["detected_size"]) == 16 * 1024 ** 2)
    sec = E.parse_security(SECURITY)
    c.check("secure boot and flash encryption read as off",
            sec["secure_boot_enabled"] is False and sec["flash_encryption_enabled"] is False, sec)
    c.check("unreported security stays unknown, never 'off'", E.parse_security(BANNER)["secure_boot_enabled"] is None)
    try:
        E.parse_chip("A fatal error occurred")
        c.check("output without a chip identity is refused", False)
    except E.EsptoolError:
        c.check("output without a chip identity is refused", True)

    c.section("partition table")
    t = E.parse_partition_table(_table("valid"))
    c.check("entries decode with OTA slot names",
            [(e["label"], e["subtype"]) for e in t["entries"]] == [("otadata", "ota"), ("app0", "ota_0"), ("app1", "ota_1"), ("nvs", "nvs")], t)
    c.check("a matching MD5 entry reads valid", t["md5"] == "valid")
    c.check("a corrupt one reads mismatch", E.parse_partition_table(_table("corrupt"))["md5"] == "mismatch")
    c.check("erased flash is no table", E.parse_partition_table(b"\xff" * 0xC00) is None)

    c.section("boot log")
    b = E.parse_boot(BOOT_OK.decode())
    c.check("a flash boot with its safe_mode count", b == {"boot_attempts": 0, "safe_mode_active": False, "booted_from_flash": True}, b)
    d = E.parse_boot("rst:0x15 (USB_UART_CHIP_RESET),boot:0x0 (DOWNLOAD(USB/UART0))\nwaiting for download")
    c.check("a restart into download mode is told apart", d["booted_from_flash"] is False, d)
    c.check("silence stays unknown", E.parse_boot("") == {"boot_attempts": None, "safe_mode_active": None, "booted_from_flash": None})

    c.section("Improv")
    pkt = S.improv_rpc(0x02)
    c.check("an RPC packet carries header, command and checksum",
            pkt[:9] == b"IMPROV\x01\x03\x02" and pkt[9:11] == b"\x02\x00" and pkt[11] == sum(pkt[:11]) & 0xFF and pkt[-1] == 0x0A, pkt)
    reply = S.improv_packet(0x04, bytes([0x03, 9, 3]) + b"abc" + bytes([4]) + b"1.25")
    packets, rest = S.parse_improv(b"[I][log]: noise\r\n" + reply + reply[:5])
    c.check("packets are found among log text, a partial one kept", len(packets) == 1 and rest == reply[:5], (packets, rest))
    c.check("RPC strings decode", S.rpc_strings(packets[0][1]) == ["abc", "1.25"], packets)
    bad = bytearray(reply)
    bad[-2] ^= 0xFF
    c.check("a bad checksum is dropped", S.parse_improv(bytes(bad))[0] == [])
    return c
