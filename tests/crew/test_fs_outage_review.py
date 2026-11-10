"""F14, F21, F56 (second pass): what the first fix of the outage faults left open.

* A seated ``close_session`` that met a hang still waited out a second timeout (the crew-notice touch)
  after it had queued the handoff, long enough for the host to cut the call off before the receipt.
* The next session's opening ``session_brief`` went out before the queued handoff was delivered.
* A crew write whose outcome is unknown could not be repeated safely: no ``Idempotency-Key``.
* A read that hung was told to check whether it had been carried out.
* The inbox tools' timeout envelope had no ``timed_out`` flag.
* A sync tool (``store_memory``, ``close_session``, ``session_brief``) that hung blocked the stdio
  server's event loop, so every other tool call of that agent waited behind it.

Through the low-level ``tools/call`` handler into the real MCP server module, against the scripted
API in ``tests/crew/fs_outage_support.py`` (no network) and a temporary home.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import types

import remembra.mcp.crew as crew_mod
import remembra.mcp.server as server
from remembra.mcp import close_queue
from remembra.mcp.crew import NOT_CARRIED_OUT, OUTCOME_UNKNOWN, TOUCH_INTERVAL_S, CrewBridge
from remembra.relay import outbox
from tests.crew.fs_outage_support import Clock, FakeApi, call, call_result, mock_client, text_of

IDEM = "idempotency-key"


class Api(FakeApi):
    """The scripted API plus the brief, the inbox, messages and the task list."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if self._is_down(path):
            return super().__call__(request)
        if path == "/session/brief":
            self.requests.append(request)
            brief = {"project_id": "outageproj", "agent_id": "codex", "rendered": "BRIEF", "warnings": []}
            return httpx.Response(200, json=brief)
        if path == "/inbox":
            self.requests.append(request)
            return httpx.Response(200, json=[])
        if path == "/crews/crew_1/messages":
            self.requests.append(request)
            body = json.loads(request.content)
            return httpx.Response(201, json={"message": {"id": "msg_1", "client_msg_id": body["client_msg_id"]}, "seq": 11})
        if path == "/crews/crew_1/tasks" and request.method == "GET":
            self.requests.append(request)
            return httpx.Response(200, json={"tasks": []})
        return super().__call__(request)

    def sent(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("REMEMBRA_API_KEY", "REMEMBRA_URL", "REMEMBRA_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture()
def api(monkeypatch, clock, home):
    fake = Api()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", mock_client(fake))
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", home)
    monkeypatch.setattr(server, "_queued_close_seats", {})
    monkeypatch.setattr(server, "_close_queue_checked_at", 0.0)
    # the background thread never runs on its own here: a test that wants it installs its own
    drainer = close_queue.Drainer(delays=())
    monkeypatch.setattr(drainer, "kick", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_close_drainer", drainer)
    yield fake
    server._client = None


def seated(api: Api) -> None:
    assert "YOU: codex-1" in call("crew_status", project_id="outageproj")
    api.requests.clear()


def queue_a_close() -> str:
    out = json.loads(call("close_session", summary="previous session", next_step="n", project_id="outageproj"))
    return str(out["status"])


def fresh_process(api: Api, monkeypatch, *, delays: tuple[float, ...] = (30.0,)) -> close_queue.Drainer:
    """A new MCP server of the same agent on this machine: no seat, a real drainer that has not run."""
    drainer = close_queue.Drainer(delays=delays)
    monkeypatch.setattr(server, "_close_drainer", drainer)
    monkeypatch.setattr(server, "_queued_close_seats", {})
    monkeypatch.setattr(server, "_close_queue_checked_at", 0.0)
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_client", mock_client(api, session="sess-next"))
    api.requests.clear()
    return drainer


# ---------------------------------------------------------------------------
# F14: the queued close is one request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("down", ["hang", "503"])
def test_a_seated_close_that_is_queued_makes_one_request_even_when_the_touch_is_due(api, clock, home, down):
    seated(api)
    clock.advance(TOUCH_INTERVAL_S * 2)  # the liveness touch and the notice poll are due, as at the end of a session
    api.down = down
    result = call_result("close_session", summary="Batch 8 is live", next_step="watch the logs")
    out = json.loads(text_of(result))
    assert out["status"] == "queued" and result.isError is False
    assert api.paths() == ["/session/close"], api.paths()  # no touch, no events poll behind it
    assert len(outbox.pending(home)) == 1


# ---------------------------------------------------------------------------
# F14: the next session's brief comes after the queued handoff
# ---------------------------------------------------------------------------


def test_the_opening_brief_of_the_next_session_is_asked_after_the_queued_close_is_delivered(api, home, monkeypatch):
    api.down = "503"
    assert queue_a_close() == "queued"
    api.down = None
    drainer = fresh_process(api, monkeypatch)
    out = json.loads(call("session_brief", project_id="outageproj", compact=True))
    assert api.paths() == ["/session/close", "/session/brief"], api.paths()  # in this order, before the brief returned
    assert out["status"] == "ok" and out["brief"] == "BRIEF"
    assert not any("queued" in w for w in out["warnings"]), out["warnings"]
    assert outbox.pending(home) == [] and not drainer.running()
    [sent] = api.closes
    assert sent["body"]["session_id"] == "sess-outage"


@pytest.mark.parametrize("compact", [True, False])
def test_a_brief_says_when_a_handoff_of_this_machine_is_still_queued(api, home, monkeypatch, compact):
    api.down = "503"
    assert queue_a_close() == "queued"
    api.down_paths = ("/session/close",)  # the brief is answered, the close still is not
    drainer = fresh_process(api, monkeypatch)
    text = call("session_brief", project_id="outageproj", compact=compact)
    assert api.paths() == ["/session/close", "/session/brief"], api.paths()
    assert "1 closing handoff from this machine is still queued" in text, text
    assert "BRIEF" in text and len(outbox.pending(home)) == 1
    assert drainer.running()  # and the background thread keeps trying
    drainer.stop()
    drainer.join(5)


def test_a_brief_with_nothing_queued_asks_only_for_the_brief(api, home, monkeypatch):
    fresh_process(api, monkeypatch)
    out = json.loads(call("session_brief", project_id="outageproj", compact=True))
    assert api.paths() == ["/session/brief"] and out["warnings"] == []


def test_a_brief_does_not_wait_the_full_timeout_for_a_queued_close(api, home, monkeypatch):
    api.down = "503"
    call("close_session", summary="previous session", next_step="n", project_id="outageproj")
    api.down_paths = ("/session/close",)
    drainer = fresh_process(api, monkeypatch)
    waits: list[Any] = []
    real = crew_mod.CrewHttp.call

    def spy(self, method, path, **kw):
        if path == "/session/close":
            waits.append(kw.get("timeout"))
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", spy)
    call("session_brief", project_id="outageproj", compact=True)
    drainer.stop()
    drainer.join(5)
    assert waits and waits[0] is not None and waits[0] <= 10.0, waits  # not the client's 30 s


# ---------------------------------------------------------------------------
# F14: a crew write can be repeated safely
# ---------------------------------------------------------------------------

REPEAT_SAFE = "Repeating the same call is safe"


def test_a_crew_write_carries_an_idempotency_key_and_its_repeat_after_an_outage_the_same_one(api):
    seated(api)
    api.down = "hang"
    out = call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass")
    assert out.startswith("CREW UNAVAILABLE: The Remembra server did not answer in time.") and REPEAT_SAFE in out, out
    assert "check (crew_status)" not in out
    api.down = None
    assert call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass").startswith("CHECKPOINT ckp_1 recorded")
    first, second = api.sent("/checkpoints")
    assert first.headers[IDEM] and first.headers[IDEM] == second.headers[IDEM]
    assert len(first.headers[IDEM]) <= 128
    # the server answered: the same call made again later is a new call
    call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass")
    assert api.sent("/checkpoints")[2].headers[IDEM] != first.headers[IDEM]


def test_a_different_call_after_an_outage_gets_its_own_key(api):
    seated(api)
    api.down = "hang"
    call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass")
    api.down = None
    call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass, and lint")
    first, second = api.sent("/checkpoints")
    assert first.headers[IDEM] != second.headers[IDEM]


def test_a_message_repeated_after_an_outage_is_the_same_message_to_the_server(api):
    seated(api)
    api.down = "hang"
    out = call("crew_say", body="Batch 8 is live", to="crew")
    assert REPEAT_SAFE in out, out
    api.down = None
    assert call("crew_say", body="Batch 8 is live", to="crew").startswith("SENT msg_1")
    first, second = (json.loads(r.content) for r in api.sent("/messages"))
    assert first["client_msg_id"].startswith("mcp-") and first["client_msg_id"] == second["client_msg_id"]
    assert first == second
    call("crew_say", body="Batch 8 is live", to="crew")  # said again on purpose: a new message
    assert json.loads(api.sent("/messages")[2].content)["client_msg_id"] != first["client_msg_id"]


def test_a_claim_repeated_after_an_outage_carries_the_same_key_and_a_new_claim_a_new_one(api):
    seated(api)
    api.down = "hang"
    api.down_paths = ("/claims",)
    out = call("crew_claim", paths=["src/app.py"])
    assert REPEAT_SAFE in out, out
    api.down = None
    assert call("crew_claim", paths=["src/app.py"]).startswith("GRANTED path src/app.py")
    assert call("crew_claim", paths=["src/app.py"]).startswith("GRANTED path src/app.py")
    one, two, three = (r.headers[IDEM] for r in api.sent("/claims") if r.method == "POST")
    assert one == two and three != one


def test_requests_that_change_nothing_carry_no_key(api):
    seated(api)
    call("crew_claim", paths=["src/app.py"])
    call("crew_task", action="list")
    for request in api.requests:
        path = request.url.path
        if request.method == "GET" or path.endswith(("/match", "/crews/join")):
            assert IDEM not in request.headers, path


# ---------------------------------------------------------------------------
# F21: a read that hangs changed nothing
# ---------------------------------------------------------------------------

NOTHING_CHANGED = "Nothing was changed: repeat it once the server answers."


def test_a_read_that_hangs_says_nothing_was_changed(api):
    seated(api)
    api.down = "hang"
    for out in (call("crew_task", action="list"), call("crew_claim", paths=["src/app.py"])):  # GET tasks; POST /match
        assert out == f"CREW UNAVAILABLE: The Remembra server did not answer in time. {NOTHING_CHANGED}", out
    assert api.paths() == ["/crews/crew_1/tasks", "/crews/crew_1/match"]


def test_a_crew_status_that_hangs_does_not_send_the_agent_to_crew_status(api):
    api.down = "hang"
    out = call("crew_status", project_id="outageproj")
    assert out.startswith("CREW UNAVAILABLE: The Remembra server did not answer in time.")
    assert "crew_status" not in out and REPEAT_SAFE in out, out


def test_a_guard_that_hangs_keeps_the_unknown_outcome(api):
    # the guard may claim for the caller (auto_claimed), so it is not a read
    seated(api)
    api.down = "hang"
    out = call("crew_guard", paths=["src/app.py"])
    assert out == f"CREW UNAVAILABLE: The Remembra server did not answer in time. {OUTCOME_UNKNOWN}", out


def test_a_refused_connection_is_still_not_carried_out(api):
    seated(api)
    api.down = "refused"
    out = call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass")
    assert out.endswith(NOT_CARRIED_OUT), out


# ---------------------------------------------------------------------------
# F56: the inbox tools' timeout envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_inbox", {}),
        ("send_to_inbox", {"to_agent": "claude-code", "subject": "deploy", "body": "the deploy is live"}),
        ("ack_inbox", {"inbox_id": "inb_1"}),
    ],
)
def test_an_inbox_call_that_gets_no_answer_says_timed_out(api, tool, args):
    api.down = "hang"
    result = call_result(tool, **args)
    out = json.loads(text_of(result))
    assert result.isError is True
    assert out["ok"] is False and out["code"] is None and out["timed_out"] is True, out


def test_an_inbox_error_the_server_reported_has_no_timed_out_flag(api):
    api.down = "503"
    out = json.loads(call("get_inbox"))
    assert out["ok"] is False and out["code"] == 503 and "timed_out" not in out


# ---------------------------------------------------------------------------
# F14: a sync tool that hangs does not block the server's event loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("store_memory", {"content": "the deploy is live"}),
        ("session_brief", {"project_id": "outageproj"}),
        ("close_session", {"summary": "done", "next_step": "n"}),
    ],
)
def test_a_hanging_sync_tool_leaves_the_event_loop_free(api, monkeypatch, tool, args):
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", None)
    loop_ran = threading.Event()
    seen: list[bool] = []

    def hang(request: httpx.Request) -> httpx.Response:
        seen.append(loop_ran.wait(5))  # the server "hangs" until the event loop got a turn (or 5 s)
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    client = server._client
    client._client = httpx.Client(transport=httpx.MockTransport(hang), headers=client._headers)
    handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=tool, arguments=args))

    async def run() -> types.CallToolResult:
        async def other_work() -> None:
            await asyncio.sleep(0.05)
            loop_ran.set()

        other = asyncio.create_task(other_work())
        result = (await handler(request)).root
        await other
        return result

    result = asyncio.run(run())
    assert seen == [True], "the event loop was blocked while the tool waited for the server"
    assert result.isError is True and json.loads(text_of(result))["timed_out"] is True


# ---------------------------------------------------------------------------
# F56: the timeout error is importable as the SDK guide says
# ---------------------------------------------------------------------------


def test_the_timeout_error_is_a_top_level_export_and_the_guide_names_the_change():
    import remembra
    from remembra.client.memory import MemoryError as SdkError

    assert issubclass(remembra.MemoryTimeoutError, SdkError) and "MemoryTimeoutError" in remembra.__all__
    assert not issubclass(remembra.MemoryTimeoutError, httpx.TimeoutException)
    guide = (Path(__file__).resolve().parents[2] / "docs" / "guides" / "python-sdk.md").read_text()
    assert "from remembra import MemoryTimeoutError" in guide and "httpx.TimeoutException" in guide
