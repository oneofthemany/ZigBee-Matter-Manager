"""
Every Tidal endpoint names its caller.

Static analysis of the route modules rather than live requests: FastAPI is not
installed on a dev box, so the handlers cannot be driven. What can be checked
without it is the thing that actually matters — that no endpoint serving one
user's Tidal data was left resolving "the" account, which is how a housemate's
library would end up on someone else's screen. Step 3 of
docs/plans/tidal-per-user-auth.md.

The manifest route is the deliberate exception: Cast devices fetch it
themselves and cannot carry a session cookie. It is listed here by name so it
stays a decision rather than an oversight, and step 4 gives it a token.
"""

from __future__ import annotations

import ast
from pathlib import Path

from harness import Checker, REPO

#: Fetched by a speaker, not a browser — see modules/auth_middleware.py.
DEVICE_FETCHED = {"/api/media/tidal/manifest/{token}.mpd"}

#: Admin-only and registry-wide rather than per-user: it reports who has linked
#: an account, so it names its caller through require_scope("admin") and has no
#: account of its own to resolve.
ADMIN_SCOPED = {"/api/media/tidal/accounts"}

#: Endpoints that read or write one user's Tidal, beyond the /tidal/ prefix.
USER_SCOPED_EXTRA = {"/api/media/local/playlist", "/api/media/local/track_url",
                     "/api/media/sync/start", "/api/media/sync/groups/config"}


def _routes(path: Path):
    """(http path, FunctionDef) for every @app.<verb>("...") handler."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and dec.args
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("get", "post", "put", "delete")
                    and isinstance(dec.args[0], ast.Constant)):
                out.append((dec.args[0].value, node))
    return out


def _has_principal(fn, dep: str = "require_authenticated") -> bool:
    """Does the handler take a principal through `Depends(<dep>)`?

    `require_scope("admin")` is a call rather than a name, so both shapes are
    accepted; either way FastAPI hands the handler an authenticated Principal.
    """
    for default in list(fn.args.defaults) + list(fn.args.kw_defaults or []):
        if not (isinstance(default, ast.Call)
                and getattr(default.func, "id", "") == "Depends"
                and default.args):
            continue
        inner = default.args[0]
        name = (getattr(inner, "id", "")
                or getattr(getattr(inner, "func", None), "id", ""))
        if name == dep:
            return True
    return False


def _calls(fn, name: str) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == name
               for n in ast.walk(fn))


def _uses_username(fn) -> bool:
    """Does the body actually reach for principal.user.username?"""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Attribute) and n.attr == "username"
                and isinstance(n.value, ast.Attribute) and n.value.attr == "user"):
            return True
    return False


def run() -> Checker:
    c = Checker("tidal-routes")

    media = _routes(REPO / "routes" / "media_routes.py")
    sync = _routes(REPO / "routes" / "cast_sync_routes.py")
    by_path = {p: fn for p, fn in media + sync}

    c.section("every Tidal endpoint names its caller")
    tidal = [(p, fn) for p, fn in media if p.startswith("/api/media/tidal/")]
    c.check("the Tidal endpoints were found", len(tidal) >= 13, len(tidal))
    for path, fn in sorted(tidal):
        if path in DEVICE_FETCHED:
            c.check(f"{path} is deliberately anonymous",
                    not _has_principal(fn))
            continue
        if path in ADMIN_SCOPED:
            c.check(f"{path} is admin-scoped",
                    _has_principal(fn, "require_scope"))
            continue
        c.check(f"{path} takes a principal", _has_principal(fn))

    c.section("and resolves that caller's own account")
    for path, fn in sorted(tidal):
        if path in DEVICE_FETCHED or path in ADMIN_SCOPED:
            continue
        # _acct() is the per-user lookup; _tidal() is the registry, which only
        # the "is the source there at all" checks may use.
        c.check(f"{path} goes through _acct()", _calls(fn, "_acct"))

    c.section("the paths that start playback are scoped to the caller")
    for path in sorted(USER_SCOPED_EXTRA):
        fn = by_path.get(path)
        if not c.check(f"{path} exists", fn is not None):
            continue
        c.check(f"{path} takes a principal", _has_principal(fn))
        # Two equivalent ways to be scoped: resolve the caller's account
        # object, or hand their username to a service that resolves it.
        c.check(f"{path} acts as the caller, not as the default account",
                _calls(fn, "_acct") or _uses_username(fn))

    c.section("the exceptions are exactly the ones declared")
    for path, fn in sorted(tidal):
        if path in ADMIN_SCOPED:
            c.check(f"{path} is not also per-user", not _calls(fn, "_acct"))
        elif path not in DEVICE_FETCHED:
            c.check(f"{path} is not quietly admin-only",
                    not _has_principal(fn, "require_scope"))

    c.section("nothing client-supplied can name an account")
    body = (REPO / "routes" / "cast_sync_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(body)
    fields = [n.target.id for cls in ast.walk(tree)
              if isinstance(cls, ast.ClassDef) and cls.name == "SyncMediaBody"
              for n in cls.body if isinstance(n, ast.AnnAssign)]
    c.check("SyncMediaBody has no owner field", "owner" not in fields, fields)

    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
