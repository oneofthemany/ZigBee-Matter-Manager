"""
The DNS sinkhole server — UDP and TCP on :53, tying matcher, resolver and stats
together.

Per query: parse the question, decide block vs forward (respecting the
enable/pause state and the allow/deny/block sets), answer, and log. Runtime
controls come from the control API and persist to state.json so they survive a
restart. See docs/beekeeper.md.
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

    # query handling
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

    # blocklist sources (user-editable, persisted to sources.json)
    def sources(self) -> list:
        """The effective blocklist sources. sources.json wins once it exists;
        otherwise the config.yaml defaults (which we seed into it on first use)."""
        path = self.cfg.sources_file
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [self._norm_source(s) for s in data if isinstance(s, dict) and s.get("url")]
        except (OSError, ValueError):
            pass
        # Seed from config defaults so the UI has something to edit from the start.
        seed = [self._norm_source(s) for s in self.cfg.blocklists]
        self._save_sources(seed)
        return seed

    @staticmethod
    def _norm_source(s: dict) -> dict:
        url = str(s.get("url") or "").strip()
        name = str(s.get("name") or url or "list").strip()
        return {"name": name, "url": url, "enabled": bool(s.get("enabled", True)),
                "slug": blocklists.slugify(name or url)}

    def _save_sources(self, sources: list) -> None:
        try:
            self.cfg.sources_file.parent.mkdir(parents=True, exist_ok=True)
            self.cfg.sources_file.write_text(json.dumps(sources, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("could not persist sources.json: %s", e)

    async def add_source(self, name: str, url: str) -> dict:
        url = (url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "error": "URL must start with http:// or https://"}
        sources = self.sources()
        if any(s["url"] == url for s in sources):
            return {"ok": False, "error": "that URL is already in the list"}
        sources.append(self._norm_source({"name": name or url, "url": url, "enabled": True}))
        self._save_sources(sources)
        return await self.refresh_now()   # fetch the new list + recompile

    async def remove_source(self, key: str) -> dict:
        """Remove by url or slug."""
        sources = self.sources()
        kept = [s for s in sources if s["url"] != key and s["slug"] != key]
        if len(kept) == len(sources):
            return {"ok": False, "error": "source not found"}
        self._save_sources(kept)
        await self.reload_matcher()
        return {"ok": True, "sources": kept}

    async def set_source_enabled(self, key: str, enabled: bool) -> dict:
        sources = self.sources()
        found = False
        for s in sources:
            if s["url"] == key or s["slug"] == key:
                s["enabled"] = enabled
                found = True
        if not found:
            return {"ok": False, "error": "source not found"}
        self._save_sources(sources)
        # Enabling may need a fetch (no cached file yet); recompile either way.
        if enabled:
            return await self.refresh_now()
        await self.reload_matcher()
        return {"ok": True, "sources": sources}

    # matcher / refresh
    def _enabled_slugs(self) -> Set[str]:
        return {s["slug"] for s in self.sources() if s.get("enabled", True)}

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
                blocklists.refresh_lists, self.cfg.lists_dir, self.sources())
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

    # runtime controls (called by control API)
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

    async def dig(self, domain: str, qtype: int = 1) -> dict:
        """Run a real query through the resolver and report the answer — the
        in-app equivalent of ``dig``. Exercises the full block/forward path so
        the result is exactly what a device on the network would get."""
        import random
        domain = (domain or "").strip().strip(".").lower()
        if not domain:
            return {"ok": False, "error": "no domain given"}
        query = wire.build_query(domain, qtype, txid=random.randint(0, 0xFFFF))
        try:
            q = wire.parse_question(query)
        except wire.DNSFormatError as e:
            return {"ok": False, "error": f"bad domain: {e}"}

        t0 = time.perf_counter()
        blocked, reason = (self.matcher.is_blocked(q.qname)
                           if self.state.blocking_active else (False, None))
        upstream = None
        cached = False
        if blocked:
            resp = wire.build_block_response(
                query, q, mode=self.cfg.sinkhole_mode, ipv4=self.cfg.sinkhole_ipv4,
                ipv6=self.cfg.sinkhole_ipv6, ttl=self.cfg.sinkhole_ttl)
        else:
            result = await self.resolver.resolve(query, q)
            if result.response is None:
                return {"ok": False, "domain": domain, "error": result.error or
                        "upstream unreachable", "elapsed_ms": (time.perf_counter() - t0) * 1000}
            resp = result.response
            upstream = result.upstream
            cached = result.cached
        rcode = struct.unpack_from("!H", resp, 2)[0] & 0xF if len(resp) >= 4 else None
        return {
            "ok": True, "domain": domain, "qtype": qtype,
            "blocked": blocked, "reason": reason,
            "blocking_active": self.state.blocking_active,
            "rcode": rcode, "rcode_name": wire.RCODE_NAMES.get(rcode, str(rcode)),
            "answers": wire.parse_answers(resp),
            "cached": cached, "upstream": upstream,
            "resolver": self.bound_address,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

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
