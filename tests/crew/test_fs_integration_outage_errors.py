"""Where the outage fixes (F14, F21, F23, F56) meet the earlier groups' fixes: both have to hold.

* F12 (a milestone close keeps the seat) and F14 (a close the server did not take is queued): a queued
  milestone is delivered without the crew token, so the late delivery does not end the crew session.
* F17 (declared work reaches the handoff, also on a server without a field for it) and F14: a queued
  close carries the declared items in the notes as well, unless the server is known to read ``done``.
* F5/F6 (a crew answer names its crew and project; a stdio server does not guess a project) and F23
  (a failed call is flagged ``isError``): the added lines keep the flag, and the refusals are flagged.
* F2 (an MCP seat beside a crew-hook session takes no claims) and F23: that refusal is a failure.
* F20 (a wait cut short is recovered by the same ``client_msg_id``) and the repeat memory of crew
  writes: one call's own id stands; only a call made again takes the id of its first attempt.

Scripted API (``tests/crew/fs_outage_support.py``, no network), tools called through ``tools/call``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import remembra.mcp.crew as crew_mod
import remembra.mcp.server as server
from remembra.mcp import close_queue
from remembra.mcp.crew import CrewBridge, FailureText, HookShadow
from remembra.relay import outbox
from tests.crew.fs_outage_support import FakeApi, call, call_result, mock_client, text_of


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("REMEMBRA_API_KEY", "REMEMBRA_URL", "REMEMBRA_AGENT_ID", "REMEMBRA_RELAY_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture()
def api(monkeypatch, home):
    fake = FakeApi()
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", mock_client(fake))
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "_CLOSE_QUEUE_HOME", home)
    monkeypatch.setattr(server, "_queued_close_seats", {})
    monkeypatch.setattr(server, "_close_queue_checked_at", 0.0)
    monkeypatch.setattr(crew_mod, "_REPEATS", crew_mod._Repeats())
    drainer = close_queue.Drainer(delays=())
    monkeypatch.setattr(drainer, "kick", lambda *a, **kw: False)  # each test delivers when it wants to
    monkeypatch.setattr(server, "_close_drainer", drainer)
    yield fake
    server._client = None


def close(**args: Any) -> dict[str, Any]:
    result = call_result("close_session", **{"summary": "Batch 8 is live", "next_step": "watch the logs", **args})
    out = json.loads(text_of(result))
    out["_is_error"] = result.isError
    return out


def deliver(home: Path) -> tuple[int, int]:
    return close_queue.drain(
        home, server._client, token_for=server._queued_close_token, on_delivered=server._queued_close_delivered
    )


# ---------------------------------------------------------------------------
# F12 + F14: a queued milestone close
# ---------------------------------------------------------------------------


def test_a_queued_milestone_close_is_delivered_without_the_crew_token_and_keeps_the_seat(api, home):
    assert "YOU: codex-1" in call("crew_status", project_id="outageproj")
    api.down = "hang"
    out = close(final=False)
    assert out["status"] == "queued" and out["_is_error"] is False, out
    assert out["crew"]["session_left"] is False and out["crew"]["note"].startswith("A milestone:"), out
    [entry] = outbox.pending(home)
    assert entry.payload["leave"] is False
    assert server._queued_close_seats == {}  # no token is kept for a close that must not end the crew session
    api.down = None
    assert deliver(home) == (1, 0)
    [sent] = api.closes
    assert sent["token"] is None and sent["body"]["leave"] is False, sent
    seat = server._crew.seat_for_scope(server._crew_caller())
    assert seat is not None and seat.left is False


def test_a_milestone_queued_after_a_final_close_takes_the_token_back(api, home):
    """The queue keeps one close per session: when a milestone replaces a queued final close, the
    delivery of the milestone must not carry the token the final close was queued with."""
    call("crew_status", project_id="outageproj")
    api.down = "hang"
    assert close()["status"] == "queued"
    assert len(server._queued_close_seats) == 1
    assert close(final=False)["status"] == "queued"
    assert server._queued_close_seats == {}
    api.down = None
    assert deliver(home) == (1, 0)
    assert [c["token"] for c in api.closes] == [None]


def test_a_final_close_still_ends_the_crew_session_and_sends_no_leave_field(api, home):
    call("crew_status", project_id="outageproj")
    out = close()
    assert out["status"] == "ok" and out["crew"]["session_left"] is True, out
    [sent] = api.closes
    assert sent["token"] == "tok-1" and "leave" not in sent["body"], sent


# ---------------------------------------------------------------------------
# F17 + F14: declared work in a queued close
# ---------------------------------------------------------------------------


def test_a_queued_close_keeps_declared_work_in_the_notes_for_a_server_not_known_to_read_it(api, home):
    api.down = "503"
    out = close(project_id="outageproj", done=["shipped the importer"], facts={"notes": "see PR 12"})
    assert out["status"] == "queued", out
    [entry] = outbox.pending(home)
    facts = entry.payload["facts"]
    assert facts["done"] == ["shipped the importer"]  # a server that has the field still gets it
    assert facts["notes"].splitlines() == ["Done (declared by the agent): shipped the importer", "see PR 12"]


def test_a_queued_close_for_a_server_seen_to_read_declared_work_sends_it_once(api, home, monkeypatch):
    client = server._client
    server._remember_server_lacks_done(client, False)  # an earlier close answer showed facts_normalized
    try:
        api.down = "503"
        assert close(project_id="outageproj", done=["shipped the importer"])["status"] == "queued"
        [entry] = outbox.pending(home)
        facts = entry.payload["facts"]
        assert facts["done"] == ["shipped the importer"] and "notes" not in facts, facts
    finally:
        server._servers_with_done.discard(client)


def test_a_queued_close_for_a_server_known_to_lack_the_field_keeps_the_items_in_the_notes_only(api, home):
    client = server._client
    server._remember_server_lacks_done(client, True)
    try:
        api.down = "503"
        assert close(project_id="outageproj", done=["shipped the importer"])["status"] == "queued"
        [entry] = outbox.pending(home)
        facts = entry.payload["facts"]
        assert "done" not in facts and facts["notes"] == "Done (declared by the agent): shipped the importer", facts
    finally:
        server._servers_without_done.discard(client)


# ---------------------------------------------------------------------------
# F5 / F6 + F23: the lines added to a crew answer keep the failure flag
# ---------------------------------------------------------------------------


def test_a_failed_crew_write_on_a_stdio_server_is_flagged_with_its_crew_and_project_lines(api, monkeypatch):
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)
    call("crew_status", project_id="outageproj")
    api.refuse_paths = {"src/a.py"}
    result = call_result("crew_claim", paths=["src/a.py"])
    lines = text_of(result).splitlines()
    assert result.isError is True
    assert lines[0].startswith("REFUSED: path src/a.py"), lines
    assert lines[-2:] == ["(in CREW outageproj as codex-1)", "(project outageproj)"], lines


def test_a_granted_claim_on_a_stdio_server_is_not_flagged(api, monkeypatch):
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)
    call("crew_status", project_id="outageproj")
    result = call_result("crew_claim", paths=["src/a.py"])
    assert result.isError is False and text_of(result).startswith("GRANTED path src/a.py"), text_of(result)


def test_a_stdio_close_that_names_no_project_is_a_flagged_error(api, home, monkeypatch):
    monkeypatch.setattr(server, "_STDIO_SERVER", True, raising=False)
    monkeypatch.setattr(server, "_LOCAL_CREW", False, raising=False)
    out = close()
    assert out["status"] == "error" and out["_is_error"] is True, out
    assert out["error"].startswith("No project selected for close_session"), out
    assert api.closes == [] and outbox.pending(home) == []


def test_a_close_refused_for_its_facts_is_a_flagged_error_with_the_expected_shape(api, home):
    api.close_status = 422
    out = close(project_id="outageproj")
    assert out["status"] == "error" and out["code"] == 422 and out["_is_error"] is True, out
    assert out["expected_facts"] == server.CLOSE_FACTS_EXAMPLE


# ---------------------------------------------------------------------------
# F2 + F23: the refusal of an MCP seat beside a crew-hook session
# ---------------------------------------------------------------------------


def test_the_hook_shadow_refusal_is_a_failure_and_stays_one_under_the_crew_tag(api, monkeypatch):
    shadow = HookShadow(agent_id="codex", callsign="codex-1", here=("cc-1",))
    assert isinstance(shadow.refusal("CLAIMED", "remembra-crew claim <zone|path>"), FailureText)
    monkeypatch.setattr(CrewBridge, "_shadow", lambda self, caller, seat: shadow)
    call("crew_status", project_id="outageproj")
    result = call_result("crew_claim", paths=["src/a.py"])
    text = text_of(result)
    assert result.isError is True and text.startswith("NOT CLAIMED: a crew-hook session is live"), text
    assert text.splitlines()[-1] == "(in CREW outageproj as codex-1)"
    assert api.count("POST", "/claims") == 0
    # giving up what the seat holds stays open, and an empty release is not a failure
    released = call_result("crew_claim", action="release", paths=["src/a.py"])
    assert released.isError is False and text_of(released).startswith("NOTHING RELEASED"), text_of(released)


# ---------------------------------------------------------------------------
# F20 + the repeat memory: which client_msg_id a message carries
# ---------------------------------------------------------------------------


class _Messages(FakeApi):
    """The scripted API plus the message route: the first ``cut`` posts that wait get no answer."""

    def __init__(self) -> None:
        super().__init__()
        self.cut = 0
        self.posted: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/crews/crew_1/messages" and request.method == "POST" and not self._is_down(path):
            self.requests.append(request)
            body = json.loads(request.content)
            self.posted.append(body)
            if body.get("wait_s") and self.cut:
                self.cut -= 1
                raise httpx.ReadTimeout("The read operation timed out", request=request)
            message = {"id": f"msg_{body['client_msg_id'][-6:]}", "client_msg_id": body["client_msg_id"], "kind": body["kind"]}
            return httpx.Response(201, json={"message": message, "seq": 11})
        return super().__call__(request)


@pytest.fixture()
def messages(api, monkeypatch):
    fake = _Messages()
    monkeypatch.setattr(server, "_client", mock_client(fake))
    return fake


def test_a_wait_cut_short_is_recovered_with_the_same_message_id_through_the_real_http_call(messages):
    call("crew_status", project_id="outageproj")
    messages.cut = 1
    text = call("crew_say", body="can you release billing?", kind="question", wait_s=40)
    assert text.startswith("SENT msg_") and "The wait for a reply was cut short" in text, text
    first, second = messages.posted
    assert first["wait_s"] and "wait_s" not in second
    assert first["client_msg_id"].startswith("mcp-") and first["client_msg_id"] == second["client_msg_id"]


def test_two_separate_messages_with_the_same_text_get_their_own_ids(messages):
    call("crew_status", project_id="outageproj")
    assert call("crew_say", body="build is green").startswith("SENT msg_")
    assert call("crew_say", body="build is green").startswith("SENT msg_")
    first, second = messages.posted
    assert first["client_msg_id"] != second["client_msg_id"]


def test_a_message_that_met_an_outage_and_is_sent_again_carries_the_first_id(messages):
    call("crew_status", project_id="outageproj")
    messages.down, messages.down_paths = "hang", ("/messages",)
    failed = call_result("crew_say", body="build is green")
    assert failed.isError is True and text_of(failed).startswith("CREW UNAVAILABLE"), text_of(failed)
    [lost] = [json.loads(r.content) for r in messages.requests if r.url.path.endswith("/messages")]
    messages.down = None
    assert call("crew_say", body="build is green").startswith("SENT msg_")
    [again] = messages.posted
    assert again["client_msg_id"] == lost["client_msg_id"]
    assert call("crew_say", body="build is green").startswith("SENT msg_")  # answered: the next one is a new message
    assert messages.posted[1]["client_msg_id"] != lost["client_msg_id"]
