"""On-demand live log streaming for the manager (CP2b, read-only).

Two sources, both available even when the app container is down:

  - **File logs**: ``${DATA_DIR}/logs/*.log`` — the manager mounts DATA_DIR, so
    launcher.log, boot_guard.log, recovery.log, upgrade_watcher.log etc. are
    readable regardless of app/container state. Streams tail + follow, and
    survives log rotation (reopens when the file shrinks or is replaced).
  - **Container logs**: ``/containers/{name}/logs`` over the runtime socket
    (same mechanism as manager.containers). Only containers visible to
    manager.containers (deployment prefix + ZMM_EXTRA_CONTAINERS) are allowed.

Streams are Server-Sent Events (one ``data:`` event per log line) so the
dashboard can use a plain EventSource. Generators end when the client
disconnects — nothing streams unless a user asked for it.
"""
import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Dict, List

import httpx

from manager.containers import detect_socket, visible_container

logger = logging.getLogger("manager.logs")

DATA_DIR = os.environ.get("ZMM_DATA_DIR") or os.environ.get("DATA_DIR") \
    or "/opt/.zigbee-matter-manager"
LOG_DIR = Path(DATA_DIR) / "logs"

POLL_INTERVAL = 1.0          # seconds between follow polls on file logs
MAX_LINE_BYTES = 16 * 1024   # guard against pathological unbroken lines


def list_log_files() -> List[Dict]:
    """Names + sizes of the *.log files the manager can stream."""
    out: List[Dict] = []
    try:
        for p in sorted(LOG_DIR.glob("*.log")):
            try:
                out.append({"name": p.name, "size": p.stat().st_size})
            except OSError:
                continue
    except OSError as e:
        logger.warning("list_log_files: %s", e)
    return out


def resolve_log_file(name: str) -> Path | None:
    """Validate a requested log name (no traversal, must exist in LOG_DIR)."""
    if not name or name != os.path.basename(name) or not name.endswith(".log"):
        return None
    p = LOG_DIR / name
    return p if p.is_file() else None


def allowed_container(name: str) -> bool:
    """Same visibility rule as manager.containers.list_containers."""
    return visible_container(name)


def _sse(line: str) -> str:
    # One event per line; SSE data must not contain raw newlines.
    return f"data: {line}\n\n"


async def stream_file(path: Path, tail: int = 200) -> AsyncIterator[str]:
    """Yield the last `tail` lines of a file, then follow appends (tail -f)."""
    yield _sse(f"── streaming {path.name} (last {tail} lines) ──")
    f = None
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
        for line in deque(f, maxlen=tail):
            yield _sse(line.rstrip("\n"))
        pos = f.tell()
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                size = path.stat().st_size
            except OSError:
                # Rotated away — wait for it to reappear.
                yield _sse("── log file removed, waiting for rotation ──")
                f.close()
                f = None
                while f is None:
                    await asyncio.sleep(POLL_INTERVAL)
                    if path.is_file():
                        f = open(path, "r", encoding="utf-8", errors="replace")
                        pos = 0
                continue
            if size < pos:  # truncated/rotated in place — start over
                f.seek(0)
                pos = 0
            chunk = f.read()
            if chunk:
                pos = f.tell()
                for line in chunk.splitlines():
                    yield _sse(line[:MAX_LINE_BYTES])
    except asyncio.CancelledError:
        raise
    except Exception as e:
        yield _sse(f"── stream error: {e} ──")
    finally:
        if f is not None:
            f.close()


def _demux(buf: bytearray, framed: bool) -> tuple[bytes, bytearray]:
    """Extract payload bytes from a docker-multiplexed stream buffer.
    Returns (payload, remaining_buffer)."""
    if not framed:
        return bytes(buf), bytearray()
    payload = bytearray()
    while len(buf) >= 8:
        size = int.from_bytes(buf[4:8], "big")
        if len(buf) < 8 + size:
            break
        payload += buf[8:8 + size]
        del buf[:8 + size]
    return bytes(payload), buf


async def stream_container(name: str, tail: int = 200) -> AsyncIterator[str]:
    """Yield a container's stdout+stderr (tail + follow) via the runtime socket."""
    sock = detect_socket()
    if not sock:
        yield _sse("── no container socket mounted ──")
        return
    yield _sse(f"── streaming container {name} (last {tail} lines) ──")
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        timeout = httpx.Timeout(10.0, read=None)  # follow=true never ends
        async with httpx.AsyncClient(transport=transport, base_url="http://d",
                                     timeout=timeout) as cx:
            params = {"stdout": "true", "stderr": "true", "follow": "true",
                      "tail": str(tail), "timestamps": "false"}
            async with cx.stream("GET", f"/containers/{name}/logs",
                                 params=params) as r:
                if r.status_code != 200:
                    yield _sse(f"── logs endpoint returned {r.status_code} ──")
                    return
                buf = bytearray()
                text = bytearray()
                framed = None  # sniff TTY-vs-multiplexed from the first bytes
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if framed is None:
                        if len(buf) < 8:
                            continue
                        # Multiplexed frames start [0|1|2, 0, 0, 0, len…];
                        # a TTY stream is raw log text (printable byte first).
                        framed = buf[0] in (0, 1, 2) and buf[1:4] == b"\x00\x00\x00"
                    payload, buf = _demux(buf, framed)
                    text += payload
                    while b"\n" in text:
                        line, _, rest = bytes(text).partition(b"\n")
                        text = bytearray(rest)
                        yield _sse(line[:MAX_LINE_BYTES]
                                   .decode("utf-8", errors="replace"))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        yield _sse(f"── stream error: {e} ──")
