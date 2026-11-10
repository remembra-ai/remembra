"""F14 (the call chain): during an outage a tool makes one failing request, not three.

A crew tool chained the liveness touch, its own request(s) and the crew-notice poll, each with its
own 30 s timeout and with failures of the first and the last swallowed: 60 to 90 s per tool call
while the server hung, long enough for the host to cut the call off. A claim of several paths tried
every path in turn. Now the first request that meets an outage ends the call, and its answer says
whether the call itself was carried out. Through the low-level ``tools/call`` handler into the real
MCP server module, against the scripted API in ``tests/crew/fs_outage_support.py``.
"""

from __future__ import annotations

import pytest

import remembra.mcp.server as server
from remembra.mcp.crew import NOT_CARRIED_OUT, REPEAT_SAFE, TOUCH_INTERVAL_S, CrewBridge
from tests.crew.fs_outage_support import Clock, FakeApi, call, call_result, mock_client, notice_event, text_of


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def api(monkeypatch, clock):
    fake = FakeApi()
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock))
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", mock_client(fake))
    yield fake
    server._client = None


def seated(api: FakeApi) -> None:
    assert "YOU: codex-1" in call("crew_status", project_id="outageproj")
    api.requests.clear()


def test_a_crew_call_ends_when_its_liveness_touch_meets_an_outage(api, clock):
    seated(api)
    clock.advance(TOUCH_INTERVAL_S + 1)  # the touch is due
    api.down = "hang"
    out = call("crew_claim", paths=["src/app.py"])
    assert out == f"CREW UNAVAILABLE: The Remembra server did not answer in time. {NOT_CARRIED_OUT}", out
    assert api.paths() == ["/crews/join"], api.paths()  # the touch, and nothing after it


def test_a_touch_the_server_refuses_does_not_stop_the_call(api, clock):
    seated(api)
    clock.advance(TOUCH_INTERVAL_S + 1)
    api.rejoin_status = 409  # an answer, not an outage: liveness stays best effort
    assert call("crew_claim", paths=["src/app.py"]).startswith("GRANTED path src/app.py")


def test_a_crew_write_that_hangs_is_one_request_and_says_the_outcome_is_unknown(api):
    seated(api)
    api.down = "hang"
    out = call("crew_checkpoint", files_changed=["src/app.py"], summary="tests pass")
    # unknown, and (a checkpoint carries an idempotency key) safe to repeat: tests/crew/test_fs_outage_review.py
    assert out == f"CREW UNAVAILABLE: The Remembra server did not answer in time. {REPEAT_SAFE}", out
    assert REPEAT_SAFE.startswith("Whether this call was carried out is not known.")
    assert api.paths() == ["/crews/crew_1/checkpoints"], api.paths()  # no notice poll behind it


def test_a_crew_write_refused_by_the_proxy_says_it_was_not_carried_out(api):
    seated(api)
    api.down = "503"
    out = call("crew_checkpoint", files_changed=["src/app.py"])
    assert out.endswith(NOT_CARRIED_OUT) and "HTTP 503" in out, out
    assert api.paths() == ["/crews/crew_1/checkpoints"], api.paths()


def test_a_memory_tool_that_meets_an_outage_is_not_followed_by_the_notice_poll(api):
    seated(api)
    api.down = "hang"
    result = call_result("recall_memories", query="deploy")
    assert result.isError is True and '"timed_out": true' in text_of(result)
    assert api.paths() == ["/memories/recall"], api.paths()


def test_a_failure_that_is_not_an_outage_still_gets_the_notice_poll(api):
    seated(api)
    api.events = [notice_event(kind="collision_notice")]
    api.recall_status = 400
    assert "crew_notice" in call("recall_memories", query="deploy")
    assert api.paths() == ["/memories/recall", "/crews/crew_1/events"], api.paths()


def test_a_claim_of_several_paths_stops_at_the_first_outage(api):
    seated(api)
    api.down, api.down_paths = "hang", ("/claims",)
    result = call_result("crew_claim", paths=["src/a.py", "src/b.py", "src/c.py"])
    lines = text_of(result).splitlines()
    assert result.isError is True
    assert lines[0].startswith("CREW UNAVAILABLE: The Remembra server did not answer in time."), lines
    assert lines[1:] == [
        "NOT TRIED: path src/b.py (the server is not answering)",
        "NOT TRIED: path src/c.py (the server is not answering)",
        "(in CREW outageproj as codex-1)",  # every crew write's answer names its crew (F6)
    ], lines
    assert api.count("POST", "/claims") == 1, api.paths()


def test_a_refused_path_does_not_stop_the_other_paths(api):
    seated(api)
    api.refuse_paths = {"src/a.py"}
    lines = call("crew_claim", paths=["src/a.py", "src/b.py"]).splitlines()
    assert lines[0].startswith("REFUSED: path src/a.py") and lines[1].startswith("GRANTED path src/b.py"), lines
