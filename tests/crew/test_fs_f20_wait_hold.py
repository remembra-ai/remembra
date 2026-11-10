"""F20: one MCP call never holds longer than a client's request timeout.

``crew_say(wait_s=60..120)`` and ``crew_claim(wait_s=…)`` held one blocking tool call for the whole
wait; the Claude desktop MCP client gives up at 60 s, so the agent got "Request timed out" with no
message id although the message was posted, and then re-sent it. The bridge now holds at most
:data:`remembra.mcp.crew.MCP_HOLD_MAX_S` per call (one budget for the whole call), keeps accepting
the documented larger values, and always answers with the id.
"""

from __future__ import annotations

import re
import time
from typing import Any

import tests.crew.test_mcp_crew as base
from remembra.crew import schemas as S
from remembra.mcp import crew as crew_mod
from tests.crew.test_mcp_crew import _two_agents, call, crew_id_of, use

live = base.live  # the module-scoped live API fixture and the bridge clock, shared with the WP-11 tests
clock = base.clock

CLIENT_TIMEOUT_S = 60.0  # the Claude desktop MCP client's request timeout


def _record(monkeypatch: Any, *, strip_wait: bool = False) -> list[dict[str, Any]]:
    """Record every crew POST's ``wait_s`` and HTTP timeout. ``strip_wait`` lets the request through
    without the server-side wait, so a test of the numbers sent does not sleep for them."""
    seen: list[dict[str, Any]] = []
    real = crew_mod.CrewHttp.call

    def spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        body = kw.get("json")
        if method == "POST" and isinstance(body, dict) and (path.endswith("/messages") or path.endswith("/claims")):
            seen.append({"path": path, "wait_s": body.get("wait_s"), "wait": body.get("wait"), "timeout": kw.get("timeout")})
            if strip_wait and body.get("wait_s"):
                kw = {**kw, "json": {k: v for k, v in body.items() if k != "wait_s"}, "timeout": None}
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", spy)
    return seen


def test_the_default_hold_leaves_room_under_a_60_second_client_timeout():
    assert crew_mod.mcp_hold_max_s() == crew_mod.MCP_HOLD_MAX_S == 45
    assert crew_mod.MCP_HOLD_MAX_S + crew_mod.MCP_HOLD_GRACE_S <= CLIENT_TIMEOUT_S - 5
    # the documented ceilings are still accepted (clamped, not refused), so old callers keep working
    assert S.validate_mcp_call("crew_say", {"body": "hi", "wait_s": 120}) == []
    assert S.validate_mcp_call("crew_claim", {"zone": "pos", "wait_s": 300}) == []


def test_crew_say_sends_at_most_the_hold_whatever_wait_was_asked(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "f20say")
    use(b)
    seen = _record(monkeypatch, strip_wait=True)
    text = call("crew_say", body="anyone?", kind="question", wait_s=120)
    assert text.startswith("SENT msg_"), text
    (post,) = [s for s in seen if s["path"].endswith("/messages")]
    assert post["wait_s"] == 45, post
    assert post["timeout"] is not None and post["timeout"] <= 45 + crew_mod.MCP_HOLD_GRACE_S < CLIENT_TIMEOUT_S, post
    # a wait at or under the hold is passed through unchanged
    seen.clear()
    call("crew_say", body="anyone at all?", kind="question", wait_s=30)
    assert [s["wait_s"] for s in seen] == [30], seen


def test_crew_say_over_the_hold_returns_the_id_and_says_what_was_waited(live, clock, monkeypatch):
    monkeypatch.setenv("REMEMBRA_MCP_WAIT_MAX_S", "2")
    assert crew_mod.mcp_hold_max_s() == 2
    project, a, a_sign, b, b_sign = _two_agents(live, "f20short")
    use(b)
    seen = _record(monkeypatch)
    started = time.monotonic()
    text = call("crew_say", body="anyone?", kind="question", wait_s=120)
    elapsed = time.monotonic() - started
    assert 1.5 <= elapsed < 15, elapsed
    assert [s["wait_s"] for s in seen] == [2], seen
    lines = text.split("\n")
    msg_id = re.match(r"SENT (msg_[0-9A-Za-z]+) ", lines[0]).group(1)
    assert "no reply yet (waited 2s of the 120 asked: one MCP call holds at most 2s)" in text, text
    assert "do not send it again" in text and f'crew_status(message="{msg_id}")' in text, text
    rows = live.crew_rows("SELECT id FROM crew_messages WHERE crew_id = ? AND kind = 'question'", (crew_id_of(live, project),))
    assert [r["id"] for r in rows] == [msg_id]  # posted once


