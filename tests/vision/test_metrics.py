"""The detector's time series (vision/metrics.py): sampling, the file, and
the buckets the System page charts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from harness import Checker

from vision import metrics as M


def run() -> Checker:
    c = Checker("metrics")
    with tempfile.TemporaryDirectory() as tmp:
        now = {"t": 1_800_000_000.0}
        det = NS(backend="coral", count=0, ms=0.0, ready=True)
        hub = NS(detector=det, workers={"a": NS(online=True), "b": NS(online=False)})
        chip = {"temp": 61.2, "throttle": 0}
        path = Path(tmp) / "metrics.jsonl"
        s = M.Sampler(hub, path, clock=lambda: now["t"], chip_fn=lambda: dict(chip))

        c.section("sampling")
        det.count, det.ms = 40, 12.34
        row = s.sample()
        c.check("a sample has the backend, looks since the last one, time per look, cameras online and the chip",
                row == {"t": 1_800_000_000, "backend": "coral", "n": 40, "ms": 12.3, "cams": 1, "temp": 61.2, "throttle": 0}, row)
        det.count = 55
        c.check("…counting only what is new", s.sample()["n"] == 15)
        det.count = 5
        c.check("a reloaded model counting from zero again isn't a negative rate", s.sample()["n"] == 5)
        det.count = 0
        c.check("no looks yet: no time per look", M.Sampler(hub, path, chip_fn=dict).sample()["ms"] is None)
        c.check("on the CPU there is no chip to read", "temp" not in M.Sampler(hub, path, chip_fn=dict).sample())

        c.section("the series")
        start = now["t"]
        for i in range(20):                               # ten minutes, one sample per 30 s
            now["t"] = start + i * 30
            chip["temp"] = 60 + i
            chip["throttle"] = 1 if i == 15 else 0
            s.write({"t": now["t"], "backend": "coral", "n": 30, "ms": 10.0, "cams": 2, **chip})
        rows = M.read(path, 1, 5, now=now["t"])
        c.check("buckets of the width asked for", len(rows) == 2 and rows[1]["ts"] - rows[0]["ts"] == 300, rows)
        c.check("temperature averaged, throttling at its worst, looks as a rate a minute",
                rows[0]["temp"] == 64.5 and rows[0]["throttle"] == 0 and rows[1]["throttle"] == 1
                and rows[0]["looks_per_min"] == 60.0, rows)
        c.check("older than asked for is left out", M.read(path, 1, 5, now=start + 7200) == [])
        with open(path, "a") as f:
            f.write("{broken\n")
        c.check("a damaged line is skipped, not fatal", len(M.read(path, 1, 5, now=now["t"])) == 2)
        c.check("no file yet: an empty series", M.read(Path(tmp) / "none.jsonl", 1, 1) == [])

        c.section("size")
        now["t"] = start + M.KEEP_S + 300
        s.compact()
        left = [json.loads(ln)["t"] for ln in path.read_text().splitlines()]
        c.check("compacting drops what is past the week kept", left and min(left) >= now["t"] - M.KEEP_S, left[:3])
    return c


if __name__ == "__main__":
    import sys
    r = run()
    print(f"\n{r.passed} passed, {len(r.failures)} failed")
    sys.exit(1 if r.failures else 0)
