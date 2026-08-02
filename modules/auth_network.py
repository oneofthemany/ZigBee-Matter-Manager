"""
Source-IP resolution and LAN classification.

Resolves the real client IP behind Cloudflare Tunnel, a reverse proxy, Tailscale
or direct access — trusting forwarding headers ONLY from a configured
trusted-proxy peer, since otherwise they are trivially spoofed. Also classifies
an IP as LAN-or-not for the network:lan_only scope. See docs/security.md.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Dict, Iterable, List, Optional, Sequence

from fastapi import Request

logger = logging.getLogger("modules.auth_network")


# Default LAN ranges. Cover RFC1918, loopback, link-local, CGNAT (which
# includes Tailscale's 100.64.0.0/10 default), and the common IPv6 ranges.
DEFAULT_LAN_RANGES: List[str] = [
    "127.0.0.0/8",      # IPv4 loopback
    "10.0.0.0/8",       # RFC1918
    "172.16.0.0/12",    # RFC1918
    "192.168.0.0/16",   # RFC1918
    "169.254.0.0/16",   # IPv4 link-local
    "100.64.0.0/10",    # CGNAT (includes Tailscale)
    "::1/128",          # IPv6 loopback
    "fc00::/7",         # IPv6 ULA
    "fe80::/10",        # IPv6 link-local
]

# Cloudflare's published IP ranges, as of late 2025. Users can override
# in config.yaml. Auto-update from https://www.cloudflare.com/ips/ is a
# nice-to-have not implemented here.
DEFAULT_CLOUDFLARE_RANGES: List[str] = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
]


def _parse_networks(items: Iterable[str]) -> List[ipaddress._BaseNetwork]:
    """Parse strings to network objects, skipping bad entries with a log."""
    out: List[ipaddress._BaseNetwork] = []
    for s in items:
        try:
            out.append(ipaddress.ip_network(s.strip(), strict=False))
        except (ValueError, TypeError) as e:
            logger.warning(f"[network] ignoring bad CIDR {s!r}: {e}")
    return out


def _ip_in_any(addr: str, networks: Sequence[ipaddress._BaseNetwork]) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for net in networks:
        # Family must match — comparing IPv4 to v6 net raises
        if ip.version != net.version:
            continue
        if ip in net:
            return True
    return False


# NetworkResolver — single instance configured at startup

class NetworkResolver:
    """
    Configured once with the user's trusted-proxy / LAN policy. Exposes
    `resolve(request)` returning the real client IP, and `is_lan(ip)` for
    LAN-or-not classification.
    """

    def __init__(
            self,
            trusted_proxies: Optional[Iterable[str]] = None,
            lan_ranges: Optional[Iterable[str]] = None,
            cloudflare_tunnel_enabled: bool = False,
            cloudflare_ranges: Optional[Iterable[str]] = None,
    ):
        self.trusted_proxies = _parse_networks(
            trusted_proxies if trusted_proxies is not None
            else ["127.0.0.0/8"]    # default: only trust localhost proxies
        )
        self.lan_ranges = _parse_networks(
            lan_ranges if lan_ranges is not None else DEFAULT_LAN_RANGES
        )
        self.cloudflare_tunnel_enabled = cloudflare_tunnel_enabled
        self.cloudflare_ranges = _parse_networks(
            cloudflare_ranges if cloudflare_ranges is not None
            else DEFAULT_CLOUDFLARE_RANGES
        )
        self._cf_warn_ts: Dict[str, float] = {}

    # core: source IP resolution

    def resolve(self, request: Request) -> str:
        """
        Return the real client IP. Falls back to the immediate peer if no
        trusted forwarded-for header is present.
        """
        peer = self._peer_ip(request)

        # Cloudflare Tunnel: trust CF-Connecting-IP only if the peer is
        # itself in Cloudflare's range OR cloudflare_tunnel_enabled was
        # set with a permissive trust policy.
        if self.cloudflare_tunnel_enabled:
            cf = request.headers.get("cf-connecting-ip")
            if cf:
                # Trust peers in Cloudflare's published ranges. Running cloudflared
                # locally gives a peer of 127.0.0.1, so trusted_proxies (which
                # usually includes 127.0.0.0/8) is honoured too.
                cf_clean = cf.split(",")[0].strip()
                if (
                        _ip_in_any(peer, self.cloudflare_ranges)
                        or _ip_in_any(peer, self.trusted_proxies)
                ):
                    if cf_clean:
                        return cf_clean
                elif cf_clean == peer:
                    # An upstream layer (e.g. uvicorn proxy-headers) already
                    # resolved the client to the same address the header
                    # claims — nothing to distrust, and nothing to gain.
                    return peer
                else:
                    # Rate-limit: one warning per peer per 10 minutes instead
                    # of one per request.
                    now = time.time()
                    if now - self._cf_warn_ts.get(peer, 0.0) > 600:
                        self._cf_warn_ts[peer] = now
                        logger.warning(
                            f"[network] CF-Connecting-IP from untrusted peer "
                            f"{peer} — ignoring (suppressing repeats for 10 min)"
                        )

        # Generic reverse-proxy: trust X-Forwarded-For only if peer is in
        # trusted_proxies. The header is "client, proxy1, proxy2, ..." —
        # we want the leftmost untrusted IP.
        xff = request.headers.get("x-forwarded-for")
        if xff and _ip_in_any(peer, self.trusted_proxies):
            # Walk right-to-left, skipping any IP that's itself in the
            # trusted-proxies list. The first non-trusted IP is the real
            # client.
            chain = [p.strip() for p in xff.split(",") if p.strip()]
            for ip in reversed(chain):
                if not _ip_in_any(ip, self.trusted_proxies):
                    return ip
            # If everyone in the chain is trusted, fall back to the
            # leftmost — that's the supposed origin.
            if chain:
                return chain[0]

        # X-Real-IP from a trusted proxy
        xri = request.headers.get("x-real-ip")
        if xri and _ip_in_any(peer, self.trusted_proxies):
            return xri.strip()

        return peer

    def request_is_https(self, request: Request) -> bool:
        """
        True if the client-facing connection is HTTPS.

        Direct TLS is trivially detected from the URL scheme. Behind a
        reverse proxy or Cloudflare Tunnel the origin connection is plain
        HTTP, so we honour X-Forwarded-Proto — but only from a trusted
        peer, same policy as the forwarded-for headers.
        """
        if request.url.scheme == "https":
            return True
        peer = self._peer_ip(request)
        trusted = _ip_in_any(peer, self.trusted_proxies) or (
            self.cloudflare_tunnel_enabled
            and _ip_in_any(peer, self.cloudflare_ranges)
        )
        if not trusted:
            return False
        xfp = request.headers.get("x-forwarded-proto", "")
        return xfp.split(",")[0].strip().lower() == "https"

    def _peer_ip(self, request: Request) -> str:
        """Immediate TCP peer per Starlette."""
        client = request.client
        if not client:
            return ""
        return client.host or ""

    # LAN classification

    def is_lan(self, ip: str) -> bool:
        return _ip_in_any(ip, self.lan_ranges)

    # introspection

    def describe(self) -> dict:
        return {
            "trusted_proxies": [str(n) for n in self.trusted_proxies],
            "lan_ranges": [str(n) for n in self.lan_ranges],
            "cloudflare_tunnel_enabled": self.cloudflare_tunnel_enabled,
        }


# Module-level singleton (set by main.py at startup)
_resolver: Optional[NetworkResolver] = None


def get_network_resolver() -> Optional[NetworkResolver]:
    return _resolver


def set_network_resolver(r: NetworkResolver) -> None:
    global _resolver
    _resolver = r