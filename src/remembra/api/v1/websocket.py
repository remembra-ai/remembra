"""WebSocket endpoint for real-time memory updates.

Security model (SEC-1):

* Every connection is authenticated exactly like REST (API key or dashboard
  JWT, including revocation / deactivation checks) and needs ``memory:recall``.
* Events are routed by the server-derived owner ``user_id`` — never by a
  client-chosen namespace — so a client can only ever receive its own tenant's
  events. Project-restricted keys only receive events for their projects, and
  subscribing to a project outside the key's allow-list is refused.
* Credentials may be sent as headers (``X-API-Key`` / ``Authorization``) or in a
  first ``{"type": "auth", ...}`` message, so browsers never have to put a token
  in the URL. Query-string credentials are still accepted for older clients.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from remembra.auth.middleware import (
    AuthenticatedUser,
    CurrentUser,
    authenticate_api_key,
    authenticate_jwt,
    has_permission,
)
from remembra.config import get_settings
from remembra.core.time import utcnow

log = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])

AUTH_MESSAGE_TIMEOUT_SECONDS = 10.0

CLOSE_UNAUTHORIZED = 4001
CLOSE_FORBIDDEN = 4003


@dataclass(eq=False)
class _Subscriber:
    websocket: WebSocket
    user_id: str
    allowed_projects: tuple[str, ...] | None  # None = unrestricted key
    project_filter: str | None

    def wants(self, project_id: str | None) -> bool:
        if self.allowed_projects is not None and project_id not in self.allowed_projects:
            return False
        return not (self.project_filter and project_id and project_id != self.project_filter)


class ConnectionManager:
    """Tenant-isolated fan-out of memory events to WebSocket subscribers."""

    def __init__(self) -> None:
        self._by_user: dict[str, set[_Subscriber]] = {}
        self._lock = asyncio.Lock()

    async def register(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            self._by_user.setdefault(subscriber.user_id, set()).add(subscriber)
        log.info("websocket_connected", user_id=subscriber.user_id, total_connections=self._count_all())

    async def unregister(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            subs = self._by_user.get(subscriber.user_id)
            if subs is not None:
                subs.discard(subscriber)
                if not subs:
                    del self._by_user[subscriber.user_id]
        log.info("websocket_disconnected", user_id=subscriber.user_id, total_connections=self._count_all())

    async def broadcast(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        user_id: str | None,
        project_id: str | None = None,
    ) -> int:
        """Send an event to the owning user's subscribers only. Returns recipients count.

        Events without an owner are dropped (fail closed) rather than fanned out.
        """
        if not user_id:
            log.warning("websocket_broadcast_without_owner_dropped", event_type=event_type)
            return 0

        message = json.dumps(
            {
                "type": event_type,
                "data": data,
                "timestamp": utcnow().isoformat() + "Z",
                "namespace": f"{user_id}:{project_id or '*'}",
                "project_id": project_id,
            }
        )

        async with self._lock:
            subscribers = list(self._by_user.get(user_id, ()))

        sent = 0
        dead: list[_Subscriber] = []
        for sub in subscribers:
            if not sub.wants(project_id):
                continue
            try:
                if sub.websocket.client_state == WebSocketState.CONNECTED:
                    await sub.websocket.send_text(message)
                    sent += 1
            except Exception as e:
                log.warning("websocket_send_failed", error_type=type(e).__name__)
                dead.append(sub)
        for sub in dead:
            await self.unregister(sub)
        return sent

    def _count_all(self) -> int:
        return sum(len(subs) for subs in self._by_user.values())

    async def get_stats(self, user_id: str | None = None) -> dict[str, Any]:
        """Connection counts. With ``user_id`` only that tenant's connections are reported."""
        async with self._lock:
            if user_id is not None:
                return {"connections": len(self._by_user.get(user_id, ()))}
            return {"total_connections": self._count_all(), "tenants": len(self._by_user)}


