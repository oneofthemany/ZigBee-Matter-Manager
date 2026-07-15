"""
WebSocket connection manager and endpoint.
Extracted from main.py.
"""
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from modules.json_helpers import prepare_for_json

# Add the imports required for auth here
from modules.auth import get_auth_manager, LAN_ONLY_SCOPE
from modules.auth_middleware import _verify_session, _derive_session_secret
from modules.auth_network import get_network_resolver

logger = logging.getLogger("websocket")


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        self.active_connections = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients with safe JSON serialization."""
        if not self.active_connections:
            return

        try:
            safe_message = prepare_for_json(message)
            json_msg = json.dumps(safe_message)
        except Exception as e:
            logger.error(f"Failed to serialise broadcast message: {e}")
            return

        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json_msg)
            except Exception:
                disconnected.append(connection)

        for ws in disconnected:
            self.disconnect(ws)


# Singleton instance
manager = ConnectionManager()


async def broadcast_event(event_type: str, data: dict):
    """Helper to broadcast events via WebSocket."""
    await manager.broadcast({"type": event_type, "payload": data})


def register_websocket_routes(app: FastAPI):
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        # Auth check: require a valid session cookie OR a bearer token
        # passed as a query param (?token=...)
        auth_mgr = get_auth_manager()
        authenticated = False
        auth_username = None

        if auth_mgr:
            # Try cookie first
            cookie = ws.cookies.get("zmm_session")
            if cookie:
                secret = _derive_session_secret(str(auth_mgr.config_path))
                username = _verify_session(cookie, secret)
                if username and username in auth_mgr.users:
                    user = auth_mgr.users[username]
                    if not user.disabled:
                        authenticated = True
                        auth_username = username

            # Fallback: ?token=... for programmatic clients
            if not authenticated:
                token = ws.query_params.get("token")
                if token:
                    verified = auth_mgr.verify_token(token)
                    if verified:
                        authenticated = True
                        auth_username = verified[0].username

        # First-run gate (mirrors auth_middleware's /api/setup/* window):
        # while no enabled admin exists, the setup wizard runs anonymously
        # and needs this WS for live coordinator-scan progress
        # (setup_scan_progress events). LAN clients only; self-closes the
        # moment an admin user is created.
        if not authenticated and auth_mgr:
            no_admin_yet = not any(
                (not u.disabled)
                and ("admins" in u.groups or "admin" in u.extra_scopes)
                for u in auth_mgr.users.values()
            )
            if no_admin_yet:
                resolver = get_network_resolver()
                is_lan = True
                if resolver is not None:
                    is_lan = resolver.is_lan(resolver.resolve(ws))
                if is_lan:
                    authenticated = True
                    logger.info("WebSocket accepted anonymously (setup phase, "
                                "no admin user yet, LAN client)")

        # LAN-only accounts may not connect from outside the LAN. The
        # resolver duck-types over WebSocket (it only touches .headers
        # and .client, both present on Starlette WebSockets).
        if authenticated and auth_username:
            resolver = get_network_resolver()
            if resolver is not None:
                client_ip = resolver.resolve(ws)
                if (
                        not resolver.is_lan(client_ip)
                        and LAN_ONLY_SCOPE
                        in auth_mgr.resolve_user_scopes(auth_username)
                ):
                    logger.warning(
                        f"WebSocket rejected: {auth_username} is LAN-only "
                        f"but connected from {client_ip}"
                    )
                    authenticated = False

        if not authenticated:
            await ws.close(code=1008)  # Policy Violation
            logger.warning("WebSocket connection rejected (no auth)")
            return

        await manager.connect(ws)
        try:
            while True:
                data = await ws.receive_text()
                logger.debug(f"WebSocket received: {data}")
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception as e:
            logger.warning(f"WebSocket error: {e}")
            manager.disconnect(ws)