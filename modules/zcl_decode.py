"""
Shared ZCL frame / value decoding — used by the full device probe
(modules/device_probe.py) and the packet debugger (modules/zigbee_debug.py).

Sizes every ZCL fixed-length type (incl. 24/40/48/56-bit ints, enum16,
floats, UTC, EUI64) so multi-attribute frames never mis-align.
"""
import struct
from typing import Any, Dict, List

# ZCL data type → (struct fmt, size); little-endian, signed where noted.
# 24/40/48/56-bit types are decoded by hand in decode_value.
_FIXED = {
    0x08: ("B", 1), 0x09: ("H", 2), 0x0A: (None, 3), 0x0B: ("I", 4),
    0x10: ("B", 1),
    0x18: ("B", 1), 0x19: ("H", 2), 0x1A: (None, 3), 0x1B: ("I", 4),
    0x1C: (None, 5), 0x1D: (None, 6), 0x1E: (None, 7), 0x1F: ("Q", 8),
    0x20: ("B", 1), 0x21: ("H", 2), 0x22: (None, 3), 0x23: ("I", 4),
    0x24: (None, 5), 0x25: (None, 6), 0x26: (None, 7), 0x27: ("Q", 8),
    0x28: ("b", 1), 0x29: ("h", 2), 0x2A: (None, -3), 0x2B: ("i", 4),
    0x2C: (None, -5), 0x2D: (None, -6), 0x2E: (None, -7), 0x2F: ("q", 8),
    0x30: ("B", 1), 0x31: ("H", 2),
    0x38: (None, 2), 0x39: ("f", 4), 0x3A: ("d", 8),
    0xE0: ("I", 4), 0xE1: ("I", 4), 0xE2: ("I", 4),
    0xE8: ("H", 2), 0xE9: ("H", 2), 0xEA: ("I", 4),
    0xF0: ("Q", 8), 0xF1: (None, 16),
}
_TYPE_NAMES = {
    0x10: "bool", 0x18: "map8", 0x19: "map16", 0x20: "uint8", 0x21: "uint16",
    0x22: "uint24", 0x23: "uint32", 0x25: "uint48", 0x28: "int8", 0x29: "int16",
    0x2A: "int24", 0x2B: "int32", 0x30: "enum8", 0x31: "enum16", 0x39: "single",
    0x41: "octstr", 0x42: "string", 0xE2: "UTC", 0xF0: "EUI64",
}


def type_name(dt: int) -> str:
    return f"0x{dt:02X}/{_TYPE_NAMES.get(dt, '?')}"


def decode_value(dt: int, buf: bytes, i: int):
    """Return (value, new_index). Raises on anything we can't size."""
    if dt in _FIXED:
        fmt, size = _FIXED[dt]
        n = abs(size)
        raw = buf[i:i + n]
        if len(raw) < n:
            raise ValueError("truncated")
        if fmt:
            return struct.unpack("<" + fmt, raw)[0], i + n
        return int.from_bytes(raw, "little", signed=size < 0), i + n
    if dt in (0x41, 0x42):                      # octet / char string, 1-byte len
        n = buf[i]
        raw = buf[i + 1:i + 1 + (0 if n == 0xFF else n)]
        val = raw.decode("utf-8", "replace") if dt == 0x42 else raw.hex()
        return val, i + 1 + len(raw)
    if dt in (0x43, 0x44):                      # long strings, 2-byte len
        n = int.from_bytes(buf[i:i + 2], "little")
        raw = buf[i + 2:i + 2 + (0 if n == 0xFFFF else n)]
        return raw.hex(), i + 2 + len(raw)
    raise ValueError(f"unsized type {type_name(dt)}")


def parse_zcl(message: bytes) -> Dict[str, Any]:
    """Decode a ZCL frame header and, for read-rsp/report, its attribute records."""
    out: Dict[str, Any] = {"hex": message.hex()}
    if len(message) < 3:
        return out
    fc = message[0]
    i = 1
    if fc & 0x04:
        out["manufacturer"] = f"0x{int.from_bytes(message[1:3], 'little'):04X}"
        i = 3
    out["tsn"] = message[i]
    cmd = message[i + 1]
    i += 2
    general = (fc & 0x03) == 0
    out["frame"] = "general" if general else "cluster-specific"
    out["command"] = f"0x{cmd:02X}"
    if not general:
        out["payload"] = message[i:].hex()
        return out

    recs: List[Dict[str, Any]] = []
    try:
        if cmd == 0x01:                         # Read Attributes Response
            out["command"] += " read_rsp"
            while i + 3 <= len(message):
                aid = int.from_bytes(message[i:i + 2], "little")
                status = message[i + 2]
                i += 3
                rec = {"attr": f"0x{aid:04X}", "status": f"0x{status:02X}"}
                if status == 0:
                    dt = message[i]
                    i += 1
                    val, i = decode_value(dt, message, i)
                    rec.update(type=type_name(dt), value=val)
                recs.append(rec)
        elif cmd == 0x0A:                       # Report Attributes
            out["command"] += " report"
            while i + 3 <= len(message):
                aid = int.from_bytes(message[i:i + 2], "little")
                dt = message[i + 2]
                i += 3
                val, i = decode_value(dt, message, i)
                recs.append({"attr": f"0x{aid:04X}", "type": type_name(dt), "value": val})
        elif cmd == 0x0B:
            out["command"] += " default_rsp"
            out["payload"] = message[i:].hex()
        else:
            out["payload"] = message[i:].hex()
    except Exception as e:
        out["parse_error"] = f"{e} at byte {i}"
    if recs:
        out["records"] = recs
    return out
