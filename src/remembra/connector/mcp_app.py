"""The remote MCP endpoint (``/mcp``) behind the connector's OAuth server.

Design:

- A dedicated FastMCP instance per app (the streamable-HTTP session manager
  can run only once per instance), in stateless JSON mode: every request
  carries its own bearer token, nothing is tied to an MCP session, and any
  worker can serve any request.
- :class:`ConnectorEndpoint` wraps the MCP ASGI app. It validates the
  ``Authorization: Bearer`` access token (hash lookup, expiry, revocation,
  resource binding, account status) before the MCP SDK runs, answers
  ``401`` with ``WWW-Authenticate: Bearer resource_metadata=...`` (the
  handshake Claude and ChatGPT use to discover the authorization server) and
  ``403 insufficient_scope`` for a ``tools/call`` the grant does not cover.
  The validated grant is attached to the ASGI scope; the MCP SDK hands tools
  the Starlette request built from that same scope.
- Tools call the existing REST routes in-process through ``httpx.ASGITransport``
  under a least-privilege principal built from the grant, so RBAC, project
  restriction, PII policy, sanitizer, usage limits and audit all apply
  exactly as for any other client. The OAuth token itself is never accepted
  by the REST API.
- Mobile-safe tool set only: brief, trail, recall, inbox send, note store,
  list projects. Nothing edits or deletes.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from remembra import __version__
from remembra.auth.middleware import AuthenticatedUser, connector_principal, get_client_ip
from remembra.config import get_settings
from remembra.connector.oauth import (
    account_allows_grant,
    protected_resource_metadata_url,
    resource_url,
)
from remembra.connector.policy import SCOPE_BRIEF, SCOPE_RECALL, SCOPE_STORE, SUPPORTED_SCOPES, format_scope, resource_matches
from remembra.connector.store import ConnectorStore, Grant
from remembra.security.error_sanitizer import sanitize_error_message

log = structlog.get_logger(__name__)

SCOPE_KEY = "remembra.connector"
_MAX_BODY_BYTES = 1_000_000
_INTERNAL_BASE = "http://connector.internal"

# OAuth scope each tool requires (None = any valid grant).
TOOL_SCOPES: dict[str, str | None] = {
    "session_brief": SCOPE_BRIEF,
    "trail": SCOPE_BRIEF,
    "recall_memories": SCOPE_RECALL,
    "send_to_inbox": SCOPE_STORE,
    "store_memory": SCOPE_STORE,
    "list_projects": None,
}

INSTRUCTIONS = (
    "Remembra is the memory your coding agents (Claude Code, Codex, Cursor, Gemini, ...) share. "
    "Use session_brief to see where work stands (latest handoff, status, recent work), trail for the "
    "handoffs and checkpoints agents left over time, and recall_memories to search. To ask a desktop "
    "agent to do something, use send_to_inbox with its agent id (for example 'claude-code'); it sees "
    "the message at its next session start. store_memory saves a note. Nothing here edits or deletes. "
    "This connection speaks as the chat app's own agent (see list_projects for its agent id and projects). "
    "A coding agent that also has a local Remembra MCP server (for example Claude Code on a desktop) "
    "should use that local server instead, so its inbox and notes stay under its own agent id."
)


@dataclass(frozen=True)
class ConnectorCall:
    """What the endpoint attaches to the ASGI scope for the tools."""

    grant: Grant
    app: Any
    client_ip: str


# ---------------------------------------------------------------------------
# In-process REST calls
# ---------------------------------------------------------------------------


class ToolFailure(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _call_from(ctx: Context[Any, Any, Any]) -> ConnectorCall:
    request = ctx.request_context.request
    call = getattr(request, "scope", {}).get(SCOPE_KEY) if request is not None else None
    if not isinstance(call, ConnectorCall):
        # Only reachable if the MCP app were served without ConnectorEndpoint.
        raise ToolFailure("Not authenticated.", 401)
    return call


def _require(call: ConnectorCall, tool: str) -> None:
    needed = TOOL_SCOPES.get(tool)
    if needed and needed not in call.grant.scopes:
        raise ToolFailure(f"This connection was not granted '{needed}'. Reconnect and allow it.", 403)


def _project(call: ConnectorCall, project_id: str | None) -> str:
    if project_id is None or not project_id.strip():
        return call.grant.default_project
    project = "-".join(project_id.split())
    if project not in call.grant.project_ids:
        raise ToolFailure(
            f"This connection can only use projects {call.grant.project_ids}. Reconnect to add '{project}'.",
            403,
        )
    return project


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)
    if isinstance(detail, list):
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail or f"HTTP {resp.status_code}")


async def _rest(
    call: ConnectorCall,
    method: str,
    path: str,
    *,
    permissions: list[str],
    params: Any = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Call a REST route in-process as the grant's user, with only ``permissions``."""
    principal = AuthenticatedUser(
        user_id=call.grant.user_id,
        api_key_id=f"oauth:{call.grant.grant_id}",
        rate_limit_tier="standard",
        name=f"connector:{call.grant.agent_id}",
        role="editor",
        scopes=permissions,
        project_ids=list(call.grant.project_ids),
    )
    transport = httpx.ASGITransport(app=call.app, client=("127.0.0.1", 0))
    headers = {"X-Forwarded-For": call.client_ip, "User-Agent": f"remembra-connector/{__version__}"}
    with connector_principal(principal):
        async with httpx.AsyncClient(transport=transport, base_url=_INTERNAL_BASE, timeout=60.0) as client:
            resp = await client.request(method, path, params=params, json=json_body, headers=headers)
    if resp.status_code >= 400:
        raise ToolFailure(sanitize_error_message(_detail(resp)), resp.status_code)
    return resp.json()


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


