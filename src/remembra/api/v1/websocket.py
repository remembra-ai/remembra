"""WebSocket endpoint for real-time memory updates.

Security model (SEC-1):

* Every connection is authenticated exactly like REST (API key or dashboard
  JWT, including revocation / deactivation checks) and needs ``memory:recall``.
* The same checks run again while the socket is open (P-348): before every
  event it is sent, every ``REVALIDATE_INTERVAL_SECONDS`` while it is idle, and
  at once when a key is revoked or deleted, a session is signed out, a password
  changes or the account is deactivated or deleted
  (:func:`recheck_user_connections`). A socket that fails them gets no further
  events and is closed: 4001 when its key or session no longer authenticates,
  4003 when it lost ``memory:recall`` or the project it follows.
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
from dataclasses import dataclass, field
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
# An idle socket re-runs the connect-time checks this often (events re-check on every send).
REVALIDATE_INTERVAL_SECONDS = 30.0
# The server sends "ping" after this long without a message from the client.
IDLE_PING_SECONDS = 60.0

CLOSE_UNAUTHORIZED = 4001
CLOSE_FORBIDDEN = 4003
CLOSE_INTERNAL_ERROR = 1011
ACCESS_ENDED_REASON = "Access revoked or expired"


@dataclass(eq=False)
class _Subscriber:
    websocket: WebSocket
    user_id: str
    allowed_projects: tuple[str, ...] | None  # None = unrestricted key
    project_filter: str | None
    # The credentials the socket connected with, kept only to re-run the same
    # checks while it is open. repr=False keeps them out of logs and tracebacks.
    api_key: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    # Set once the socket's access has ended; the close frame is sent by this task.
    closing: asyncio.Task[None] | None = field(default=None, repr=False)

    def wants(self, project_id: str | None) -> bool:
        if self.allowed_projects is not None and project_id not in self.allowed_projects:
            return False
        return not (self.project_filter and project_id and project_id != self.project_filter)

    def may_follow(self, project_id: str | None) -> bool:
        """Whether the key's current project allow-list lets this socket follow ``project_id``."""
        return not (project_id and self.allowed_projects is not None and project_id not in self.allowed_projects)


