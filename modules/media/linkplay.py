"""
LinkPlay (WiiM and other LinkPlay-platform) device directory.

Finds LinkPlay boxes among the Cast devices already discovered — every WiiM an
OpenZone can stream to is a Cast target, and a LinkPlay box answers
``getStatusEx`` on the same host — and reads which *input* each one is on.

That input is the one thing Cast cannot tell us: a WiiM switched to HDMI-ARC
simply looks like a Cast receiver that stopped playing, and re-LOADing it (the
interruption rung) switches it straight back to the network input, pulling the
TV's audio off the speaker. OpenZone asks here before any LOAD (open-zone.md
§7.1, "yielded").

Mode values: WiiM HTTP API v1.2 §2.2 (``getPlayerStatus`` ``mode``), plus
values observed on hardware the PDF predates (HDMI-ARC = 49 on a WiiM Ultra).
Inputs a box has, and the ``switchmode`` word for each, are not in the PDF:
they follow python-linkplay (the library Home Assistant uses), which reads
them from ``getStatusEx`` ``plm_support`` — on a WiiM Ultra that decodes to
line-in, Bluetooth, optical, HDMI and phono, which is its back panel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Dict, Iterable, List, Optional

import httpx

logger = logging.getLogger("modules.media.linkplay")

# Streaming protocols other than ours, by mode (HTTP API v1.2 §2.2).
_PROTOCOL_MODES = {
    1: "AirPlay",
    2: "DLNA",
    11: "USB drive",
    16: "TF card",
    21: "USB drive",
    31: "Spotify Connect",
    32: "TIDAL Connect",
    36: "Qobuz Connect",
    99: "a multiroom group",
}
# Named inputs inside the input band. The band, not this table, is the rule:
# a model with an input the PDF does not list (HDMI-ARC arrived after v1.2)
# is still an input, so still someone else's.
_INPUT_NAMES = {
    40: "AUX-In",
    41: "Bluetooth",
    42: "external storage",
    43: "Optical-In",
    44: "RCA",
    45: "Coaxial-In",
    46: "FM",
    47: "Line-In 2",
    48: "XLR",
    49: "HDMI-ARC",
    50: "Mirror",
    51: "USB-DAC",
    52: "TF card",
    53: "external Bluetooth",
    54: "Phono",
    56: "Optical-In 2",
    57: "Coaxial-In 2",
    58: "ARC",
    60: "voice mail",
}
INPUT_BAND = range(40, 70)

# Probing every Cast host once is cheap; re-probing a non-LinkPlay host is not
# free, and DHCP can hand its address to a WiiM later, hence a slow retry
# rather than never.
PROBE_TIMEOUT_S = 2.5
MISS_RETRY_S = 3600.0
# The pre-LOAD check sits in front of a recovery, so it must be quick.
MODE_TIMEOUT_S = 1.5
# How long a foreign reading still answers for an unreachable httpapi. Failing
# open on a box last seen on HDMI is exactly the recast this exists to stop.
MODE_MEMORY_S = 120.0


def mode_owner(mode: Optional[int]) -> Optional[str]:
    """What, other than a network stream, owns a device in this mode — or
    None when it is on the network input (idle, playlist, or a Cast)."""
    if mode is None:
        return None
    if mode in _PROTOCOL_MODES:
        return _PROTOCOL_MODES[mode]
    if mode in INPUT_BAND:
        return _INPUT_NAMES.get(mode, f"input {mode}")
    return None


# The inputs a box can be switched to: (plm_support bit, switchmode word,
# playing mode it then reports, label). The words are case-sensitive.
INPUTS = (
    (2, "line-in", 40, "Line-in"),
    (4, "bluetooth", 41, "Bluetooth"),
    (8, "udisk", 21, "USB drive"),
    (16, "optical", 43, "Optical"),
    (32, "RCA", 44, "RCA"),
    (64, "co-axial", 45, "Coaxial"),
    (128, "FM", 46, "FM"),
    (256, "line-in2", 47, "Line-in 2"),
    (512, "XLR", 48, "XLR"),
    (1024, "HDMI", 49, "HDMI-ARC"),
    (2048, "cd", 50, "CD"),
    (8192, "TFcard", 16, "TF card"),
    (32768, "PCUSB", 51, "USB-DAC"),
    (65536, "phono", 54, "Phono"),
    (262144, "optical2", 56, "Optical 2"),
    (524288, "co-axial2", 57, "Coaxial 2"),
    (4194304, "ARC", 58, "ARC"),
)
NETWORK_INPUT = ("wifi", "Network (Wi-Fi / Cast)")
# Output interface (HTTP API v1.2 §2.10).
OUTPUTS = {1: "Optical (S/PDIF)", 2: "Line out (AUX)", 3: "Coaxial"}
# setPlayerCmd:loopmode values the API documents (§2.3.13).
LOOP_MODES = {0: "In order", -1: "Repeat all", 1: "Repeat one",
              2: "Shuffle + repeat"}


def supported_inputs(plm_support) -> list:
    """``[{"id", "label", "mode"}]`` for the inputs a box reports, network
    first. An unreadable mask offers only the inputs every WiiM has."""
    try:
        mask = int(str(plm_support), 0)
    except (TypeError, ValueError):
        mask = 2 | 4                      # line-in + Bluetooth
    out = [{"id": NETWORK_INPUT[0], "label": NETWORK_INPUT[1], "mode": 10}]
    out += [{"id": word, "label": label, "mode": mode}
            for bit, word, mode, label in INPUTS if mask & bit]
    return out


def input_id(mode: Optional[int], cast_mode: Optional[int] = None) -> str:
    """The switchmode word for the input a box is on — "wifi" for anything
    that is not a physical input, "" when unknown."""
    if mode is None:
        return ""
    for _bit, word, m, _label in INPUTS:
        if m == mode:
            return word
    return "" if is_input(mode) and mode != cast_mode else NETWORK_INPUT[0]


def is_input(mode: Optional[int]) -> bool:
    """A physical input (or a LinkPlay group): never Cast's own mode, so never
    learned as one — a stale Cast status must not excuse an HDMI reading."""
    return mode is not None and (mode in INPUT_BAND or mode == 99)


def _is_linkplay(ex: object) -> bool:
    if not isinstance(ex, dict):
        return False
    fw = str(ex.get("firmware", "")).lower()
    return bool(ex.get("project")) or fw.startswith("linkplay")


class LinkPlayDirectory:
    def __init__(self, seed_ips: Iterable[str] = (), state_file: str = ""):
        self._client: Optional[httpx.AsyncClient] = None
        self._scheme: Dict[str, str] = {}
        self._devices: Dict[str, dict] = {}          # ip -> identity
        self._misses: Dict[str, float] = {}          # ip -> retry after
        self._last_mode: Dict[str, tuple] = {}       # ip -> (t, mode)
        self._probing: set = set()
        self._seed = [ip.strip() for ip in seed_ips if ip and ip.strip()]
        self._listeners: List = []
        # The mode each box reports while playing a Cast stream, learned from
        # a zone playing on it (learn_cast_mode). Persisted: an idle box
        # often keeps reporting it, and a session start must not read Cast's
        # own mode as someone else's before it has been learned again.
        self._state_file = state_file
        self._cast_modes: Dict[str, int] = {}
        if state_file:
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._cast_modes = {
                    k: int(v) for k, v in (saved.get("cast_modes") or {}).items()
                    if not is_input(int(v))}
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Could not read {state_file}: {e}")

    # HTTP plumbing
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Newer firmware serves HTTPS only, with a self-signed cert.
            self._client = httpx.AsyncClient(verify=False)
        return self._client

    async def _get_json(self, ip: str, command: str,
                        timeout: float) -> Optional[dict]:
        schemes = [self._scheme[ip]] if ip in self._scheme else ["https", "http"]
        for scheme in schemes:
            try:
                resp = await self._http().get(
                    f"{scheme}://{ip}/httpapi.asp",
                    params={"command": command}, timeout=timeout)
                resp.raise_for_status()
                data = json.loads(resp.text)
            except (httpx.HTTPError, ValueError) as e:
                logger.debug(f"LinkPlay {ip} {scheme} '{command}': {e}")
                continue
            self._scheme[ip] = scheme
            return data if isinstance(data, dict) else None
        return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # Discovery
    def on_found(self, fn) -> None:
        """Call ``fn(ip, identity)`` for each newly found device."""
        self._listeners.append(fn)

    def devices(self) -> Dict[str, dict]:
        return dict(self._devices)

    def is_linkplay(self, ip: str) -> bool:
        return ip in self._devices

    async def probe(self, ip: str) -> Optional[dict]:
        """Identify ``ip`` as LinkPlay (identity dict) or not (None)."""
        ex = await self._get_json(ip, "getStatusEx", PROBE_TIMEOUT_S)
        if not _is_linkplay(ex):
            self._misses[ip] = time.monotonic() + MISS_RETRY_S
            return None
        ident = {
            "ip": ip,
            "name": ex.get("DeviceName") or ex.get("ssid") or ip,
            "project": (ex.get("project") or "").strip(),
            "uuid": ex.get("uuid") or "",
            "mac": ex.get("MAC") or "",
            "firmware": ex.get("firmware") or "",
        }
        new = ip not in self._devices
        self._devices[ip] = ident
        self._misses.pop(ip, None)
        if new:
            logger.info(f"LinkPlay device found: {ident['name']} at {ip} "
                        f"({ident['project'] or 'unknown model'})")
            for fn in self._listeners:
                try:
                    fn(ip, ident)
                except Exception as e:
                    logger.debug(f"LinkPlay listener failed for {ip}: {e}")
        return ident

    async def discover(self, hosts: Iterable[str]) -> List[str]:
        """Probe hosts not yet known either way. Returns newly found IPs."""
        now = time.monotonic()
        todo = []
        for ip in list(self._seed) + [h for h in hosts if h]:
            if (ip in self._devices or ip in self._probing
                    or self._misses.get(ip, 0.0) > now or ip in todo):
                continue
            todo.append(ip)
        if not todo:
            return []
        self._probing.update(todo)
        try:
            found = await asyncio.gather(*(self.probe(ip) for ip in todo),
                                         return_exceptions=True)
        finally:
            self._probing.difference_update(todo)
        return [ip for ip, r in zip(todo, found) if isinstance(r, dict)]

    # Input ownership
    async def input_mode(self, ip: str,
                         timeout: float = MODE_TIMEOUT_S) -> Optional[int]:
        st = await self._get_json(ip, "getPlayerStatus", timeout)
        if st is None:
            return None
        try:
            mode = int(st.get("mode"))
        except (TypeError, ValueError):
            return None
        self._last_mode[ip] = (time.monotonic(), mode)
        return mode

    async def foreign_owner(self, ip: str) -> Optional[str]:
        """What owns this device's input when it is not the network — e.g.
        "HDMI-ARC" — or None when a Cast LOAD would not take it from anyone.

        An unreachable httpapi answers from the last reading while it is
        recent: that failing open is the recast this check exists to stop.
        """
        return (await self.read(ip) or {}).get("owner")

    async def read(self, ip: str) -> Optional[dict]:
        """``{"mode", "owner", "input", "stale"}`` for a known box, else None.
        ``owner`` is None on the network input or in the learned Cast mode;
        ``stale`` marks an answer taken from memory because the box did not
        reply."""
        if ip not in self._devices:
            return None
        mode = await self.input_mode(ip)
        stale = mode is None
        if stale:
            seen = self._last_mode.get(ip)
            if seen and time.monotonic() - seen[0] < MODE_MEMORY_S:
                mode = seen[1]
        if mode is None:
            return None
        owner = None if mode == self._cast_modes.get(ip) else mode_owner(mode)
        return {"mode": mode, "owner": owner, "input": is_input(mode),
                "stale": stale}

    def learn_cast_mode(self, ip: str, mode: int) -> bool:
        """Record ``mode`` as what this box reports while a zone plays on it.
        Refused for a physical input: that reading beside a PLAYING Cast
        status means the status is stale, not that Cast is an input."""
        if ip not in self._devices or is_input(mode):
            return False
        if self._cast_modes.get(ip) == mode:
            return False
        self._cast_modes[ip] = mode
        name = self._devices[ip].get("name", ip)
        logger.info(f"LinkPlay {name} reports mode {mode} while casting"
                    f"{f' (was classed as {mode_owner(mode)})' if mode_owner(mode) else ''}")
        self._save()
        return True

    def cast_mode(self, ip: str) -> Optional[int]:
        return self._cast_modes.get(ip)

    def _save(self) -> None:
        if not self._state_file:
            return
        try:
            d = os.path.dirname(self._state_file) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"cast_modes": self._cast_modes}, f, indent=2)
            os.replace(tmp, self._state_file)
        except Exception as e:
            logger.warning(f"Could not write {self._state_file}: {e}")

    async def owner_for_host(self, host: str) -> Optional[str]:
        """``foreign_owner`` for a Cast host, identifying it first if the
        discovery pass has not reached it yet (a zone started right after
        boot)."""
        if host not in self._devices:
            await self.discover([host])
        return await self.foreign_owner(host)

    async def read_host(self, host: str) -> Optional[dict]:
        """``read`` for a Cast host, identifying it first if needed."""
        if host not in self._devices:
            await self.discover([host])
        return await self.read(host)
