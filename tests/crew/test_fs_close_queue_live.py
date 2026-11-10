"""F14 end to end: a close queued during an outage is delivered to a real server and ends the crew session.

The MCP server module talks to a live uvicorn API (relay routes, crew routers, a real SQLite database
and ``crew.db``; ``tests/crew/mcp_support.py``). The outage is a transport that refuses the close
request while it is "down"; everything else, and the delivery, is the real request path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp import close_queue
from remembra.mcp.crew import CrewBridge
from remembra.relay import outbox
from tests.crew.mcp_support import live_server

OWNER_EMAIL = "owner@example.com"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("mcp-close-queue")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


class Outage(httpx.HTTPTransport):
    """The real transport, except that requests are refused or left unanswered while ``down`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.down: str | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.down == "refused":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if self.down == "hang":
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return super().handle_request(request)


def call(name: str, **args: Any) -> str:
    out = asyncio.run(server.mcp.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else getattr(out, "content", out)
    return str(blocks[0].text)


@pytest.fixture()
def session(live, tmp_path, monkeypatch):
    transport = Outage()
    client = Memory(
        base_url=live.url,
        api_key=live.state["key"],
        agent_id="codex",
        session_id=f"sess-{tmp_path.name}",
        project=f"closeq-{tmp_path.name}".lower().replace("_", "-"),
        provenance_source="mcp",
    )
    client._client = httpx.Client(transport=transport, headers=client._headers, timeout=client.timeout)
    drainer = close_queue.Drainer(delays=())
    monkeypatch.setattr(drainer, "kick", lambda *a, **kw: False)  # the test delivers when it wants to
    monkeypatch.setattr(server, "_close_drainer", drainer)
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_queued_close_seats", {})
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", tmp_path)
    monkeypatch.setattr(server, "_client", client)
    yield client, transport, tmp_path
    server._client = None


def crew_session_state(live, project: str) -> str:
    crew = live.crew_rows("SELECT id FROM crews WHERE project_id = ?", (project,))[0]["id"]
    rows = live.crew_rows("SELECT state FROM crew_sessions WHERE crew_id = ? ORDER BY joined_at DESC", (crew,))
    return str(rows[0]["state"])


@pytest.mark.parametrize("down", ["refused", "hang"])
def test_a_close_queued_during_an_outage_reaches_the_server_and_ends_the_crew_session(live, session, down):
    client, transport, home = session
    project = client.project
    assert "YOU: " in call("crew_status", project_id=project)
    assert crew_session_state(live, project) == "active"

    transport.down = down
    out = json.loads(call("close_session", summary="Batch 8 is live", next_step="watch the logs"))
    assert out["status"] == "queued" and out["delivered"] is False, out
    assert len(outbox.pending(home)) == 1
    assert crew_session_state(live, project) == "active"  # nothing reached the server

    transport.down = None
    delivered, left = close_queue.drain(
        home, client, token_for=server._queued_close_token, on_delivered=server._queued_close_delivered
    )
    assert (delivered, left) == (1, 0)
    assert outbox.pending(home) == []
    assert crew_session_state(live, project) == "ended"  # the delivery carried the session token

    brief = client.session_brief(project_id=project)
    handoff = json.dumps(brief)
    assert "Batch 8 is live" in handoff and "watch the logs" in handoff, handoff[:600]

    # the same session closing again afterwards updates that handoff: one handoff, no duplicate
    again = json.loads(call("close_session", summary="Batch 8 is live", next_step="watch the logs"))
    assert again["status"] == "ok", again
    trail = client.trail(project_id=project)
    handoffs = [i for i in trail["items"] if i["memory_type"] == "handoff"]
    assert len(handoffs) == 1 and handoffs[0]["detail"]["next"] == "watch the logs", trail
