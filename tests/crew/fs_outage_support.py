"""A scripted Remembra API for the outage and failure-flag tests (``httpx.MockTransport``, no network).

It answers the few routes the MCP crew tools use (join, match, claims, events, session close, memories)
and can be switched into an outage: a proxy 503, a hang (read timeout) or a refused connection. Every
request is recorded, so a test can assert what was and was not sent.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp import types

import remembra.mcp.server as server
from remembra.client.memory import Memory

JOIN = {
    "crew_id": "crew_1",
    "session_id": "cs_1",
    "session_token": "tok-1",
    "callsign": "codex-1",
    "seq": 5,
    "session": {"agent_verified": True, "current_task_id": None},
}


class FakeApi:
    def __init__(self) -> None:
        self.down: str | None = None  # None, "503", "hang", "refused"
        self.down_paths: tuple[str, ...] = ()  # when set, only these path suffixes are down
        self.requests: list[httpx.Request] = []
        self.refuse_paths: set[str] = set()  # path globs whose claim is refused (held by cc-2)
        self.queue_paths: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []
        self.recall_status = 200

    # -- inspection -------------------------------------------------------------

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path.removeprefix("/api/v1") for r in self.requests if method is None or r.method == method]

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for r in self.requests if r.method == method and r.url.path.endswith(suffix))

    # -- the server -------------------------------------------------------------

    def _is_down(self, path: str) -> bool:
        return bool(self.down) and (not self.down_paths or any(path.endswith(p) for p in self.down_paths))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        if self._is_down(path):
            if self.down == "hang":
                raise httpx.ReadTimeout("The read operation timed out", request=request)
            if self.down == "refused":
                raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
            return httpx.Response(503, text="no available server\n")
        body = json.loads(request.content) if request.content else {}
        if path == "/crews/join":
            return httpx.Response(200, json=JOIN)
        if path == "/crews/crew_1/match":
            return httpx.Response(200, json={"paths": [{"path": p} for p in body.get("paths") or []]})
        if path == "/crews/crew_1/claims" and request.method == "POST":
            glob = str(body.get("path_glob"))
            if glob in self.refuse_paths:
                detail = {
                    "error": "conflict",
                    "message": "held",
                    "blockers": [{"holder_callsign": "cc-2", "reason": "held"}],
                }
                return httpx.Response(409, json={"detail": detail})
            state = "queued" if glob in self.queue_paths else "active"
            claim = {"id": "clm_1", "state": state, "mode": body.get("mode"), "epoch": 1, "path_glob": glob}
            return httpx.Response(201, json={"claim": claim, "blockers": [{"holder_callsign": "cc-2", "reason": "held"}]})
        if path == "/crews/crew_1/claims":
            return httpx.Response(200, json={"claims": []})
        if path == "/crews/crew_1/events":
            events, self.events = self.events, []
            return httpx.Response(200, json={"events": events, "last_seq": 5 + len(events), "has_more": False})
        if path == "/session/close":
            self.closes.append({"body": body, "token": request.headers.get("x-remembra-crew-session")})
            left = bool(request.headers.get("x-remembra-crew-session"))
            return httpx.Response(
                200,
                json={
                    "handoff_id": f"hnd_{len(self.closes)}",
                    "project_id": body.get("project_id") or "outageproj",
                    "changed": True,
                    "headline": "handoff stored",
                    "crew": {"session_left": left, "claims_released": [], "claims_reserved": [], "tasks_stalled": []},
                },
            )
        if path == "/memories/recall":
            if self.recall_status != 200:
                return httpx.Response(self.recall_status, json={"detail": "Invalid request: bad filter"})
            return httpx.Response(200, json={"context": "", "memories": [], "entities": []})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": server.__version__})
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok", "degraded_components": [], "components": {}})
        return httpx.Response(404, json={"detail": "Not Found"})


def notice_event(title: str = "codex-2 asks you to release pos", kind: str = "mention") -> dict[str, Any]:
    item = {"id": "item_1", "audience": "session", "recipient": "cs_1", "kind": kind, "title": title, "priority": 2}
    return {"type": "inbox.item_created", "payload": {"item": item}}


def mock_client(api: Any, *, agent_id: str = "codex", project: str = "outageproj", session: str = "sess-outage") -> Memory:
    client = Memory(
        base_url="https://remembra.test",
        api_key="test-key",
        agent_id=agent_id,
        session_id=session,
        project=project,
        provenance_source="mcp",
    )
    client._client = httpx.Client(transport=httpx.MockTransport(api), headers=client._headers, timeout=client.timeout)
    return client


def call_result(name: str, **args: Any) -> types.CallToolResult:
    """The tool's result as an MCP client receives it: through the low-level ``tools/call`` handler."""
    handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args))
    result = asyncio.run(handler(request)).root
    assert isinstance(result, types.CallToolResult), result
    return result


def text_of(result: types.CallToolResult) -> str:
    assert len(result.content) == 1 and isinstance(result.content[0], types.TextContent), result.content
    return result.content[0].text


def call(name: str, **args: Any) -> str:
    return text_of(call_result(name, **args))
