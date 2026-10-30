"""
Espressif speakers on USB serial: find them, read their log, set their Wi-Fi
over Improv, and inspect, back up or flash them with esptool — without an
accidental reset and without ever touching the Zigbee radio's port.
docs/esp-serial.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import termios
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from modules.esp_esptool import (EsptoolError, Runner, StubSession, flash_size_bytes, parse_boot,
                                 parse_chip, parse_flash, parse_partition_table, parse_security,
                                 run_esptool, strip_ansi)

logger = logging.getLogger("modules.esp_serial")

ESPRESSIF_VID = "303a"
COOLDOWN_S = 120              # past ESPHome's 60 s "boot is good", so the safe_mode count resets
SAFE_MODE_ATTEMPTS = 10       # ESPHome safe_mode default; a build may set another
BOOT_LISTEN_S = 6.0
LOG_KEEP = 2000
LOG_IDLE_S = 60               # a reader nobody polls stops
CAPTURE_MAX = 8 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
BAUD = "921600"
IMAGE_MAGIC = 0xE9            # factory and full-flash images start with the bootloader header
SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def data_dir() -> Path:
    return Path(os.environ.get("ZMM_DATA_DIR", "./data")) / "esp_serial"


class CooldownError(Exception):
    def __init__(self, remaining: int, previous: Optional[str]) -> None:
        super().__init__(
            f"ZMM restarted this speaker {COOLDOWN_S - remaining} s ago. Another restart before its "
            f"firmware judges the boot good counts toward ESPHome safe mode ({SAFE_MODE_ATTEMPTS} by "
            f"default), a recovery mode with no normal function. Wait {remaining} s.")
        self.remaining = remaining
        self.previous = previous


class BusyError(Exception):
    pass


class ImprovError(Exception):
    pass


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Ports ────────────────────────────────────────────────────────────────────

def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def scan_ports(sysfs: str = "/sys/class/tty") -> List[Dict[str, str]]:
    """USB serial ttys with the identity of the USB device behind them."""
    found = []
    for entry in sorted(Path(sysfs).glob("tty*")):
        if not re.fullmatch(r"tty(ACM|USB)\d+", entry.name):
            continue
        d = (entry / "device").resolve()
        for _ in range(4):                     # ACM: interface's parent; usb-serial: one more up
            if (d / "idVendor").exists():
                break
            d = d.parent
        found.append({"name": entry.name, "vendor_id": _read(d / "idVendor").lower(),
                      "product_id": _read(d / "idProduct").lower(), "serial": _read(d / "serial"),
                      "product": _read(d / "product"), "manufacturer": _read(d / "manufacturer")})
    return found


# ── Restart ledger ───────────────────────────────────────────────────────────

class RestartLedger:
    """Restarts ZMM caused, per speaker, across app restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data.get("devices"), dict) else {"devices": {}}
        except (OSError, ValueError, AttributeError):
            return {"devices": {}}

    def remaining(self, key: str, now: Optional[float] = None) -> int:
        restarts = self._read()["devices"].get(key, {}).get("restarts", [])
        if not restarts:
            return 0
        return max(0, int(COOLDOWN_S - ((now or time.time()) - restarts[-1]) + 0.999))

    def admit(self, key: str, override: bool) -> Optional[str]:
        """Refuse inside the cooldown unless overridden; record before any reset can happen."""
        data = self._read()
        dev = data["devices"].setdefault(key, {"restarts": [], "last_boot_attempts": None})
        previous = _iso(dev["restarts"][-1]) if dev["restarts"] else None
        left = self.remaining(key)
        if left and not override:
            raise CooldownError(left, previous)
        dev["restarts"] = (dev["restarts"] + [time.time()])[-20:]
        _write_json(self.path, data)
        return previous

    def record_boot(self, key: str, attempts: Optional[int]) -> None:
        data = self._read()
        if key in data["devices"]:
            data["devices"][key]["last_boot_attempts"] = attempts
            _write_json(self.path, data)


# ── Raw serial I/O ───────────────────────────────────────────────────────────

