"""Request body size caps, enforced before any route code reads the body (DEP-1).

FastAPI parses a form or upload body *before* it runs a route's dependencies,
and authentication is a dependency. Without a cap, anyone can make the single
API worker parse an arbitrarily large body before the 401. This ASGI middleware
runs first:

* a ``Content-Length`` over the cap is refused with 413 before a byte is read;
* a body without one (chunked) is counted as it arrives, and reading stops the
  moment it passes the cap, so an oversized stream is never buffered.

Caps (``limit_for``):

* 1 MiB for every request by default;
* 64 KiB for ``application/x-www-form-urlencoded`` on every route: no route takes
  a large urlencoded form, and it is the costliest body to parse;
* a larger cap only on the routes whose own validated limits need one, and only
  for the content type they accept (see ``ROUTE_LIMITS``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MiB = 1024 * 1024

DEFAULT_MAX_BODY_BYTES = 1 * MiB
FORM_MAX_BODY_BYTES = 64 * 1024
# The upload route refuses files over 50 MiB itself (transfer.py); the extra
# MiB is room for the multipart framing around a file of exactly 50 MiB.
IMPORT_FILE_MAX_BODY_BYTES = 51 * MiB

_JSON = "application/json"
_MULTIPART = "multipart/form-data"
_URLENCODED = "application/x-www-form-urlencoded"

# (method, path) -> (content type the route accepts, cap). Each larger cap is
# the route's own validated maximum plus JSON overhead:
#   /memories/batch       100 items x 50,000 chars, plus optional embeddings
#   /ingest/conversation  200 messages x 50,000 chars
#   /ingest/changelog     500,000 chars
ROUTE_LIMITS: Mapping[tuple[str, str], tuple[str, int]] = {
    ("POST", "/api/v1/transfer/import/file"): (_MULTIPART, IMPORT_FILE_MAX_BODY_BYTES),
    ("POST", "/api/v1/memories/batch"): (_JSON, 8 * MiB),
    ("POST", "/api/v1/ingest/conversation"): (_JSON, 12 * MiB),
    ("POST", "/api/v1/ingest/conversation/stream"): (_JSON, 12 * MiB),
    ("POST", "/api/v1/ingest/changelog"): (_JSON, 4 * MiB),
}


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


def _media_type(scope: Scope) -> str:
    return (_header(scope, b"content-type") or "").split(";", 1)[0].strip().lower()


def limit_for(
    scope: Scope,
    *,
    default_limit: int = DEFAULT_MAX_BODY_BYTES,
    form_limit: int = FORM_MAX_BODY_BYTES,
) -> int:
    """The body cap in bytes for this HTTP request."""
    media_type = _media_type(scope)
    if media_type == _URLENCODED:
        return form_limit
    path = str(scope.get("path") or "/")
    root_path = str(scope.get("root_path") or "")
    if root_path and path.startswith(root_path):  # served under a prefix (uvicorn --root-path)
        path = path[len(root_path) :] or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    route = ROUTE_LIMITS.get((str(scope.get("method") or "").upper(), path))
    if route is not None and media_type == route[0]:
        return route[1]
    return default_limit


def _describe(limit: int) -> str:
    if limit % MiB == 0:
        return f"{limit // MiB} MiB"
    if limit % 1024 == 0:
        return f"{limit // 1024} KiB"
    return f"{limit} bytes"


class RequestBodyTooLarge(HTTPException):
    """Raised from ``receive`` when a streamed body passes its cap.

    An ``HTTPException``: FastAPI re-raises those from body parsing (instead of
    turning them into a 400), so the app's handler answers 413.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413,
            detail=f"Request body too large. The limit for this request is {_describe(limit)}.",
            headers={"Connection": "close"},
        )


class BodySizeLimitMiddleware:
    """Pure ASGI middleware: see the module docstring."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = DEFAULT_MAX_BODY_BYTES,
        form_limit: int = FORM_MAX_BODY_BYTES,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.form_limit = form_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = limit_for(scope, default_limit=self.default_limit, form_limit=self.form_limit)
        declared = _header(scope, b"content-length")
        if declared is not None:
            if not declared.strip().isdigit():
                await _reply(send, 400, "Invalid Content-Length header.")
                return
            if int(declared) > limit:
                await _reply(send, 413, RequestBodyTooLarge(limit).detail)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge(limit)
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge as exc:
            # Nothing below turned it into a response (e.g. a route that
            # streams the body itself). Answer it here if we still can.
            if response_started:
                raise
            await _reply(send, 413, exc.detail)


async def _reply(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
