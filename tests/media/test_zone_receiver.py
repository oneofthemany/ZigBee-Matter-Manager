"""
OpenZone: the custom receiver as a transport for the engine (open-zone.md
§4.4a, §4.1b).

The real engine serves its real :8010 listener; the receivers are simulated
over a real WebSocket, with the page's own rules — blocks back to back on a
DAC clock that drifts, a flush cuts at a frame, a start names a load — and
report the frame leaving the DAC as the page does. Alignment is judged from
ground truth, not from the engine's own sensor: which timeline sample each
device is outputting at the same instant, mapped through what was served.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import struct
import tempfile
import time
import wave
from types import SimpleNamespace

import numpy as np
import websockets

from harness import Checker

from modules.media import cast_sync
from modules.media.cast_sync import (OpenZone, RATE, CHANNELS, WS_PCM,
                                     _auth_mac, _pair_code)

APP = "ZMMTEST1"
START_LEAD = 0.25
LATE_GUARD = 0.02
LEVEL = {"a": 1000, "b": 2000, "c": 3000}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wav(path: str, level: int, secs: float) -> None:
    frames = np.full((int(secs * RATE), 2), level, dtype="<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames.tobytes())


class FakeReceiver:
    """sync_receiver.html's playback rules on a simulated DAC clock."""

    def __init__(self, port: int, ppm: float, out_latency_s: float = 0.02):
        self.port = port
        self.ppm = ppm
        self.out_latency_s = out_latency_s
        self.t0 = time.monotonic()
        self.ws = None
        self.sid = None
        self.load = None
        self.sched: list = []        # [frame, n, ctx_t0, level]
        self.next_t = None
        self.next_frame = 0
        self.underruns = 0
        self.flushes = 0
        self.stops = 0
        self.volume = None
        self.registered = None
        self.paired = None
        self.errors: list = []
        self.sent: list = []          # every JSON message this client sent
        self.key = secrets.token_hex(32)
        self.cid = None
        self.closed = False
        self._tasks: list = []

    def ctx(self, t: float = None) -> float:
        t = time.monotonic() if t is None else t
        return (t - self.t0) * (1.0 + self.ppm * 1e-6)

    async def _connect(self) -> None:
        self.ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws",
                                           max_size=None)
        self._tasks = [asyncio.create_task(self._recv()),
                       asyncio.create_task(self._report())]

    async def send(self, obj: dict) -> None:
        self.sent.append(obj)
        await self.ws.send(json.dumps(obj))

    async def register(self, cid: str, name: str) -> None:
        """Standalone: join on its own, as /receiver does in a browser."""
        self.cid = cid
        if self.ws is None:
            await self._connect()
        await self.send({"type": "register", "id": cid, "name": name,
                         "ua": "fake"})

    async def on_start(self, payload: dict) -> None:
        if (payload["sid"], payload["load"]) == (self.sid, self.load) \
                and self.ws is not None:
            return
        self.sid, self.load = payload["sid"], payload["load"]
        self.reset()
        if self.ws is None:
            await self._connect()
        await self.ws.send(json.dumps({"type": "hello", "sid": self.sid,
                                       "load": self.load}))

    def reset(self) -> None:
        self.sched, self.next_t, self.next_frame = [], None, 0

    async def _recv(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    self._pcm(msg)
                    continue
                d = json.loads(msg)
                if d["type"] == "flush":
                    self._flush(d["frame"])
                elif d["type"] == "start":
                    self.sid, self.load = d["sid"], d["load"]
                    self.reset()
                    await self.ws.send(json.dumps(
                        {"type": "hello", "sid": self.sid, "load": self.load}))
                elif d["type"] == "stop":
                    self.reset()
                    self.sid = None
                    self.stops += 1
                elif d["type"] == "volume":
                    self.volume = (d["level"], d["muted"])
                elif d["type"] == "registered":
                    self.registered = d
                    self.paired = d["paired"]
                elif d["type"] == "challenge":
                    await self.send({"type": "auth", "mac": _auth_mac(
                        self.key, self.cid, d["nonce"])})
                elif d["type"] == "enrol":
                    await self.send({"type": "enrol", "key": self.key})
                elif d["type"] == "paired":
                    self.paired = True
                elif d["type"] == "forgotten":
                    self.paired = False
                    self.key = secrets.token_hex(32)
                elif d["type"] == "error":
                    self.errors.append(d["error"])
        except Exception:
            pass

    def _pcm(self, msg: bytes) -> None:
        kind, frame = struct.unpack("<BQ", msg[:9])
        assert kind == WS_PCM
        pcm = np.frombuffer(msg[9:], dtype="<i2")
        n = len(pcm) // CHANNELS
        t = self.ctx()
        if self.next_t is None or self.next_t < t + LATE_GUARD:
            if self.next_t is not None:
                self.underruns += 1
            self.next_t = t + START_LEAD
        elif frame != self.next_frame:
            self.next_t += (frame - self.next_frame) / RATE
        self.sched.append([frame, n, self.next_t, int(round(pcm.mean()))])
        self.next_t += n / RATE
        self.next_frame = frame + n

    def _flush(self, frame: int) -> None:
        cut, keep = None, []
        for e in self.sched:
            if e[0] >= frame:
                cut = e[2] if cut is None else min(cut, e[2])
                continue
            if e[0] + e[1] > frame:
                e[1] = frame - e[0]
                cut = e[2] + e[1] / RATE
            keep.append(e)
        self.sched = keep
        if cut is not None:
            self.next_t, self.next_frame = cut, frame
        self.flushes += 1

    def output(self, t: float = None):
        """(stream frame, block level) leaving the DAC at server time t."""
        ct = self.ctx(t) - self.out_latency_s
        for f, n, t0, level in self.sched:
            if t0 <= ct < t0 + n / RATE:
                return f + (ct - t0) * RATE, level
        return None, None

    async def _report(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            f, _ = self.output(now)
            msg = ({"type": "pos", "frame": int(round(f)), "at": now,
                    "state": "playing"} if f is not None
                   else {"type": "pos", "frame": 0, "at": 0, "state": "buffering"})
            try:
                await self.ws.send(json.dumps(msg))
            except Exception:
                return

    async def drop(self) -> None:
        """The socket goes away, as when an assistant takes the device."""
        ws, self.ws = self.ws, None
        for t in self._tasks:
            t.cancel()
        self.reset()
        await ws.close()

    async def close(self) -> None:
        self.closed = True
        if self.ws is not None:
            await self.drop()


class _Cast:
    def __init__(self):
        self.status = SimpleNamespace(app_id=APP, display_name="")
        self.media_controller = SimpleNamespace(
            status=SimpleNamespace(content_id="", player_state="", title=""))
        self.cast_info = SimpleNamespace(host="127.0.0.1")


class _Provider:
    def __init__(self, ids):
        self._casts = {i: _Cast() for i in ids}
        self._infos = {i: SimpleNamespace(friendly_name=f"Speaker {i}",
                                          host="127.0.0.1", is_group=False)
                       for i in ids}

    async def _get_cast(self, uuid_str):
        return self._casts.get(uuid_str)

    def _ensure_app(self, cast, app_id):
        cast.status.app_id = app_id


class _Rig:
    def __init__(self, tmp: str, ppm: dict, connect: bool = True,
                 source_delay_s: float = 1.5, clients: dict = None):
        """``ppm`` are Cast-launched receivers, ``clients`` standalone ones
        that register themselves; both keyed by a short name."""
        self.port = _free_port()
        self.prov = _Provider(list(ppm))
        z = OpenZone(self.prov, {
            "app_id": APP, "http_port": self.port,
            "source_delay_s": source_delay_s, "ring_capacity_s": 20.0,
            "trims_file": f"{tmp}/t.json", "model_trims_file": f"{tmp}/mt.json",
            "groups_file": f"{tmp}/g.json", "model_file": f"{tmp}/m.json",
            "policy_file": f"{tmp}/p.json", "last_file": f"{tmp}/l.json",
            "trim_graph_file": f"{tmp}/tg.json",
            "receivers_file": f"{tmp}/rx.json",
        })
        self.z = z
        self.tmp = tmp
        self.rx = {i: FakeReceiver(self.port, p) for i, p in ppm.items()}
        self.pids = {i: f"cast:{i}" for i in ppm}
        for i, p in (clients or {}).items():
            self.rx[i] = FakeReceiver(self.port, p)
            self.pids[i] = f"receiver:{self.cid(i)}"
        self.starts: list = []
        self.loaded: list = []
        self.connect = connect

        async def push_start(cast, uuid_str, payload):
            self.starts.append((uuid_str, payload["load"]))
            if self.connect:
                await self.rx[uuid_str].on_start(payload)

        z._push_start = push_start
        z._play_stream = lambda cast, url: self.loaded.append(url)

        async def resolver(media_type, source_id, owner=""):
            return f"{tmp}/{source_id}.wav"

        z._url_resolver = resolver

    async def start(self, media=None, test_signal: bool = False) -> dict:
        if not test_signal:
            media = media or {"items": [{"source_id": "a",
                                         "duration_ms": 600_000}],
                              "media_type": "x"}
        self.z.start()
        for _ in range(100):
            try:
                _, w = await asyncio.open_connection("127.0.0.1", self.port)
                w.close()
                break
            except OSError:
                await asyncio.sleep(0.05)
        for i in self.rx:
            if self.pids[i].startswith("receiver:") and i not in self.offline:
                await self.register(i)
        return await self.z.start_session(list(self.pids.values()),
                                          media=media)

    offline: set = set()

    @staticmethod
    def cid(i) -> str:
        return f"client-{i}-0123456789"

    async def register(self, i, pair: bool = True) -> None:
        r = self.rx[i]
        r.registered = None
        await r.register(self.cid(i), f"Tablet {i}")
        await self.until(lambda: r.registered is not None, 5.0)
        if pair and not r.paired:
            await self.z.pair_receiver(self.cid(i), _pair_code(r.key))
            await self.until(lambda: r.paired, 3.0)

    def stream(self, i):
        return next(s for s in self.z._streams.values()
                    if s.player_id == self.pids[i])

    def timeline_at(self, i, t: float):
        """Timeline sample device ``i`` is outputting at server time t, from
        what was actually served to it (the reader history)."""
        frame, _ = self.rx[i].output(t)
        st = self.stream(i)
        if frame is None or len(st.hist) < 2:
            return None
        h = list(st.hist)
        for a, b in zip(h, h[1:]):
            if a[0] <= frame < b[0]:
                return a[1] + (frame - a[0]) * (b[1] - a[1]) / (b[0] - a[0])
        return None

    async def until(self, cond, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            await asyncio.sleep(0.1)
        return cond()

    async def stop(self):
        await self.z.stop_session()
        for r in self.rx.values():
            await r.close()
        self.z.stop()
        await asyncio.sleep(0.3)


def _spread_ms(rig: _Rig, ids) -> float:
    t = time.monotonic()
    pos = [rig.timeline_at(i, t) for i in ids]
    if any(p is None for p in pos):
        return float("inf")
    return (max(pos) - min(pos)) / RATE * 1000


def _aligned(c: Checker, tmp: str) -> None:
    c.section("two receivers on drifting clocks play one timeline")

    async def go():
        rig = _Rig(tmp, {"a": +45.0, "b": -35.0})
        res = await rig.start()
        c.check("the session starts in receiver mode",
                res.get("success") and res.get("mode") == "receiver", res)
        ok = await rig.until(lambda: all(
            s.transport == "ws" and s.connected for s in rig.z._streams.values()),
            10.0)
        c.check("both receivers connect over the socket", ok,
                [(s.name, s.transport, s.connected) for s in rig.z._streams.values()])
        await rig.until(lambda: rig.z._target_lag is not None
                        and all(s.acquired for s in rig.z._streams.values()), 30.0)
        c.check("the ladder acquires both from the receivers' reports",
                all(s.acquired for s in rig.z._streams.values()),
                [(s.name, s.acquired, s.stats) for s in rig.z._streams.values()])
        await asyncio.sleep(12.0)
        spreads = []
        for _ in range(8):
            spreads.append(_spread_ms(rig, ("a", "b")))
            await asyncio.sleep(0.5)
        worst = max(spreads)
        c.check("80 ppm apart, they stay within 2 ms of each other",
                worst < 2.0, [round(x, 2) for x in spreads])
        c.check("no receiver underran",
                all(r.underruns == 0 for r in rig.rx.values()),
                {i: r.underruns for i, r in rig.rx.items()})
        c.check("nothing fell back to the default receiver", not rig.loaded,
                rig.loaded)
        await rig.stop()
    asyncio.run(go())


def _test_signal(c: Checker, tmp: str) -> None:
    c.section("the test signal is corrected like media, and can be aligned by ear")

    async def go():
        rig = _Rig(tmp, {"a": +40.0, "b": -40.0})
        res = await rig.start(test_signal=True)
        c.check("the session plays the test signal",
                res.get("source") == "generated", res)
        await rig.until(lambda: rig.z._target_lag is not None
                        and all(s.acquired for s in rig.z._streams.values()), 30.0)
        c.check("the ladder acquires both devices",
                all(s.acquired for s in rig.z._streams.values()),
                [(s.name, s.stats.get("offset_ms")) for s in rig.z._streams.values()])
        await asyncio.sleep(8.0)
        worst = max([_spread_ms(rig, ("a", "b")) for _ in range(3)])
        c.check("80 ppm apart, they stay within 2 ms", worst < 2.0, worst)
        res = await rig.z.align_start("cast:a", "cast:b")
        c.check("the by-ear probe finds timeline ahead for its clicks",
                res.get("success"), res)
        rig.z.align_cancel()
        await rig.stop()
    asyncio.run(go())


def _tight(c: Checker, tmp: str) -> None:
    c.section("a receiver's ladder closes errors a Cast ladder would leave")

    async def after_disturbance(ladder) -> float:
        saved = cast_sync.LADDER_RECEIVER
        cast_sync.LADDER_RECEIVER = ladder
        try:
            rig = _Rig(tmp, {"a": +10.0, "b": -10.0})
            await rig.start()
            await rig.until(lambda: rig.z._target_lag is not None
                            and all(s.acquired for s in rig.z._streams.values()),
                            30.0)
            await asyncio.sleep(6.0)
            rig.rx["b"].t0 += 0.012       # b drops 12 ms behind, as a hiccup does
            await asyncio.sleep(22.0)
            worst = max([_spread_ms(rig, ("a", "b")) for _ in range(3)])
            await rig.stop()
            return worst
        finally:
            cast_sync.LADDER_RECEIVER = saved

    async def go():
        tight = await after_disturbance(cast_sync.LADDER_RECEIVER)
        loose = await after_disturbance(cast_sync.LADDER_CAST)
        c.check("22 s after a 12 ms hiccup the pair is back within 2 ms",
                tight < 2.0, tight)
        c.check("the Cast thresholds leave it still out by more (control)",
                loose > tight + 2.0, (tight, loose))
    asyncio.run(go())


def _clients(c: Checker, tmp: str) -> None:
    c.section("clients that join on their own play in the same zone as Cast")

    async def go():
        rig = _Rig(tmp, {"a": +30.0}, clients={"t": -25.0, "u": +5.0})
        res = await rig.start()
        c.check("a zone of one Cast receiver and two clients starts",
                res.get("success"), res)
        await rig.until(lambda: rig.z._target_lag is not None
                        and all(s.acquired for s in rig.z._streams.values()), 30.0)
        c.check("all three are acquired",
                all(s.acquired for s in rig.z._streams.values()),
                [(s.name, s.transport, s.acquired) for s in rig.z._streams.values()])
        c.check("the clients were started on their own sockets, not Cast",
                {u for u, _ in rig.starts} == {"a"}, rig.starts)
        await asyncio.sleep(8.0)
        worst = max([_spread_ms(rig, ("a", "t", "u")) for _ in range(3)])
        c.check("all three stay within 2 ms of each other", worst < 2.0, worst)
        names = {r["name"] for r in rig.z.receivers()}
        c.check("the clients are listed by the names they gave",
                {"Tablet t", "Tablet u"} <= names, names)
        saved = json.load(open(f"{tmp}/rx.json"))
        c.check("…and remembered under the ids they generated",
                rig.cid("t") in saved and rig.cid("u") in saved, list(saved))

        c.check("a client is sent its volume when it registers",
                rig.rx["u"].volume == (1.0, False), rig.rx["u"].volume)
        await rig.z.set_receiver_volume(rig.cid("t"), level=0.4)
        c.check("a volume change reaches the client",
                await rig.until(lambda: rig.rx["t"].volume == (0.4, False), 3.0),
                rig.rx["t"].volume)

        before = rig.stream("t").reloads
        await rig.rx["t"].drop()
        await rig.until(lambda: rig.stream("t").parked_since is not None, 3.0)
        c.check("a client that disconnects is parked at once",
                rig.stream("t").parked_since is not None)
        t0 = time.monotonic()
        rig.rx["t"].volume = None
        await rig.register("t")
        back = await rig.until(lambda: rig.stream("t").parked_since is None
                               and rig.stream("t").connected, 10.0)
        c.check("registering again rejoins it promptly, not on the 30 s probe",
                back and time.monotonic() - t0 < 5.0, time.monotonic() - t0)
        c.check("…and its volume is restored",
                await rig.until(lambda: rig.rx["t"].volume == (0.4, False), 3.0),
                rig.rx["t"].volume)
        await rig.until(lambda: rig.stream("t").acquired, 20.0)
        await asyncio.sleep(6.0)
        worst = max([_spread_ms(rig, ("a", "t", "u")) for _ in range(3)])
        c.check("…back in step with the others", worst < 3.0, worst)
        c.check("…without charging a reload to the re-align budget",
                rig.stream("t").reloads_since_align == 0,
                (rig.stream("t").reloads, before))

        await rig.z.stop_session()
        await rig.until(lambda: rig.rx["u"].stops > 0, 3.0)
        c.check("stopping the zone sends clients back to idle",
                rig.rx["u"].stops == 1 and rig.rx["u"].sid is None, rig.rx["u"].stops)
        c.check("…still connected, ready for the next zone",
                rig.cid("u") in rig.z._client_ws)
        await rig.stop()
    asyncio.run(go())


def _client_late(c: Checker, tmp: str) -> None:
    c.section("a client offline when the zone starts joins when it connects")

    async def go():
        rig = _Rig(tmp, {"a": 0.0}, clients={"v": +10.0})
        rig.offline = {"v"}
        await rig.start()
        await rig.until(lambda: rig.stream("v").parked_since is not None, 5.0)
        c.check("it is parked, not waited for",
                rig.stream("v").parked_since is not None)
        await rig.until(lambda: rig.stream("a").acquired, 30.0)
        c.check("the rest of the zone plays on", rig.stream("a").acquired)
        await rig.register("v")
        ok = await rig.until(lambda: rig.stream("v").acquired, 25.0)
        c.check("connecting later brings it into the zone", ok,
                rig.stream("v").stats)
        await asyncio.sleep(5.0)
        c.check("…in step", _spread_ms(rig, ("a", "v")) < 3.0,
                _spread_ms(rig, ("a", "v")))
        await rig.stop()
    asyncio.run(go())


def _provider(c: Checker, tmp: str) -> None:
    c.section("the controller sees clients as players a zone can drive")

    async def go():
        from modules.media.players.receiver import ReceiverPlayerProvider
        rig = _Rig(tmp, {}, clients={"w": 0.0})
        rig.z.start()
        await asyncio.sleep(0.3)
        prov = ReceiverPlayerProvider(rig.z)
        c.check("a client is a zone transport", prov.zone_transport)
        await rig.register("w")
        players = await prov.list_players()
        mine = [p for p in players if p.player_id == rig.pids["w"]]
        c.check("a connected client is an available player",
                len(mine) == 1 and mine[0].available and mine[0].name == "Tablet w",
                [p.to_dict() for p in players])
        await rig.rx["w"].drop()
        await rig.until(lambda: rig.cid("w") not in rig.z._client_ws, 3.0)
        st = await prov.get_state(rig.pids["w"])
        c.check("a disconnected one stays listed, unavailable",
                st is not None and not st.available, st)
        err = None
        try:
            await prov.play_url(rig.pids["w"], None)
        except RuntimeError as e:
            err = str(e)
        c.check("it refuses to play outside a zone, saying why",
                err and "zone" in err, err)
        await rig.register("w")
        first = rig.rx["w"]
        second = FakeReceiver(rig.port, 0.0)
        second.key = first.key        # the same device: a second tab
        got = []

        async def watch_first():
            try:
                async for m in first.ws:
                    if isinstance(m, str):
                        got.append(json.loads(m)["type"])
            except Exception:
                pass
        for t in first._tasks:
            t.cancel()
        watcher = asyncio.create_task(watch_first())
        await second.register(rig.cid("w"), "Tablet w")
        await rig.until(lambda: "replaced" in got, 3.0)
        c.check("a second socket for one client takes over and tells the first",
                "replaced" in got
                and rig.z._client_ws.get(rig.cid("w")) is not None, got)
        watcher.cancel()
        await second.close()
        bad = FakeReceiver(rig.port, 0.0)
        await bad.register("x", "short id")
        await asyncio.sleep(0.3)
        c.check("an id that is not one a client generated is refused",
                "x" not in rig.z._client_ws and bad.registered is None)
        await bad.close()
        await rig.rx["w"].close()
        rig.z.stop()
        await asyncio.sleep(0.3)
    asyncio.run(go())


def _pairing(c: Checker, tmp: str) -> None:
    c.section("a client is admitted by pairing, and proves it on every connect")

    async def go():
        from modules.media.players.receiver import ReceiverPlayerProvider
        rig = _Rig(tmp, {}, clients={"p": 0.0})
        rig.z.start()
        await asyncio.sleep(0.3)
        prov = ReceiverPlayerProvider(rig.z)
        # As the controller resolves it in the app.
        rig.z.set_provider_resolver(
            lambda pid: prov if pid.startswith("receiver:") else None)
        r, cid = rig.rx["p"], rig.cid("p")
        await rig.register("p", pair=False)
        c.check("a new client enrols and waits to be paired",
                r.registered and r.paired is False, r.registered)
        c.check("…it is not offered as a player",
                not [p for p in await prov.list_players()
                     if p.player_id == rig.pids["p"]])
        c.check("…nor accepted into a zone", not rig.z._zone_capable(rig.pids["p"])
                and rig.z.save_group("Kitchen", [rig.pids["p"]]).get("success") is not True)
        status = json.dumps(rig.z.status())
        c.check("the key appears nowhere in the status",
                r.key not in status and "key" not in json.dumps(rig.z.receivers()))
        import os, stat
        mode = stat.S_IMODE(os.stat(f"{tmp}/rx.json").st_mode)
        c.check("the registry holding it is owner-only on disk", mode == 0o600, oct(mode))

        res = await rig.z.pair_receiver(cid, "000000" if _pair_code(r.key) != "000000"
                                        else "111111")
        c.check("a wrong code is refused, saying how many tries are left",
                not res["success"] and res.get("left") == 4, res)
        res = await rig.z.pair_receiver(cid, _pair_code(r.key)[:3] + " "
                                        + _pair_code(r.key)[3:])
        c.check("the code it shows pairs it (spaces allowed)", res["success"], res)
        c.check("…and the client is told",
                await rig.until(lambda: r.paired is True, 3.0))
        c.check("…and is now a player",
                rig.pids["p"] in [p.player_id for p in await prov.list_players()])
        c.check("…a zone can drive", rig.z._zone_capable(rig.pids["p"]))

        await r.drop()
        await rig.until(lambda: cid not in rig.z._client_ws, 3.0)
        await rig.register("p", pair=False)
        c.check("reconnecting answers a challenge and comes back paired",
                r.paired is True and any(m["type"] == "auth" for m in r.sent), r.sent)
        c.check("…the key went over the wire once, at enrolment, never again",
                sum(1 for m in r.sent if "key" in m) == 1,
                [m["type"] for m in r.sent])

        thief = FakeReceiver(rig.port, 0.0)      # knows the id, not the key
        await thief.register(cid, "Kitchen tablet")
        await asyncio.sleep(0.5)
        c.check("a client claiming a paired id without its key is refused",
                "authentication failed" in thief.errors, thief.errors)
        c.check("…and cannot evict the real one",
                rig.z._client_ws.get(cid) is not None and r.ws is not None
                and rig.z._client_ws.get(cid) is not None)
        await thief.close()

        res = await rig.z.start_session([rig.pids["p"]], media={
            "items": [{"source_id": "a", "duration_ms": 600_000}],
            "media_type": "x"})
        await rig.until(lambda: rig.stream("p").connected, 10.0)
        st = rig.stream("p")
        other = FakeReceiver(rig.port, 0.0)
        await other._connect()
        await other.send({"type": "hello", "sid": st.sid, "load": st.ws_load})
        await asyncio.sleep(0.5)
        c.check("a socket that has not proved it is the client cannot take its stream",
                "unknown sid" in other.errors and st.ws is not None
                and st.ws is not getattr(other, "ws", None), other.errors)
        await other.close()
        await rig.z.stop_session()

        old_code = _pair_code(r.key)
        ok = await rig.z.forget_receiver(cid)
        c.check("forgetting it tells the client and drops it",
                ok and await rig.until(lambda: r.paired is False, 3.0)
                and cid not in rig.z._clients)
        await asyncio.sleep(0.3)
        r.ws = None
        await rig.register("p", pair=False)
        c.check("…it can only come back by enrolling again, with a new code",
                r.paired is False and _pair_code(r.key) != old_code
                and rig.z._clients[cid]["key"] == r.key)
        for _ in range(5):
            res = await rig.z.pair_receiver(cid, "999999" if _pair_code(r.key) != "999999"
                                            else "888888")
        c.check("five wrong codes drop the enrolment",
                not res["success"] and cid not in rig.z._clients, res)
        await r.close()
        rig.z.stop()
        await asyncio.sleep(0.3)
    asyncio.run(go())


def _fallback(c: Checker, tmp: str) -> None:
    c.section("a device whose receiver never answers falls back alone")

    async def go():
        saved = cast_sync.RECEIVER_CONNECT_MAX_S
        cast_sync.RECEIVER_CONNECT_MAX_S = 1.0
        try:
            rig = _Rig(tmp, {"a": 0.0}, connect=False)
            await rig.start()
            await rig.until(lambda: rig.loaded, 5.0)
            st = rig.stream("a")
            c.check("it is LOADed on the default receiver instead",
                    len(rig.loaded) == 1 and "/sync/stream/" in rig.loaded[0],
                    rig.loaded)
            c.check("its transport says so", st.transport == "http"
                    and st.http_fallback, (st.transport, st.http_fallback))
            await rig.stop()
        finally:
            cast_sync.RECEIVER_CONNECT_MAX_S = saved
    asyncio.run(go())


def _dropped(c: Checker, tmp: str) -> None:
    c.section("a dropped socket is restarted and re-aligned")

    async def go():
        rig = _Rig(tmp, {"a": +20.0, "b": -20.0})
        await rig.start()
        await rig.until(lambda: rig.z._target_lag is not None
                        and all(s.acquired for s in rig.z._streams.values()), 30.0)
        await asyncio.sleep(3.0)
        before = len(rig.starts)
        await rig.rx["b"].drop()
        ok = await rig.until(lambda: len(rig.starts) > before
                             and rig.stream("b").connected, 15.0)
        c.check("the engine sends a fresh load over Cast", ok,
                rig.starts[before:])
        c.check("…counted as a reload", rig.stream("b").reloads >= 1,
                rig.stream("b").reloads)
        await rig.until(lambda: rig.stream("b").acquired, 20.0)
        await asyncio.sleep(6.0)
        worst = max([_spread_ms(rig, ("a", "b")) for _ in range(3)])
        c.check("and it is back in step with the other", worst < 3.0, worst)
        await rig.stop()
    asyncio.run(go())


def _skip(c: Checker, tmp: str) -> None:
    c.section("a skip flushes the receivers and is heard within 1.5 s")

    async def heard_after_skip(flush: bool) -> float:
        rig = _Rig(tmp, {"a": +10.0, "b": -10.0}, source_delay_s=3.0)
        if not flush:
            rig.z._flush_receivers = lambda: None
        rows = [{"source_id": "a", "duration_ms": 60_000},
                {"source_id": "b", "duration_ms": 60_000},
                {"source_id": "c", "duration_ms": 60_000}]
        await rig.start({"items": rows, "media_type": "x"})
        await rig.until(lambda: rig.z._target_lag is not None
                        and all(s.acquired for s in rig.z._streams.values()), 30.0)
        await asyncio.sleep(2.0)
        t0 = time.monotonic()
        await rig.z.skip_to(2)
        heard = {}

        def both_hear_c():
            now = time.monotonic()
            for i, r in rig.rx.items():
                level = r.output(now)[1]
                if i not in heard and level is not None \
                        and abs(level - LEVEL["c"]) <= 2:
                    heard[i] = now - t0
            return len(heard) == 2

        await rig.until(both_hear_c, 10.0)
        spread = max(heard.values()) - min(heard.values()) if len(heard) == 2 \
            else float("inf")
        await rig.stop()
        return max(heard.values()) if len(heard) == 2 else float("inf"), spread

    async def go():
        fast, spread = await heard_after_skip(flush=True)
        slow, _ = await heard_after_skip(flush=False)
        c.check("the target is heard within 1.5 s of the skip", fast < 1.5, fast)
        c.check("…on both receivers together", spread < 0.01, spread)
        c.check("flushing saves the receivers' queued audio (control)",
                slow - fast > 0.6, (fast, slow))
    asyncio.run(go())


def run() -> Checker:
    c = Checker("zone_receiver")
    with tempfile.TemporaryDirectory() as tmp:
        for sid in LEVEL:
            _wav(f"{tmp}/{sid}.wav", LEVEL[sid], 120.0)
        _aligned(c, tmp)
        _test_signal(c, tmp)
        _tight(c, tmp)
        _clients(c, tmp)
        _client_late(c, tmp)
        _provider(c, tmp)
        _pairing(c, tmp)
        _fallback(c, tmp)
        _dropped(c, tmp)
        _skip(c, tmp)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
