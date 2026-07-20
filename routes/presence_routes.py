"""
Presence Users API routes — auth-aware version.

Scope model
-----------
- `admin`                              full control over presence users
- `presence:read`                      list/view presence state for all users
- `presence:write`                     update any user's location
- `presence:write:<user_id>`           update only a specific user's location
                                       (this is what mobile-app tokens get)

The fix endpoint is the hot path called by the companion app every few minutes
(or on geofence transitions). To minimize attack surface, we issue mobile
tokens with ONLY `presence:write:<user_id>` and nothing else. A leaked phone
token can update one person's location and nothing more.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from modules.auth_middleware import (
    Principal, require_scope, require_any_scope, require_authenticated,
)
from modules.auth_secure import require_presence_mfa, user_has_mfa

logger = logging.getLogger("modules.presence_routes")


class UserUpsert(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=32)
    display_name: str = Field(..., min_length=1, max_length=64)
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    radius_m: float = 100.0
    hysteresis_m: float = 30.0
    stale_after_s: float = 1800.0
    min_accuracy_m: float = 250.0
    enabled: bool = True
    owntracks_user: Optional[str] = None
    owntracks_device: Optional[str] = None
    # Login account this presence user belongs to. None = standalone tracker,
    # which cannot satisfy the MFA policy because there is no account to enrol.
    account: Optional[str] = None


class FixReport(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    accuracy: Optional[float] = Field(None, ge=0.0)
    timestamp: Optional[float] = None


class ManualSet(BaseModel):
    presence: str = Field(..., pattern="^(home|away|unknown)$")


def register_presence_routes(app: FastAPI, presence_manager_getter: Callable):

    def _mgr():
        mgr = presence_manager_getter()
        if not mgr:
            raise HTTPException(503, "Presence manager not initialised")
        return mgr

    def _account_for(user_id: str) -> Optional[str]:
        """
        The login account a presence user belongs to, or None if standalone.

        Every policy decision about a presence user resolves through here, so
        the account link is read from the record rather than inferred from the
        id. A missing presence user returns None, which the MFA gate treats as
        "no account" and refuses — the safe reading when we cannot identify who
        a location belongs to.
        """
        dev = _mgr().get_user(user_id)
        return getattr(dev.cfg, "account", None) if dev else None

    def _gate_presence_mfa(user_id: str) -> None:
        """Apply the MFA policy to whichever account owns this presence user."""
        account = _account_for(user_id)
        if not account:
            raise HTTPException(
                403,
                f"Presence user '{user_id}' is not linked to a login account, "
                "so MFA cannot be verified. Link it to an account, or delete it "
                "if it is left over from a deleted user."
            )
        require_presence_mfa(account)

    @app.get("/api/presence/users")
    async def list_users(_=Depends(require_scope("presence:read"))):
        # Annotate rather than filter: an admin needs to see that a user's
        # presence is stalled *and why*. Silently omitting them would present
        # a missing MFA enrolment as a broken phone.
        #
        # `orphaned` distinguishes the two ways mfa_ok can be false. Without an
        # account there is nothing to enrol, so "enable MFA" is useless advice —
        # the record is a leftover from a deleted user and should be removed.
        from modules.auth import get_auth_manager
        amgr = get_auth_manager()

        users = _mgr().list_users()
        for u in users:
            if not isinstance(u, dict) or not u.get("user_id"):
                continue
            # Resolve through the explicit link, never the id. A presence user
            # whose account was renamed still points at the right account; one
            # that never had an account is standalone rather than broken.
            account = u.get("account")
            u["mfa_ok"] = bool(account) and user_has_mfa(account)
            u["orphaned"] = bool(amgr) and bool(account) and account not in amgr.users
        return {"users": users}

    @app.get("/api/presence/users/{user_id}")
    async def get_user(
            user_id: str,
            request: Request,
    ):
        # Per-user scope check, mirroring report_fix below: a companion phone
        # needs its OWN home lat/lon/radius to arm a geofence, but "presence:read"
        # means "read EVERY user's location". Handing that to a phone would let a
        # stolen device token track the whole household. presence:read:<user_id>
        # keeps the phone's token to exactly one person — itself.
        principal: Optional[Principal] = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(401, "Authentication required",
                                headers={"WWW-Authenticate": "Bearer"})

        from modules.auth import scope_matches
        if not (
                scope_matches(f"presence:read:{user_id}", principal.scopes)
                or scope_matches("presence:read", principal.scopes)
                or scope_matches("admin", principal.scopes)
        ):
            raise HTTPException(403, f"Token lacks scope: presence:read:{user_id}")

        # Policy gate, checked after the scope check so the error names the
        # actual obstacle rather than hiding it behind a permissions message.
        _gate_presence_mfa(user_id)

        dev = _mgr().get_user(user_id)
        if not dev:
            raise HTTPException(404, "User not found")
        return {
            **dev.cfg.to_dict(),
            "ieee": dev.ieee,
            "state": dict(dev.state),
            "last_seen": dev.last_seen,
        }

    @app.post("/api/presence/users")
    async def upsert_user(
            payload: UserUpsert,
            _=Depends(require_scope("admin")),
    ):
        result = await _mgr().upsert_user(payload.dict())
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.delete("/api/presence/users/{user_id}")
    async def delete_user(
            user_id: str,
            _=Depends(require_scope("admin")),
    ):
        result = await _mgr().delete_user(user_id)
        if not result.get("success"):
            raise HTTPException(404, result.get("error"))
        return result

    @app.post("/api/presence/users/{user_id}/fix")
    async def report_fix(
            user_id: str,
            fix: FixReport,
            request: Request,
    ):
        # Per-user scope check: presence:write:<user_id> OR presence:write OR admin.
        # We do this manually because FastAPI Depends doesn't see path params
        # at scope construction time.
        principal: Optional[Principal] = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(401, "Authentication required",
                                headers={"WWW-Authenticate": "Bearer"})

        from modules.auth import scope_matches
        wanted = f"presence:write:{user_id}"
        if not (
                scope_matches(wanted, principal.scopes)
                or scope_matches("presence:write", principal.scopes)
                or scope_matches("admin", principal.scopes)
        ):
            raise HTTPException(403, f"Token lacks scope: {wanted}")

        _gate_presence_mfa(user_id)

        result = await _mgr().report_pwa_fix(
            user_id=user_id,
            lat=fix.lat,
            lon=fix.lon,
            accuracy=fix.accuracy,
            timestamp=fix.timestamp,
        )
        if not result.get("success") and not result.get("ignored"):
            raise HTTPException(400, result.get("error"))
        return result

    @app.post("/api/presence/users/{user_id}/manual")
    async def manual_override(
            user_id: str,
            body: ManualSet,
            _=Depends(require_any_scope("admin", "presence:write")),
    ):
        result = await _mgr().manual_set(user_id, body.presence)
        if not result.get("success"):
            raise HTTPException(400, result.get("error"))
        return result

    logger.info("Presence user routes registered (auth-protected)")