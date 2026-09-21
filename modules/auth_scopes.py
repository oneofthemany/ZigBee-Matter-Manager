"""
Path → scope table: the authoritative answer to "what does this request need?"

Deny by default. An `/api/` path that matches no prefix requires `admin`, so a
new route is locked until someone maps it here rather than open until someone
remembers to guard it. `tests/auth/test_scope_coverage.py` fails the build on
any unmapped path.

Standard library only, and deliberately free of any FastAPI import: the
coverage test reads the route table out of the source files and must run on a
box with none of the app's dependencies installed (AGENTS.md §The dev box).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

#: Required scope for routes that any authenticated principal may call.
#: These resolve "me" from the principal and scope their own results —
#: own tokens, own password, own push subscriptions, own message threads —
#: so a scope gate here would lock users out of their own data.
AUTHENTICATED = "@authenticated"

#: Scope demanded by an `/api/` path that matches no prefix below.
UNMAPPED_SCOPE = "admin"

#: Methods that only read. Everything else is treated as a write.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Prefix → {METHOD: scope}. "*" is the fallback for methods not named.
# Longest matching prefix wins, so "/api/device_overrides" resolves ahead of
# "/api/device". Order in this list is irrelevant; length decides.
PATH_SCOPES: List[Tuple[str, Dict[str, str]]] = [
    # Self-service: the route resolves the caller and returns only their own
    # data. Finer gates (admin for user/group management) stay as per-route
    # require_scope dependencies.
    ("/api/auth",              {"*": AUTHENTICATED}),
    ("/api/messages",          {"*": AUTHENTICATED}),
    ("/api/push",              {"*": AUTHENTICATED}),
    ("/api/wiki",              {"*": AUTHENTICATED}),
    ("/api/therapy",           {"*": AUTHENTICATED}),

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

    # Automation. These live in modules/*_api.py rather than routes/, which is
    # how they were missed on the first pass — see tests/auth/harness.py.
    ("/api/workers",           {"GET": "automation:read", "*": "automation:write"}),
    ("/api/rotary-bindings",   {"GET": "automation:read", "*": "automation:write"}),
    ("/api/automations",       {"GET": "automation:read", "*": "automation:write"}),
    ("/api/swarm",             {"GET": "automation:read", "*": "automation:write"}),

    # The AI assistant writes automations, so it sits with them — but the
    # provider endpoints install software on the host and configure a model
    # backend, which is admin work whatever the assistant is used for.
    ("/api/ai",                {"GET": "automation:read", "*": "automation:write"}),
    ("/api/ai/config",         {"GET": "system:read", "*": "admin"}),
    ("/api/ai/host",           {"GET": "system:read", "*": "admin"}),
    ("/api/ai/ollama",         {"GET": "system:read", "*": "admin"}),
    ("/api/ai/sglang",         {"GET": "system:read", "*": "admin"}),

    # Telemetry reads are the whole point of system:read; /db/prune destroys
    # history and is not something a read-only account should reach.
    ("/api/telemetry",         {"GET": "system:read", "*": "system:write"}),
    ("/api/telemetry/db",      {"GET": "system:read", "*": "admin"}),

    # Zigbee zone calibration — router aggressiveness, keyed by ieee.
    ("/api/zones",             {"GET": "device:read", "*": "device:write"}),

    # Climate.
    ("/api/heating",           {"GET": "heating:read", "*": "heating:write"}),
    ("/api/ac",                {"GET": "heating:read", "*": "heating:write"}),

    # Media.
    ("/api/media",             {"GET": "media:read", "*": "media:write"}),
    ("/api/tts",               {"GET": "media:read", "*": "media:write"}),

    # Energy and tariffs.
    ("/api/octopus",           {"GET": "energy:read", "*": "energy:write"}),

    # Locks. Separate from device:* on purpose — see KNOWN_SCOPES.
    ("/api/security",          {"GET": "security:read", "*": "security:write"}),

    # Presence, people and places. Per-user gates
    # (presence:write:<id>) remain as route dependencies.
    ("/api/presence",          {"GET": "presence:read", "*": "presence:write"}),
    # Every route under /api/presence/users resolves the target user and
    # checks presence:{read,write}:<user_id> itself, which a prefix table
    # cannot express — a blanket presence:write here would lock a phone out
    # of its own user. The routes are the gate; a new one added here without
    # its own check would be open to any principal.
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


def scope_for_path(path: str, method: str) -> Optional[str]:
    """Return the scope `method path` requires, or None if it is not an API path.

    `AUTHENTICATED` means "any principal will do". An `/api/` path matching no
    prefix returns UNMAPPED_SCOPE, never None — that is the deny-by-default.
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
