"""
The Energy tab finds its sockets itself, by name, whether or not they count kWh.

The Aurora double socket has no metering cluster, so its usage is integrated
from the power it reports; a socket with a counter keeps the counter's figure.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import duckdb

from harness import Checker

import modules.telemetry_db as telemetry_db
from routes.octopus_routes import _names_and_live_power

MEDIA = "00:15:8d:00:02:56:f8:bf"
HALLWAY = "54:ef:44:10:01:5a:14:eb"


def run() -> Checker:
    c = Checker("usage_sources")

    c.section("devices are named and their live power read from the service")
    zs = NS(friendly_names={MEDIA: "Socket - Media"},
            devices={MEDIA: NS(state={"power": 69.0}),
                     HALLWAY: NS(state={"power": 11.0}),
                     "aa:bb": NS(state={"temperature": 20})})
    names, live = _names_and_live_power(zs)
    c.check("a named device shows its name", names.get(MEDIA) == "Socket - Media", names)
    c.check("an unnamed one falls back to its address", names.get(HALLWAY) == HALLWAY, names)
    c.check("every device with power is a source", live == {MEDIA: 69.0, HALLWAY: 11.0}, live)
    c.check("a service with no devices yields nothing", _names_and_live_power(NS()) == ({}, {}))

    c.section("a socket with no energy counter gets kWh from its power")
    tmp = tempfile.mkdtemp(prefix="zmm_usage_")
    saved = telemetry_db._db
    db = duckdb.connect(str(Path(tmp) / "t.duckdb"))
    db.execute("""CREATE TABLE device_states (ts TIMESTAMP NOT NULL DEFAULT now(),
                  ieee VARCHAR NOT NULL, attribute VARCHAR NOT NULL,
                  value VARCHAR, numeric_val DOUBLE)""")

    def put(ieee, attr, minutes_ago, val):
        db.execute("INSERT INTO device_states (ts, ieee, attribute, value, numeric_val) VALUES "
                   f"(now()::TIMESTAMP - INTERVAL {minutes_ago} MINUTE, ?, ?, ?, ?)",
                   [ieee, attr, str(val), val])

    # 120 W for an hour in 5-minute readings, then off; one reading left to stand.
    for m in range(180, 120, -5):
        put(MEDIA, "power", m, 120.0)
    put(MEDIA, "power", 120, 0.0)
    put(MEDIA, "power", 90, 600.0)
    put(MEDIA, "power", 30, 0.0)
    put(HALLWAY, "power", 100, 500.0)
    put(HALLWAY, "energy", 100, 16.0)
    put(HALLWAY, "energy", 10, 16.5)
    telemetry_db._db = db
    try:
        rows = telemetry_db.query_plug_energy_from_power_by_day(days=7)
        kwh = {}
        for r in rows:
            kwh[r["ieee"]] = kwh.get(r["ieee"], 0.0) + r["kwh"]
        # 120 W x 1 h = 0.12 kWh; 600 W held for the 15-minute cap = 0.15 kWh.
        c.check("readings integrate to energy, a silent hour held only 15 minutes",
                abs(kwh.get(MEDIA, 0) - 0.27) < 0.005, kwh)
        c.check("a socket with a counter is left to its counter", HALLWAY not in kwh, kwh)
        counted = {r["ieee"] for r in telemetry_db.query_plug_energy_by_day(days=7)}
        c.check("and still appears from it", counted == {HALLWAY}, counted)
    finally:
        telemetry_db._db = saved
        db.close()
    return c
