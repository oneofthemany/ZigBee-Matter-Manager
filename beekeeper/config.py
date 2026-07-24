"""Beekeeper configuration: the ``beekeeper:`` section of config.yaml + defaults.

The sidecar is a separate process from the main ZMM app, so it reads
config.yaml itself rather than being handed a config object. Paths are resolved
from the repo root (this file's grandparent) so it works regardless of the
process' working directory, with env overrides for containerised layouts.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("beekeeper.config")

# Repo root = parent of the beekeeper/ package directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    override = os.environ.get("ZMM_CONFIG")
    if override:
        return Path(override)
    return _REPO_ROOT / "config" / "config.yaml"


def _data_dir() -> Path:
    override = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR")
    base = Path(override) / "data" if override else _REPO_ROOT / "data"
    return base / "beekeeper"


def logs_dir() -> Path:
    """Where beekeeper.log lives. Sits alongside the app's other logs in
    ``${DATA_DIR}/logs`` so the manager's file-log streamer picks it up exactly
    like launcher.log / zigbee.log; falls back to ``<repo>/logs`` for source runs.
    """
    override = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR")
    return (Path(override) / "logs") if override else (_REPO_ROOT / "logs")


# Shipped defaults. Two conservative, widely-used lists so blocking works on
# first run; the user prunes/extends these from the UI. These are DATA — the
# domains live in files under data/beekeeper/lists/ once fetched.
DEFAULT_BLOCKLISTS: List[Dict[str, Any]] = [
    # The community "block, don't break" default: HaGeZi Multi PRO (ads +
    # tracking + malware, tuned to minimise breakage) plus OISD Big as a broad,
    # conservative safety net. Comprehensive without the false positives of the
    # aggressive/parental-control lists (NSFW, anti-piracy, DNS-bypass, …).
    {
        "name": "HaGeZi Multi PRO",
        "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/hosts/pro.txt",
        "enabled": True,
    },
    {
        "name": "OISD Big",
        "url": "https://big.oisd.nl/domainswild",
        "enabled": True,
    },
]

DEFAULT_UPSTREAMS = ["1.1.1.1", "9.9.9.9"]


@dataclass
class Config:
    enabled: bool = False

    # Listener. address="" → auto-detect the primary LAN IPv4 at startup so the
    # sinkhole serves the whole network without colliding with systemd-resolved
    # on 127.0.0.53 (see docs/beekeeper.md).
    listen_address: str = ""
    listen_port: int = 53

    upstreams: List[str] = field(default_factory=lambda: list(DEFAULT_UPSTREAMS))
    upstream_timeout: float = 3.0

    sinkhole_mode: str = "zero"        # "zero" | "nxdomain"
    sinkhole_ipv4: str = "0.0.0.0"
    sinkhole_ipv6: str = "::"
    sinkhole_ttl: int = 60

    cache_enabled: bool = True
    cache_max_entries: int = 10000
    cache_ttl: int = 300               # fixed positive-cache cap (seconds)

    blocklists: List[Dict[str, Any]] = field(
        default_factory=lambda: [dict(b) for b in DEFAULT_BLOCKLISTS])
    refresh_interval_hours: float = 24.0

    control_host: str = "127.0.0.1"
    control_port: int = 8053

    log_queries: bool = True
    query_retention_days: int = 7

    # Resolved at runtime, not from yaml.
    data_dir: Path = field(default_factory=_data_dir)

    # ── Derived paths ────────────────────────────────────────────────────────
    @property
    def lists_dir(self) -> Path:
        return self.data_dir / "lists"

    @property
    def stats_db(self) -> Path:
        return self.data_dir / "stats.db"

    @property
    def state_file(self) -> Path:
        # Runtime overrides the UI can set without editing yaml (paused-until,
        # enable toggle, per-session custom allow/deny — see server/control).
        return self.data_dir / "state.json"

    @property
    def allowlist_file(self) -> Path:
        return self.data_dir / "allowlist.txt"

    @property
    def denylist_file(self) -> Path:
        return self.data_dir / "denylist.txt"

    @property
    def sources_file(self) -> Path:
        # User-managed blocklist sources (seeded from config.yaml on first use).
        # Once this exists it is authoritative, so UI edits survive restarts
        # without rewriting the commented config.yaml.
        return self.data_dir / "sources.json"

    # ── Loading ──────────────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "Config":
        raw: Dict[str, Any] = {}
        path = _config_path()
        try:
            with open(path) as f:
                raw = (yaml.safe_load(f) or {}).get("beekeeper") or {}
        except FileNotFoundError:
            logger.warning("config.yaml not found at %s — using Beekeeper defaults", path)
        except Exception as e:  # malformed yaml shouldn't crash the resolver
            logger.error("failed to read %s: %s — using Beekeeper defaults", path, e)

        cfg = cls()
        cfg.enabled = bool(raw.get("enabled", cfg.enabled))

        listen = raw.get("listen") or {}
        cfg.listen_address = str(listen.get("address", cfg.listen_address) or "")
        cfg.listen_port = int(listen.get("port", cfg.listen_port))

        ups = raw.get("upstreams")
        if isinstance(ups, list) and ups:
            cfg.upstreams = [str(u).strip() for u in ups if str(u).strip()]
        cfg.upstream_timeout = float(raw.get("upstream_timeout", cfg.upstream_timeout))

        sink = raw.get("sinkhole") or {}
        cfg.sinkhole_mode = str(sink.get("mode", cfg.sinkhole_mode)).lower()
        cfg.sinkhole_ipv4 = str(sink.get("ipv4", cfg.sinkhole_ipv4))
        cfg.sinkhole_ipv6 = str(sink.get("ipv6", cfg.sinkhole_ipv6))
        cfg.sinkhole_ttl = int(sink.get("ttl", cfg.sinkhole_ttl))

        cache = raw.get("cache") or {}
        cfg.cache_enabled = bool(cache.get("enabled", cfg.cache_enabled))
        cfg.cache_max_entries = int(cache.get("max_entries", cfg.cache_max_entries))
        cfg.cache_ttl = int(cache.get("ttl", cfg.cache_ttl))

        bl = raw.get("blocklists")
        if isinstance(bl, list):
            cfg.blocklists = [
                {
                    "name": str(item.get("name") or item.get("url") or "list"),
                    "url": str(item.get("url") or "").strip(),
                    "enabled": bool(item.get("enabled", True)),
                }
                for item in bl if isinstance(item, dict) and item.get("url")
            ]
        cfg.refresh_interval_hours = float(
            raw.get("refresh_interval_hours", cfg.refresh_interval_hours))

        control = raw.get("control") or {}
        cfg.control_host = str(control.get("host", cfg.control_host))
        cfg.control_port = int(control.get("port", cfg.control_port))

        cfg.log_queries = bool(raw.get("log_queries", cfg.log_queries))
        cfg.query_retention_days = int(
            raw.get("query_retention_days", cfg.query_retention_days))

        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.sinkhole_mode not in ("zero", "nxdomain"):
            logger.warning("unknown sinkhole mode %r — falling back to 'zero'",
                           self.sinkhole_mode)
            self.sinkhole_mode = "zero"
        # Fail loudly-but-safely on a bad sinkhole IP rather than at query time.
        try:
            ipaddress.IPv4Address(self.sinkhole_ipv4)
        except ValueError:
            logger.error("bad sinkhole ipv4 %r — using 0.0.0.0", self.sinkhole_ipv4)
            self.sinkhole_ipv4 = "0.0.0.0"
        try:
            ipaddress.IPv6Address(self.sinkhole_ipv6)
        except ValueError:
            logger.error("bad sinkhole ipv6 %r — using ::", self.sinkhole_ipv6)
            self.sinkhole_ipv6 = "::"

    def resolve_listen_address(self) -> str:
        """The address to bind. Empty config → primary LAN IPv4 (best effort).

        We open a throwaway UDP socket toward a public IP; the OS picks the
        egress interface and its source address without sending a packet. Falls
        back to 0.0.0.0 (all interfaces) if detection fails.
        """
        if self.listen_address:
            return self.listen_address
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("1.1.1.1", 53))
                addr = s.getsockname()[0]
            finally:
                s.close()
            if addr and not addr.startswith("127."):
                return addr
        except OSError as e:
            logger.warning("LAN IP auto-detect failed (%s); binding 0.0.0.0", e)
        return "0.0.0.0"

    def to_public_dict(self) -> Dict[str, Any]:
        """Config as surfaced by the control API (no secrets here, but keep the
        shape stable and JSON-friendly for the UI)."""
        return {
            "enabled": self.enabled,
            "listen": {"address": self.listen_address, "port": self.listen_port,
                       "resolved_address": self.resolve_listen_address()},
            "upstreams": list(self.upstreams),
            "upstream_timeout": self.upstream_timeout,
            "sinkhole": {"mode": self.sinkhole_mode, "ipv4": self.sinkhole_ipv4,
                         "ipv6": self.sinkhole_ipv6, "ttl": self.sinkhole_ttl},
            "cache": {"enabled": self.cache_enabled,
                      "max_entries": self.cache_max_entries, "ttl": self.cache_ttl},
            "blocklists": [dict(b) for b in self.blocklists],
            "refresh_interval_hours": self.refresh_interval_hours,
            "log_queries": self.log_queries,
            "query_retention_days": self.query_retention_days,
        }


def ensure_data_dirs(cfg: Config) -> None:
    """Create data/beekeeper/{,lists} on first run."""
    cfg.lists_dir.mkdir(parents=True, exist_ok=True)
