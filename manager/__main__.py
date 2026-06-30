"""Entry point: `python -m manager` → uvicorn serving the sidecar on :8001.

The manager matches the app's scheme: if the app's self-signed TLS cert exists in
the (mounted) data dir, it serves HTTPS so it's reachable at the SAME scheme as
the app — https://<host>:8001 — which also satisfies the HSTS that the app's
https://:8000 imposes on the whole host. If no cert is present (app running plain
HTTP), it falls back to HTTP on :8001.
"""
import os

import uvicorn


def _tls_paths():
    """Return (cert, key) reusing the app's self-signed pair if both exist, else
    (None, None) → plain HTTP. Honours ZMM_MANAGER_CERT/KEY, then the app's
    default data/certs/ location under ZMM_DATA_DIR."""
    data_dir = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
        or "/opt/.zigbee-matter-manager"
    cert = os.environ.get("ZMM_MANAGER_CERT") \
        or os.path.join(data_dir, "data", "certs", "cert.pem")
    key = os.environ.get("ZMM_MANAGER_KEY") \
        or os.path.join(data_dir, "data", "certs", "key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    return None, None


def main():
    port = int(os.environ.get("ZMM_MANAGER_PORT", "8001"))
    cert, key = _tls_paths()
    kwargs = {"host": "0.0.0.0", "port": port, "log_level": "info"}
    if cert and key:
        kwargs["ssl_certfile"] = cert
        kwargs["ssl_keyfile"] = key
        print(f"[manager] serving HTTPS on :{port} (cert={cert})", flush=True)
    else:
        print(f"[manager] no TLS cert found — serving HTTP on :{port}", flush=True)
    uvicorn.run("manager.app:app", **kwargs)


if __name__ == "__main__":
    main()