def test_a_wait_that_times_out_at_http_level_still_answers_with_the_message_id(live, clock, monkeypatch):
    """The server kept the request past the HTTP timeout (slow network, a proxy): the message was
    committed before the wait began, so the bridge asks for it again by its client_msg_id."""
    project, a, a_sign, b, b_sign = _two_agents(live, "f20http")
    use(b)
    real = crew_mod.CrewHttp.call
    posts: list[dict[str, Any]] = []

    def cut(self: Any, method: str, path: str, **kw: Any) -> Any:
        body = kw.get("json")
        if method == "POST" and path.endswith("/messages") and isinstance(body, dict):
            posts.append(dict(body))
            if body.get("wait_s"):
                real(self, method, path, **{**kw, "json": {k: v for k, v in body.items() if k != "wait_s"}, "timeout": None})
                raise crew_mod.CrewApiError(0, "timeout", "The Remembra server did not answer in time.")
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", cut)
    text = call("crew_say", body="can you release billing?", kind="request_release", to=f"@{a_sign}", wait_s=40)
    assert text.startswith("SENT msg_"), text
    assert "The wait for a reply was cut short" in text and "do not send it again" in text, text
    assert len(posts) == 2 and posts[0]["client_msg_id"] == posts[1]["client_msg_id"] and "wait_s" not in posts[1], posts
    rows = live.crew_rows(
        "SELECT id FROM crew_messages WHERE crew_id = ? AND kind = 'request_release'", (crew_id_of(live, project),)
    )
    assert len(rows) == 1 and rows[0]["id"] in text  # one message, and the agent has its id


def test_crew_claim_wait_is_one_budget_for_the_whole_call(live, clock, monkeypatch):
    monkeypatch.setenv("REMEMBRA_MCP_WAIT_MAX_S", "3")
    project, a, a_sign, b, b_sign = _two_agents(live, "f20claim")
    use(a)
    assert call("crew_claim", zone="billing").startswith("GRANTED zone billing")
    assert call("crew_claim", zone="reports").startswith("GRANTED zone reports")
    use(b)
    seen = _record(monkeypatch)
    started = time.monotonic()
    text = call("crew_claim", paths=["src/app/billing/a.ts", "src/app/reports/b.ts"], wait_s=40)
    elapsed = time.monotonic() - started
    waits = [s["wait_s"] or 0 for s in seen if s["path"].endswith("/claims")]
    assert len(waits) == 2 and sum(waits) <= 3, seen
    assert all(s["wait"] is True for s in seen), seen  # still queued behind the holder, just not held
    assert elapsed < 15, elapsed
    assert text.count("QUEUED zone ") == 2, text
    assert "of the 40 asked: one MCP call holds at most 3s" in text, text
    assert "call crew_claim again with wait_s to keep waiting" in text, text


def test_crew_claim_at_the_documented_ceiling_sends_at_most_the_hold(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "f20ceiling")
    use(a)
    assert call("crew_claim", zone="billing").startswith("GRANTED zone billing")
    use(b)
    seen = _record(monkeypatch, strip_wait=True)
    text = call("crew_claim", zone="billing", wait_s=300)
    assert text.startswith("QUEUED zone billing"), text
    (post,) = seen
    assert post["wait_s"] == 45 and post["timeout"] <= 45 + crew_mod.MCP_HOLD_GRACE_S < CLIENT_TIMEOUT_S, post


def test_the_hold_override_is_bounded_and_ignores_rubbish(monkeypatch):
    for raw, want in (("", 45), ("abc", 45), ("-5", 45), ("0", 45), ("20", 20), ("100000", 300), (" 90 ", 90)):
        monkeypatch.setenv("REMEMBRA_MCP_WAIT_MAX_S", raw)
        assert crew_mod.mcp_hold_max_s() == want, raw
    monkeypatch.delenv("REMEMBRA_MCP_WAIT_MAX_S")
    assert crew_mod.mcp_hold_max_s() == 45


def test_the_tool_text_no_longer_promises_a_wait_a_client_cannot_survive():
    tools = base.server.mcp._tool_manager._tools
    for name in ("crew_say", "crew_claim"):
        doc = tools[name].fn.__doc__ or ""
        assert "≤120" not in doc and "≤300" not in doc and "0–120" not in doc and "0–300" not in doc, name
        assert "45" in doc, name
