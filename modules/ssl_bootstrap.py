"""
Self-signed HTTPS certificate bootstrap — the single source of truth, used by
main.py's entry point.

Auto-generates on first boot and never regenerates an existing pair. SANs cover
localhost, the hostname and every non-loopback IPv4, plus ZMM_CERT_SANS for
bridged containers. See docs/security.md.
"""
import ipaddress
import logging
import os
import socket
import subprocess

logger = logging.getLogger("modules.ssl_bootstrap")

DEFAULT_CERT = "./data/certs/cert.pem"
DEFAULT_KEY = "./data/certs/key.pem"

SANS_ENV = "ZMM_CERT_SANS"


def _local_ipv4s() -> list[str]:
    """Every non-loopback IPv4 this host can see. Best-effort; never raises."""
    ips: set[str] = set()

    # UDP "connect" to a TEST-NET address: sends nothing, but makes the kernel
    # pick the outbound interface, which is the address clients reach us on.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))   # RFC5737 TEST-NET-1, never routed
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass

    return sorted(i for i in ips if not i.startswith("127."))


def _classify(value: str) -> str | None:
    """'1.2.3.4' -> 'IP:1.2.3.4'; 'zmm.local' -> 'DNS:zmm.local'; junk -> None."""
    v = value.strip()
    if not v:
        return None
    try:
        ipaddress.ip_address(v)
        return f"IP:{v}"
    except ValueError:
        # openssl would choke on a SAN containing a comma or space.
        return f"DNS:{v}" if all(c not in v for c in ", \t") else None


def build_san(hostname: str, extra: list[str] | None = None) -> str:
    """
    The subjectAltName line. Deterministic order, de-duplicated, so two runs on
    the same host produce the same cert contents.
    """
    entries: list[str] = []
    seen: set[str] = set()

    def add(item: str | None) -> None:
        if item and item not in seen:
            seen.add(item)
            entries.append(item)

    add("DNS:localhost")
    add(_classify(hostname))
    add("IP:127.0.0.1")
    for ip in _local_ipv4s():
        add(f"IP:{ip}")
    for e in (extra or []):
        add(_classify(e))

    return "subjectAltName=" + ",".join(entries)


def ensure_self_signed_cert(cert_path: str = DEFAULT_CERT,
                            key_path: str = DEFAULT_KEY,
                            hostname: str | None = None,
                            extra_sans: list[str] | None = None) -> str:
    """
    Ensure a self-signed cert/key pair exists at the given paths.

    Returns one of:
      "preserved"        — both files already existed; left untouched
      "generated"        — a fresh pair was created
      "failed:<reason>"  — generation was needed but failed (caller decides
                            whether to fall back to HTTP)
    """
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return "preserved"

    cert_dir = os.path.dirname(cert_path) or "."
    try:
        os.makedirs(cert_dir, exist_ok=True)
    except Exception as e:
        logger.error("Could not create cert dir %s: %s", cert_dir, e)
        return f"failed:mkdir:{e}"

    hostname = hostname or socket.gethostname() or "zigbee-manager"

    extras = list(extra_sans or [])
    env_sans = os.environ.get(SANS_ENV, "")
    if env_sans:
        extras.extend(p for p in env_sans.split(",") if p.strip())

    san = build_san(hostname, extras)
    logger.warning("Generating self-signed cert at %s (CN=%s, SAN=%s)",
                   cert_path, hostname, san)

    try:
        result = subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_path, "-out", cert_path,
                "-days", "3650", "-nodes",
                "-subj", f"/CN={hostname}",
                "-addext", san,
            ],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        logger.error("openssl binary not found — cannot generate self-signed cert")
        return "failed:openssl-missing"
    except Exception as e:
        logger.error("openssl invocation failed: %s", e)
        return f"failed:{e}"

    if result.returncode != 0:
        logger.error("openssl failed: %s", result.stderr)
        return f"failed:{(result.stderr or '').strip()[:160]}"

    try:
        os.chmod(key_path, 0o600)
    except Exception as e:
        logger.warning("Could not chmod key file %s: %s", key_path, e)

    return "generated"