# Global connection manager instance
connection_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager (for dependency injection)."""
    return connection_manager


def _credentials_from_headers(websocket: WebSocket) -> tuple[str | None, str | None]:
    api_key = websocket.headers.get("x-api-key")
    token = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:].strip()
        if bearer.startswith("rem_"):
            api_key = api_key or bearer
        else:
            token = bearer
    return api_key, token


async def _authenticate(websocket: WebSocket, api_key: str | None, token: str | None) -> AuthenticatedUser | None:
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthenticatedUser(user_id="default_user", api_key_id="dev_key", rate_limit_tier="standard")
    # authenticate_* only use .app/.state, which WebSocket shares with Request.
    conn: Any = websocket
    if token:
        user = await authenticate_jwt(conn, token)
        if user:
            return user
    if api_key:
        return await authenticate_api_key(conn, api_key)
    return None


def _project_allowed(user: AuthenticatedUser, project_id: str | None) -> bool:
    return not (project_id and user.project_ids and project_id not in user.project_ids)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    namespace: str | None = Query(None, description="Deprecated; routing is derived from your credentials"),
    project_id: str | None = Query(None, description="Optional project ID filter"),
    api_key: str | None = Query(None, description="Deprecated: prefer header or auth message"),
    token: str | None = Query(None, description="Deprecated: prefer header or auth message"),
) -> None:
    """Real-time memory events for the authenticated tenant.

    Events: ``memory.created``, ``memory.updated``, ``memory.superseded``,
    ``memory.deleted``. Send ``ping`` for ``pong``; send
    ``{"type": "subscribe", "project_id": "..."}`` to change the project filter.
    """
    await websocket.accept()

    header_key, header_token = _credentials_from_headers(websocket)
    api_key = header_key or api_key
    token = header_token or token
    if websocket.query_params.get("api_key") or websocket.query_params.get("token"):
        log.info("websocket_query_credentials_deprecated")

    if not api_key and not token and get_settings().auth_enabled:
        # Browser clients authenticate with a first message so tokens stay out of URLs.
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_MESSAGE_TIMEOUT_SECONDS)
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "auth":
                api_key = msg.get("api_key") or None
                token = msg.get("token") or None
                project_id = msg.get("project_id", project_id)
        except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            pass

    user = await _authenticate(websocket, api_key, token)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Authentication required")
        return
    if not has_permission(user, "memory:recall"):
        await websocket.close(code=CLOSE_FORBIDDEN, reason="memory:recall permission required")
        return
    if not _project_allowed(user, project_id):
        await websocket.close(code=CLOSE_FORBIDDEN, reason="No access to project")
        return

    subscriber = _Subscriber(
        websocket=websocket,
        user_id=user.user_id,
        allowed_projects=tuple(user.project_ids) if user.project_ids else None,
        project_filter=project_id,
    )
    await connection_manager.register(subscriber)

    def _ns() -> str:
        return f"{user.user_id}:{subscriber.project_filter or '*'}"

    try:
        await websocket.send_json(
            {
                "type": "connected",
                "data": {
                    "namespace": _ns(),
                    "project_id": subscriber.project_filter,
                    "message": "Connected to Remembra real-time updates",
                },
                "timestamp": utcnow().isoformat() + "Z",
            }
        )

        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except TimeoutError:
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
                continue

            if data == "ping":
                await websocket.send_text("pong")
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict) or msg.get("type") != "subscribe":
                continue

            new_project = msg.get("project_id", subscriber.project_filter)
            if not _project_allowed(user, new_project):
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {"message": "No access to project", "project_id": new_project},
                        "timestamp": utcnow().isoformat() + "Z",
                    }
                )
                continue
            subscriber.project_filter = new_project
            await websocket.send_json(
                {
                    "type": "subscribed",
                    "data": {"namespace": _ns(), "project_id": subscriber.project_filter},
                    "timestamp": utcnow().isoformat() + "Z",
                }
            )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("websocket_error", error_type=type(e).__name__)
    finally:
        await connection_manager.unregister(subscriber)


@router.get("/ws/stats", tags=["websocket"])
async def websocket_stats(request: Request, current_user: CurrentUser) -> dict[str, Any]:
    """Connection statistics: your own connections (platform totals for superadmins)."""
    from remembra.auth.superadmin import is_superadmin

    if await is_superadmin(request, current_user):
        return await connection_manager.get_stats()
    return await connection_manager.get_stats(user_id=current_user.user_id)
