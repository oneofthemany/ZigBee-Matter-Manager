"""Upstream forwarding + a small positive cache.

Unblocked queries are relayed verbatim to the configured upstream resolvers
(Cloudflare 1.1.1.1 and Quad9 9.9.9.9 by default) with failover: try each in
turn until one answers within the timeout. The upstream's response bytes are
returned unchanged — we never re-encode an answer (see beekeeper.wire).

A tiny LRU cache holds recent UDP answers keyed by (qname, qtype, qclass). It
uses a single fixed TTL cap rather than parsing per-record TTLs from the wire —
a deliberate simplification for a home resolver: at worst a name is served from
cache for up to ``cache.ttl`` seconds (default 300) past its real TTL. Cached
answers are reused across clients by rewriting the 2-byte transaction id.
Only UDP answers are cached and only the UDP path reads the cache, so a large
TCP-only answer is never squeezed back out over UDP.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import wire

logger = logging.getLogger("beekeeper.resolver")

_TC_BIT = 0x0200  # truncated flag in the response flags field


@dataclass
class ResolveResult:
    response: Optional[bytes]
    cached: bool = False
    upstream: Optional[str] = None
    error: Optional[str] = None


class _UDPQueryProtocol(asyncio.DatagramProtocol):
    """One-shot: send a query, resolve a future with the first datagram back."""

    def __init__(self, future: "asyncio.Future[bytes]"):
        self._future = future

    def datagram_received(self, data: bytes, addr) -> None:
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc and not self._future.done():
            self._future.set_exception(exc)


class UpstreamResolver:
    def __init__(self, upstreams: List[str], timeout: float = 3.0,
                 port: int = 53, cache_enabled: bool = True,
                 cache_max: int = 10000, cache_ttl: int = 300):
        self.upstreams = list(upstreams) or ["1.1.1.1"]
        self.timeout = timeout
        self.port = port
        self.cache_enabled = cache_enabled
        self.cache_max = cache_max
        self.cache_ttl = cache_ttl
        # key -> (response_bytes, expiry_monotonic)
        self._cache: "OrderedDict[Tuple[str, int, int], Tuple[bytes, float]]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    # ── cache ────────────────────────────────────────────────────────────────
    @staticmethod
    def _key(q: wire.Question) -> Tuple[str, int, int]:
        return (q.qname, q.qtype, q.qclass)

    def _cache_get(self, q: wire.Question) -> Optional[bytes]:
        if not self.cache_enabled:
            return None
        key = self._key(q)
        entry = self._cache.get(key)
        if not entry:
            return None
        data, expiry = entry
        if expiry < time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)      # LRU touch
        return data

    def _cache_put(self, q: wire.Question, response: bytes) -> None:
        if not self.cache_enabled:
            return
        # Don't cache truncated or transient-failure answers.
        if len(response) < 12:
            return
        flags = struct.unpack_from("!H", response, 2)[0]
        if flags & _TC_BIT:
            return
        rcode = flags & 0xF
        if rcode not in (wire.RCODE_NOERROR, wire.RCODE_NXDOMAIN):
            return
        key = self._key(q)
        self._cache[key] = (response, time.monotonic() + self.cache_ttl)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max:
            self._cache.popitem(last=False)   # evict least-recently-used

    def cache_stats(self) -> dict:
        return {"entries": len(self._cache), "hits": self._hits,
                "misses": self._misses, "max": self.cache_max}

    def clear_cache(self) -> int:
        n = len(self._cache)
        self._cache.clear()
        return n

    # ── forwarding ───────────────────────────────────────────────────────────
    async def _query_udp_one(self, upstream: str, query: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[bytes]" = loop.create_future()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPQueryProtocol(fut), remote_addr=(upstream, self.port))
        try:
            transport.sendto(query)
            return await asyncio.wait_for(fut, self.timeout)
        finally:
            transport.close()

    async def _query_tcp_one(self, upstream: str, query: bytes) -> bytes:
        # RFC 1035 §4.2.2: TCP DNS messages are prefixed with a 2-byte length.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(upstream, self.port), self.timeout)
        try:
            writer.write(struct.pack("!H", len(query)) + query)
            await writer.drain()
            hdr = await asyncio.wait_for(reader.readexactly(2), self.timeout)
            (length,) = struct.unpack("!H", hdr)
            body = await asyncio.wait_for(reader.readexactly(length), self.timeout)
            return body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def resolve(self, query: bytes, q: wire.Question, tcp: bool = False) -> ResolveResult:
        """Forward ``query`` upstream (or serve from cache on the UDP path)."""
        if not tcp:
            cached = self._cache_get(q)
            if cached is not None:
                self._hits += 1
                return ResolveResult(wire.patch_id(cached, q.txid), cached=True)
            self._misses += 1

        last_err: Optional[str] = None
        for upstream in self.upstreams:
            try:
                if tcp:
                    resp = await self._query_tcp_one(upstream, query)
                else:
                    resp = await self._query_udp_one(upstream, query)
                if resp and not tcp:
                    self._cache_put(q, resp)
                return ResolveResult(resp, cached=False, upstream=upstream)
            except asyncio.TimeoutError:
                last_err = f"timeout talking to {upstream}"
                logger.debug(last_err)
            except Exception as e:
                last_err = f"{upstream}: {e}"
                logger.debug("upstream error %s", last_err)
        return ResolveResult(None, error=last_err or "all upstreams failed")
