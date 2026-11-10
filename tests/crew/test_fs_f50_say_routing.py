"""F50: ``crew_say`` reports who the message reached, not every ``@token`` in the body.

``SENT msg_… to @codex-5, @4b478d5`` named a commit SHA as a recipient: the line was built from
the raw tokens the server parsed out of the body, and the routing the server returned (who was
queued, what matched nothing) was thrown away. A callsign with no live session was reported as
"to" as well, so the sender never learned it reached nobody.
"""

from __future__ import annotations

from typing import Any

import tests.crew.test_mcp_crew as base
from remembra.mcp import crew as crew_mod
from tests.crew.test_mcp_crew import _two_agents, call, crew_id_of, use

live = base.live  # the module-scoped live API fixture and the bridge clock, shared with the WP-11 tests
clock = base.clock


def _mentions_queued_for(live: Any, project: str, callsign: str) -> int:
    rows = live.crew_rows(
        "SELECT i.id FROM crew_inbox_items i JOIN crew_sessions s ON s.id = i.recipient"
        " WHERE i.crew_id = ? AND i.kind = 'mention' AND s.callsign = ?",
        (crew_id_of(live, project), callsign),
    )
    return len(rows)


def test_a_commit_sha_in_the_body_is_not_reported_as_a_recipient(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f50sha")
    use(a)
    text = call("crew_say", body="deploying feat/team-invites @4b478d5 now", kind="note", to=f"@{b_sign}")
    first, *rest = text.split("\n")
    assert first.startswith("SENT msg_") and first.endswith(
        f"(seq {first.split('(seq ')[1].split(',')[0]}, note) to @{b_sign}"
    ), text
    assert "4b478d5" not in first, text
    assert any(ln.startswith("NOT DELIVERED: @4b478d5 ") and "no live callsign, agent, zone or task" in ln for ln in rest), text
    assert _mentions_queued_for(live, project, b_sign) == 1


def test_a_callsign_with_no_live_session_is_not_reported_as_sent_to(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f50dead")
    use(a)
    text = call("crew_say", body="can you release billing?", kind="request_release", to="@codex-99")
    first, *rest = text.split("\n")
    assert first.startswith("SENT msg_") and " to " not in first, text
    assert any(ln.startswith("NOT DELIVERED: @codex-99 ") for ln in rest), text
    assert "crew_status" in text  # where to see who is here


def test_to_an_agent_is_added_even_when_the_body_names_a_longer_callsign(live, clock):
    """``to="@codex"`` used to be dropped when the body contained ``@codex-99`` (a substring test), so
    the intended agent was never mentioned."""
    project, a, a_sign, b, b_sign = _two_agents(live, "f50substr")
    use(a)
    text = call("crew_say", body="@codex-99 said the POS zone is free", to="@codex")
    first, *rest = text.split("\n")
    assert first.endswith(f" to @{b_sign}"), text  # b is the live codex session
    assert any(ln.startswith("NOT DELIVERED: @codex-99 ") for ln in rest), text
    assert _mentions_queued_for(live, project, b_sign) == 1
    # a target the body already names as its own token is not added twice
    again = call("crew_say", body=f"@{b_sign} and once more", to=f"@{b_sign}")
    body = live.crew_rows("SELECT body FROM crew_messages WHERE id = ?", (again.split()[1],))[0]["body"]
    assert body == f"@{b_sign} and once more"


def test_a_message_that_reached_nobody_says_so(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f50self")
    use(a)
    text = call("crew_say", body="note to self", to=f"@{a_sign}")
    first, *rest = text.split("\n")
    assert " to " not in first, text
    assert any(ln.startswith("REACHED NO ONE: ") for ln in rest), text


def test_crew_and_the_human_are_named_as_what_they_are(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f50crew")
    use(a)
    text = call("crew_say", body="standup in five", to="@crew")
    assert text.split("\n")[0].endswith(f" to @{b_sign}, the crew inbox"), text
    human = call("crew_say", body="need a decision on GCT rounding", kind="question", to="@mani")
    assert human.split("\n")[0].endswith(" to @mani (needs-you)"), human
    assert "NOT DELIVERED" not in text + human and "REACHED NO ONE" not in text + human


def test_an_agent_without_a_live_session_gets_it_in_the_agent_inbox(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f50inbox")
    use(b)
    closed = call("close_session", summary="done for today", end_reason="done", project_id=project)
    assert '"session_left": true' in closed, closed
    use(a)
    text = call("crew_say", body="the migration is ready for you", to="@codex")
    first = text.split("\n")[0]
    assert first.endswith(" to the inbox of codex (no live session; get_inbox shows it)"), text
    assert f"@{b_sign}" not in first


def test_a_response_without_routing_does_not_claim_delivery(live, clock, monkeypatch):
    """A replayed post (same client_msg_id) and an older server return no routing: the tokens are
    listed as mentions, never as recipients."""
    project, a, a_sign, b, b_sign = _two_agents(live, "f50old")
    use(a)
    real = crew_mod.CrewHttp.call

    def no_routing(self: Any, method: str, path: str, **kw: Any) -> Any:
        status, body, etag = real(self, method, path, **kw)
        if method == "POST" and path.endswith("/messages") and isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "routing"}
        return status, body, etag

    monkeypatch.setattr(crew_mod.CrewHttp, "call", no_routing)
    text = call("crew_say", body="heads up @4b478d5", to=f"@{b_sign}")
    first = text.split("\n")[0]
    assert " to " not in first and first.endswith(f"mentions @{b_sign}, @4b478d5 (delivery not reported by the server)"), text


def test_the_bridge_reads_mentions_with_the_servers_grammar():
    from remembra.crew import channel

    assert crew_mod._MENTION_RE.pattern == channel.MENTION_RE.pattern
    for text in ("@codex-5 at feat/x @4b478d5.", "mail a@b.c and path/@x", "@Crew: @zone:pos, @task:t-3 -@no"):
        assert crew_mod.body_mentions(text) == set(channel.parse_mentions(text)), text
    assert crew_mod.body_mentions("") == set() and crew_mod.body_mentions(None) == set()


def test_sent_line_helper_handles_missing_and_odd_fields():
    line = crew_mod.sent_lines
    assert line({}, {}, "chat", lambda sid: None) == ["SENT None (seq None, chat)"]
    res = {"seq": 3, "routing": {"sessions": ["cs_1", "cs_2"], "unresolved": None, "agent_inbox": None}}
    msg = {"id": "msg_01A", "mentions": ["crew"]}
    out = line(res, msg, "chat", {"cs_1": "cc-1"}.get)
    assert out == ["SENT msg_01A (seq 3, chat) to @cc-1, 1 other session"]
    hostile = {"seq": 4, "routing": {"unresolved": ["ok-token", "<b>bad</b> token"], "sessions": []}}
    out = line(hostile, {"id": "msg_01B", "mentions": ["ok-token"]}, "note", lambda sid: None)
    assert out[0] == "SENT msg_01B (seq 4, note)"
    assert out[1].startswith("NOT DELIVERED: @ok-token ") and "<b>" not in "\n".join(out)
