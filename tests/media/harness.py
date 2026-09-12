"""
Shared scaffolding for the media tests.

Plain scripts, matching tests/fuel and tests/swarm: the project carries no test
framework, and each module exposes `run()` driven by tests/media/run_all.py.

Nothing here is stubbed. modules.media.sources.tidal imports only the standard
library plus the media models, and modules.auth only the standard library plus
yaml, so the real classes are exercised — which is the point, since what these
tests mostly assert is that the Tidal registry reads a real User correctly.
tidalapi is never imported: no test logs in.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

#: Repository root — the route tests read source files out of it.
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Checker:
    """Collects pass/fail lines so a module can report as a group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.passed = 0

    def section(self, title: str) -> None:
        print(f"\n  {title}")

    def check(self, label: str, ok: bool, detail: object = "") -> bool:
        if ok:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failures.append(f"{self.name}: {label}")
            print(f"    FAIL {label}  <- {detail!r}"[:400])
        return bool(ok)


class TempSessions:
    """Point the Tidal module's session paths at a throwaway directory.

    The paths are module constants read at call time, so redirecting them here
    covers the account files, the legacy file and the migration between them
    without any of it touching ./data.
    """

    def __init__(self, module):
        self.module = module
        self._saved = {}
        self._tmp = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = {
            "SESSION_DIR": self.module.SESSION_DIR,
            "LEGACY_SESSION_PATH": self.module.LEGACY_SESSION_PATH,
        }
        self.dir = root / "media" / "tidal"
        self.legacy = root / "media" / "tidal_session.json"
        self.legacy.parent.mkdir(parents=True, exist_ok=True)
        self.module.SESSION_DIR = str(self.dir)
        self.module.LEGACY_SESSION_PATH = str(self.legacy)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(self.module, k, v)
        self._tmp.cleanup()
        return False

    def write_legacy(self, token: str = "legacy-token") -> None:
        self.legacy.write_text('{"access_token": "%s"}' % token)

    def write_account(self, username: str, token: str = "tok") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{username}.json").write_text('{"access_token": "%s"}' % token)

    def names(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.name for p in self.dir.iterdir())


def auth_with(*users) -> None:
    """Register a real AuthManager holding these (username, groups) users.

    Real User objects on purpose: the registry reads `.disabled`, `.groups` and
    `.extra_scopes`, and a stub would keep passing if any of those were renamed.
    """
    from modules.auth import AuthManager, User, set_auth_manager
    mgr = AuthManager()
    for username, groups in users:
        mgr.users[username] = User(username=username, groups=list(groups))
    set_auth_manager(mgr)


def auth_clear() -> None:
    from modules import auth
    auth._manager = None
