"""
Every writer of config/secrets.yaml keeps the other integrations' secrets.

Each used to rebuild the file from an empty dict when it failed to parse, so
one bad edit to secrets.yaml and the next save of any credential wiped every
other one. They now share modules/secrets_file.py, which refuses instead.
"""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path

from harness import Checker

import yaml

from modules import blueair_controller, cameras, go2rtc, notify_channels, secrets_file
from modules.fuel.providers import uk_fuel_finder
from modules.media.players.airplay import SecretsCredentialStore

OTHERS = {"fuel_finder": {"client_id": "keep-me", "client_secret": "s"}}
BROKEN = "fuel_finder: [unclosed\n"


def _writers(file: str):
    """(name, section it owns, save) — each save writes through the module's real path."""
    store = SecretsCredentialStore(file)
    store._creds = {"receiver-1": "pairing"}
    return [
        ("blueair", "blueair", lambda: blueair_controller.save_credentials("u", "p")),
        ("cameras", "cameras", lambda: cameras._write_credentials({"cam1": {"username": "u", "password": "p"}})),
        ("go2rtc", "go2rtc", lambda: go2rtc.save_settings({"url": "http://127.0.0.1:1984"})),
        ("notify channels", "notify_channels", lambda: notify_channels._write_hub({"ntfy_server": "https://ntfy.sh"})),
        ("fuel finder", "fuel_finder", lambda: uk_fuel_finder.save_credentials("id", "secret")),
        ("airplay", "airplay", store._write),
    ]


def run() -> Checker:
    c = Checker("secrets_writers")
    modules = (blueair_controller, cameras, go2rtc, notify_channels, uk_fuel_finder)
    saved = [m.SECRETS_FILE for m in modules]
    with tempfile.TemporaryDirectory() as tmp:
        file = str(Path(tmp) / "secrets.yaml")
        for m in modules:
            m.SECRETS_FILE = file
        try:
            for name, section, save in _writers(file):
                c.section(name)
                others = {} if section == "fuel_finder" else OTHERS
                Path(file).write_text(yaml.safe_dump({**others, "airplay": {"note": "kept"}}
                                                     if section == "airplay" else others))
                save()
                data = yaml.safe_load(Path(file).read_text())
                c.check("a save keeps every other integration's section",
                        all(data.get(k) == v for k, v in others.items()), data)
                c.check("and writes its own", bool(data.get(section)), data)
                if section == "airplay":
                    c.check("merging into the section, not replacing it",
                            data["airplay"].get("note") == "kept"
                            and data["airplay"]["credentials"] == {"receiver-1": "pairing"}, data)
                c.check("the file is readable by its owner only",
                        stat.S_IMODE(Path(file).stat().st_mode) == 0o600)

                Path(file).write_text(BROKEN)
                try:
                    save()
                    refused = False
                except secrets_file.SecretsUnreadable:
                    refused = True
                c.check("a secrets.yaml that does not parse is refused, not wiped",
                        refused and Path(file).read_text() == BROKEN, Path(file).read_text())
        finally:
            for m, f in zip(modules, saved):
                m.SECRETS_FILE = f
    return c