class ConnectionManager:
    """Tenant-isolated fan-out of memory events to WebSocket subscribers."""

    def __init__(self) -> None:
        self._by_user: dict[str, set[_Subscriber]] = {}
        self._lock = asyncio.Lock()
        self._closing: set[asyncio.Task[None]] = set()

    async def register(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            self._by_user.setdefault(subscriber.user_id, set()).add(subscriber)
        log.info("websocket_connected", user_id=subscriber.user_id, total_connections=self._count_all())

    async def unregister(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            subs = self._by_user.get(subscriber.user_id)
            if subs is None or subscriber not in subs:
                return
            subs.discard(subscriber)
            if not subs:
                del self._by_user[subscriber.user_id]
        log.info("websocket_disconnected", user_id=subscriber.user_id, total_connections=self._count_all())

    async def recheck(self, subscriber: _Subscriber) -> bool:
        """Re-run the connect-time checks for an open socket. True while it may still get events.

        The credential is validated exactly like REST (key active, account
        active, JWT signature/expiry/logout/session cut-off), then
        ``memory:recall`` and the followed project are checked again. On failure
        the socket is unregistered at once, so no further event reaches it, and
        closed: 4001 when the key or session no longer authenticates, 4003 when
        it lost ``memory:recall`` or its project, 1011 when the check itself
        could not run.
        """
        if subscriber.closing is not None:
            return False
        try:
            user = await _authenticate(subscriber.websocket, subscriber.api_key, subscriber.token, record_use=False)
        except Exception as e:
            log.warning("websocket_recheck_failed", user_id=subscriber.user_id, error_type=type(e).__name__)
            await self._end(subscriber, CLOSE_INTERNAL_ERROR, "Could not re-check access")
            return False
        if user is None or user.user_id != subscriber.user_id:
            await self._end(subscriber, CLOSE_UNAUTHORIZED, ACCESS_ENDED_REASON)
            return False
        if not has_permission(user, "memory:recall"):
            await self._end(subscriber, CLOSE_FORBIDDEN, "memory:recall permission required")
            return False
        if not _project_allowed(user, subscriber.project_filter):
            await self._end(subscriber, CLOSE_FORBIDDEN, "No access to project")
            return False
        subscriber.allowed_projects = tuple(user.project_ids) if user.project_ids else None
        return True

    async def recheck_user(self, user_id: str) -> int:
        """Re-check every open socket of ``user_id`` now. Returns how many were closed."""
        async with self._lock:
            subscribers = list(self._by_user.get(user_id, ()))
        closed = 0
        for sub in subscribers:
            if not await self.recheck(sub):
                closed += 1
        return closed

    async def _end(self, subscriber: _Subscriber, code: int, reason: str) -> None:
        """Stop sending to ``subscriber`` now and close its socket without blocking the caller.

        Closing can wait on a slow client, so it runs in its own task; the
        socket's handler awaits that task before it returns.
        """
        await self.unregister(subscriber)
        if subscriber.closing is not None:
            return
        task = asyncio.create_task(_close_quietly(subscriber.websocket, code, reason))
        subscriber.closing = task
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        log.info("websocket_access_ended", user_id=subscriber.user_id, code=code, reason=reason)

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
            # Re-check before every send: a revoked key, a signed-out session or a
            # deactivated account never gets another event, even with no hook run.
            if not await self.recheck(sub) or not sub.wants(project_id):
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


async def recheck_user_connections(user_id: str) -> None:
    """Re-check every open socket of ``user_id`` now; call right after cutting that account's access.

    Sockets whose key or session no longer passes are closed at once. Never
    raises: cutting access must not fail because of socket bookkeeping.
    """
    try:
        await connection_manager.recheck_user(user_id)
    except Exception as e:
        log.warning("websocket_recheck_user_failed", user_id=user_id, error_type=type(e).__name__)


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close(code=code, reason=reason)
    except Exception as e:
        log.debug("websocket_close_failed", error_type=type(e).__name__)


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


async def _authenticate(
    websocket: WebSocket, api_key: str | None, token: str | None, *, record_use: bool = True
) -> AuthenticatedUser | None:
    """Validate the socket's credentials like REST. ``record_use=False`` leaves the key's last-used time alone."""
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
        return await authenticate_api_key(conn, api_key, record_use=record_use)
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
        api_key=api_key,
        token=token,
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

        loop = asyncio.get_running_loop()
        next_recheck = loop.time() + REVALIDATE_INTERVAL_SECONDS
        next_ping = loop.time() + IDLE_PING_SECONDS
        while subscriber.closing is None:
            now = loop.time()
            if now >= next_recheck:
                if not await connection_manager.recheck(subscriber):
                    break
                next_recheck = now + REVALIDATE_INTERVAL_SECONDS
            if now >= next_ping:
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
                next_ping = now + IDLE_PING_SECONDS
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(), timeout=max(0.0, min(next_recheck, next_ping) - loop.time())
                )
            except TimeoutError:
                continue
            next_ping = loop.time() + IDLE_PING_SECONDS

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
            if not subscriber.may_follow(new_project):
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
        if subscriber.closing is not None:
            # Send the close frame (and its code) before the handler returns.
            await subscriber.closing


@router.get("/ws/stats", tags=["websocket"])
async def websocket_stats(request: Request, current_user: CurrentUser) -> dict[str, Any]:
    """Connection statistics: your own connections (platform totals for superadmins)."""
    from remembra.auth.superadmin import is_superadmin

    if await is_superadmin(request, current_user):
        return await connection_manager.get_stats()
    return await connection_manager.get_stats(user_id=current_user.user_id)
