"""F21: a gateway 503 is an outage, not "crew mode is not enabled", and crew tools answer as crew text.

``error_text`` used to map every HTTP 503 to the feature-off sentence, so a reverse proxy with no
backend ("no available server") told agents that crew mode was switched off. ``crew_status`` with a
location resolved the project through the SDK client and returned a raw JSON envelope for the same
outage. Unit tests use synthetic ``httpx.Response`` objects; tool tests go through
``FastMCP.call_tool`` into the real MCP server module with an ``httpx.MockTransport`` (no network).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.mcp.crew import CrewApiError, CrewBridge, _error_from, error_text

NOT_ENABLED = "Crew mode is not enabled"


def _response(status: int, *, text: str | None = None, body: Any = None) -> httpx.Response:
    request = httpx.Request("POST", "https://remembra.test/api/v1/crews/c1/claims")
    if body is not None:
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, text=text or "", request=request)


def _text_of(out: Any) -> str:
    if isinstance(out, tuple):
        out = out[0]
    blocks = getattr(out, "content", out)
    return str(blocks[0].text)


def call(name: str, **args: Any) -> str:
    return _text_of(asyncio.run(server.mcp.call_tool(name, args)))


def mock_client(handler: Any, *, agent_id: str = "codex", project: str = "outageproj") -> Memory:
    client = Memory(
        base_url="https://remembra.test",
        api_key="test-key",
        agent_id=agent_id,
        session_id="sess-outage",
        project=project,
        provenance_source="mcp",
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler), headers=client._headers, timeout=client.timeout)
    return client


@pytest.fixture()
def fresh(monkeypatch):
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield
    server._client = None


JOIN = {
    "crew_id": "crew_1",
    "session_id": "cs_1",
    "session_token": "tok-1",
    "callsign": "codex-1",
    "seq": 5,
    "session": {"agent_verified": True, "current_task_id": None},
}


def joined_then(answer: Any) -> Any:
    """A server where the crew join works and every later request gets ``answer(request)``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/crews/join":
            return httpx.Response(200, json=JOIN)
        return answer(request)

    return handler


# ---------------------------------------------------------------------------
# error_text
# ---------------------------------------------------------------------------


def test_a_proxy_503_is_reported_as_an_outage_not_as_crew_mode_off():
    text = error_text(_error_from(_response(503, text="no available server\n")), "crew")
    assert text.startswith("CREW UNAVAILABLE: "), text
    assert NOT_ENABLED not in text, text
    assert "HTTP 503" in text and "no available server" in text, text


def test_the_genuine_crew_off_answer_still_says_crew_mode_is_not_enabled():
    off = _response(503, body={"detail": {"error": "crew_unavailable", "message": "Crew mode is not enabled."}})
    assert error_text(_error_from(off), "crew") == "CREW UNAVAILABLE: Crew mode is not enabled on this Remembra server."
    # an older server without the crew routes answers the framework's bare 404
    missing = _response(404, body={"detail": "Not Found"})
    assert error_text(_error_from(missing), "crew") == "CREW UNAVAILABLE: Crew mode is not enabled on this Remembra server."


@pytest.mark.parametrize(
    "response",
    [
        _response(503, body={"detail": {"error": "db_locked", "message": "The database is busy."}}),
        _response(503, body={"detail": "Service temporarily unavailable"}),
        _response(503, text="<html><body>503 Service Temporarily Unavailable</body></html>"),
    ],
)
def test_an_app_503_with_another_cause_is_not_reported_as_crew_mode_off(response):
    text = error_text(_error_from(response), "crew")
    assert text.startswith("CREW UNAVAILABLE: ") and NOT_ENABLED not in text and "HTTP 503" in text, text


@pytest.mark.parametrize("status", [502, 504, 520, 521, 522, 523, 524])
def test_other_gateway_answers_read_as_an_outage_with_their_status(status):
    text = error_text(_error_from(_response(status, text="upstream gone")), "crew")
    assert text.startswith("CREW UNAVAILABLE: ") and f"HTTP {status}" in text and NOT_ENABLED not in text, text


def test_a_503_relayed_by_the_local_daemon_has_no_dangling_colon():
    # crewd's api op reports a non-JSON 503 as error "503" with an empty message
    text = error_text(CrewApiError(503, "503", ""), "crew")
    assert "HTTP 503." in text and "HTTP 503:" not in text and NOT_ENABLED not in text, text
    # and an unreachable server as status 0 with no message at all
    blank = error_text(CrewApiError(0, "unreachable", ""), "crew")
    assert blank.startswith("CREW UNAVAILABLE: The Remembra server could not be reached"), blank


def test_errors_that_are_not_outages_keep_their_text():
    assert error_text(CrewApiError(429, "rate_limited", "slow down", {"retry_after_s": 7}), "say") == (
        "RATE LIMITED (say): retry in 7s."
    )
    assert error_text(CrewApiError(409, "stale", "Version changed."), "task") == "CREW ERROR (task: 409 stale): Version changed."
    assert error_text(CrewApiError(0, "timeout", "The Remembra server did not answer in time."), "crew").startswith(
        "CREW UNAVAILABLE: The Remembra server did not answer in time."
    )


# ---------------------------------------------------------------------------
# Through the MCP tools
# ---------------------------------------------------------------------------


def test_crew_claim_during_a_proxy_outage_says_outage(fresh):
    server._client = mock_client(joined_then(lambda r: httpx.Response(503, text="no available server\n")))
    out = call("crew_claim", paths=["src/app.py"])
    assert out.startswith("CREW UNAVAILABLE: "), out
    assert NOT_ENABLED not in out and "HTTP 503" in out, out


def test_crew_status_with_a_location_answers_crew_text_during_an_outage(fresh, tmp_path):
    server._client = mock_client(lambda r: httpx.Response(503, text="no available server\n"))
    out = call("crew_status", root_path=str(tmp_path))
    assert out.startswith("CREW UNAVAILABLE: "), out
    assert NOT_ENABLED not in out and "HTTP 503" in out and "no available server" in out, out
    with pytest.raises(ValueError):
        json.loads(out)


def test_crew_status_with_a_location_answers_crew_text_when_the_server_hangs(fresh, tmp_path):
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    server._client = mock_client(hang)
    out = call("crew_status", root_path=str(tmp_path))
    assert out.startswith("CREW UNAVAILABLE: The Remembra server did not answer in time."), out


def test_crew_status_with_a_location_answers_crew_text_when_the_server_is_unreachable(fresh, tmp_path):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 61] Connection refused", request=request)

    server._client = mock_client(refuse)
    out = call("crew_status", root_path=str(tmp_path))
    assert out.startswith("CREW UNAVAILABLE: "), out
    assert "unreachable" in out or "could not be reached" in out, out


def test_a_rejected_key_on_the_project_lookup_keeps_its_own_error(fresh, tmp_path):
    # not an outage: the SDK client's error envelope is unchanged
    server._client = mock_client(lambda r: httpx.Response(401, json={"detail": "Invalid API key"}))
    out = json.loads(call("crew_status", root_path=str(tmp_path)))
    assert out == {"status": "error", "error": "Request failed: Invalid API key", "code": 401}
