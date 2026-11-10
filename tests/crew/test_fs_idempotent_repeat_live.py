"""F14 end to end: a crew write the server carried out but could not answer is recorded once when repeated.

The MCP server module talks to a live uvicorn API (crew routers, a real ``crew.db``;
``tests/crew/mcp_support.py``). The outage is a transport that delivers the request and then loses
the answer (a read timeout), the case in which the agent cannot know whether its call landed. The
repeat is the same tool call made again, as an agent makes it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp.crew import REPEAT_SAFE, CrewBridge
from tests.crew.mcp_support import live_server

OWNER_EMAIL = "owner@example.com"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("mcp-idem-repeat")) as s:
        s.state["owner"] = s.user(OWNER_EMAIL)
        s.state["key"] = s.key(s.state["owner"], "admin")
        yield s


class LostAnswer(httpx.HTTPTransport):
    """The real transport, except that the answer to a request whose path ends in ``lose`` never arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.lose: str | None = None
        self.keys: list[str | None] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        if self.lose and request.url.path.endswith(self.lose):
            self.keys.append(request.headers.get("idempotency-key"))
            response.read()
            response.close()
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        if request.method != "GET" and request.url.path.endswith(("/checkpoints", "/messages", "/claims")):
            self.keys.append(request.headers.get("idempotency-key"))
        return response


def call(name: str, **args: Any) -> str:
    out = asyncio.run(server.mcp.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else getattr(out, "content", out)
    return str(blocks[0].text)


@pytest.fixture()
def session(live, tmp_path, monkeypatch):
    transport = LostAnswer()
    client = Memory(
        base_url=live.url,
        api_key=live.state["key"],
        agent_id="codex",
        session_id=f"sess-{tmp_path.name}",
        project=f"idem-{tmp_path.name}".lower().replace("_", "-"),
        provenance_source="mcp",
    )
    client._client = httpx.Client(transport=transport, headers=client._headers, timeout=client.timeout)
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", None)
    monkeypatch.setattr(server, "_client", client)
    assert "YOU: " in call("crew_status", project_id=client.project)
    crew = live.crew_rows("SELECT id FROM crews WHERE project_id = ?", (client.project,))[0]["id"]
    yield live, transport, crew
    server._client = None


def count(live, table: str, crew: str) -> int:
    return int(live.crew_rows(f"SELECT COUNT(*) AS n FROM {table} WHERE crew_id = ?", (crew,))[0]["n"])  # noqa: S608


def test_a_message_whose_answer_was_lost_is_one_message_after_the_repeat(session):
    live, transport, crew = session
    transport.lose = "/messages"
    out = call("crew_say", body="Batch 8 is live", to="crew")
    assert out.startswith("CREW UNAVAILABLE: The Remembra server did not answer in time.") and REPEAT_SAFE in out, out
    assert count(live, "crew_messages", crew) == 1  # it did land; the agent cannot know
    transport.lose = None
    assert call("crew_say", body="Batch 8 is live", to="crew").startswith("SENT msg_")
    assert count(live, "crew_messages", crew) == 1
    # said again on purpose after the server answered: a second message
    assert call("crew_say", body="Batch 8 is live", to="crew").startswith("SENT msg_")
    assert count(live, "crew_messages", crew) == 2


def test_a_checkpoint_whose_answer_was_lost_is_one_checkpoint_after_the_repeat(session):
    live, transport, crew = session
    args = {"files_changed": ["src/app.py"], "summary": "tests pass", "commits": ["abc1234"]}
    transport.lose = "/checkpoints"
    out = call("crew_checkpoint", **args)
    assert REPEAT_SAFE in out, out
    assert count(live, "crew_checkpoints", crew) == 1
    transport.lose = None
    again = call("crew_checkpoint", **args)
    assert again.startswith("CHECKPOINT ckp_") and "recorded" in again.splitlines()[0], again  # the first answer, replayed
    assert count(live, "crew_checkpoints", crew) == 1
    first, second = transport.keys
    assert first and first == second


def test_a_path_claim_whose_answer_was_lost_is_one_claim_after_the_repeat(session):
    live, transport, crew = session
    transport.lose = "/claims"
    out = call("crew_claim", paths=["src/idem/app.py"])
    assert REPEAT_SAFE in out, out
    assert count(live, "crew_claims", crew) == 1
    transport.lose = None
    again = call("crew_claim", paths=["src/idem/app.py"])
    assert again.startswith("GRANTED path src/idem/app.py"), again
    assert count(live, "crew_claims", crew) == 1
    first, second = transport.keys
    assert first and first == second
