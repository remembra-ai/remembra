"""The desk's only way to read: fixed GETs through this app, in process, as the asking user.

The ``connector/mcp_app.py`` pattern: ``httpx.ASGITransport`` over the app,
under :func:`~remembra.auth.middleware.connector_principal` with the desk's
read principal, so every policy the routes enforce (RBAC, project access,
the brief's trust policy, rate limits, audit) applies unchanged. On top:

* a path outside :data:`ALLOWED_PATHS`, or a brief without ``preview=1``,
  is refused before any request is built (:class:`ToolNotAllowed`);
* :class:`ReadOnlyASGI` answers 405 to every method but GET without calling
  the app, and the app refuses a ``marshal:`` principal's writes again
  (``refuse_delegated_writes``).

Each call runs in its own task, so the principal and the request's logging
context never leak into the caller's.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from remembra import __version__
from remembra.auth.middleware import AuthenticatedUser, connector_principal
from remembra.marshal.desk.constants import TOOL_TIMEOUT_S

BRIEF_PATH = "/api/v1/session/brief"
ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/trail/summary",
        "/api/v1/trail",
        "/api/v1/trail/diagnosis",
        BRIEF_PATH,
        "/api/v1/inbox/summary",
        "/api/v1/cloud/usage/summary",
        "/api/v1/cloud/usage/daily",
        "/api/v1/cloud/plan",
    }
)
INTERNAL_BASE = "http://marshal.internal"
_METHOD_NOT_ALLOWED = b'{"detail":"Marshal reads only (GET)."}'


class ToolNotAllowed(Exception):
    """A read the desk may never make (refused before anything is sent)."""


class ReadOnlyASGI:
    """Wraps the app: GET passes through; any other method, or a non-HTTP scope, gets 405 and never reaches it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("method", "")).upper() != "GET":
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"content-type", b"application/json"), (b"allow", b"GET")],
                }
            )
            await send({"type": "http.response.body", "body": _METHOD_NOT_ALLOWED})
            return
        await self.app(scope, receive, send)


@dataclass(frozen=True)
class ClientResult:
    status: int
    json: Any
    ms: int


def check_allowed(path: str, params: dict[str, Any]) -> None:
    if path not in ALLOWED_PATHS:
        raise ToolNotAllowed(path)
    if path == BRIEF_PATH and str(params.get("preview", "")).lower() not in ("1", "true"):
        raise ToolNotAllowed(f"{path} without preview=1")


class DeskClient:
    """GETs under the desk's read principal for one conversation."""

    def __init__(self, app: Any, principal: AuthenticatedUser, client_ip: str) -> None:
        self.app = app
        self.principal = principal
        self.client_ip = client_ip

    async def get(self, path: str, params: dict[str, Any] | None = None) -> ClientResult:
        query = dict(params or {})
        check_allowed(path, query)
        return await asyncio.create_task(self._get(path, query), name="marshal-desk-read")

    async def _get(self, path: str, params: dict[str, Any]) -> ClientResult:
        transport = httpx.ASGITransport(app=ReadOnlyASGI(self.app), client=("127.0.0.1", 0))
        headers = {"X-Forwarded-For": self.client_ip, "User-Agent": f"remembra-marshal/{__version__}"}
        started = time.monotonic()
        with connector_principal(self.principal):
            async with (
                asyncio.timeout(TOOL_TIMEOUT_S),
                httpx.AsyncClient(transport=transport, base_url=INTERNAL_BASE, timeout=TOOL_TIMEOUT_S) as client,
            ):
                resp = await client.get(path, params=params, headers=headers)
        ms = int((time.monotonic() - started) * 1000)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return ClientResult(status=resp.status_code, json=body, ms=ms)
