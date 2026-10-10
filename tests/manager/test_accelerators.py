"""The detection-hardware probe (manager/accelerators.py) against fake sysfs trees."""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

from harness import Checker


def _tree(root: Path, pci=(), usb=(), dev=(), cpuinfo="model name\t: Test CPU\nflags\t\t: fpu avx2\n"):
    for addr, vendor, device, cls, driver in pci:
        d = root / "sys/bus/pci/devices" / addr
        d.mkdir(parents=True)
        (d / "vendor").write_text(vendor + "\n")
        (d / "device").write_text(device + "\n")
        (d / "class").write_text(cls + "\n")
        if driver:
            drv = root / "sys/bus/pci/drivers" / driver
            drv.mkdir(parents=True, exist_ok=True)
            os.symlink(drv, d / "driver")
    for name, vid, pid, speed in usb:
        d = root / "sys/bus/usb/devices" / name
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "speed").write_text(speed + "\n")
    (root / "dev").mkdir(exist_ok=True)
    for n in dev:
        (root / "dev" / n).write_text("")
    (root / "proc").mkdir(exist_ok=True)
    (root / "proc/cpuinfo").write_text(cpuinfo)


def _probe(root: Path):
    os.environ.update(ZMM_SYSFS_ROOT=str(root / "sys"), ZMM_DEV_ROOT=str(root / "dev"),
                      ZMM_PROC_ROOT=str(root / "proc"))
    import manager.accelerators
    return importlib.reload(manager.accelerators).probe()


CORAL = ("0000:02:00.0", "0x1ac1", "0x089a", "0x088000")
IGPU = ("0000:00:02.0", "0x8086", "0x7d55", "0x030000", "i915")
NIC = ("0000:03:00.0", "0x8086", "0x15f3", "0x020000", "igc")


