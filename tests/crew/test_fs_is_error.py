"""F23: a failed tool call is flagged ``isError`` in the MCP result, with the same text as before.

Every failure used to come back as an ordinary string from a ``-> str`` tool, which the MCP SDK
stamps ``isError=false``: a refused claim, an outage, a rejected argument and a server error all
looked like successful calls to anything that reads the protocol flag. Each test goes through the
low-level ``tools/call`` handler (what an MCP client talks to) into the real MCP server module,
against a scripted API (``tests/crew/fs_outage_support.py``).
"""

from __future__ import annotations

import json

import pytest

import remembra.mcp.server as server
from remembra.mcp.crew import CrewBridge
from tests.crew.fs_outage_support import FakeApi, call_result, mock_client, notice_event, text_of


@pytest.fixture()
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", mock_client(fake))
    yield fake
    server._client = None


def sent_once(result) -> str:
    """The result's text, which is all of it: one text block and no ``structuredContent`` copy (F47: no tool
    advertises an output schema any more, so there is no ``{"result": text}`` wrapper to satisfy)."""
    text = text_of(result)
    assert [block.type for block in result.content] == ["text"], result.content
    assert result.structuredContent is None
    return text


def failed(result) -> str:
    """The text of a result that must be flagged as an error, sent once like a successful one."""
    text = sent_once(result)
    assert result.isError is True, text
    return text


def succeeded(result) -> str:
    text = sent_once(result)
    assert result.isError is False, text
    return text


# ---------------------------------------------------------------------------
# Crew tools
# ---------------------------------------------------------------------------


def test_rejected_arguments_are_an_error(api):
    assert failed(call_result("crew_claim", zone="pos", wait_s=301)).startswith("INVALID ARGUMENTS: wait_s")
    assert failed(call_result("crew_say", body="hi", wait_s=121)).startswith("INVALID ARGUMENTS: wait_s")
    assert api.requests == []


def test_a_refused_claim_is_an_error(api):
    api.refuse_paths = {"src/app.py"}
    text = failed(call_result("crew_claim", paths=["src/app.py"]))
    assert text.startswith("REFUSED: path src/app.py held EXCLUSIVELY by cc-2"), text


def test_a_partly_refused_claim_is_an_error_and_still_lists_what_was_granted(api):
    api.refuse_paths = {"src/b.py"}
    text = failed(call_result("crew_claim", paths=["src/a.py", "src/b.py"]))
    assert text.splitlines()[0].startswith("GRANTED path src/a.py"), text
    assert text.splitlines()[1].startswith("REFUSED: path src/b.py"), text


def test_a_claim_that_met_an_outage_is_an_error(api):
    succeeded(call_result("crew_status", project_id="outageproj"))
    api.down = "503"
    text = failed(call_result("crew_claim", paths=["src/app.py"]))
    assert text.startswith("CREW UNAVAILABLE: "), text


def test_crew_status_failures_are_errors_too(api):
    api.down = "503"
    assert failed(call_result("crew_status", project_id="outageproj")).startswith("CREW UNAVAILABLE: ")
    server._client = mock_client(api, agent_id="")
    api.down = None
    assert failed(call_result("crew_status", project_id="outageproj")).startswith("CREW: Crew mode needs this agent's id")


def test_granted_queued_and_nothing_to_release_are_not_errors(api):
    assert succeeded(call_result("crew_claim", paths=["src/a.py"])).startswith("GRANTED path src/a.py")
    api.queue_paths = {"src/q.py"}
    assert succeeded(call_result("crew_claim", paths=["src/q.py"])).startswith("QUEUED path src/q.py")
    assert succeeded(call_result("crew_claim", action="release", paths=["src/none.py"])).startswith("NOTHING RELEASED")
    assert "CREW " in succeeded(call_result("crew_status", project_id="outageproj"))


# ---------------------------------------------------------------------------
# Memory and inbox tools
# ---------------------------------------------------------------------------


def test_a_server_error_on_a_memory_tool_is_an_error(api):
    api.down = "503"
    out = json.loads(failed(call_result("recall_memories", query="deploy")))
    assert out["status"] == "error" and out["code"] == 503, out


def test_a_refused_request_on_a_memory_tool_is_an_error(api):
    out = json.loads(failed(call_result("store_memory", content="x", memory_type="nonsense")))
    assert out["status"] == "error" and "memory_type must be one of" in out["error"]
    out = json.loads(failed(call_result("recall_memories", query="")))
    assert out["status"] == "error", out
    assert api.requests == []


def test_an_inbox_failure_is_an_error(api):
    server._client = mock_client(api, agent_id="")
    out = json.loads(failed(call_result("get_inbox")))
    assert out["ok"] is False, out


def test_a_successful_memory_call_is_not_an_error(api):
    assert "memories" in succeeded(call_result("recall_memories", query="deploy"))


def test_health_check_reporting_a_dead_server_has_itself_succeeded(api):
    api.down = "refused"
    out = json.loads(succeeded(call_result("health_check")))
    assert out["status"] == "error" and out["server"], out


def test_a_failed_call_still_carries_the_crew_notice(api):
    succeeded(call_result("crew_status", project_id="outageproj"))
    api.events = [notice_event("codex-2 asks you to release pos", kind="collision_notice")]
    api.recall_status = 400
    out = json.loads(failed(call_result("recall_memories", query="deploy")))
    assert out["status"] == "error" and out["code"] == 400
    assert out["crew_notice"].startswith("CREW (codex-1): codex-2 asks you to release pos"), out
