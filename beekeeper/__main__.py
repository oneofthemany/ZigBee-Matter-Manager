"""Beekeeper sidecar entrypoint: ``python -m beekeeper``.

Starts the loopback control API (always) and the DNS listeners (when
``beekeeper.enabled`` is true in config.yaml). Keeping the control API up even
while the resolver is off lets the ZMM UI show status and flip the service on
without a systemd round-trip. Runs until SIGTERM/SIGINT, then shuts down
cleanly so the SQLite writer flushes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import uvicorn

from .config import Config
from .control import build_app
from .server import BeekeeperServer

logging.basicConfig(
    level=os.environ.get("BEEKEEPER_LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("beekeeper")


async def _amain() -> None:
    cfg = Config.load()
    server = BeekeeperServer(cfg)

    # Bind :53 if the persisted master switch says so, else the config default.
    if server.boot_should_bind(cfg.enabled):
        try:
            await server.start()
        except OSError as e:
            # e.g. :53 held by systemd-resolved. Keep the control API up so the
            # UI can report the problem and the operator can fix the port.
            logger.error("Beekeeper DNS listeners failed to bind (%s). "
                         "Control API stays up; resolver is OFF. See docs/beekeeper.md "
                         "for the systemd-resolved port-53 fix.", e)
    else:
        logger.info("Beekeeper resolver OFF (master switch/config) — control API only")

    control = build_app(server)
    uv_config = uvicorn.Config(control, host=cfg.control_host, port=cfg.control_port,
                               log_level="warning", access_log=False)
    uv_server = uvicorn.Server(uv_config)

    stop_event = asyncio.Event()

    def _request_stop(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # e.g. on platforms without signal handlers
            pass

    control_task = asyncio.create_task(uv_server.serve())
    logger.info("Beekeeper control API on http://%s:%d", cfg.control_host, cfg.control_port)

    await stop_event.wait()
    logger.info("shutting down Beekeeper")
    uv_server.should_exit = True
    await control_task
    await server.stop()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
