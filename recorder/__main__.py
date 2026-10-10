"""Sidecar entrypoint: ``python -m recorder``."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import PORT
from .core import Recorder
from .server import load_token, serve
from .store import ROOT, Store

LOGS = Path(os.environ.get("ZMM_LOGS_DIR", "./logs"))


def _logging() -> None:
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(os.environ.get("ZMM_RECORDER_LOG_LEVEL", "INFO"))
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(LOGS / "recorder.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("no recorder.log: %s", e)


async def _amain() -> None:
    log = logging.getLogger("recorder")
    store = Store(ROOT)
    rec = Recorder(store)
    # What the app last asked for: recording resumes without waiting for it.
    saved = store.load_config()
    if saved:
        try:
            rec.configure(saved, persist=False)
            log.info("resuming %d camera(s) from the saved config", len(saved))
        except ValueError as e:
            log.warning("saved config ignored: %s", e)
    host, port = os.environ.get("ZMM_RECORDER_HOST", "127.0.0.1"), int(os.environ.get("ZMM_RECORDER_PORT", PORT))
    loop = asyncio.get_running_loop()
    serve(rec, load_token(ROOT / "token"), host, port, loop)
    log.info("listening on %s:%s, recording to %s", host, port, ROOT)
    task = asyncio.create_task(rec.run())
    done = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, done.set)
    await done.wait()
    task.cancel()
    rec.stop()
    await asyncio.sleep(1)                               # ffmpeg closes its segments


if __name__ == "__main__":
    _logging()
    asyncio.run(_amain())
