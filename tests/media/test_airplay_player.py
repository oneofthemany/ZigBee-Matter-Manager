"""
The AirPlay player provider.

pyatv is replaced by an in-memory fake (a dev box has neither a receiver nor,
necessarily, pyatv), but ffmpeg is real when it is installed: the fake
receiver drains the actual MP3 pipe, so a track reaching its natural end, and
pause/resume restarting the decoder at an offset, are exercised end to end on
this side of the RAOP connection.

What the fake enforces is pyatv's own contract: one stream per receiver
(stream_file raises if a second starts before the first has wound down), and
remote_control.stop ending the stream.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from enum import Enum
from pathlib import Path

from harness import Checker

FFMPEG = shutil.which("ffmpeg")


class PairingRequirement(Enum):
    NotNeeded = 3
    Mandatory = 5


class Protocol(Enum):
    RAOP = 5


class FakeService:
    def __init__(self, pairing=PairingRequirement.NotNeeded):
        self.pairing, self.credentials = pairing, None


class FakeConfig:
    def __init__(self, ident, name, raop=True, pairing=PairingRequirement.NotNeeded):
        self.identifier, self.name = ident, name
        self._raop = FakeService(pairing) if raop else None

    def get_service(self, protocol):
        return self._raop if protocol == Protocol.RAOP else None

    def set_credentials(self, protocol, creds):
        if self._raop is not None:
            self._raop.credentials = creds


class FakeStream:
    def __init__(self, atv):
        self.atv = atv
        self.streaming = False
        self.calls = []

    async def stream_file(self, reader, metadata=None):
        if self.streaming:
            raise RuntimeError("already streaming to device")   # pyatv InvalidStateError
        self.streaming = True
        self.atv.stop_event = asyncio.Event()
        self.calls.append(metadata)
        try:
            if self.atv.fail_stream:
                raise ConnectionError("receiver went away")
            self.atv.bytes = 0
            while not self.atv.stop_event.is_set():
                chunk = await reader.read(8192)
                if not chunk:
                    break
                self.atv.bytes += len(chunk)
                # Real RAOP sends at playback speed; a short pause per chunk
                # keeps a pause/stop test from outrunning the decoder.
                await asyncio.sleep(0.01)
        finally:
            self.streaming = False


class FakeRemote:
    def __init__(self, atv):
        self.atv = atv

    async def stop(self):
        self.atv.stops += 1
        if getattr(self.atv, "stop_event", None):
            self.atv.stop_event.set()


class FakeAudio:
    def __init__(self):
        self.volume, self.sets = 40.0, []

    async def set_volume(self, level):
        self.sets.append(level)
        self.volume = level


class FakeAtv:
    def __init__(self):
        self.stream, self.remote_control, self.audio = FakeStream(self), FakeRemote(self), FakeAudio()
        self.stops, self.closed, self.fail_stream, self.bytes = 0, 0, False, 0
        self.listener = None

    def close(self):
        self.closed += 1
        return set()


class FakePairing:
    def __init__(self, ok=True):
        self.ok, self.service, self.begun, self.pinned, self.closed = ok, FakeService(), False, None, False
        self.device_provides_pin, self.has_paired = True, False

    async def begin(self):
        self.begun = True

    def pin(self, pin):
        self.pinned = pin

    async def finish(self):
        self.has_paired = self.ok and self.pinned == "1234"
        if self.has_paired:
            self.service.credentials = "CREDS-XYZ"

    async def close(self):
        self.closed = True


def _load(scan_results=(), manual_results=(), pairing=None):
    lib = types.ModuleType("pyatv")
    const = types.ModuleType("pyatv.const")
    const.PairingRequirement, const.Protocol = PairingRequirement, Protocol
    iface = types.ModuleType("pyatv.interface")

    class MediaMetadata:
        def __init__(self, title=None, artist=None, album=None, artwork=None, duration=None):
            self.title, self.artist, self.duration = title, artist, duration
    iface.MediaMetadata = MediaMetadata
    lib.scans = []

    async def scan(loop, timeout=5, hosts=None, protocol=None):
        lib.scans.append(hosts)
        return list(manual_results if hosts else scan_results)

    async def connect(config, loop, protocol=None):
        return FakeAtv()

    async def pair(config, protocol, loop):
        return pairing or FakePairing()

    lib.scan, lib.connect, lib.pair, lib.const, lib.interface = scan, connect, pair, const, iface
    saved = {k: sys.modules.get(k) for k in ("pyatv", "pyatv.const", "pyatv.interface")}
    sys.modules.update({"pyatv": lib, "pyatv.const": const, "pyatv.interface": iface})
    sys.modules.pop("modules.media.players.airplay", None)
    try:
        return importlib.import_module("modules.media.players.airplay"), lib
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _run(coro):
    return asyncio.run(coro)


def _cmd(c: Checker) -> None:
    c.section("decoder command")
    A, _ = _load()
    p = A.AirPlayPlayerProvider(discovery=False, ffmpeg="ffmpeg")
    http = p._ffmpeg_cmd("https://radio/stream.aac", 0)
    c.check("remote sources reconnect", "-reconnect" in http and "-ss" not in http, http)
    tail = http[http.index("-i") + 2:]
    c.check("output is 44.1k stereo MP3 on stdout",
            tail == ["-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", "-"],
            tail)
    seek = p._ffmpeg_cmd("/tmp/track.mp3", 61500)
    c.check("resume seeks before the input", seek.index("-ss") < seek.index("-i")
            and seek[seek.index("-ss") + 1] == "61.500" and "-reconnect" not in seek, seek)


def _discovery(c: Checker) -> None:
    c.section("discovery and pairing state")
    homepod = FakeConfig("HP1", "Kitchen HomePod")
    appletv = FakeConfig("ATV1", "Lounge Apple TV", pairing=PairingRequirement.Mandatory)
    no_raop = FakeConfig("MAC1", "Laptop", raop=False)
    manual = FakeConfig("SPK1", "Office speaker")
    A, lib = _load(scan_results=[homepod, appletv, no_raop], manual_results=[manual])

    store = A.CredentialStore()
    p = A.AirPlayPlayerProvider(device_hosts=["10.0.0.9"], credential_store=store, ffmpeg="ffmpeg")
    _run(p.start())
    c.check("RAOP receivers from discovery and manual hosts are listed",
            set(p._devices) == {"HP1", "ATV1", "SPK1"}, sorted(p._devices))
    c.check("manual hosts are scanned directly", ["10.0.0.9"] in lib.scans, lib.scans)

    states = {s.player_id: s for s in _run(p.list_players())}
    c.check("a receiver needing pairing is listed but unavailable",
            states["airplay:ATV1"].available is False and p.pairing_required("airplay:ATV1"))
    c.check("a receiver without access control is available", states["airplay:HP1"].available)

    try:
        _run(p.play_url("airplay:ATV1", A.MediaItem(url="x", title="t")))
        c.check("playing to an unpaired receiver is refused", False)
    except RuntimeError as e:
        c.check("playing to an unpaired receiver is refused with a pointer to pairing",
                "pair" in str(e).lower(), str(e))

    async def poll_during_scan():
        slow = A.AirPlayPlayerProvider(discovery=False, ffmpeg="ffmpeg")
        release = asyncio.Event()

        async def _slow():
            await release.wait()
        slow._scan = _slow
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await slow.list_players()
        waited = loop.time() - t0
        pending = slow._scan_task is not None and not slow._scan_task.done()
        release.set()
        await slow._scan_task
        return waited, pending
    waited, pending = _run(poll_during_scan())
    c.check("a stale scan runs in the background, not inside the poll", waited < 0.5 and pending,
            (waited, pending))

    c.section("pairing")
    good = FakePairing()
    A, _ = _load(scan_results=[FakeConfig("ATV1", "Lounge Apple TV", pairing=PairingRequirement.Mandatory)],
                 pairing=good)
    store = A.CredentialStore()
    p = A.AirPlayPlayerProvider(discovery=True, credential_store=store, ffmpeg="ffmpeg")

    async def pair_flow():
        await p.start()
        try:
            await p.pair_finish("airplay:ATV1", "1234")
            no_begin = False
        except ValueError:
            no_begin = True
        began = await p.pair_begin("airplay:ATV1")
        wrong = await p.pair_finish("airplay:ATV1", "0000")
        return no_begin, began, wrong
    no_begin, began, wrong = _run(pair_flow())
    c.check("finishing without starting is refused", no_begin)
    c.check("begin reports the PIN comes from the device", began == {"device_provides_pin": True}, began)
    c.check("a wrong PIN does not pair, and the handler is closed",
            wrong is False and good.closed and store.get("ATV1") is None)

    good2 = FakePairing()
    A2, _ = _load(scan_results=[FakeConfig("ATV1", "Lounge Apple TV", pairing=PairingRequirement.Mandatory)],
                  pairing=good2)
    store2 = A2.CredentialStore()
    p2 = A2.AirPlayPlayerProvider(discovery=True, credential_store=store2, ffmpeg="ffmpeg")

    async def pair_ok():
        await p2.start()
        await p2.pair_begin("airplay:ATV1")
        return await p2.pair_finish("airplay:ATV1", "1234")
    ok = _run(pair_ok())
    dev = p2._devices["ATV1"]
    c.check("the right PIN pairs and stores credentials",
            ok and store2.get("ATV1") == "CREDS-XYZ"
            and dev.config.get_service(Protocol.RAOP).credentials == "CREDS-XYZ")
    c.check("a paired receiver no longer needs pairing", not p2.pairing_required("airplay:ATV1"))

    c.section("credential store")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "secrets.yaml"
        path.write_text("blueair:\n  username: me\n  password: pw\n")
        s = A2.SecretsCredentialStore(str(path))
        _run(s.set("ATV1", "CREDS-XYZ"))
        mode = stat.S_IMODE(path.stat().st_mode)
        c.check("credentials file is 0600", mode == 0o600, oct(mode))
        import yaml
        data = yaml.safe_load(path.read_text())
        c.check("other secrets are preserved", data.get("blueair", {}).get("password") == "pw", data)
        c.check("credentials reload after a restart", A2.SecretsCredentialStore(str(path)).get("ATV1") == "CREDS-XYZ")


def _playback(c: Checker) -> None:
    c.section("playback (real ffmpeg)")
    if not FFMPEG:
        print("    skipped (ffmpeg not installed)")
        return

    tmp = tempfile.TemporaryDirectory()
    track = Path(tmp.name) / "tone.mp3"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:a", "libmp3lame", "-b:a", "128k",
                    str(track)], check=True)
    long_track = Path(tmp.name) / "long.mp3"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=120", "-c:a", "libmp3lame", "-b:a", "128k",
                    str(long_track)], check=True)

    A, _ = _load(scan_results=[FakeConfig("HP1", "Kitchen HomePod")])

    async def wait_for(pred, timeout=20):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return False

    async def scenario():
        p = A.AirPlayPlayerProvider(discovery=True, ffmpeg=FFMPEG)
        await p.start()
        pid = "airplay:HP1"
        dev = p._devices["HP1"]
        out = {}

        # A finite track plays to its natural end.
        item = A.MediaItem(url=str(track), title="Tone", artist="Test", media_type="url", duration_ms=2000)
        await p.play_url(pid, item)
        await wait_for(lambda: dev.session.state == A.PlaybackState.PLAYING)
        meta = dev.atv.stream.calls[-1]
        out["meta"] = (meta.title, meta.artist, meta.duration)
        out["ended"] = await wait_for(lambda: dev.session.task.done())
        s = await p.get_state(pid)
        out["end_state"] = (s.state, s.ended, dev.atv.bytes > 0)

        # Pause mid-track remembers the position; resume seeks to it.
        cmds = []
        orig = p._ffmpeg_cmd
        p._ffmpeg_cmd = lambda url, off: (cmds.append(off), orig(url, off))[1]
        long_item = A.MediaItem(url=str(long_track), title="Long", media_type="url", duration_ms=120_000)
        await p.play_url(pid, long_item)
        await asyncio.sleep(0.6)
        await p.pause(pid)
        paused = await p.get_state(pid)
        proc = dev.session.proc
        out["paused"] = (paused.state, paused.position_ms, dev.session.task.done(),
                         proc.returncode is not None, dev.atv.stops)
        await asyncio.sleep(0.3)
        out["frozen"] = (await p.get_state(pid)).position_ms == paused.position_ms
        await p.resume(pid)
        await wait_for(lambda: dev.session.state == A.PlaybackState.PLAYING)
        out["resume_offset"] = (cmds[-1], paused.position_ms)

        # Starting something new while playing winds the old stream down first.
        streams_before = len(dev.atv.stream.calls)
        await p.play_url(pid, item)
        await wait_for(lambda: len(dev.atv.stream.calls) > streams_before)
        await asyncio.sleep(0.1)
        out["switch"] = (dev.session.item.title, dev.session.error)

        # A live stream that ends on its own is not "ended".
        live = A.MediaItem(url=str(track), title="Radio", media_type="radio")
        await p.play_url(pid, live)
        await wait_for(lambda: dev.session.task.done())
        s = await p.get_state(pid)
        out["live_end"] = (s.ended, s.duration_ms)

        # Pausing a live stream restarts it from the live edge on resume.
        cmds.clear()
        live_long = A.MediaItem(url=str(long_track), title="Radio", media_type="radio")
        await p.play_url(pid, live_long)
        await asyncio.sleep(0.4)
        await p.pause(pid)
        await p.resume(pid)
        out["live_resume"] = cmds[-1]

        await p.stop_playback(pid)
        out["stopped"] = (dev.session is None, (await p.get_state(pid)).state)

        # A stream failure leaves the player idle with the error, and drops the connection.
        await p.play_url(pid, item)
        await wait_for(lambda: dev.session.state == A.PlaybackState.PLAYING)
        await p.stop_playback(pid)
        dev.atv.fail_stream = True
        failing_atv = dev.atv
        await p.play_url(pid, item)
        await wait_for(lambda: dev.session.task.done())
        out["failure"] = (dev.session.state, dev.session.error, dev.atv is None, failing_atv.closed)

        # Volume and emulated mute.
        await p.set_volume(pid, 0.8)
        await p.set_muted(pid, True)
        muted = await p.get_state(pid)
        await p.set_muted(pid, False)
        unmuted = await p.get_state(pid)
        out["volume"] = (dev.atv.audio.sets, muted.muted, muted.volume, unmuted.muted, unmuted.volume)
        await p.stop()
        return out

    import gc
    import warnings
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            out = _run(scenario())
            gc.collect()
    finally:
        tmp.cleanup()
    leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]

    c.check("metadata carries title, artist and duration", out["meta"] == ("Tone", "Test", 2.0), out["meta"])
    c.check("a finite track streams to its natural end", out["ended"])
    c.check("a finished track is idle and flagged ended, having sent audio",
            out["end_state"] == (A.PlaybackState.IDLE, True, True), out["end_state"])
    state, pos, done, killed, stops = out["paused"]
    c.check("pause stops the stream, kills the decoder and records a position",
            state == A.PlaybackState.PAUSED and pos > 0 and done and killed and stops >= 1, out["paused"])
    c.check("position does not advance while paused", out["frozen"])
    c.check("resume restarts the decoder at the paused position",
            out["resume_offset"][0] == out["resume_offset"][1] and out["resume_offset"][0] > 0, out["resume_offset"])
    c.check("switching tracks never hits pyatv's one-stream guard",
            out["switch"] == ("Tone", ""), out["switch"])
    c.check("a live stream ending is not reported as a finished track",
            out["live_end"] == (False, 0), out["live_end"])
    c.check("a paused live stream resumes from the live edge", out["live_resume"] == 0, out["live_resume"])
    c.check("stop clears the session", out["stopped"] == (True, A.PlaybackState.IDLE), out["stopped"])
    fstate, ferr, dropped, closed = out["failure"]
    c.check("a failed stream is idle with its error and a dropped connection",
            fstate == A.PlaybackState.IDLE and "went away" in ferr and dropped and closed >= 1, out["failure"])
    c.check("stopped decoders leave no open pipes or processes behind", leaks == [], leaks[:3])
    c.check("volume is 0-100 on the wire; mute is volume 0 and back",
            out["volume"] == ([80.0, 0.0, 80.0], True, 0.0, False, 0.8), out["volume"])


def _real_library(c: Checker) -> None:
    """Every pyatv name and parameter the provider relies on, checked against
    the installed library — the fake above only proves the provider agrees
    with itself."""
    import inspect
    for k in ("pyatv", "pyatv.const", "pyatv.interface"):
        sys.modules.pop(k, None)
    try:
        import pyatv
        from pyatv import const, interface
    except ImportError:
        print("\n  real-library contract: skipped (pyatv not installed)")
        return

    c.section("real pyatv contract")
    params = lambda f: set(inspect.signature(f).parameters)
    c.check("scan takes timeout, hosts and protocol", {"timeout", "hosts", "protocol"} <= params(pyatv.scan))
    c.check("connect takes a protocol", "protocol" in params(pyatv.connect))
    c.check("pair takes config, protocol and loop", {"config", "protocol", "loop"} <= params(pyatv.pair))
    c.check("RAOP protocol and Mandatory pairing exist",
            hasattr(const.Protocol, "RAOP") and hasattr(const.PairingRequirement, "Mandatory"))
    c.check("MediaMetadata takes title, artist and duration",
            {"title", "artist", "duration"} <= params(interface.MediaMetadata))
    c.check("stream_file takes a reader and metadata",
            "metadata" in params(interface.Stream.stream_file))
    c.check("config can store credentials", hasattr(interface.BaseConfig, "set_credentials"))
    c.check("pairing handler exposes pin, device_provides_pin, has_paired, begin, finish, close",
            all(hasattr(interface.PairingHandler, n) for n in
                ("pin", "device_provides_pin", "has_paired", "begin", "finish", "close")))
    c.check("device exposes audio, stream, remote_control and close",
            all(hasattr(interface.AppleTV, n) for n in ("audio", "stream", "remote_control", "close")))
    c.check("audio has volume and set_volume",
            hasattr(interface.Audio, "volume") and hasattr(interface.Audio, "set_volume"))
    c.check("remote control has stop", hasattr(interface.RemoteControl, "stop"))
    try:
        from pyatv.protocols.raop.audio_source import open_source
        c.check("RAOP audio source accepts an asyncio StreamReader",
                "StreamReader" in str(inspect.signature(open_source).parameters["source"].annotation))
    except ImportError as e:
        c.check("RAOP audio source importable", False, e)

    sys.modules.pop("modules.media.players.airplay", None)
    mod = importlib.import_module("modules.media.players.airplay")
    c.check("the provider imports against the real library", hasattr(mod, "AirPlayPlayerProvider"))


def _routes(c: Checker) -> None:
    """Pairing endpoints through FastAPI, where it is installed."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n  pairing routes: skipped (fastapi not installed)")
        return
    from routes.media_routes import register_media_routes

    c.section("pairing routes")

    class FakeProvider:
        def __init__(self):
            self.begun, self.pins = [], []

        async def list_players(self):
            from modules.media.models import PlayerState
            return [PlayerState(player_id="airplay:ATV1", provider="airplay", name="Lounge", available=False)]

        def pairing_required(self, pid):
            return True

        async def pair_begin(self, pid):
            self.begun.append(pid)
            return {"device_provides_pin": True}

        async def pair_finish(self, pid, pin):
            self.pins.append((pid, pin))
            return pin == "1234"

    provider = FakeProvider()
    svc = types.SimpleNamespace(enabled=True, airplay=provider)
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        scopes = request.headers.get("x-test-scopes")
        if scopes is not None:
            request.state.principal = types.SimpleNamespace(scopes=set(scopes.split(",")))
        return await call_next(request)

    register_media_routes(app, lambda: svc)
    client = TestClient(app)
    admin, viewer = {"x-test-scopes": "admin"}, {"x-test-scopes": "media:control"}

    c.check("listing receivers needs admin",
            client.get("/api/media/airplay/devices", headers=viewer).status_code == 403)
    r = client.get("/api/media/airplay/devices", headers=admin).json()
    c.check("receivers list their pairing requirement",
            r["success"] and r["devices"][0]["pairing_required"] is True, r)
    c.check("pairing needs admin",
            client.post("/api/media/airplay/airplay:ATV1/pair/begin", headers=viewer).status_code == 403
            and provider.begun == [])
    r = client.post("/api/media/airplay/airplay:ATV1/pair/begin", headers=admin).json()
    c.check("begin reaches the provider", r == {"success": True, "device_provides_pin": True}
            and provider.begun == ["airplay:ATV1"], r)
    r = client.post("/api/media/airplay/airplay:ATV1/pair/finish", headers=admin, json={"pin": " 0000 "}).json()
    c.check("a wrong PIN reports failure", r["success"] is False and "PIN" in r["error"], r)
    r = client.post("/api/media/airplay/airplay:ATV1/pair/finish", headers=admin, json={"pin": " 1234 "}).json()
    c.check("the PIN is trimmed and a right one succeeds",
            r == {"success": True} and provider.pins[-1] == ("airplay:ATV1", "1234"), r)
    svc.airplay = None
    r = client.get("/api/media/airplay/devices", headers=admin).json()
    c.check("AirPlay disabled gives a readable error", r["success"] is False and "not enabled" in r["error"], r)


def run() -> Checker:
    c = Checker("airplay_player")
    _cmd(c)
    _discovery(c)
    _playback(c)
    _real_library(c)
    _routes(c)
    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
