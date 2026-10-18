"""The Octopus API key lives in config/secrets.yaml, never the tracked config.yaml."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from harness import Checker

from modules import octopus, secrets_file

CONFIG = """\
# Comments in config.yaml must survive the move.
octopus:
  # Octopus Energy (Energy tab).
  enabled: true
  api_key: sk_live_legacy     # left here by an older version
  account_number: A-1234ABCD
fuel:
  finder:
    enabled: true
"""


def _route(c: Checker, tmp: Path, secrets: Path) -> None:
    """Settings → APIs → Octopus, through the real config route."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        print("    skipped: FastAPI not installed (run under the lockfile venv)")
        return
    from routes.config_routes import register_config_routes

    c.section("the settings route")
    work = tmp / "work"
    (work / "config").mkdir(parents=True)
    config = work / "config" / "config.yaml"
    config.write_text(CONFIG.replace("  api_key: sk_live_legacy     # left here by an older version\n", ""))
    if secrets.exists():
        secrets.unlink()
    here = os.getcwd()
    os.chdir(work)
    try:
        app = FastAPI()
        register_config_routes(app, lambda: None)
        client = TestClient(app)
        shown = client.get("/api/config/structured").json()["config"]["octopus"]
        c.check("with no key stored, the form says none is set",
                shown["api_key"] == "" and shown["api_key_set"] is False, shown)
        saved = client.post("/api/config/structured",
                            json={"config": {"octopus": {"api_key": "sk_live_route"}}}).json()
        c.check("saving a key from Settings succeeds", saved.get("success") is True, saved)
        c.check("and writes it to secrets.yaml",
                secrets_file.read_section("octopus").get("api_key") == "sk_live_route")
        c.check("and not to config.yaml", "sk_live_route" not in config.read_text(), config.read_text())
        shown = client.get("/api/config/structured").json()["config"]["octopus"]
        c.check("the form then says a key is set, without echoing it",
                shown["api_key"] == "" and shown["api_key_set"] is True, shown)

        config.write_text(CONFIG)
        secrets.unlink()
        client.post("/api/config/structured", json={"config": {"octopus": {"enabled": True}}})
        c.check("a save with a legacy key still in config.yaml moves it out",
                secrets_file.read_section("octopus").get("api_key") == "sk_live_legacy"
                and "sk_live_legacy" not in config.read_text(), config.read_text())
    finally:
        os.chdir(here)


def run() -> Checker:
    c = Checker("octopus_secrets")
    saved_env = {k: os.environ.get(k) for k in ("ZMM_SECRETS_FILE", octopus.API_KEY_ENV)}
    tmp = Path(tempfile.mkdtemp(prefix="zmm_octopus_"))
    secrets = tmp / "secrets.yaml"
    os.environ["ZMM_SECRETS_FILE"] = str(secrets)
    os.environ.pop(octopus.API_KEY_ENV, None)
    try:
        c.section("secrets file")
        secrets.write_text("blueair:\n  username: u\n  password: p\n")
        octopus.save_api_key("sk_live_new")
        c.check("saving the key keeps every other integration's secrets",
                secrets_file.read_section("blueair") == {"username": "u", "password": "p"},
                secrets.read_text())
        c.check("the key is stored under octopus.api_key",
                secrets_file.read_section("octopus") == {"api_key": "sk_live_new"},
                secrets.read_text())
        c.check("the file is readable by its owner only",
                stat.S_IMODE(secrets.stat().st_mode) == 0o600, oct(secrets.stat().st_mode))

        broken = "blueair: [unclosed\n"
        secrets.write_text(broken)
        try:
            octopus.save_api_key("sk_live_other")
            refused = False
        except secrets_file.SecretsUnreadable:
            refused = True
        c.check("a file that does not parse is refused, not rewritten from nothing",
                refused and secrets.read_text() == broken, secrets.read_text())

        c.section("which key wins")
        secrets.write_text("octopus:\n  api_key: sk_live_file\n")
        c.check("secrets.yaml wins over a key still in config.yaml",
                octopus.resolve_api_key({"api_key": "sk_live_config"}) == ("sk_live_file", "secrets_file"))
        os.environ[octopus.API_KEY_ENV] = "sk_live_env"
        c.check("the environment wins over secrets.yaml",
                octopus.resolve_api_key({}) == ("sk_live_env", "environment"))
        os.environ.pop(octopus.API_KEY_ENV)
        secrets.unlink()
        c.check("with nothing else, a legacy config key still works until moved",
                octopus.resolve_api_key({"api_key": "sk_live_config"}) == ("sk_live_config", "config"))

        c.section("moving a key out of config.yaml")
        config = tmp / "config.yaml"
        config.write_text(CONFIG)
        block = {"enabled": True, "api_key": "sk_live_legacy", "account_number": "A-1234ABCD"}
        octopus.migrate_api_key(block, str(config))
        text = config.read_text()
        c.check("the key is moved into secrets.yaml",
                secrets_file.read_section("octopus").get("api_key") == "sk_live_legacy",
                secrets.read_text() if secrets.exists() else None)
        c.check("config.yaml no longer holds it", "api_key" not in text, text)
        c.check("every other line of config.yaml, comments included, is unchanged",
                text == CONFIG.replace("  api_key: sk_live_legacy     # left here by an older version\n", ""),
                text)
        c.check("the loaded config forgets it too", "api_key" not in block, block)

        config.write_text(CONFIG)
        secrets.write_text("octopus:\n  api_key: sk_live_kept\n")
        octopus.migrate_api_key({"api_key": "sk_live_legacy"}, str(config))
        c.check("a key already in secrets.yaml is kept, and the config copy still removed",
                secrets_file.read_section("octopus")["api_key"] == "sk_live_kept"
                and "api_key" not in config.read_text())

        config.write_text(CONFIG)
        secrets.write_text(broken)
        block = {"api_key": "sk_live_legacy"}
        octopus.migrate_api_key(block, str(config))
        c.check("if secrets.yaml cannot be written, the key stays in config.yaml",
                "api_key: sk_live_legacy" in config.read_text() and block.get("api_key"))

        _route(c, tmp, secrets)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return c