def run() -> Checker:
    c = Checker("accelerators")
    data_dir = os.environ["ZMM_DATA_DIR"]
    try:
        with tempfile.TemporaryDirectory() as t:
            _tree(Path(t), pci=[(*CORAL, None), IGPU, NIC])
            p = _probe(Path(t))
            coral = next(d for d in p["devices"] if d["kind"] == "coral")
            c.check("an M.2 Coral on the bus without its driver is found but not ready",
                    coral["address"] == "0000:02:00.0" and coral["driver"] is None and not coral["ready"], coral)
            c.check("…and says why", "gasket/apex" in coral["note"], coral["note"])
            c.check("so detection falls back to the next usable device, and says the Coral could be used",
                    p["recommended"] == "intel_gpu" and p["could_use"] == "coral", p)
            c.check("an Intel network card isn't mistaken for Intel graphics",
                    [d["kind"] for d in p["devices"]] == ["intel_gpu", "coral"], [d["kind"] for d in p["devices"]])
            c.check("the CPU is described", p["cpu"]["model"] == "Test CPU" and p["cpu"]["avx2"], p["cpu"])

        with tempfile.TemporaryDirectory() as t:
            _tree(Path(t), pci=[(*CORAL, "apex"), IGPU], dev=["apex_0"])
            p = _probe(Path(t))
            coral = next(d for d in p["devices"] if d["kind"] == "coral")
            c.check("with the apex driver bound the Coral is ready and preferred",
                    coral["ready"] and coral["device_nodes"] == ["apex_0"] and p["recommended"] == "coral"
                    and p["could_use"] is None, p)

            apex = Path(t) / "sys/class/apex/apex_0"
            apex.mkdir(parents=True)
            for name, v in (("temp", 91100), ("trip_point0_temp", 84800), ("trip_point1_temp", 89800),
                            ("trip_point2_temp", 94800), ("hw_temp_warn2", 99800)):
                (apex / name).write_text(f"{v}\n")
            th = next(d for d in _probe(Path(t))["devices"] if d["kind"] == "coral")["thermal"]
            c.check("the chip's temperature and throttle points are reported in °C",
                    th["temp_c"] == 91.1 and th["throttle_at_c"] == [84.8, 89.8, 94.8]
                    and th["shutdown_at_c"] == 99.8, th)
            c.check("…and how far it has been slowed", th["throttle_step"] == 2, th)

        with tempfile.TemporaryDirectory() as t:
            os.environ["ZMM_DATA_DIR"] = t
            import manager.accelerators
            acc = importlib.reload(manager.accelerators)
            c.check("with no host helper report the driver state is unknown, not an error",
                    acc.driver_status()["known"] is False)
            r = acc.request_driver("install")
            trig = Path(t) / "data/coral/driver_action"
            c.check("asking for the driver writes the trigger the host watches",
                    r["success"] and trig.read_text() == "install" and acc.driver_status()["pending"], r)
            c.check("an unknown action is refused", not acc.request_driver("rm -rf")["success"])
            trig.unlink()
            r = acc.set_throttle(70)
            c.check("setting the throttle writes the temperature and asks the host to apply it",
                    r["success"] and (Path(t) / "data/coral/thermal").read_text() == "70" and trig.read_text() == "check", r)
            c.check("a temperature outside 50–85 °C is refused, as is junk",
                    not acc.set_throttle(95)["success"] and not acc.set_throttle(40)["success"]
                    and not acc.set_throttle("hot")["success"]
                    and (Path(t) / "data/coral/thermal").read_text() == "70")
            (Path(t) / "data/coral/driver_status.json").write_text('{"state": "done", "installed": true}')
            trig.unlink()
            st = acc.driver_status()
            c.check("the host's report is passed through", st["known"] and st["installed"] and not st["pending"], st)

            # Watchdog: a fitted Coral is set up to load at boot without anyone asking.
            tree = Path(t) / "hw"
            _tree(tree, pci=[(*CORAL, "apex")])
            os.environ.update(ZMM_SYSFS_ROOT=str(tree / "sys"), ZMM_DEV_ROOT=str(tree / "dev"))
            acc = importlib.reload(manager.accelerators)
            status = Path(t) / "data/coral/driver_status.json"
            trig = Path(t) / "data/coral/driver_action"
            status.write_text('{"state": "done", "installed": false, "loaded": true, "backend": "systemd"}')
            w = {}
            c.check("loaded by hand but not at boot: the watchdog asks for the boot-time driver",
                    acc.ensure_driver(w, now=1000) == "install" and trig.read_text() == "install")
            c.check("…not again while that request is waiting", acc.ensure_driver(w, now=5000) is None)
            trig.unlink()
            c.check("…nor within a quarter of an hour of asking", acc.ensure_driver(w, now=1500) is None)
            status.write_text('{"state": "running", "installed": false}')
            c.check("…nor while a build is under way", acc.ensure_driver({}, now=1000) is None)
            status.write_text('{"state": "failed", "installed": false, "detail": "Secure Boot"}')
            w = {"requested_at": 1000}
            c.check("after a failure it waits hours, not minutes, before trying again",
                    acc.ensure_driver(w, now=1000 + 3600) is None and acc.ensure_driver(w, now=1000 + 7 * 3600) == "install")
            trig.unlink()
            status.write_text('{"state": "done", "installed": true, "loaded": true}')
            c.check("installed: left alone", acc.ensure_driver({}, now=99999) is None and not trig.exists())
            status.write_text('{"state": "done", "installed": false, "loaded": true, "backend": "none"}')
            c.check("no service manager on the host and loaded: nothing more it can do", acc.ensure_driver({}, now=99999) is None)
            _tree(Path(t) / "nocoral", pci=[IGPU])
            os.environ.update(ZMM_SYSFS_ROOT=str(Path(t) / "nocoral/sys"))
            acc = importlib.reload(manager.accelerators)
            status.unlink()
            c.check("no Coral fitted: never asks", acc.ensure_driver({}, now=99999) is None and not trig.exists())

        with tempfile.TemporaryDirectory() as t:
            _tree(Path(t), pci=[(*CORAL, "apex")])          # nothing in /dev, as inside a container
            (Path(t) / "sys/bus/pci/devices/0000:02:00.0/apex/apex_0").mkdir(parents=True)
            coral = _probe(Path(t))["devices"][0]
            c.check("the device node is found from sysfs when this container's /dev doesn't have it",
                    coral["ready"] and coral["device_nodes"] == ["apex_0"], coral)

        with tempfile.TemporaryDirectory() as t:
            _tree(Path(t), usb=[("2-1", "1a6e", "089a", "480"), ("1-4", "046d", "c52b", "12")])
            p = _probe(Path(t))
            c.check("a USB Coral is found, before or after its firmware loads, and other USB devices aren't",
                    len(p["devices"]) == 1 and p["devices"][0]["bus"] == "usb" and p["recommended"] == "coral", p)
            c.check("on a USB 2 port it says a USB 3 port is faster", "USB 3" in p["devices"][0]["note"])

        with tempfile.TemporaryDirectory() as t:
            _tree(Path(t), cpuinfo="Features\t: fp asimd\n")
            p = _probe(Path(t))
            c.check("with nothing fitted it is the CPU, ARM included",
                    p["devices"] == [] and p["recommended"] == "cpu" and p["cpu"]["neon"], p)

        with tempfile.TemporaryDirectory() as t:
            p = _probe(Path(t) / "missing")
            c.check("no sysfs at all still answers", p["recommended"] == "cpu" and p["devices"] == [])
    finally:
        os.environ["ZMM_DATA_DIR"] = data_dir
        for k in ("ZMM_SYSFS_ROOT", "ZMM_DEV_ROOT", "ZMM_PROC_ROOT"):
            os.environ.pop(k, None)
        import manager.accelerators
        importlib.reload(manager.accelerators)
    return c
