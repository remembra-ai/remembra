"""F14 (the closing handoff): a close the server did not take is queued and delivered, not dropped.

``close_session`` was one request with no retry and no queue: during an outage the handoff was lost
and the agent could not tell. Now a stdio MCP server queues it in the relay outbox of its machine
and delivers it itself (background thread, or the next MCP server of this agent on its first tool
call); a relay hook may deliver it too, with the same key only.

Everything runs against a scripted API (``tests/crew/fs_outage_support.py``, no network) and a
temporary home; the tools are called through the low-level ``tools/call`` handler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.mcp.crew import CrewBridge
from remembra.relay import outbox
from tests.crew.fs_outage_support import FakeApi, call, call_result, mock_client, text_of


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("REMEMBRA_API_KEY", "REMEMBRA_URL", "REMEMBRA_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture()
def api(monkeypatch, home):
    from remembra.mcp import close_queue

    fake = FakeApi()
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", mock_client(fake))
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", home)
    monkeypatch.setattr(server, "_queued_close_seats", {})
    monkeypatch.setattr(server, "_close_queue_checked_at", 0.0)
    # the background thread never runs on its own here: each test delivers when it wants to
    drainer = close_queue.Drainer(delays=())
    monkeypatch.setattr(drainer, "kick", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_close_drainer", drainer)
    yield fake
    server._client = None


def entries(home: Path) -> list[outbox.Entry]:
    return outbox.pending(home)


def close(**args: Any) -> dict[str, Any]:
    result = call_result("close_session", **{"summary": "Batch 8 is live", "next_step": "watch the logs", **args})
    out = json.loads(text_of(result))
    out["_is_error"] = result.isError
    return out


def deliver(home: Path) -> tuple[int, int]:
    from remembra.mcp import close_queue

    return close_queue.drain(
        home, server._client, token_for=server._queued_close_token, on_delivered=server._queued_close_delivered
    )


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("down", "code"), [("503", 503), ("hang", None), ("refused", None)])
def test_a_close_the_server_did_not_take_is_queued_and_says_so(api, home, down, code):
    api.down = down
    out = close(project_id="outageproj")
    assert out["status"] == "queued" and out["delivered"] is False and out["code"] == code, out
    assert out["_is_error"] is False
    assert "NOT visible to other agents yet" in out["note"] and "sent again automatically" in out["note"]
    assert api.count("POST", "/session/close") == 1  # one attempt, then the queue
    [entry] = entries(home)
    assert (entry.agent_id, entry.session_id) == ("codex", "sess-outage")
    assert entry.payload["summary"] == "Batch 8 is live" and entry.payload["project_id"] == "outageproj"
    assert entry.payload["facts"]["next_step"] == "watch the logs"
    assert entry.payload["closed_at"]  # a late delivery keeps the time the session ended
    assert entry.data["config_source"] == "mcp" and entry.data["key_fp"] == outbox.key_mark("test-key")
    assert entry.url == "https://remembra.test"
    raw = entry.path.read_text()
    assert "test-key" not in raw and "tok-1" not in raw  # never the key, never the crew token
    assert entry.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_a_close_the_server_rejects_is_an_error_and_is_not_queued(api, home, status):
    api.close_status = status
    out = close(project_id="outageproj")
    assert out["status"] == "error" and out["code"] == status and out["_is_error"] is True, out
    assert entries(home) == []


def test_without_a_local_queue_the_close_fails_as_before(api, home, monkeypatch):
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", None)  # a remote transport, or in-process
    api.down = "503"
    out = close(project_id="outageproj")
    assert out["status"] == "error" and out["code"] == 503 and out["_is_error"] is True, out
    assert not (home / ".remembra").exists()


def test_a_close_without_an_agent_id_is_not_queued(api, home, monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "")
    monkeypatch.setattr(server, "_client", mock_client(api, agent_id=""))
    api.down = "503"
    assert close(project_id="outageproj")["status"] == "error"
    assert entries(home) == []


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_the_queued_close_is_delivered_once_the_server_answers(api, home):
    api.down = "503"
    assert close(project_id="outageproj")["status"] == "queued"
    assert deliver(home) == (0, 1)  # still down: kept
    [entry] = entries(home)
    assert entry.attempts == 2 and entry.data["last_status"] == 503
    api.down = None
    assert deliver(home) == (1, 0)
    assert entries(home) == []
    [sent] = api.closes
    assert sent["body"]["summary"] == "Batch 8 is live" and sent["body"]["session_id"] == "sess-outage"
    assert sent["body"]["closed_at"]
    assert deliver(home) == (0, 0)  # nothing is sent twice
    assert len(api.closes) == 1


def test_a_seated_close_keeps_the_crew_seat_until_delivery_and_then_ends_the_crew_session(api, home):
    assert "YOU: codex-1" in call("crew_status", project_id="outageproj")
    api.down = "hang"
    out = close()
    assert out["status"] == "queued" and out["crew"]["session_left"] is False, out
    seat = server._crew.seat_for_scope(server._crew_caller())
    assert seat is not None and seat.left is False
    api.down = None
    assert deliver(home) == (1, 0)
    [sent] = api.closes
    assert sent["token"] == "tok-1"  # the delivery carried the crew session token (never written to disk)
    assert seat.left is True and server._queued_close_seats == {}


def test_closing_again_successfully_supersedes_the_queued_close(api, home):
    api.down = "503"
    assert close(project_id="outageproj")["status"] == "queued"
    api.down = None
    out = close(project_id="outageproj", summary="Batch 8 is live and verified")
    assert out["status"] == "ok" and out["handoff_id"] == "hnd_1", out
    assert entries(home) == []  # the queued one is gone: nothing older is sent after the newer close
    assert deliver(home) == (0, 0) and len(api.closes) == 1


def test_closing_again_during_the_outage_replaces_the_queued_close(api, home):
    api.down = "503"
    close(project_id="outageproj")
    close(project_id="outageproj", summary="second try")
    [entry] = entries(home)
    assert entry.payload["summary"] == "second try"


def test_only_this_agents_closes_for_this_server_and_key_are_delivered(api, home):
    from remembra.mcp import close_queue

    api.down = "503"
    close(project_id="outageproj")
    api.down = None
    other_key = mock_client(api)
    other_key.api_key = "another-key"
    assert close_queue.drain(home, other_key) == (0, 0)
    other_server = mock_client(api)
    other_server.base_url = "https://elsewhere.test"
    assert close_queue.drain(home, other_server) == (0, 0)
    assert close_queue.drain(home, mock_client(api, agent_id="claude-code")) == (0, 0)
    # a close the relay hook queued (its own config source) is the relay's to send, not the MCP server's
    outbox.enqueue(
        home,
        {"agent_id": "codex", "session_id": "hook-session", "facts": {}},
        url="https://remembra.test",
        config_source="env",
        error="ReadTimeout",
        api_key="test-key",
    )
    assert api.closes == [] and len(entries(home)) == 2
    assert close_queue.drain(home, server._client) == (1, 0)
    assert [e.session_id for e in entries(home)] == ["hook-session"]


def test_a_queued_close_the_server_rejects_for_good_is_dropped_and_a_refused_one_is_kept(api, home):
    api.down = "503"
    close(project_id="outageproj")
    api.down, api.close_status = None, 422
    assert deliver(home) == (0, 0)
    assert entries(home) == [] and "dropped codex session sess-outage" in outbox.log_path(home).read_text()

    api.down, api.close_status = "503", 0
    close(project_id="outageproj")
    api.down, api.close_status = None, 403
    assert deliver(home) == (0, 0)  # refused: kept for the owner, not sent again on its own
    [entry] = entries(home)
    assert entry.data["last_status"] == 403
    api.close_status = 0
    assert deliver(home) == (0, 0) and api.closes == []


def test_the_background_thread_delivers_as_soon_as_the_server_answers(api, home, monkeypatch):
    from remembra.mcp import close_queue

    drainer = close_queue.Drainer(delays=(0.01, 0.01, 0.01))
    monkeypatch.setattr(server, "_close_drainer", drainer)
    api.down = "503"
    assert close(project_id="outageproj")["status"] == "queued"  # starts the thread
    drainer.join(5)
    assert not drainer.running() and len(entries(home)) == 1  # it gave up after its attempts, the entry is kept
    assert entries(home)[0].attempts == 4
    api.down = None
    assert drainer.kick(home, server._client) is True
    drainer.join(5)
    assert entries(home) == [] and len(api.closes) == 1


def test_a_new_mcp_server_delivers_what_the_previous_one_left_on_its_first_tool_call(api, home, monkeypatch):
    from remembra.mcp import close_queue

    api.down = "503"
    assert close(project_id="outageproj")["status"] == "queued"
    api.down = None
    # a fresh process: no seat, no remembered token, a drainer that has not run
    drainer = close_queue.Drainer(delays=())
    monkeypatch.setattr(server, "_close_drainer", drainer)
    monkeypatch.setattr(server, "_queued_close_seats", {})
    monkeypatch.setattr(server, "_close_queue_checked_at", 0.0)
    monkeypatch.setattr(server, "_client", mock_client(api, session="sess-next"))
    assert json.loads(call("health_check"))["status"] == "ok"
    drainer.join(5)
    assert entries(home) == []
    [sent] = api.closes
    assert sent["body"]["session_id"] == "sess-outage" and sent["token"] is None


# ---------------------------------------------------------------------------
# The relay hook and an MCP-queued close
# ---------------------------------------------------------------------------


def test_the_relay_sends_an_mcp_queued_close_only_with_the_key_it_was_queued_with(api, home, monkeypatch):
    from remembra.relay.cli import replay_config

    api.down = "503"
    close(project_id="outageproj")
    [entry] = entries(home)

    monkeypatch.setenv("REMEMBRA_URL", "https://remembra.test")
    monkeypatch.setenv("REMEMBRA_API_KEY", "another-key")
    config, held = replay_config(entry)
    assert config is None and "the MCP server sends it itself" in str(held)

    monkeypatch.setenv("REMEMBRA_API_KEY", "test-key")
    config, held = replay_config(entry)
    assert held is None and config is not None and config.api_key == "test-key"

    monkeypatch.setenv("REMEMBRA_URL", "https://elsewhere.test")
    config, held = replay_config(entry)
    assert config is None and "only sent to the server it was queued for" in " ".join(str(held).split())

    monkeypatch.delenv("REMEMBRA_API_KEY")
    config, held = replay_config(entry)
    assert config is None and held