class RawPort:
    """
    Opened without setting DTR or RTS: on USB-Serial/JTAG, RTS with DTR low
    resets the chip. Raw and without echo (a cooked tty echoes the log back
    into the device), HUPCL off so closing drops nothing; settings restored.
    """

    def __init__(self, path: str, write: bool = False) -> None:
        self.fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._saved = termios.tcgetattr(self.fd)
            a = termios.tcgetattr(self.fd)
            a[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                      | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
            a[1] &= ~termios.OPOST
            a[2] = (a[2] & ~(termios.CSIZE | termios.PARENB | termios.HUPCL)) | termios.CS8
            a[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            a[6][termios.VMIN], a[6][termios.VTIME] = 0, 0
            termios.tcsetattr(self.fd, termios.TCSANOW, a)
        except Exception:
            os.close(self.fd)
            raise

    def read(self) -> bytes:
        try:
            return os.read(self.fd, 65536)
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self._saved)
        except termios.error:
            pass
        finally:
            os.close(self.fd)


async def collect(port: RawPort, seconds: float) -> bytes:
    loop = asyncio.get_running_loop()
    end, buf = loop.time() + seconds, bytearray()
    while loop.time() < end:
        chunk = port.read()
        if chunk:
            buf += chunk[:max(0, CAPTURE_MAX - len(buf))]
        else:
            await asyncio.sleep(0.05)
    return bytes(buf)


class LogReader:
    """One port's live log, kept as numbered lines for polling clients."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lines: deque = deque(maxlen=LOG_KEEP)
        self.seq = 0
        self.resets = 0
        self.error: Optional[str] = None
        self._partial = ""
        self._task: Optional[asyncio.Task] = None
        self._polled = time.monotonic()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self.error = None
            self._polled = time.monotonic()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        try:
            port = RawPort(self.path)
        except OSError as e:
            self.error = str(e)
            return
        try:
            while time.monotonic() - self._polled < LOG_IDLE_S:
                chunk = port.read()
                if chunk:
                    self.feed(chunk.decode("utf-8", "replace"))
                else:
                    await asyncio.sleep(0.1)
        except OSError as e:                    # unplugged
            self.error = str(e)
        finally:
            port.close()

    def feed(self, text: str) -> None:
        parts = re.split(r"\r?\n|\r", strip_ansi(self._partial + text))
        self._partial = parts.pop()
        for line in parts:
            if line.strip():
                self.seq += 1
                self.lines.append((self.seq, line))
                if line.startswith("rst:0x"):
                    self.resets += 1

    def after(self, seq: int) -> List[Dict[str, Any]]:
        self._polled = time.monotonic()
        return [{"seq": s, "text": t} for s, t in self.lines if s > seq]


# ── Improv serial ────────────────────────────────────────────────────────────

IMPROV_STATES = {1: "stopped", 2: "ready", 3: "provisioning", 4: "provisioned"}
IMPROV_ERRORS = {1: "invalid packet", 2: "unknown command", 3: "unable to connect to the network",
                 4: "not authorised", 0xFF: "unknown error"}


def improv_packet(kind: int, data: bytes) -> bytes:
    body = b"IMPROV" + bytes([1, kind, len(data)]) + data
    return body + bytes([sum(body) & 0xFF, 0x0A])


def improv_rpc(command: int, data: bytes = b"") -> bytes:
    return improv_packet(0x03, bytes([command, len(data)]) + data)


def parse_improv(buf: bytes) -> Tuple[List[Tuple[int, bytes]], bytes]:
    """Complete, checksummed packets in buf, and what to keep for the next read."""
    packets = []
    i = buf.find(b"IMPROV")
    while i != -1 and len(buf) - i >= 9:
        n = buf[i + 8]
        end = i + 9 + n
        if len(buf) <= end:
            break
        if sum(buf[i:end]) & 0xFF == buf[end]:
            packets.append((buf[i + 7], bytes(buf[i + 9:end])))
        buf = buf[end + 1:]
        i = buf.find(b"IMPROV")
    return packets, (buf[i:] if i != -1 else buf[-5:])


def rpc_strings(data: bytes) -> List[str]:
    out, j, end = [], 2, 2 + data[1]
    while j < end and j < len(data):
        n = data[j]
        out.append(data[j + 1:j + 1 + n].decode("utf-8", "replace"))
        j += 1 + n
    return out


# ── Manager ──────────────────────────────────────────────────────────────────

class EspSerialManager:
    def __init__(self, coordinator_port: Callable[[], Optional[str]], *, sysfs: str = "/sys/class/tty",
                 dev_root: str = "/dev", runner: Runner = run_esptool, root: Optional[Path] = None) -> None:
        self._coordinator = coordinator_port
        self.sysfs, self.dev_root, self.runner = sysfs, dev_root, runner
        self.root = root or data_dir()
        self.ledger = RestartLedger(self.root / "restarts.json")
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._logs: Dict[str, LogReader] = {}
        self._tasks: set = set()                # the loop holds tasks weakly

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def ports(self) -> Dict[str, Any]:
        coordinator = self._coordinator()
        coordinator = os.path.realpath(coordinator) if coordinator else None
        shown, hidden = [], 0
        for p in scan_ports(self.sysfs):
            path = os.path.join(self.dev_root, p["name"])
            if p["vendor_id"] != ESPRESSIF_VID or os.path.realpath(path) == coordinator:
                hidden += 1
                continue
            key = f"usb:{p['serial']}" if p["serial"] else f"port:{path}"
            shown.append({**p, "path": path, "key": key, "cooldown_s": self.ledger.remaining(key),
                          "busy": self._lock(p["name"]).locked(),
                          "logging": bool(self._logs.get(p["name"]) and self._logs[p["name"]].running)})
        return {"ports": shown, "hidden": hidden}

    def port(self, name: str) -> Dict[str, Any]:
        """The only way to a port: re-scanned, so the vendor and coordinator rules hold every time."""
        for p in self.ports()["ports"]:
            if p["name"] == name:
                return p
        raise LookupError(f"{name} is not an Espressif speaker port ZMM may use")

    # Log

    def log(self, name: str, after: int) -> Dict[str, Any]:
        p = self.port(name)
        reader = self._logs.get(name)
        if reader is None:
            reader = self._logs[name] = LogReader(p["path"])
        if not self._lock(name).locked():
            reader.start()
        return {"lines": reader.after(after), "seq": reader.seq, "running": reader.running,
                "paused": self._lock(name).locked(), "resets": reader.resets, "error": reader.error}

    async def stop_log(self, name: str) -> None:
        reader = self._logs.pop(name, None)
        if reader:
            await reader.stop()

    @contextlib.asynccontextmanager
    async def _exclusive(self, name: str):
        """One user of a port at a time: the log pauses, since its reads would steal esptool's."""
        lock = self._lock(name)
        if lock.locked():
            raise BusyError(f"{name} is busy with another operation")
        async with lock:
            reader = self._logs.get(name)
            was = bool(reader and reader.running)
            if reader:
                await reader.stop()
            try:
                yield
            finally:
                if was and reader:
                    reader.start()

    # Improv

    async def _improv(self, path: str, packet: bytes,
                      want: Callable[[int, bytes], Optional[Dict[str, Any]]], timeout: float) -> Dict[str, Any]:
        port = RawPort(path, write=True)
        try:
            port.write(packet)
            loop = asyncio.get_running_loop()
            end, buf = loop.time() + timeout, b""
            while loop.time() < end:
                chunk = port.read()
                if not chunk:
                    await asyncio.sleep(0.05)
                    continue
                packets, buf = parse_improv(buf + chunk)
                for kind, data in packets:
                    if kind == 0x02 and data and data[0]:
                        raise ImprovError(f"The speaker reported: {IMPROV_ERRORS.get(data[0], data[0])}")
                    found = want(kind, data)
                    if found is not None:
                        return found
            raise ImprovError("No Improv answer: the firmware may not include improv_serial")
        finally:
            port.close()

    async def improv_status(self, name: str) -> Dict[str, Any]:
        p = self.port(name)
        async with self._exclusive(name):
            state = await self._improv(p["path"], improv_rpc(0x02),
                                       lambda k, d: {"state": IMPROV_STATES.get(d[0], str(d[0]))}
                                       if k == 0x01 and d else None, 3)
            info = await self._improv(p["path"], improv_rpc(0x03),
                                      lambda k, d: {"strings": rpc_strings(d)}
                                      if k == 0x04 and d and d[0] == 0x03 else None, 3)
        s = info["strings"] + ["", "", "", ""]
        return {**state, "firmware": s[0], "version": s[1], "chip": s[2], "device_name": s[3]}

    async def improv_wifi(self, name: str, ssid: str, password: str) -> Dict[str, Any]:
        """The password goes to the speaker only: never logged, never stored."""
        raw_ssid, raw_pw = ssid.encode(), password.encode()
        if not 1 <= len(raw_ssid) <= 32:
            raise ValueError("The network name must be 1 to 32 bytes")
        if len(raw_pw) > 64:
            raise ValueError("The password must be at most 64 bytes")
        p = self.port(name)
        data = bytes([len(raw_ssid)]) + raw_ssid + bytes([len(raw_pw)]) + raw_pw
        async with self._exclusive(name):
            result = await self._improv(p["path"], improv_rpc(0x01, data),
                                        lambda k, d: {"urls": rpc_strings(d)}
                                        if k == 0x04 and d and d[0] == 0x01 else None, 45)
        return {"provisioned": True, **result}

    # Restarting operations

    def precheck(self, name: str, acknowledge_restart: Any, override_cooldown: bool) -> Dict[str, Any]:
        """Refuse before a job starts: the caller hears why at once."""
        p = self.port(name)
        if acknowledge_restart is not True:
            raise ValueError("This restarts the speaker. Confirm it (acknowledge_restart); quick "
                             f"restarts count toward ESPHome safe mode ({SAFE_MODE_ATTEMPTS} by default).")
        if self._lock(name).locked():
            raise BusyError(f"{name} is busy with another operation")
        left = self.ledger.remaining(p["key"])
        if left and not override_cooldown:
            raise CooldownError(left, None)
        return p

    async def _session(self, name: str, override: bool,
                       steps: Callable[[StubSession], Awaitable[Any]]) -> Tuple[Any, Dict[str, Any]]:
        p = self.port(name)
        previous = self.ledger.admit(p["key"], override)
        session = StubSession(p["path"], self.runner)
        value: Any = None
        failure: Optional[BaseException] = None
        boot_port: Optional[RawPort] = None
        boot_error: Optional[str] = None
        try:
            value = await steps(session)
            try:                                # open before the reset: the boot log arrives at once
                boot_port = RawPort(p["path"])
            except OSError as e:
                boot_error = str(e)
        except Exception as e:                  # noqa: BLE001 — reset first, then report
            failure = e
        try:
            await session.leave()
        except Exception as e:                  # noqa: BLE001
            failure = failure or e
        if failure is not None:
            if boot_port:
                boot_port.close()
            raise failure
        text = b""
        if boot_port:
            try:
                text = await collect(boot_port, BOOT_LISTEN_S)
            finally:
                boot_port.close()
        boot_text = text.decode("utf-8", "replace")
        if name in self._logs:
            self._logs[name].feed(boot_text)
        boot = parse_boot(boot_text)
        self.ledger.record_boot(p["key"], boot["boot_attempts"])
        return value, {"previous_restart_at": previous, "cooldown_s": COOLDOWN_S,
                       "cooldown_overridden": bool(override), **boot,
                       "boot_log_bytes": len(text) if boot_port else None, "boot_log_error": boot_error,
                       "safe_mode_default_attempts": SAFE_MODE_ATTEMPTS}

    async def info(self, name: str, override: bool = False) -> Dict[str, Any]:
        async def steps(s: StubSession) -> Dict[str, Any]:
            flash_out = await s.connect()
            security = await s.step(["get-security-info"])
            with tempfile.TemporaryDirectory() as tmp:
                table_path = os.path.join(tmp, "partition-table.bin")
                await s.step(["read-flash", "--no-progress", "0x8000", "0xc00", table_path])
                table = parse_partition_table(Path(table_path).read_bytes())
            return {"chip": parse_chip(flash_out), "flash": parse_flash(flash_out),
                    "security": parse_security(security), "partition_table": table}

        async with self._exclusive(name):
            value, restart = await self._session(name, override, steps)
        return {**value, "restart": restart}

    async def backup(self, name: str, override: bool = False) -> Dict[str, Any]:
        folder = self.root / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f".partial-{uuid.uuid4().hex}.bin"

        async def steps(s: StubSession) -> Tuple[Dict[str, Any], int]:
            out = await s.connect()
            size = flash_size_bytes(parse_flash(out)["detected_size"])
            if not size:
                raise EsptoolError("esptool did not report the flash size")
            await s.step(["-b", BAUD, "read-flash", "--no-progress", "0", str(size), str(partial)],
                         timeout=120 + size / 10_000)
            return parse_chip(out), size

        try:
            async with self._exclusive(name):
                p = self.port(name)
                (chip, size), restart = await self._session(name, override, steps)
            if partial.stat().st_size != size:
                raise EsptoolError(f"esptool wrote {partial.stat().st_size} bytes, not {size}")
            sha = await asyncio.to_thread(_sha256, partial)
            bid = f"{time.strftime('%Y%m%d-%H%M%S')}-{chip['mac'].replace(':', '')}"
            os.replace(partial, folder / f"{bid}.bin")
            meta = {"id": bid, "kind": "backup", "port_key": p["key"], "mac": chip["mac"],
                    "chip": chip["type"], "bytes": size, "sha256": sha, "created_at": _iso(time.time())}
            _write_json(folder / f"{bid}.json", meta)
            return {**meta, "restart": restart}
        finally:
            partial.unlink(missing_ok=True)

    # Images and backups

    def _metas(self, kind: str) -> List[Dict[str, Any]]:
        out = []
        for f in sorted((self.root / kind).glob("*.json"), reverse=True):
            with contextlib.suppress(OSError, ValueError):
                out.append(json.loads(f.read_text()))
        return out

    def library(self) -> Dict[str, Any]:
        return {"images": self._metas("images"), "backups": self._metas("backups")}

    def save_image(self, data: bytes, name: str) -> Dict[str, Any]:
        if not data or len(data) > MAX_IMAGE:
            raise ValueError(f"An image is 1 byte to {MAX_IMAGE // (1024 * 1024)} MB")
        if data[0] != IMAGE_MAGIC:
            raise ValueError("Not a factory or full-flash image: it must start with the ESP bootloader "
                             "(0xE9). App-only update images go through the speaker's own update.")
        sha = hashlib.sha256(data).hexdigest()
        iid = sha[:12]
        folder = self.root / "images"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{iid}.bin").write_bytes(data)
        meta = {"id": iid, "kind": "image", "name": (name or "image.bin")[:120], "bytes": len(data),
                "sha256": sha, "created_at": _iso(time.time())}
        _write_json(folder / f"{iid}.json", meta)
        return meta

    def delete_image(self, iid: str) -> bool:
        if not SAFE_ID.match(iid):
            return False
        found = False
        for suffix in (".bin", ".json"):
            f = self.root / "images" / f"{iid}{suffix}"
            if f.exists():
                f.unlink()
                found = True
        return found

    def source(self, ref: str) -> Tuple[Path, Dict[str, Any]]:
        kind, _, iid = (ref or "").partition(":")
        folder = {"image": "images", "backup": "backups"}.get(kind)
        if not folder or not SAFE_ID.match(iid):
            raise LookupError("Choose an uploaded image or a backup")
        path = self.root / folder / f"{iid}.bin"
        try:
            meta = json.loads((self.root / folder / f"{iid}.json").read_text())
        except (OSError, ValueError):
            raise LookupError(f"No {kind} {iid}") from None
        if not path.exists():
            raise LookupError(f"No {kind} {iid}")
        return path, meta

    @staticmethod
    def confirm_code(port: Dict[str, Any]) -> str:
        """What the user types to flash: the end of the USB serial, which on these chips is the MAC."""
        return (re.sub(r"[^0-9A-Za-z]", "", port.get("serial") or "")[-6:] or port["name"]).upper()

    def flash_precheck(self, name: str, ref: str, confirm: str, acknowledge_restart: Any,
                       override_cooldown: bool) -> Dict[str, Any]:
        p = self.precheck(name, acknowledge_restart, override_cooldown)
        self.source(ref)
        if not any(b.get("port_key") == p["key"] for b in self._metas("backups")):
            raise ValueError("Back up this speaker first: flashing replaces what is on it.")
        if (confirm or "").strip().upper() != self.confirm_code(p):
            raise ValueError(f"Type {self.confirm_code(p)} to confirm which speaker to flash")
        return p

    async def flash(self, name: str, ref: str, override: bool = False) -> Dict[str, Any]:
        path, meta = self.source(ref)

        async def steps(s: StubSession) -> Dict[str, Any]:
            out = await s.connect()
            size = flash_size_bytes(parse_flash(out)["detected_size"])
            if size and meta["bytes"] > size:
                raise EsptoolError(f"The image ({meta['bytes']} bytes) is larger than the flash ({size})")
            result = await s.step(["-b", BAUD, "write-flash", "0x0", str(path)],
                                  timeout=120 + meta["bytes"] / 5_000)
            return {"verified": "Hash of data verified" in result, "bytes": meta["bytes"]}

        async with self._exclusive(name):
            value, restart = await self._session(name, override, steps)
        return {**value, "source": ref, "restart": restart}

    # Jobs

    def start_job(self, kind: str, name: str, work: Callable[[], Awaitable[Dict[str, Any]]]) -> Dict[str, Any]:
        jid = uuid.uuid4().hex[:8]
        job = {"id": jid, "kind": kind, "port": name, "state": "running", "started_at": _iso(time.time()),
               "result": None, "error": None}
        self.jobs[jid] = job

        async def run() -> None:
            try:
                job["result"] = await work()
                job["state"] = "done"
            except Exception as e:              # noqa: BLE001 — the job reports it
                job["error"], job["state"] = str(e), "failed"
                logger.warning("[esp_serial] %s on %s failed: %s", kind, name, e)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        for old in list(self.jobs)[:-20]:
            if self.jobs[old]["state"] != "running":
                del self.jobs[old]
        return job


_manager: Optional[EspSerialManager] = None


def get_esp_serial_manager() -> Optional[EspSerialManager]:
    return _manager


def set_esp_serial_manager(m: Optional[EspSerialManager]) -> None:
    global _manager
    _manager = m
