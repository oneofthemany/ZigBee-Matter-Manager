"""Entry point: `python -m manager` → uvicorn serving the sidecar on :8001.

CP2a serves plain HTTP on :8001 (simplest to test; the app's HTTPS cert handling
can be matched later). Access it at http://<host>:8001 — note: HTTP, unlike the
app's HTTPS on :8000.
"""
import os

import uvicorn


def main():
    port = int(os.environ.get("ZMM_MANAGER_PORT", "8001"))
    uvicorn.run("manager.app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
