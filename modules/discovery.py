"""
mDNS advertisement — so the companion app can find the hub.

The point is not saving an IP address: a geofence reports when you leave home,
exactly when a LAN address stops resolving, so the app must pair against the
tunnel URL — the one thing a user cannot guess and will mistype. Advertising it
on the home network hands the phone that address while it is somewhere
trustworthy. It carries no secrets; pairing still needs a token and, for a
self-signed cert, a hand-confirmed fingerprint.
"""

from __future__ import annotations

import logging
import socket
from typing import Any, Dict, List, Optional

logger = logging.getLogger("modules.discovery")

SERVICE_TYPE = "_zmm._tcp.local."

# mDNS TXT values are bytes and the whole record wants to stay inside one
# packet. A tunnel hostname is short; anything longer than this is a
# misconfiguration worth noticing rather than truncating silently.
MAX_TXT_VALUE = 255


def _primary_ipv4() -> Optional[str]:
    """
    The address other hosts on the LAN would reach us on.

    Uses a UDP connect to a public address, which selects a route without
    sending anything — more reliable than gethostbyname, which frequently
    answers 127.0.0.1 on Linux.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class HubAdvertiser:
    """Publishes this hub on the local network over mDNS."""

    def __init__(
            self,
            port: int,
            public_url: str = "",
            https: bool = True,
            instance_name: str = "",
    ) -> None:
        self.port = int(port)
        self.public_url = (public_url or "").strip().rstrip("/")
        self.https = https
        self.instance_name = instance_name or socket.gethostname().split(".")[0] or "zmm"
        self._zc = None
        self._info = None

    def _txt(self) -> Dict[bytes, bytes]:
        local_url = f"{'https' if self.https else 'http'}://{_primary_ipv4() or ''}:{self.port}"
        txt: Dict[bytes, bytes] = {
            b"path": b"/",
            b"local_url": local_url.encode()[:MAX_TXT_VALUE],
        }
        if self.public_url:
            # The value the phone should actually pair with. Named explicitly
            # rather than implied, so a client can tell "no tunnel configured"
            # from "tunnel configured but unreachable".
            txt[b"public_url"] = self.public_url.encode()[:MAX_TXT_VALUE]
        return txt

    def start(self) -> bool:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.info("[discovery] zeroconf not available; not advertising")
            return False

        ip = _primary_ipv4()
        if not ip:
            logger.warning("[discovery] no LAN address found; not advertising")
            return False

        try:
            self._zc = Zeroconf()
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.instance_name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties=self._txt(),
                server=f"{self.instance_name}.local.",
            )
            self._zc.register_service(self._info)
            logger.info(
                "[discovery] advertising %s on %s:%d (public_url=%s)",
                SERVICE_TYPE, ip, self.port, self.public_url or "none",
            )
            return True
        except Exception as e:                                  # noqa: BLE001
            # Never fatal: mDNS is blocked on plenty of networks, and the app
            # still works with a manually typed address.
            logger.warning("[discovery] could not advertise: %s", e)
            self.stop()
            return False

    def update_public_url(self, url: str) -> None:
        """Re-advertise after the tunnel hostname changes."""
        new = (url or "").strip().rstrip("/")
        if new == self.public_url:
            return
        self.public_url = new
        if self._zc and self._info:
            try:
                self._info.properties = self._txt()
                self._zc.update_service(self._info)
                logger.info("[discovery] public_url updated to %s", new or "none")
            except Exception as e:                              # noqa: BLE001
                logger.warning("[discovery] update failed: %s", e)

    def stop(self) -> None:
        try:
            if self._zc and self._info:
                self._zc.unregister_service(self._info)
        except Exception:                                       # noqa: BLE001
            pass
        try:
            if self._zc:
                self._zc.close()
        except Exception:                                       # noqa: BLE001
            pass
        self._zc = self._info = None


_advertiser: Optional[HubAdvertiser] = None


def get_advertiser() -> Optional[HubAdvertiser]:
    return _advertiser


def set_advertiser(a: Optional[HubAdvertiser]) -> None:
    global _advertiser
    _advertiser = a
