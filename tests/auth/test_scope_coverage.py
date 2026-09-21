"""
Every /api/ route resolves to a scope, and the scopes it resolves to are real.

The point of the table in modules/auth_scopes.py is that adding a route cannot
quietly add an unguarded route. These checks are what make that true: a new
prefix nobody mapped lands on `admin` and shows up here as a deliberate
decision to make, not as a hole.
"""

from __future__ import annotations

from harness import Checker, frontend_api_paths, registered_routes

from modules.auth import KNOWN_SCOPES, DEFAULT_GROUPS, scope_matches
from modules.auth_scopes import (
    AUTHENTICATED,
    PATH_SCOPES,
    UNMAPPED_SCOPE,
    scope_for_path,
)

#: Routes that resolve to `admin` only because no prefix matches them. Empty
#: on purpose: an entry here is a route somebody has not classified yet.
EXPECTED_UNMAPPED: set = set()


def run() -> Checker:
    c = Checker("scope_coverage")
    routes = registered_routes()

    c.section("route table is intact")
    # Pinned so a change in registration style cannot silently shrink the set
    # the rest of this file checks. The first version of this scan looked only
    # in routes/ and missed the 100 routes the modules/*_api.py files register,
    # which is exactly the failure these floors exist to catch.
    c.check("source scan finds the whole route surface",
            len(routes) >= 590, len(routes))
    api = [(m, p, f) for m, p, f in routes if p.startswith("/api/")]
    c.check("the API surface is the bulk of it", len(api) >= 578, len(api))
    c.check("the modules/*_api.py routers are included",
            {"ai_api.py", "automation_api.py", "telemetry_api.py",
             "zones_api.py"} <= {f for _, _, f in api},
            sorted({f for _, _, f in api})[:8])

    c.section("every /api/ route resolves to a scope")
    unmapped = []
    for method, path, src in api:
        if method == "WEBSOCKET":
            continue
        matched = any(path == pre or path.startswith(pre + "/")
                      for pre, _ in PATH_SCOPES)
        if not matched:
            unmapped.append(f"{method} {path} ({src})")
    c.check("no /api/ route falls through to the unmapped default",
            set(unmapped) <= EXPECTED_UNMAPPED, sorted(unmapped)[:12])

    c.section("resolved scopes exist")
    bad = sorted({
        s for m, p, _ in api
        if (s := scope_for_path(p, m)) is not None
        and s != AUTHENTICATED
        and s.split(":")[0] not in {k.split(":")[0] for k in KNOWN_SCOPES}
    })
    c.check("every resolved scope is a known scope", not bad, bad)

    c.section("the dangerous routes need more than a session")
    for method, path, need in [
        ("POST", "/api/editor/save", "admin"),
        ("GET", "/api/editor/read", "admin"),
        ("POST", "/api/upgrade/start", "admin"),
        ("POST", "/api/backup/restore", "system:write"),
        ("POST", "/api/config/save", "system:write"),
        ("POST", "/api/system/restart", "system:write"),
        ("POST", "/api/security/lock/front/unlock", "security:write"),
    ]:
        c.check(f"{method} {path} requires {need}",
                scope_for_path(path, method) == need,
                scope_for_path(path, method))

    c.section("a leaked phone token cannot execute code")
    phone = {"presence:read:sean", "presence:write:sean"}
    for method, path in [("POST", "/api/editor/save"),
                         ("POST", "/api/system/restart"),
                         ("POST", "/api/backup/restore"),
                         ("POST", "/api/config/save"),
                         ("POST", "/api/security/lock/front/unlock")]:
        need = scope_for_path(path, method)
        c.check(f"presence-scoped token is refused {method} {path}",
                not scope_matches(need, phone), need)
    c.check("but it still reaches its own presence route",
            scope_matches("presence:read:sean", phone))

    c.section("a new unmapped route is closed, not open")
    c.check("unknown /api/ path demands admin",
            scope_for_path("/api/brand_new_thing/do", "POST") == UNMAPPED_SCOPE)
    c.check("unknown /api/ GET demands admin too",
            scope_for_path("/api/brand_new_thing/do", "GET") == UNMAPPED_SCOPE)
    c.check("non-API paths are not the middleware's business",
            scope_for_path("/static/app.js", "GET") is None)

    c.section("longest prefix wins")
    c.check("/api/device_overrides does not inherit /api/device",
            scope_for_path("/api/device_overrides/x", "GET") == "device:read")
    c.check("/api/devices resolves on its own entry",
            scope_for_path("/api/devices", "GET") == "device:read")
    c.check("a prefix does not leak across a word boundary",
            scope_for_path("/api/mediaplayer/x", "GET") == UNMAPPED_SCOPE)

    c.section("read and write are actually distinguished")
    for path, read, write in [("/api/heating/zones", "heating:read", "heating:write"),
                              ("/api/media/players", "media:read", "media:write"),
                              ("/api/octopus/rates", "energy:read", "energy:write"),
                              ("/api/groups", "group:read", "group:write")]:
        c.check(f"GET {path} is a read", scope_for_path(path, "GET") == read)
        c.check(f"POST {path} is a write", scope_for_path(path, "POST") == write)
    c.check("HEAD follows GET",
            scope_for_path("/api/heating/zones", "HEAD") == "heating:read")

    c.section("self-service routes stay reachable")
    for path in ["/api/auth/tokens", "/api/auth/mfa/status",
                 "/api/messages/threads", "/api/push/subscribe"]:
        c.check(f"{path} needs only a principal",
                scope_for_path(path, "POST") == AUTHENTICATED)

    c.section("the UI only calls paths the table maps")
    fe = frontend_api_paths()
    c.check("the frontend scan found the real call sites",
            len(fe) >= 350, len(fe))
    fe_unmapped = sorted(
        p for p in fe
        if not any(p == pre or p.startswith(pre + "/") for pre, _ in PATH_SCOPES)
    )
    c.check("no UI call falls through to the unmapped default",
            not fe_unmapped,
            [f"{p} <- {sorted(fe[p])[:2]}" for p in fe_unmapped[:10]])

    c.section("the shipped groups can still use the app")
    for group in ("users", "viewers"):
        granted = set(DEFAULT_GROUPS[group])
        writes = group == "users"
        denied = sorted({
            f"{m} {p}" for m, p, _ in api
            if m != "WEBSOCKET"
            and (writes or m == "GET")
            and (s := scope_for_path(p, m)) not in (None, AUTHENTICATED)
            and not scope_matches(s, granted)
            and not s.startswith("admin")
        })
        # Everything left must be a system:write path — those are admin work
        # by design, not a lockout.
        leaked = [d for d in denied
                  if scope_for_path(d.split(" ", 1)[1],
                                    d.split(" ", 1)[0]) != "system:write"]
        c.check(f"'{group}' is not locked out of ordinary use", not leaked,
                leaked[:12])

    c.check("'viewers' cannot write anything",
            all(not scope_matches(s, set(DEFAULT_GROUPS["viewers"]))
                for s in ("device:write", "heating:write", "media:write",
                          "security:write", "system:write", "admin")))

    return c


if __name__ == "__main__":
    run()
