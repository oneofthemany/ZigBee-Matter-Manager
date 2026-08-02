"""
Zero-dependency DNS message codec for the sinkhole.

Forwarding means only the question section is ever decoded, blocked names get a
synthesised fixed-shape response, and allowed names are relayed as raw bytes
with at most the 2-byte id rewritten. So arbitrary upstream RRsets never need
serialising. RFC 1035 section 4. See docs/beekeeper.md.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional, Tuple

# Record types we name explicitly (numeric everywhere else is fine)
TYPE_A = 1
TYPE_NS = 2
TYPE_CNAME = 5
TYPE_SOA = 6
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_AAAA = 28
TYPE_HTTPS = 65

CLASS_IN = 1

# RCODEs used when synthesising block answers.
RCODE_NOERROR = 0
RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
RCODE_REFUSED = 5

_HEADER = struct.Struct("!HHHHHH")  # id, flags, qd, an, ns, ar


class DNSFormatError(ValueError):
    """Raised when a datagram is too short or malformed to parse."""


@dataclass
class Question:
    """The decoded first question plus everything the server needs downstream."""
    txid: int          # transaction id (header bytes 0-1)
    flags: int         # raw 16-bit flags field of the query
    qname: str         # lowercased, no trailing dot, e.g. "ads.example.com"
    qtype: int         # A / AAAA / ...
    qclass: int        # normally IN (1)
    q_end: int         # byte offset just past the question section
    rd: bool           # recursion-desired bit, echoed into block responses

    @property
    def opcode(self) -> int:
        return (self.flags >> 11) & 0xF


# Decoding

def _read_name(buf: bytes, offset: int) -> Tuple[str, int]:
    """Decode a (possibly compressed) domain name.

    Returns (name, next_offset) where next_offset is the position immediately
    after the name *in the original stream* — for a compressed name that is the
    position after the 2-byte pointer, not wherever the pointer jumped to.
    """
    labels = []
    next_offset: Optional[int] = None
    jumps = 0
    pos = offset
    n = len(buf)
    while True:
        if pos >= n:
            raise DNSFormatError("name runs past end of message")
        length = buf[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:            # compression pointer (two high bits set)
            if pos + 1 >= n:
                raise DNSFormatError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | buf[pos + 1]
            if next_offset is None:
                next_offset = pos + 2         # resume here once we've followed the chain
            jumps += 1
            if jumps > 128:                   # bounded jumps defuse pointer loops
                raise DNSFormatError("too many compression pointers")
            if pointer >= n:
                raise DNSFormatError("compression pointer past end of message")
            pos = pointer
            continue
        if length & 0xC0:
            raise DNSFormatError("reserved label length bits set")
        pos += 1
        if pos + length > n:
            raise DNSFormatError("label runs past end of message")
        labels.append(buf[pos:pos + length].decode("latin-1"))
        pos += length
    if next_offset is None:
        next_offset = pos
    return ".".join(labels).lower(), next_offset


def parse_question(data: bytes) -> Question:
    """Decode the header and first question of a DNS query.

    Raises DNSFormatError on anything we can't read; the caller turns that into
    a FORMERR response rather than crashing the server loop.
    """
    if len(data) < 12:
        raise DNSFormatError("shorter than DNS header")
    txid, flags, qdcount, _an, _ns, _ar = _HEADER.unpack_from(data, 0)
    if qdcount < 1:
        raise DNSFormatError("no question in query")
    qname, pos = _read_name(data, 12)
    if pos + 4 > len(data):
        raise DNSFormatError("truncated question")
    qtype, qclass = struct.unpack_from("!HH", data, pos)
    return Question(
        txid=txid,
        flags=flags,
        qname=qname,
        qtype=qtype,
        qclass=qclass,
        q_end=pos + 4,
        rd=bool(flags & 0x0100),
    )


# Encoding block responses

def _response_header(txid: int, query_flags: int, rcode: int, ancount: int) -> bytes:
    """Header for a synthetic answer: QR=1, RA=1, opcode/RD copied from query."""
    opcode = (query_flags >> 11) & 0xF
    rd = query_flags & 0x0100
    flags = 0x8000 | (opcode << 11) | rd | 0x0080 | (rcode & 0xF)   # 0x8000 QR, 0x0080 RA
    return _HEADER.pack(txid, flags, 1, ancount, 0, 0)


def _answer_rr(qtype: int, ttl: int, rdata: bytes) -> bytes:
    # Name is a pointer to the question at offset 12 (0xC00C), always present
    # because we echo the question section verbatim right after the header.
    return struct.pack("!HHHIH", 0xC00C, qtype, CLASS_IN, ttl, len(rdata)) + rdata


def build_block_response(
    query: bytes,
    q: Question,
    *,
    mode: str = "zero",
    ipv4: str = "0.0.0.0",
    ipv6: str = "::",
    ttl: int = 60,
) -> bytes:
    """Build the response we return for a blocked name.

    mode:
      "nxdomain" — reply NXDOMAIN (domain does not exist) for every type.
      "zero"     — answer A/AAAA queries with ``ipv4``/``ipv6`` (0.0.0.0 / ::),
                   and NODATA (NOERROR, no answer) for other types so the
                   client stops asking without being told the name is missing.

    The question section is echoed byte-for-byte from the query so the answer's
    0xC00C name pointer resolves correctly regardless of the original QNAME.
    """
    question = query[12:q.q_end]

    if mode == "nxdomain":
        return _response_header(q.txid, q.flags, RCODE_NXDOMAIN, 0) + question

    # "zero"/sinkhole mode.
    rr = b""
    if q.qtype == TYPE_A:
        rr = _answer_rr(TYPE_A, ttl, _pack_ipv4(ipv4))
    elif q.qtype == TYPE_AAAA:
        rr = _answer_rr(TYPE_AAAA, ttl, _pack_ipv6(ipv6))
    # Any other qtype (HTTPS/65, TXT, MX, ...) → NODATA: NOERROR with no answer.
    ancount = 1 if rr else 0
    return _response_header(q.txid, q.flags, RCODE_NOERROR, ancount) + question + rr


def build_error_response(query: bytes, rcode: int) -> bytes:
    """Minimal error reply (e.g. SERVFAIL/REFUSED) echoing id + question if any.

    Used when upstream fails or a query is malformed. Falls back to a bare
    header when the question can't be located.
    """
    if len(query) < 12:
        # Can't even trust the id — return a 12-byte header of zeros with rcode.
        return _HEADER.pack(0, 0x8000 | 0x0080 | (rcode & 0xF), 0, 0, 0, 0)
    txid, flags = struct.unpack_from("!HH", query, 0)
    try:
        q = parse_question(query)
        header = _response_header(txid, flags, rcode, 0)
        return header + query[12:q.q_end]
    except DNSFormatError:
        return _HEADER.pack(txid, 0x8000 | 0x0080 | (rcode & 0xF), 0, 0, 0, 0)


# ID patching for cache reuse

def patch_id(message: bytes, txid: int) -> bytes:
    """Return ``message`` with its transaction id replaced.

    Cached upstream answers are stored once and reused across clients, each of
    which chose its own random id; we rewrite the first two bytes to match the
    querying client so the reply is accepted.
    """
    if len(message) < 2:
        return message
    return struct.pack("!H", txid & 0xFFFF) + message[2:]


def message_txid(message: bytes) -> Optional[int]:
    if len(message) < 2:
        return None
    return struct.unpack_from("!H", message, 0)[0]


# Query builder + answer parser (for the in-app "dig" tester)

def build_query(qname: str, qtype: int = TYPE_A, txid: int = 0, rd: bool = True) -> bytes:
    """Encode a minimal DNS query (one question, no EDNS)."""
    flags = 0x0100 if rd else 0x0000
    header = _HEADER.pack(txid & 0xFFFF, flags, 1, 0, 0, 0)
    labels = b"".join(bytes([len(l)]) + l.encode("idna" if any(ord(c) > 127 for c in l)
                                                else "ascii")
                      for l in qname.strip(".").split(".") if l)
    question = labels + b"\x00" + struct.pack("!HH", qtype, CLASS_IN)
    return header + question


def parse_answers(message: bytes) -> list:
    """Best-effort extract of the answer section as [{"type","name","data"}].

    A/AAAA records are decoded to printable addresses; other types report their
    rdata length. Returns [] on any malformation — this only feeds the UI tester,
    never the resolver hot path.
    """
    import ipaddress
    out: list = []
    try:
        _txid, _flags, qd, an, _ns, _ar = _HEADER.unpack_from(message, 0)
        pos = 12
        for _ in range(qd):                      # skip question section
            _name, pos = _read_name(message, pos)
            pos += 4                             # qtype + qclass
        for _ in range(an):
            name, pos = _read_name(message, pos)
            rtype, _rclass, _ttl, rdlen = struct.unpack_from("!HHIH", message, pos)
            pos += 10
            rdata = message[pos:pos + rdlen]
            pos += rdlen
            if rtype == TYPE_A and rdlen == 4:
                out.append({"type": "A", "name": name, "data": str(ipaddress.IPv4Address(rdata))})
            elif rtype == TYPE_AAAA and rdlen == 16:
                out.append({"type": "AAAA", "name": name, "data": str(ipaddress.IPv6Address(rdata))})
            elif rtype == TYPE_CNAME:
                cname, _ = _read_name(message, pos - rdlen)
                out.append({"type": "CNAME", "name": name, "data": cname})
            else:
                out.append({"type": str(rtype), "name": name, "data": f"({rdlen} bytes)"})
    except Exception:
        pass
    return out


RCODE_NAMES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
               4: "NOTIMP", 5: "REFUSED"}


# Small address packers (avoid pulling in socket/ipaddress on the hot path)

def _pack_ipv4(addr: str) -> bytes:
    parts = addr.split(".")
    if len(parts) != 4:
        raise ValueError(f"bad IPv4 sinkhole address: {addr!r}")
    return bytes(int(p) & 0xFF for p in parts)


def _pack_ipv6(addr: str) -> bytes:
    import ipaddress
    return ipaddress.IPv6Address(addr).packed
