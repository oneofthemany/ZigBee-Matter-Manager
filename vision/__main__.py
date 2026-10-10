"""Sidecar entrypoint: ``python -m vision``."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import PORT
from .detector import Detector
from .server import Hub, load_token, serve

DATA = Path(os.environ.get("ZMM_VISION_DIR", "./data/vision"))
LOGS = Path(os.environ.get("ZMM_LOGS_DIR", "./logs"))
LOAD_RETRY_S = 60


def _logging() -> None:
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(os.environ.get("ZMM_VISION_LOG_LEVEL", "INFO"))
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(LOGS / "vision.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("no vision.log: %s", e)


def main() -> None:
    _logging()
    log = logging.getLogger("vision")
    detector = Detector(DATA / "models", want=os.environ.get("ZMM_VISION_BACKEND", "cpu"),
                        threads=int(os.environ.get("ZMM_VISION_THREADS", "2")))
    hub = Hub(detector)
    host, port = os.environ.get("ZMM_VISION_HOST", "127.0.0.1"), int(os.environ.get("ZMM_VISION_PORT", PORT))
    serve(hub, load_token(DATA / "token"), host, port)
    log.info("listening on %s:%s", host, port)

    def load() -> None:
        # The API is up first so ZMM can show "downloading the model" rather than "down".
        while not detector.ready:
            try:
                detector.load()
            except Exception as e:                        # noqa: BLE001
                detector.note = f"can't load the model: {e}"
                log.error(detector.note)
                time.sleep(LOAD_RETRY_S)
        hub.bump()

    threading.Thread(target=load, name="load", daemon=True).start()
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    for w in list(hub.workers.values()):
        w.stop()


if __name__ == "__main__":
    main()
