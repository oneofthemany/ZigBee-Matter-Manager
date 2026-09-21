"""
Path → scope table. Deny by default: an unmapped `/api/` path needs `admin`.

Standard library only — the coverage test imports this on a box with none of
the app's dependencies (AGENTS.md §The dev box). Model: auth.md §Scopes.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

#: Any principal. These routes resolve "me" and scope their own results, so
#: a scope gate would lock users out of their own data.
AUTHENTICATED = "@authenticated"

#: Scope demanded by an `/api/` path that matches no prefix below.
UNMAPPED_SCOPE = "admin"

#: Methods that only read. Everything else is treated as a write.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Prefixes where a write means running code the hub will execute, so `admin`
#: alone is not enough — a stolen session cookie carries no second factor.
#: Reads are exempt: browsing a file is not executing one.
STEP_UP_PREFIXES: Tuple[str, ...] = (
    "/api/editor",          # writes Python the app then runs, test-deploy included
    "/api/backup/restore",  # restores over config, auth and the device DB
)

# Prefix → {METHOD: scope}; "*" is the method fallback. Longest prefix wins,
# so "/api/device_overrides" resolves ahead of "/api/device".
PATH_SCOPES: List[Tuple[str, Dict[str, str]]] = [
    # Self-service. Finer gates stay as per-route require_scope dependencies.
    ("/api/auth",              {"*": AUTHENTICATED}),
    ("/api/messages",          {"*": AUTHENTICATED}),
    ("/api/push",              {"*": AUTHENTICATED}),
    ("/api/wiki",              {"*": AUTHENTICATED}),
    ("/api/therapy",           {"*": AUTHENTICATED}),
    # Anonymous in practice (ANONYMOUS_PATHS); mapped so coverage sees intent.
    ("/api/csp",               {"*": AUTHENTICATED}),

    # Code execution and system state.
    ("/api/editor",            {"*": "admin"}),
    ("/api/setup",             {"*": "admin"}),
    ("/api/upgrade",           {"GET": "system:read", "*": "admin"}),
    ("/api/backup",            {"GET": "system:read", "*": "system:write"}),
    ("/api/config",            {"GET": "system:read", "*": "system:write"}),
    ("/api/system",            {"GET": "system:read", "*": "system:write"}),
    ("/api/debug",             {"GET": "system:read", "*": "system:write"}),
    ("/api/resilience",        {"GET": "system:read", "*": "system:write"}),
    ("/api/performance",       {"GET": "system:read", "*": "system:write"}),
    ("/api/error_stats",       {"GET": "system:read", "*": "system:write"}),
    ("/api/routes",            {"GET": "system:read", "*": "admin"}),
    ("/api/ha",                {"GET": "system:read", "*": "system:write"}),
    ("/api/signals",           {"GET": "system:read", "*": "system:write"}),
    ("/api/remote-access",     {"GET": "system:read", "*": "admin"}),

    # Network plumbing: the mesh, the broker, the border router.
    ("/api/network",           {"GET": "system:read", "*": "system:write"}),
    ("/api/mqtt",              {"GET": "system:read", "*": "system:write"}),
    ("/api/mqtt_explorer",     {"GET": "system:read", "*": "system:write"}),
    ("/api/otbr",              {"GET": "system:read", "*": "system:write"}),
    ("/api/multipan",          {"GET": "system:read", "*": "system:write"}),
    ("/api/adblock",           {"GET": "system:read", "*": "system:write"}),
    ("/api/ban",               {"*": "system:write"}),
    ("/api/banned",            {"GET": "system:read", "*": "system:write"}),
    ("/api/unban",             {"*": "system:write"}),
    ("/api/alerts",            {"GET": "system:read", "*": "system:write"}),

    # Devices and the Zigbee mesh.
    ("/api/device",            {"GET": "device:read", "*": "device:write"}),
    ("/api/devices",           {"GET": "device:read", "*": "device:write"}),
    ("/api/device_overrides",  {"GET": "device:read", "*": "device:write"}),
    ("/api/profiles",          {"GET": "device:read", "*": "device:write"}),
    ("/api/zigbee",            {"GET": "device:read", "*": "device:write"}),
    ("/api/ota",               {"GET": "device:read", "*": "device:write"}),
    ("/api/join_history",      {"GET": "device:read", "*": "device:write"}),
    ("/api/permit_join",       {"GET": "device:read", "*": "device:write"}),
    ("/api/touchlink",         {"*": "device:write"}),
    ("/api/blueair",           {"GET": "device:read", "*": "device:write"}),
    ("/api/tabs",              {"GET": "device:read", "*": "device:write"}),
    ("/api/chambers",          {"GET": "device:read", "*": "device:write"}),
    ("/api/frames",            {"GET": "device:read", "*": "device:write"}),

    ("/api/groups",            {"GET": "group:read", "*": "group:write"}),
    ("/api/matter",            {"GET": "matter:read", "*": "matter:write"}),

    # Automation. Registered in modules/*_api.py, not routes/.
    ("/api/workers",           {"GET": "automation:read", "*": "automation:write"}),
    ("/api/rotary-bindings",   {"GET": "automation:read", "*": "automation:write"}),
    ("/api/automations",       {"GET": "automation:read", "*": "automation:write"}),
    ("/api/swarm",             {"GET": "automation:read", "*": "automation:write"}),

    # Writes automations, so it sits with them. The provider endpoints
    # install software on the host, which is admin work regardless.
    ("/api/ai",                {"GET": "automation:read", "*": "automation:write"}),
    ("/api/ai/config",         {"GET": "system:read", "*": "admin"}),
    ("/api/ai/host",           {"GET": "system:read", "*": "admin"}),
    ("/api/ai/ollama",         {"GET": "system:read", "*": "admin"}),
    ("/api/ai/sglang",         {"GET": "system:read", "*": "admin"}),

    # /db/prune destroys history, so it is not a system:write matter.
    ("/api/telemetry",         {"GET": "system:read", "*": "system:write"}),
    ("/api/telemetry/db",      {"GET": "system:read", "*": "admin"}),

    # Zigbee zone calibration: router aggressiveness, keyed by ieee.
    ("/api/zones",             {"GET": "device:read", "*": "device:write"}),

    # Climate.
    ("/api/heating",           {"GET": "heating:read", "*": "heating:write"}),
    ("/api/ac",                {"GET": "heating:read", "*": "heating:write"}),

    # Media.
    ("/api/media",             {"GET": "media:read", "*": "media:write"}),
    ("/api/tts",               {"GET": "media:read", "*": "media:write"}),

    # Energy and tariffs.
    ("/api/octopus",           {"GET": "energy:read", "*": "energy:write"}),

    # Separate from device:* on purpose — see KNOWN_SCOPES.
    ("/api/security",          {"GET": "security:read", "*": "security:write"}),

    ("/api/presence",          {"GET": "presence:read", "*": "presence:write"}),
    # Routes here check presence:{read,write}:<user_id> themselves, which a
    # prefix cannot express. They are the gate; one added without its own
    # check is open to any principal.
    ("/api/presence/users",    {"*": AUTHENTICATED}),
    ("/api/places",            {"GET": "presence:read", "*": "admin"}),
    ("/api/journeys",          {"GET": "presence:read", "*": "admin"}),

    # Ambient read-only data.
    ("/api/sun",               {"GET": "system:read", "*": "system:write"}),
    ("/api/weather",           {"GET": "system:read", "*": "system:write"}),
    ("/api/fuel",              {"GET": "system:read", "*": "admin"}),
    ("/api/map",               {"GET": "system:read", "*": "admin"}),
    ("/api/geocode",           {"GET": "system:read", "*": "admin"}),
]

# Longest prefix first, resolved once at import.
_SORTED: List[Tuple[str, Dict[str, str]]] = sorted(
    PATH_SCOPES, key=lambda kv: len(kv[0]), reverse=True
)


def needs_step_up(path: str, method: str) -> bool:
    """True if this request needs a recently re-verified second factor."""
    if method.upper() in READ_METHODS:
        return False
    return any(path == p or path.startswith(p + "/") or path.startswith(p)
               for p in STEP_UP_PREFIXES)


def scope_for_path(path: str, method: str) -> Optional[str]:
    """Scope `method path` requires; None if it is not an API path.

    An `/api/` path matching no prefix returns UNMAPPED_SCOPE, never None.
    """
    if not path.startswith("/api/") and path != "/api":
        return None
    method = method.upper()
    for prefix, methods in _SORTED:
        if path == prefix or path.startswith(prefix + "/"):
            if method in methods:
                return methods[method]
            if method in READ_METHODS and "GET" in methods:
                return methods["GET"]
            return methods.get("*", UNMAPPED_SCOPE)
    return UNMAPPED_SCOPE
