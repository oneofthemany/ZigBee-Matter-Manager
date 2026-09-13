"""
The Blueair controller.

Two layers, because the library needs Python 3.12 and the dev box may not have it:

  * Always: a fake `blueair_api` module stands in for the cloud client, so
    credentials, login (current + classic generations), caching and the
    control mapping are exercised with no network and no library.
  * When the real blueair_api imports (the 3.12 container, or a 3.12 venv):
    real DeviceAws objects parse Blueair-shaped payloads served by a fake API
    object, and the writes the controller issues are checked at the
    set_device_info boundary — the last point before the cloud. This is what
    catches a library upgrade renaming a field or changing a value scale.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

from harness import Checker

from modules import blueair_controller as B


def _run(coro):
    return asyncio.run(coro)


class FakeLoginError(Exception):
    pass


class FakeAws:
    """The DeviceAws attributes and setters the controller touches."""

    def __init__(self, uuid, name="Bedroom", hw="nb_h", count=91, **attrs):
        self.uuid, self.name, self.name_api, self.mac = uuid, name, name, "aa:bb"
        self.type_name, self.model_name, self.hw = "Blueair", "HealthProtect 7470i", hw
        self.fan_speed_count = count
        self.standby, self.fan_speed, self.fan_auto_mode = False, 37, False
        self.night_mode, self.child_lock, self.brightness = False, False, 80
        self.germ_shield, self.filter_usage_percentage = False, 42
        self.pm1 = self.pm10 = NotImplemented
        self.pm2_5, self.voc, self.total_voc, self.co2 = 7, NotImplemented, NotImplemented, NotImplemented
        self.temperature, self.humidity = 21, 45
        self.__dict__.update(attrs)
        self.refreshes, self.writes, self.fail_refresh = 0, [], False

    async def refresh(self):
        self.refreshes += 1
        await asyncio.sleep(0)
        if self.fail_refresh:
            raise RuntimeError("rate limited")

    def __getattr__(self, name):
        if name.startswith("set_"):
            async def _set(value):
                self.writes.append((name, value))
                attr = {"set_standby": "standby", "set_fan_auto_mode": "fan_auto_mode"}.get(name, name[4:])
                setattr(self, attr, value)
            return _set
        raise AttributeError(name)


class FakeClassic(FakeAws):
    def __init__(self, uuid, **attrs):
        super().__init__(uuid, name="Classic", **attrs)
        self.model = "Classic 480i"
        for n in ("standby", "night_mode", "germ_shield", "filter_usage_percentage",
                  "fan_speed_count", "model_name", "hw"):
            self.__dict__.pop(n, None)
        self.fan_speed, self.brightness, self.filter_expired = 2, 3, False

    async def set_fan_speed(self, value):
        self.writes.append(("set_fan_speed", value))
        self.fan_speed = int(value)


class FakeApi:
    closed = 0

    async def cleanup_client_session(self):
        FakeApi.closed += 1


def _fake_library(aws=(), classic=(), aws_error=None, classic_error=None):
    lib = types.ModuleType("blueair_api")
    lib.LoginError = FakeLoginError
    lib.calls = []

    async def get_aws_devices(username, password, region):
        lib.calls.append(("aws", username, region))
        if aws_error:
            raise aws_error
        return FakeApi(), list(aws)

    async def get_devices(username, password):
        lib.calls.append(("classic", username))
        if classic_error:
            raise classic_error
        return FakeApi(), list(classic)

    lib.get_aws_devices, lib.get_devices = get_aws_devices, get_devices
    return lib


class Env:
    """Temporary secrets file, credential env vars and a fake library."""

    def __init__(self, lib=None, secrets=None, env=None):
        self.lib, self.secrets, self.env = lib, secrets, env or {}

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_file = B.SECRETS_FILE
        B.SECRETS_FILE = str(Path(self._tmp.name) / "secrets.yaml")
        if self.secrets is not None:
            import yaml
            Path(B.SECRETS_FILE).write_text(yaml.dump(self.secrets))
        self._saved_env = {k: os.environ.pop(k, None) for k in (B.ENV_USERNAME, B.ENV_PASSWORD)}
        os.environ.update(self.env)
        self._saved_lib = sys.modules.get("blueair_api")
        if self.lib is not None:
            sys.modules["blueair_api"] = self.lib
        return self

    def __exit__(self, *exc):
        B.SECRETS_FILE = self._saved_file
        for k, v in self._saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        if self._saved_lib is None:
            sys.modules.pop("blueair_api", None)
        else:
            sys.modules["blueair_api"] = self._saved_lib
        self._tmp.cleanup()


CREDS = {"blueair": {"username": "me@example.com", "password": "hunter2"}}


def _credentials(c: Checker) -> None:
    c.section("credentials")
    with Env():
        c.check("nothing configured", B.credentials_status() == {
            "configured": False, "source": "none", "username": ""})
        B.save_credentials("me@example.com", "hunter2")
        mode = stat.S_IMODE(os.stat(B.SECRETS_FILE).st_mode)
        c.check("secrets file is 0600", mode == 0o600, oct(mode))
        st = B.credentials_status()
        c.check("status reports the saved account", st["configured"] and st["source"] == "secrets_file"
                and st["username"] == "me@example.com", st)
        c.check("status never carries the password", "hunter2" not in repr(st))
        try:
            B.save_credentials("me@example.com", " ")
            c.check("a blank password is refused", False)
        except ValueError:
            c.check("a blank password is refused", True)

    with Env(secrets={"fuel_finder": {"client_id": "x", "client_secret": "y"}}):
        B.save_credentials("me@example.com", "hunter2")
        import yaml
        data = yaml.safe_load(Path(B.SECRETS_FILE).read_text())
        c.check("other integrations' secrets are preserved", data.get("fuel_finder") == {
            "client_id": "x", "client_secret": "y"}, data)

    with Env(secrets=CREDS, env={B.ENV_USERNAME: "env@example.com", B.ENV_PASSWORD: "envpass"}):
        c.check("the environment wins over the file",
                B.resolve_credentials() == ("env@example.com", "envpass")
                and B.credentials_status()["source"] == "environment")


def _config(c: Checker) -> None:
    c.section("config")
    ctl = B.BlueairController({"enabled": True, "region": "US", "poll_interval_seconds": 5})
    c.check("region is case-insensitive", ctl.region == "us")
    c.check("poll interval is floored against rate limits", ctl.poll_seconds == B.MIN_POLL_SECONDS)
    c.check("an unknown region falls back to the default",
            B.BlueairController({"region": "mars"}).region == B.DEFAULT_REGION)

    with Env(lib=_fake_library(), secrets=CREDS):
        for label, cfg, needle in (("disabled", {"enabled": False}, "disabled"),):
            try:
                _run(B.BlueairController(cfg).list_devices())
                c.check(f"{label} raises", False)
            except B.BlueairError as e:
                c.check(f"{label} raises a readable error", needle in str(e), str(e))
    with Env(lib=_fake_library()):
        try:
            _run(B.BlueairController({"enabled": True}).list_devices())
            c.check("no account raises", False)
        except B.BlueairError as e:
            c.check("no account points at Settings", "Settings" in str(e), str(e))


def _login(c: Checker) -> None:
    c.section("login")
    aws, classic = FakeAws("A1"), FakeClassic("C1")
    lib = _fake_library(aws=[aws], classic=[classic])
    with Env(lib=lib, secrets=CREDS):
        ctl = B.BlueairController({"enabled": True, "region": "eu"})
        devices = _run(ctl.list_devices())
        c.check("both generations are listed", sorted(d["id"] for d in devices) == ["A1", "C1"], devices)
        c.check("login uses the configured region", ("aws", "me@example.com", "eu") in lib.calls)

    lib = _fake_library(aws=[aws], classic_error=RuntimeError("no classic devices"))
    with Env(lib=lib, secrets=CREDS):
        ctl = B.BlueairController({"enabled": True})
        c.check("an account with no classic devices still works",
                [d["id"] for d in _run(ctl.list_devices())] == ["A1"])

    lib = _fake_library(aws_error=FakeLoginError("bad credentials"))
    with Env(lib=lib, secrets=CREDS):
        try:
            _run(B.BlueairController({"enabled": True, "region": "au"}).list_devices())
            c.check("a login failure raises", False)
        except B.BlueairError as e:
            c.check("a login failure names the region to check", "au" in str(e) and "region" in str(e), str(e))

    lib = _fake_library(aws_error=RuntimeError("cloud down"), classic_error=RuntimeError("also down"))
    with Env(lib=lib, secrets=CREDS):
        try:
            _run(B.BlueairController({"enabled": True}).list_devices())
            c.check("both APIs failing raises", False)
        except B.BlueairError as e:
            c.check("both APIs failing raises the cause", "cloud down" in str(e), str(e))

    with Env(secrets=CREDS):
        sys.modules["blueair_api"] = None   # import of a None entry raises ImportError
        try:
            _run(B.BlueairController({"enabled": True}).list_devices())
            c.check("a missing library raises", False)
        except B.BlueairError as e:
            c.check("a missing library says so", "not installed" in str(e), str(e))

    live, probe = FakeAws("LIVE"), FakeAws("PROBE")
    lib = _fake_library(aws=[live])
    with Env(lib=lib, secrets=CREDS):
        ctl = B.BlueairController({"enabled": True})
        _run(ctl.list_devices())
        lib.get_aws_devices = (lambda orig: (lambda **kw: _fake_library(aws=[probe]).get_aws_devices(**kw)))(lib.get_aws_devices)
        found = _run(ctl.test_login("other@example.com", "pw", "us"))
        c.check("test login reports the tested account's devices", [d["id"] for d in found] == ["PROBE"], found)
        c.check("test login leaves the live session alone", ctl.device_ids() == ["LIVE"], ctl.device_ids())


def _caching(c: Checker) -> None:
    c.section("status cache")
    dev = FakeAws("A1")
    with Env(lib=_fake_library(aws=[dev]), secrets=CREDS):
        ctl = B.BlueairController({"enabled": True})

        async def burst():
            return await asyncio.gather(*(ctl.status("A1", max_age=60) for _ in range(5)))
        _run(burst())
        c.check("a burst of reads makes one cloud refresh", dev.refreshes == 1, dev.refreshes)
        _run(ctl.status("A1", max_age=60))
        c.check("a fresh cached read makes no cloud call", dev.refreshes == 1)
        _run(ctl.status("A1", max_age=0))
        c.check("max_age=0 forces a refresh", dev.refreshes == 2)

        dev.fail_refresh = True
        s = _run(ctl.status("A1", max_age=0))
        c.check("a failed refresh serves the last reading, flagged stale",
                s.get("stale") and s["online"] is False and s["pm2_5"] == 7, s)
        c.check("the failure is recorded for the settings page", ctl.last_error == "rate limited")

        try:
            _run(ctl.status("NOPE", max_age=0))
            c.check("an unknown device raises", False)
        except B.BlueairError:
            c.check("an unknown device raises", True)


def _control(c: Checker) -> None:
    c.section("control — current generation")
    purifier = FakeAws("P", hw="nb_h", count=91)
    humid = FakeAws("H", hw="hum_s", count=3, fan_speed=2)
    no_night = FakeAws("N", night_mode=NotImplemented)
    with Env(lib=_fake_library(aws=[purifier, humid, no_night]), secrets=CREDS):
        ctl = B.BlueairController({"enabled": True})
        s = _run(ctl.status("P"))
        c.check("fan speed is a percentage of the model's scale", s["fan_speed_pct"] == 41, s["fan_speed_pct"])
        c.check("power is the inverse of standby", s["power"] is True)

        s = _run(ctl.control("P", {"power": False}))
        c.check("power off writes standby=True", purifier.writes[-1] == ("set_standby", True) and s["power"] is False)
        _run(ctl.control("P", {"fan_speed_pct": 100}))
        c.check("100% is the top gear on a 0-91 model", purifier.writes[-1] == ("set_fan_speed", 91))
        _run(ctl.control("H", {"fan_speed_pct": 0}))
        c.check("a humidifier's lowest speed is gear 1, not 0", humid.writes[-1] == ("set_fan_speed", 1))
        _run(ctl.control("P", {"brightness": 250}))
        c.check("brightness is clamped", purifier.writes[-1] == ("set_brightness", 100))
        try:
            _run(ctl.control("N", {"night_mode": True}))
            c.check("an unsupported control is refused", False)
        except B.BlueairError as e:
            c.check("an unsupported control is refused before any write",
                    "night mode" in str(e) and no_night.writes == [], (str(e), no_night.writes))

    c.section("control — classic generation")
    classic = FakeClassic("C")
    with Env(lib=_fake_library(classic=[classic], aws_error=RuntimeError("none")), secrets=CREDS):
        ctl = B.BlueairController({"enabled": True})
        s = _run(ctl.status("C"))
        c.check("classic power comes from fan speed", s["power"] is True and s["fan_speed_pct"] == 67, s)
        c.check("classic has no night mode", s["capabilities"]["night_mode"] is False)
        _run(ctl.control("C", {"power": False}))
        c.check("classic power off is fan speed 0", classic.writes[-1] == ("set_fan_speed", "0"))
        _run(ctl.control("C", {"power": True}))
        c.check("classic power on from off picks speed 1", classic.writes[-1] == ("set_fan_speed", "1"))
        n = len(classic.writes)
        classic.fan_speed = 3
        _run(ctl.control("C", {"power": True}))
        c.check("classic power on while running changes nothing", len(classic.writes) == n)
        _run(ctl.control("C", {"fan_speed_pct": 50}))
        c.check("classic fan percent maps onto 0-3", classic.writes[-1] == ("set_fan_speed", "2"))
        _run(ctl.control("C", {"brightness": 9}))
        c.check("classic brightness tops out at 4", classic.writes[-1] == ("set_brightness", 4))


def _real_library(c: Checker) -> None:
    """Contract checks against the installed blueair_api, when there is one."""
    saved = sys.modules.pop("blueair_api", None)
    try:
        lib = importlib.import_module("blueair_api")
    except ImportError:
        print("\n  real-library contract: skipped (blueair_api not installed)")
        return
    except SyntaxError:
        print("\n  real-library contract: skipped (blueair_api needs Python 3.12+)")
        return
    finally:
        if saved is not None:
            sys.modules["blueair_api"] = saved

    c.section("real blueair_api contract")

    class CloudApi:
        def __init__(self):
            self.writes = []

        async def device_info(self, name, uuid):
            ctl = lambda n: {"n": n, "v": 0}
            return {
                "configuration": {
                    "di": {"name": "Lounge", "hw": "nb_h", "sku": "unknown", "cfv": "1", "mfv": "1",
                           "ofv": "1", "ds": "SN1"},
                    "ds": {"pm2_5": {"n": "pm2_5", "i": 0, "e": True, "fe": True, "ot": "", "tn": "", "ttl": 0},
                           "t": {"n": "t", "i": 0, "e": True, "fe": True, "ot": "", "tn": "", "ttl": 0}},
                    "dc": {n: ctl(n) for n in ("standby", "fanspeed", "automode", "childlock",
                                               "nightmode", "brightness", "filterusage")},
                },
                "states": [
                    {"n": "standby", "vb": False}, {"n": "fanspeed", "v": 64},
                    {"n": "automode", "vb": True}, {"n": "childlock", "vb": False},
                    {"n": "nightmode", "vb": False}, {"n": "brightness", "v": 55},
                    {"n": "filterusage", "v": 12},
                ],
            }

        async def device_sensors(self, name, uuid):
            return [{"sensors": ["pm2_5", "t"], "datapoints": [[1000, 9, 22], [2000, 11, 23]]}]

        async def set_device_info(self, uuid, service, verb, value):
            self.writes.append((service, verb, value))
            return True

        async def cleanup_client_session(self):
            pass

    api = CloudApi()

    async def scenario():
        dev = await lib.DeviceAws.create_device(api=api, uuid="U1", name="Lounge", mac="m", type_name="purifier")
        ctl = B.BlueairController({"enabled": True})
        ctl._devices = {"U1": (dev, "aws")}
        ctl._devices_at = float("inf")
        ctl._session_key = ("me@example.com", ctl.region)
        s = await ctl.status("U1", max_age=0)
        await ctl.control("U1", {"power": False, "fan_speed_pct": 100, "auto": False})
        return s

    with Env(secrets=CREDS):
        s = _run(scenario())
    c.check("the real library parses power, auto and brightness",
            s["power"] is True and s["auto"] is True and s["brightness"] == 55, s)
    c.check("the real library's latest sensor reading is used", s["pm2_5"] == 11 and s["temperature_c"] == 23, s)
    c.check("fan speed is scaled by the real fan_speed_count (64/91)", s["fan_speed_pct"] == 70, s["fan_speed_pct"])
    c.check("filter usage comes through", s["filter_usage_pct"] == 12)
    c.check("whole-number states are ints, not SenML floats",
            type(s["brightness"]) is int and type(s["filter_usage_pct"]) is int,
            (s["brightness"], s["filter_usage_pct"]))
    c.check("an unsupported field reports as absent, not NotImplemented",
            s["pm10"] is None and "NotImplemented" not in repr(s))
    c.check("writes reach the cloud with the wire names and verbs Blueair expects",
            api.writes == [("standby", "vb", True), ("automode", "vb", False), ("fanspeed", "v", 91)],
            api.writes)


def run() -> Checker:
    c = Checker("blueair_controller")
    _credentials(c)
    _config(c)
    _login(c)
    _caching(c)
    _control(c)
    _real_library(c)
    return c


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
