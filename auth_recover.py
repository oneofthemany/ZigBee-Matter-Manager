#!/usr/bin/env python3
"""
Break-glass account recovery. Runs inside the container, never over the network.

    podman exec -it zigee-matter-manager python3 /app/auth_recover.py list
    podman exec -it zigee-matter-manager python3 /app/auth_recover.py reset-password sean
    podman exec -it zigee-matter-manager python3 /app/auth_recover.py disable-mfa sean
    podman exec -it zigee-matter-manager python3 /app/auth_recover.py make-admin sean
    podman exec -it zigee-matter-manager python3 /app/auth_recover.py create-admin rescue

The realistic lockout is a lost TOTP device with the recovery codes gone, not
a scope mistake — an admin satisfies every scope check by construction
(auth.md §How a scope is enforced).

Requires write access to data/auth.yaml, which is already root-equivalent, so
this grants no privilege that the shell running it lacks. Every action prints
and logs at WARNING, so it shows up in the app log rather than happening
quietly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.auth import AuthManager, CONFIG_PATH, DEFAULT_GROUPS, Group  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("auth_recover")

ADMIN_GROUP = "admins"


def _load(path: Path) -> AuthManager:
    if not path.exists():
        sys.exit(f"No auth store at {path}. Is the container path right?")
    auth = AuthManager(config_path=path)
    auth.load()
    return auth


def _new_password() -> str:
    return secrets.token_urlsafe(12)


def _mfa_on(auth: AuthManager, username: str) -> bool:
    # Display only: an unreadable MFA store must not stop `list` working,
    # which is the one command you run when already locked out.
    try:
        from modules.auth_secure import SecureAuthManager
        return bool(SecureAuthManager(auth).mfa_status(username).get("enabled"))
    except (ImportError, KeyError, AttributeError, OSError):
        return False


def cmd_list(auth: AuthManager, _args) -> None:
    print(f"{'user':20} {'groups':24} {'admin':6} {'mfa':5} disabled")
    for name, u in sorted(auth.users.items()):
        is_admin = ADMIN_GROUP in u.groups or "admin" in u.extra_scopes
        print(f"{name:20} {','.join(u.groups) or '-':24} "
              f"{'yes' if is_admin else '-':6} "
              f"{'on' if _mfa_on(auth, name) else '-':5} "
              f"{'YES' if u.disabled else '-'}")


def _require(auth: AuthManager, username: str) -> None:
    if username not in auth.users:
        sys.exit(f"No such user: {username}. Run 'list' to see them.")


def cmd_reset_password(auth: AuthManager, args) -> None:
    _require(auth, args.username)
    pw = args.password or _new_password()
    asyncio.run(auth.update_user(args.username, password=pw, disabled=False))
    logger.warning("[recover] password reset for '%s' via auth_recover",
                   args.username)
    print(f"\n  user:     {args.username}\n  password: {pw}\n")
    print("Shown once. Existing sessions for this user are invalidated.")


def cmd_disable_mfa(auth: AuthManager, args) -> None:
    _require(auth, args.username)
    from modules.auth_secure import SecureAuthManager
    asyncio.run(SecureAuthManager(auth).disable_mfa(args.username))
    logger.warning("[recover] MFA disabled for '%s' via auth_recover",
                   args.username)
    print(f"MFA disabled for '{args.username}'. Re-enrol from Settings → Users.")


def cmd_make_admin(auth: AuthManager, args) -> None:
    _require(auth, args.username)
    user = auth.users[args.username]
    if ADMIN_GROUP not in auth.groups:
        sys.exit(f"No '{ADMIN_GROUP}' group in the store.")
    groups = sorted(set(user.groups) | {ADMIN_GROUP})
    asyncio.run(auth.update_user(args.username, groups=groups, disabled=False))
    logger.warning("[recover] '%s' added to %s via auth_recover",
                   args.username, ADMIN_GROUP)
    print(f"'{args.username}' is now in {ADMIN_GROUP}.")


def cmd_create_admin(auth: AuthManager, args) -> None:
    if args.username in auth.users:
        sys.exit(f"'{args.username}' already exists — use reset-password.")
    if ADMIN_GROUP not in auth.groups:
        auth.groups[ADMIN_GROUP] = Group(
            name=ADMIN_GROUP, scopes=list(DEFAULT_GROUPS[ADMIN_GROUP]))
    pw = args.password or _new_password()
    asyncio.run(auth.create_user(
        args.username, pw, groups=[ADMIN_GROUP],
        description="Created via auth_recover"))
    logger.warning("[recover] admin '%s' created via auth_recover", args.username)
    print(f"\n  user:     {args.username}\n  password: {pw}\n")
    print("Shown once. Delete this account once you are back in.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--store", default=str(CONFIG_PATH),
                    help=f"auth store path (default {CONFIG_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show users, admin status and MFA state")

    for name, fn, needs_pw in (
        ("reset-password", cmd_reset_password, True),
        ("disable-mfa", cmd_disable_mfa, False),
        ("make-admin", cmd_make_admin, False),
        ("create-admin", cmd_create_admin, True),
    ):
        p = sub.add_parser(name)
        p.add_argument("username")
        if needs_pw:
            p.add_argument("--password", help="default: generated and printed")
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    auth = _load(Path(args.store))
    (getattr(args, "fn", None) or cmd_list)(auth, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
