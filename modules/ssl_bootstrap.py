"""
Self-signed cert bootstrap.

Single source of truth for generating the app's self-signed HTTPS cert, shared by:
  - main.py's entry point — auto-generates on first boot so the app serves
    HTTPS out of the box (no user configuration needed), which is what the
    watchdog / manager / container healthcheck all expect.
  - the /api/ssl/toggle route — the manual "Enable HTTPS" switch in Settings.

Design rules (kept identical to the original route logic):
  - NEVER regenerate an existing pair — browsers that already trust the cert
    would break, which is the leading cause of "this site is unsafe" after a
    config tweak.
  - Sensible SANs (localhost, hostname, 127.0.0.1) so the internal probes and
    localhost browsing don't hit name/IP mismatches.
  - Lock the private key to 0600 (openssl writes 0644 by default).
"""
import logging
import os
import socket
import subprocess

logger = logging.getLogger("modules.ssl_bootstrap")

DEFAULT_CERT = "./data/certs/cert.pem"
DEFAULT_KEY = "./data/certs/key.pem"


def ensure_self_signed_cert(cert_path: str = DEFAULT_CERT,
                            key_path: str = DEFAULT_KEY,
                            hostname: str | None = None) -> str:
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
    san = f"subjectAltName=DNS:localhost,DNS:{hostname},IP:127.0.0.1"
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