async def _run(ctx: Context[Any, Any, Any], tool: str, body: Callable[[ConnectorCall], Awaitable[dict[str, Any]]]) -> str:
    try:
        call = _call_from(ctx)
        _require(call, tool)
        return _dump({"status": "ok", **(await body(call))})
    except ToolFailure as e:
        return json.dumps({"status": "error", "error": str(e), "code": e.code})
    except Exception as e:
        log.error("connector_tool_failed", tool=tool, error_type=type(e).__name__)
        return json.dumps({"status": "error", "error": sanitize_error_message(e)})


def _memory_view(m: dict[str, Any]) -> dict[str, Any]:
    meta = m.get("metadata") or {}
    return {
        "id": m.get("id"),
        "content": m.get("content"),
        "created_at": m.get("created_at"),
        "memory_type": m.get("memory_type"),
        "agent_id": meta.get("agent_id"),
        "project_id": m.get("project_id"),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


async def session_brief(
    ctx: Context[Any, Any, Any],
    project_id: str | None = None,
    agent_id: str | None = None,
    recent_n: int = 10,
) -> str:
    """Where work stands: the latest handoff an agent left, current status values,
    the most recent memories by time, and an agent's unread inbox.

    Args:
        project_id: One of this connection's projects (default: the first one).
        agent_id: Whose inbox to include, e.g. "claude-code" (default: this connection's agent).
        recent_n: Recent memories to include (0-50, default 10).
    """

    async def body(call: ConnectorCall) -> dict[str, Any]:
        project = _project(call, project_id)
        params = {
            "project_id": project,
            "agent_id": (agent_id or call.grant.agent_id).strip(),
            "recent_n": max(0, min(recent_n, 50)),
        }
        brief = await _rest(call, "GET", "/api/v1/session/brief", permissions=["memory:recall"], params=params)
        return dict(brief)

    return await _run(ctx, "session_brief", body)


async def trail(ctx: Context[Any, Any, Any], project_id: str | None = None, limit: int = 20, since: str | None = None) -> str:
    """The trail your agents left: handoffs and checkpoints, newest first.

    Args:
        project_id: One of this connection's projects (default: the first one).
        limit: How many entries (1-50, default 20).
        since: Only entries created at or after this ISO date/time (e.g. "2026-09-20").
    """

    async def body(call: ConnectorCall) -> dict[str, Any]:
        project = _project(call, project_id)
        params: list[tuple[str, str | int]] = [
            ("project_id", project),
            ("memory_type", "handoff"),
            ("memory_type", "checkpoint"),
            ("order", "desc"),
            ("limit", max(1, min(limit, 50))),
        ]
        if since:
            params.append(("start", since))
        result = await _rest(call, "GET", "/api/v1/timeline", permissions=["memory:recall"], params=params)
        entries = [_memory_view(m) for m in result.get("memories", [])]
        return {"project_id": project, "count": len(entries), "total": result.get("total", len(entries)), "trail": entries}

    return await _run(ctx, "trail", body)


async def recall_memories(ctx: Context[Any, Any, Any], query: str, limit: int = 5, project_id: str | None = None) -> str:
    """Search memory (semantic + keyword). For "what happened recently" use session_brief or trail.

    Args:
        query: What to look for, in plain language.
        limit: Maximum memories (1-20, default 5).
        project_id: One of this connection's projects (default: the first one).
    """

    async def body(call: ConnectorCall) -> dict[str, Any]:
        if not query or not query.strip():
            raise ToolFailure("query must not be empty.", 400)
        project = _project(call, project_id)
        payload = {"query": query, "limit": max(1, min(limit, 20)), "project_id": project}
        result = await _rest(call, "POST", "/api/v1/memories/recall", permissions=["memory:recall"], json_body=payload)
        memories = [
            {**_memory_view(m), "relevance": round(float(m.get("relevance") or 0.0), 3)} for m in result.get("memories", [])
        ]
        extra = {"degraded": result["degraded"]} if result.get("degraded") else {}
        return {"project_id": project, **extra, "context": result.get("context"), "count": len(memories), "memories": memories}

    return await _run(ctx, "recall_memories", body)


def _expires_at(expires_in: str | None) -> str | None:
    if not expires_in:
        return None
    text = expires_in.strip().lower()
    units = {"h": 3600, "d": 86400, "w": 604800}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise ToolFailure("expires_in must look like '12h', '7d' or '2w'.", 400)
    return (datetime.now(UTC) + timedelta(seconds=int(text[:-1]) * units[text[-1]])).isoformat()


async def send_to_inbox(
    ctx: Context[Any, Any, Any],
    to_agent: str,
    subject: str,
    body: str,
    project_id: str | None = None,
    expires_in: str | None = None,
) -> str:
    """Leave an instruction for another agent. It appears in that agent's
    session brief the next time it starts a session (e.g. Claude Code on the desktop).

    Args:
        to_agent: Recipient agent id, e.g. "claude-code", "codex", "cursor".
        subject: One-line subject.
        body: The instruction, with the context the agent needs.
        project_id: Project the instruction is about (default: this connection's first project).
        expires_in: Optional expiry such as "12h", "7d" or "2w".
    """

    async def run(call: ConnectorCall) -> dict[str, Any]:
        project = _project(call, project_id)
        recipient = (to_agent or "").strip()
        if not recipient:
            raise ToolFailure("to_agent must not be empty.", 400)
        payload = {
            "to_agent": recipient,
            "subject": subject,
            "body": body,
            "from_agent": call.grant.agent_id,  # the grant's label; the caller can't spoof a sender
            "metadata": {"project_id": project, "via": "connector", "connection_id": call.grant.grant_id},
            "expires_at": _expires_at(expires_in),
        }
        result = await _rest(call, "POST", "/api/v1/inbox/send", permissions=["memory:store"], json_body=payload)
        return {
            "inbox_id": result.get("inbox_id"),
            "delivered_to": recipient,
            "from_agent": call.grant.agent_id,
            "project_id": project,
            "created_at": result.get("created_at"),
            "note": f"'{recipient}' sees this in its session brief at its next session start.",
        }

    return await _run(ctx, "send_to_inbox", run)


async def store_memory(
    ctx: Context[Any, Any, Any], content: str, tags: list[str] | None = None, project_id: str | None = None
) -> str:
    """Save a note to memory. Stored verbatim as one new memory (never merged
    into or replacing an existing one), attributed to this connection's agent.

    Args:
        content: The note.
        tags: Optional short labels.
        project_id: One of this connection's projects (default: the first one).
    """

    async def body(call: ConnectorCall) -> dict[str, Any]:
        project = _project(call, project_id)
        clean_tags = [str(t).strip()[:64] for t in (tags or []) if str(t).strip()][:10]
        metadata: dict[str, Any] = {
            "agent_id": call.grant.agent_id,
            "source": "connector",
            "kind": "note",
            "connection_id": call.grant.grant_id,
        }
        if clean_tags:
            metadata["tags"] = clean_tags
        payload = {
            "content": content,
            "project_id": project,
            "memory_type": "observation",
            # Atomic store: no fact split and no consolidation, so a note can
            # never update or supersede an existing memory.
            "skip_extraction": True,
            "metadata": metadata,
        }
        result = await _rest(call, "POST", "/api/v1/memories", permissions=["memory:store"], json_body=payload)
        return {"id": result.get("id"), "project_id": project, "stored_as": "note", "store_status": result.get("status")}

    return await _run(ctx, "store_memory", body)


async def list_projects(ctx: Context[Any, Any, Any]) -> str:
    """The projects this connection may use (the first is the default), its agent name and permissions."""

    async def body(call: ConnectorCall) -> dict[str, Any]:
        grant = call.grant
        return {
            "projects": [{"project_id": p, "default": i == 0} for i, p in enumerate(grant.project_ids)],
            "agent_id": grant.agent_id,
            "scopes": grant.scopes,
            "note": "To use another project, disconnect and reconnect this connector and select it.",
        }

    return await _run(ctx, "list_projects", body)


def build_connector_mcp() -> FastMCP:
    """A fresh FastMCP instance with the connector's tools (one per app)."""
    server = FastMCP(
        name="remembra",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # Bearer tokens (never cookies) authenticate every request, so DNS
        # rebinding can't borrow a victim's credentials; the Host header is
        # the public domain behind the proxy and is not pinned here.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    server._mcp_server.version = __version__
    server.add_tool(session_brief, title="Session Brief", annotations=_READ)
    server.add_tool(trail, title="Agent Trail", annotations=_READ)
    server.add_tool(recall_memories, title="Recall Memories", annotations=_READ)
    server.add_tool(send_to_inbox, title="Send To Agent Inbox", annotations=_WRITE)
    server.add_tool(store_memory, title="Store Note", annotations=_WRITE)
    server.add_tool(list_projects, title="List Projects", annotations=_READ)
    return server


# ---------------------------------------------------------------------------
# ASGI endpoint with bearer authentication
# ---------------------------------------------------------------------------


def _www_authenticate(error: str | None = None, description: str | None = None, scope: str | None = None) -> str:
    settings = get_settings()
    parts = [f'resource_metadata="{protected_resource_metadata_url(settings)}"']
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    parts.append(f'scope="{scope or format_scope(SUPPORTED_SCOPES)}"')
    return "Bearer " + ", ".join(parts)


def _tool_calls(body: bytes) -> list[str]:
    """Names of tools invoked by a JSON-RPC body (single message or batch)."""
    try:
        parsed = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return []
    messages = parsed if isinstance(parsed, list) else [parsed]
    names: list[str] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            params = msg.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            names.append(str(name) if name is not None else "")
    return names


class ConnectorEndpoint:
    """ASGI app for ``/mcp``: OAuth bearer check, then the MCP SDK."""

    def __init__(self, mcp: FastMCP) -> None:
        self.mcp = mcp
        mcp.streamable_http_app()  # creates the session manager (stateless, JSON responses)
        self.session_manager = mcp.session_manager

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self.session_manager.run():
            yield

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        settings = get_settings()
        request = Request(scope, receive)
        store: ConnectorStore | None = getattr(request.app.state, "connector_store", None)
        if store is None or not settings.connector_enabled:
            await JSONResponse({"detail": "Not found"}, status_code=404)(scope, receive, send)
            return

        auth = request.headers.get("authorization", "")
        if auth[:7].lower() != "bearer " or not auth[7:].strip():
            await self._deny(scope, receive, send, 401, _www_authenticate(), "Authorization required.")
            return
        grant = await store.grant_for_access_token(auth[7:].strip())
        if (
            grant is None
            or not resource_matches(grant.resource, resource_url(settings))
            or not await account_allows_grant(request.app.state.db, grant)
        ):
            await self._deny(
                scope,
                receive,
                send,
                401,
                _www_authenticate("invalid_token", "The access token is invalid or expired"),
                "Invalid or expired access token.",
            )
            return

        body = b""
        if request.method == "POST":
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > _MAX_BODY_BYTES:  # stop reading; never buffer an oversized body
                    await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
                    return
                chunks.append(chunk)
            body = b"".join(chunks)
            for name in _tool_calls(body):
                needed = TOOL_SCOPES.get(name)
                if needed and needed not in grant.scopes:
                    challenge = _www_authenticate(
                        "insufficient_scope", f"{name} requires {needed}", format_scope([*grant.scopes, needed])
                    )
                    await self._deny(scope, receive, send, 403, challenge, f"The connection lacks the '{needed}' scope.")
                    return

        await store.touch_grant(grant.grant_id)
        scope[SCOPE_KEY] = ConnectorCall(grant=grant, app=scope.get("app"), client_ip=get_client_ip(request))

        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            message: dict[str, Any] = await receive()
            return message

        await self.session_manager.handle_request(scope, replay if request.method == "POST" else receive, send)

    @staticmethod
    async def _deny(scope: Any, receive: Any, send: Any, status_code: int, challenge: str, detail: str) -> None:
        response = JSONResponse({"error": detail}, status_code=status_code, headers={"WWW-Authenticate": challenge})
        await response(scope, receive, send)
