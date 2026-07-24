"""The DNS sinkhole server: UDP + TCP on :53, tying matcher/resolver/stats.

Per query: parse the question, decide block vs. forward (respecting the
enable/pause runtime state and the allow/deny/block sets), answer, and log.
Blocked names get a synthesised sinkhole/NXDOMAIN answer; everything else is
forwarded upstream. Runtime controls (enable, disable, pause, refresh, reload)
are driven by the control API and persisted to ``state.json`` so they survive a
restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Optional, Set

from . import blocklists, wire
from .config import Config, ensure_data_dirs
from .resolver import UpstreamResolver
from .stats import Stats

logger = logging.getLogger("beekeeper.server")


class RuntimeState:
    """User-toggleable state that outlives a restart (kept out of config.yaml so
    the UI can flip it without rewriting yaml)."""

    def __init__(self, path):
        self.path = path
        self.enabled = True              # blocking on/off (vs. pass-through resolve)
        self.paused_until = 0.0
        # Whether the resolver should bind :53 at boot. None → follow the
        # config.yaml default; True/False once the user has flipped the master
        # switch, so it survives a restart without rewriting config.yaml.
        self.service_enabled = None
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.enabled = bool(data.get("enabled", True))
            self.paused_until = float(data.get("paused_until", 0.0))
            se = data.get("service_enabled", None)
            self.service_enabled = None if se is None else bool(se)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(
                {"enabled": self.enabled, "paused_until": self.paused_until,
                 "service_enabled": self.service_enabled}),
                encoding="utf-8")
        except OSError as e:
            logger.warning("could not persist runtime state: %s", e)

    @property
    def blocking_active(self) -> bool:
        if not self.enabled:
            return False
        if self.paused_until and time.time() < self.paused_until:
            return False
        return True

    def status(self) -> dict:
        paused = bool(self.paused_until and time.time() < self.paused_until)
        return {"enabled": self.enabled, "blocking_active": self.blocking_active,
                "paused": paused,
                "paused_until": self.paused_until if paused else 0.0}


class _UDPServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "BeekeeperServer"):
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 12 or self.transport is None:
            return  # too short to be a DNS message with an id — drop
        # Handle off the receive callback so one slow upstream can't stall others.
        asyncio.create_task(self.server.handle_udp(data, addr, self.transport))


class BeekeeperServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.matcher = blocklists.Matcher()
        self.resolver = UpstreamResolver(
            cfg.upstreams, timeout=cfg.upstream_timeout,
            cache_enabled=cfg.cache_enabled, cache_max=cfg.cache_max_entries,
            cache_ttl=cfg.cache_ttl)
        self.stats = Stats(cfg.stats_db, retention_days=cfg.query_retention_days,
                           enabled=cfg.log_queries)
        self.state = RuntimeState(cfg.state_file)
        self._udp_transport: Optional[asyncio.DatagramTransport] = None
        self._tcp_server: Optional[asyncio.AbstractServer] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._refreshing = False
        self._last_refresh_meta: list = []
        self.bound_address: Optional[str] = None
        self.running = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.running:
            return
        ensure_data_dirs(self.cfg)
        self.stats.start()
        await self.reload_matcher()
        # First run with no cached lists → pull them in the background so the
        # resolver is up immediately (forwarding only until lists land).
        if self.matcher.block_count == 0:
            logger.info("no cached blocklists — scheduling initial refresh")
            asyncio.create_task(self.refresh_now())

        addr = self.cfg.resolve_listen_address()
        port = self.cfg.listen_port
        loop = asyncio.get_running_loop()
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPServerProtocol(self), local_addr=(addr, port))
        self._tcp_server = await asyncio.start_server(self._handle_tcp, addr, port)
        self.bound_address = f"{addr}:{port}"
        self.running = True
        logger.info("Beekeeper listening on %s (udp+tcp)", self.bound_address)
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self, keep_stats: bool = False) -> None:
        """Stop the DNS listeners. keep_stats leaves the stats writer running so
        a live stop/start (UI toggle) doesn't lose the counters/log thread."""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._udp_transport:
            self._udp_transport.close()
            self._udp_transport = None
        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        self.bound_address = None
        self.running = False
        if not keep_stats:
            self.stats.stop()

    # ── query handling ───────────────────────────────────────────────────────
    async def handle_udp(self, data: bytes, addr, transport) -> None:
        try:
            response = await self._process(data, tcp=False, client=addr[0])
            if response:
                transport.sendto(response, addr)
        except Exception:
            logger.exception("error handling UDP query from %s", addr)

    async def _handle_tcp(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = peer[0] if peer else "?"
        try:
            # A TCP connection may carry multiple length-prefixed messages.
            while True:
                hdr = await reader.readexactly(2)
                (length,) = struct.unpack("!H", hdr)
                if length == 0:
                    break
                query = await reader.readexactly(length)
                response = await self._process(query, tcp=True, client=client)
                if response:
                    writer.write(struct.pack("!H", len(response)) + response)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            logger.exception("error handling TCP query from %s", client)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _process(self, query: bytes, *, tcp: bool, client: str) -> Optional[bytes]:
        t0 = time.perf_counter()
        try:
            q = wire.parse_question(query)
        except wire.DNSFormatError:
            return wire.build_error_response(query, wire.RCODE_FORMERR)

        blocked = False
        reason = None
        if self.state.blocking_active:
            blocked, reason = self.matcher.is_blocked(q.qname)

        if blocked:
            response = wire.build_block_response(
                query, q, mode=self.cfg.sinkhole_mode,
                ipv4=self.cfg.sinkhole_ipv4, ipv6=self.cfg.sinkhole_ipv6,
                ttl=self.cfg.sinkhole_ttl)
            self.stats.record(client=client, qname=q.qname, qtype=q.qtype,
                              blocked=True, reason=reason, rcode=0,
                              elapsed_ms=(time.perf_counter() - t0) * 1000)
            return response

        result = await self.resolver.resolve(query, q, tcp=tcp)
        if result.response is None:
            self.stats.record(client=client, qname=q.qname, qtype=q.qtype,
                              blocked=False, upstream=result.upstream,
                              rcode=wire.RCODE_SERVFAIL,
                              elapsed_ms=(time.perf_counter() - t0) * 1000)
            return wire.build_error_response(query, wire.RCODE_SERVFAIL)

        rcode = None
        if len(result.response) >= 4:
            rcode = struct.unpack_from("!H", result.response, 2)[0] & 0xF
        self.stats.record(client=client, qname=q.qname, qtype=q.qtype,
                          blocked=False, cached=result.cached,
                          upstream=result.upstream, rcode=rcode,
                          elapsed_ms=(time.perf_counter() - t0) * 1000)
        return result.response

    # ── matcher / refresh ────────────────────────────────────────────────────
    def _enabled_slugs(self) -> Set[str]:
        return {blocklists.slugify(b.get("name") or b.get("url"))
                for b in self.cfg.blocklists if b.get("enabled", True)}

    async def reload_matcher(self) -> None:
        """Recompile the block/allow/deny sets off the event loop and swap in."""
        matcher = await asyncio.to_thread(
            blocklists.compile_matcher, self.cfg.lists_dir,
            self.cfg.allowlist_file, self.cfg.denylist_file, self._enabled_slugs())
        self.matcher = matcher

    async def refresh_now(self) -> dict:
        """Fetch every enabled source, then recompile. Safe to call repeatedly."""
        if self._refreshing:
            return {"ok": False, "error": "refresh already in progress"}
        self._refreshing = True
        try:
            metas = await asyncio.to_thread(
                blocklists.refresh_lists, self.cfg.lists_dir, self.cfg.blocklists)
            self._last_refresh_meta = [m.to_dict() for m in metas]
            await self.reload_matcher()
            return {"ok": True, "lists": self._last_refresh_meta,
                    "block_count": self.matcher.block_count}
        finally:
            self._refreshing = False

    async def _refresh_loop(self) -> None:
        interval = max(0.5, self.cfg.refresh_interval_hours) * 3600
        while True:
            try:
                await asyncio.sleep(interval)
                logger.info("scheduled blocklist refresh")
                await self.refresh_now()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("scheduled refresh failed")

    # ── runtime controls (called by control API) ─────────────────────────────
    def set_enabled(self, enabled: bool) -> None:
        self.state.enabled = enabled
        if enabled:
            self.state.paused_until = 0.0
        self.state.save()

    def pause(self, minutes: float) -> float:
        self.state.paused_until = time.time() + max(0.0, minutes) * 60
        self.state.save()
        return self.state.paused_until

    def resume(self) -> None:
        self.state.paused_until = 0.0
        self.state.save()

    def set_service_enabled(self, enabled: bool) -> None:
        """Persist the master boot switch (bind :53 on next start or not)."""
        self.state.service_enabled = enabled
        self.state.save()

    def boot_should_bind(self, config_default: bool) -> bool:
        """Whether to bind :53 at process start: the persisted master switch if
        the user has set it, else the config.yaml default."""
        if self.state.service_enabled is None:
            return config_default
        return self.state.service_enabled

    async def add_rule(self, kind: str, domain: str) -> bool:
        """Append a domain to the allow or deny list file and recompile."""
        domain = (domain or "").strip().lower().rstrip(".")
        if not domain:
            return False
        path = self.cfg.allowlist_file if kind == "allow" else self.cfg.denylist_file
        existing = blocklists._read_domain_file(path)
        cleaned = blocklists._clean_domain(domain)
        if not cleaned or cleaned in existing:
            await self.reload_matcher()
            return bool(cleaned)
        with open(path, "a", encoding="utf-8") as f:
            f.write(cleaned + "\n")
        await self.reload_matcher()
        return True

    async def remove_rule(self, kind: str, domain: str) -> bool:
        domain = (domain or "").strip().lower().rstrip(".")
        path = self.cfg.allowlist_file if kind == "allow" else self.cfg.denylist_file
        existing = blocklists._read_domain_file(path)
        if domain not in existing:
            return False
        existing.discard(domain)
        path.write_text("\n".join(sorted(existing)) + ("\n" if existing else ""),
                        encoding="utf-8")
        await self.reload_matcher()
        return True

    def list_rules(self, kind: str) -> list:
        path = self.cfg.allowlist_file if kind == "allow" else self.cfg.denylist_file
        return sorted(blocklists._read_domain_file(path))

    def check_domain(self, domain: str) -> dict:
        """Would this name be blocked right now? (UI 'test a domain' helper.)"""
        blocked, reason = self.matcher.is_blocked((domain or "").strip().lower())
        return {"domain": domain, "blocked": blocked, "reason": reason,
                "blocking_active": self.state.blocking_active}

    def status(self) -> dict:
        return {
            "running": self.running,
            "listening": self.bound_address,
            "runtime": self.state.status(),
            "matcher": {"blocked": len(self.matcher.blocked),
                        "denied": len(self.matcher.denied),
                        "allowed": len(self.matcher.allowed)},
            "cache": self.resolver.cache_stats(),
            "counters": self.stats.counters(),
            "refreshing": self._refreshing,
            "last_refresh": self._last_refresh_meta,
        }
