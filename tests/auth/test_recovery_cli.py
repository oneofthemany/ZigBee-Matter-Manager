"""
auth_recover.py gets you back in.

Break-glass that has rotted is worse than none, so each command runs against
a throwaway store and the result is checked in the store, not in the output.

Needs the app's deps, so run_all skips it on a bare host.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from harness import Checker

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "auth_recover.py"


def _seed(store: Path) -> None:
    from modules.auth import AuthManager, DEFAULT_GROUPS, Group
    a = AuthManager(config_path=store)
    a.load()
    for name, scopes in DEFAULT_GROUPS.items():
        a.groups[name] = Group(name=name, scopes=list(scopes))
    asyncio.run(a.create_user("sean", "correct-horse", groups=["admins"]))
    asyncio.run(a.create_user("guest", "correct-horse", groups=["viewers"]))


def _run(store: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), "--store", str(store), *args],
        capture_output=True, text=True, cwd=str(REPO))


def _load(store: Path):
    from modules.auth import AuthManager
    a = AuthManager(config_path=store)
    a.load()
    return a


def _enrol_mfa(store: Path, username: str) -> None:
    from modules.auth_mfa import _b32_decode, _hotp
    from modules.auth_secure import SecureAuthManager
    a = _load(store)
    sec = SecureAuthManager(a)
    secret, _ = asyncio.run(sec.begin_enrolment(username))
    asyncio.run(sec.finish_enrolment(
        username, _hotp(_b32_decode(secret), int(time.time()) // 30)))


def run() -> Checker:
    c = Checker("recovery_cli")

    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "auth.yaml"
        _seed(store)

        c.section("it reports the state you need to decide")
        r = _run(store, "list")
        c.check("list succeeds", r.returncode == 0, r.stderr[-200:])
        c.check("it names every user",
                "sean" in r.stdout and "guest" in r.stdout, r.stdout)
        c.check("it marks who is admin", "yes" in r.stdout, r.stdout)

        c.section("a forgotten password is recoverable")
        r = _run(store, "reset-password", "sean")
        pw = next((ln.split(":", 1)[1].strip() for ln in r.stdout.splitlines()
                   if ln.strip().startswith("password:")), "")
        c.check("reset succeeds", r.returncode == 0, r.stderr[-200:])
        c.check("the new password logs in", _load(store).verify_password("sean", pw), pw)
        c.check("the old one does not",
                not _load(store).verify_password("sean", "correct-horse"))

        c.section("a lost TOTP device is recoverable")
        _enrol_mfa(store, "sean")
        from modules.auth_secure import SecureAuthManager
        c.check("MFA is on before",
                SecureAuthManager(_load(store)).mfa_status("sean")["enabled"])
        r = _run(store, "disable-mfa", "sean")
        c.check("disable-mfa succeeds", r.returncode == 0, r.stderr[-200:])
        c.check("MFA is off after",
                not SecureAuthManager(_load(store)).mfa_status("sean")["enabled"])

        c.section("a hub with no usable admin is recoverable")
        r = _run(store, "make-admin", "guest")
        c.check("make-admin succeeds", r.returncode == 0, r.stderr[-200:])
        c.check("guest is now an admin",
                "admins" in _load(store).users["guest"].groups)
        r = _run(store, "create-admin", "rescue")
        pw = next((ln.split(":", 1)[1].strip() for ln in r.stdout.splitlines()
                   if ln.strip().startswith("password:")), "")
        c.check("create-admin succeeds", r.returncode == 0, r.stderr[-200:])
        c.check("the rescue account logs in",
                _load(store).verify_password("rescue", pw), pw)
        c.check("and holds admin",
                "admin" in _load(store).resolve_user_scopes("rescue"))

        c.section("it refuses rather than tracebacks")
        for args in (("reset-password", "nobody"), ("disable-mfa", "nobody"),
                     ("make-admin", "nobody"), ("create-admin", "sean")):
            r = _run(store, *args)
            c.check(f"{args[0]} {args[1]} exits non-zero", r.returncode != 0)
            c.check(f"{args[0]} {args[1]} prints no traceback",
                    "Traceback" not in r.stderr, r.stderr[-160:])

        r = _run(Path(td) / "absent.yaml", "list")
        c.check("a missing store is a message, not a crash",
                r.returncode != 0 and "Traceback" not in r.stderr, r.stderr[-160:])

    return c


if __name__ == "__main__":
    run()
