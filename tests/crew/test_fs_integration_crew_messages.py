"""Integration of the crew-messages group (F7, F20) with the earlier groups (F5, F11).

Two things only exist once the branches meet: ``crew_status`` has both ``join`` (F5) and ``message`` (F7),
and the wait cap has two knobs, the bridge's ``max_wait_s`` (F11, ``REMEMBRA_MCP_MAX_WAIT_S``) and
``REMEMBRA_MCP_WAIT_MAX_S`` (F20). Every test goes through ``FastMCP.call_tool`` into the real MCP server
module against a live local API (``tests/crew/mcp_support.py``).
"""

from __future__ import annotations

import re
import time

import tests.crew.test_mcp_crew as base
from remembra.crew import schemas as S
from remembra.mcp import crew as crew_mod
from remembra.mcp import server
from remembra.mcp.crew import CrewBridge
from tests.crew.test_mcp_crew import _two_agents, call, crew_id_of, use

live = base.live  # the module-scoped live API fixture and the bridge clock, shared with the WP-11 tests
clock = base.clock


def test_crew_status_contract_has_join_and_message():
    spec = next(t for t in S.MCP_TOOLS if t.name == "crew_status")
    assert [p.name for p in spec.params] == ["project_id", "git_remote", "root_path", "verbose", "join", "message"]
    assert S.validate_mcp_call("crew_status", {"join": False}) == []
    assert S.validate_mcp_call("crew_status", {"message": "msg_01ABC"}) == []


def test_a_message_read_with_join_false_is_refused_and_joins_nothing(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "intmsglook")
    use(b)
    sent = call("crew_say", body="the migration is half applied", kind="note")
    msg_id = re.search(r"msg_[0-9A-Za-z]+", sent).group(0)
    crew = crew_id_of(live, project)
    before = live.crew_rows("SELECT id, last_seen_at FROM crew_sessions WHERE crew_id = ? ORDER BY id", (crew,))
    use(a)
    text = call("crew_status", project_id=project, join=False, message=msg_id)
    assert "without join=False" in text, text
    assert "half applied" not in text, text
    assert live.crew_rows("SELECT id, last_seen_at FROM crew_sessions WHERE crew_id = ? ORDER BY id", (crew,)) == before
    # the same read with a seat returns the body
    assert "half applied" in call("crew_status", project_id=project, message=msg_id)


def test_the_wait_cap_is_the_bridges_unless_the_per_call_override_is_set(monkeypatch):
    monkeypatch.delenv(crew_mod.MCP_HOLD_ENV, raising=False)
    assert crew_mod.MAX_WAIT_S == crew_mod.MCP_HOLD_MAX_S
    assert CrewBridge().hold_max_s() == crew_mod.MCP_HOLD_MAX_S
    assert CrewBridge(max_wait_s=7).hold_max_s() == 7
    monkeypatch.setenv(crew_mod.MCP_HOLD_ENV, "3")
    assert CrewBridge(max_wait_s=7).hold_max_s() == 3
    for rubbish in ("", "abc", "0", "-2"):
        monkeypatch.setenv(crew_mod.MCP_HOLD_ENV, rubbish)
        assert CrewBridge(max_wait_s=7).hold_max_s() == 7, rubbish


def test_a_say_cut_by_the_bridge_cap_says_what_was_held_and_how_to_read_the_reply(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "intsaycut")
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock, max_wait_s=1))
    use(b)
    started = time.monotonic()
    text = call("crew_say", body="anyone?", kind="question", wait_s=8)
    assert time.monotonic() - started < 5
    msg_id = re.match(r"SENT (msg_[0-9A-Za-z]+) ", text).group(1)
    assert "no reply yet (waited 1s of the 8 asked: one MCP call holds at most 1s)" in text, text
    assert "Waits are cut to 1s per call." in text and "do not send it again" in text, text
    assert f'crew_status(message="{msg_id}")' in text, text


def test_a_claim_cut_by_the_bridge_cap_holds_one_budget_and_says_so(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "intclaimcut")
    use(a)
    assert call("crew_claim", zone="billing").startswith("GRANTED zone billing")
    assert call("crew_claim", zone="reports").startswith("GRANTED zone reports")
    monkeypatch.setattr(server, "_crew", CrewBridge(clock=clock, max_wait_s=2))
    use(b)
    started = time.monotonic()
    text = call("crew_claim", paths=["src/app/billing/a.ts", "src/app/reports/b.ts"], wait_s=30)
    assert time.monotonic() - started < 6
    assert text.count("QUEUED zone ") == 2, text
    assert "of the 30 asked: one MCP call holds at most 2s" in text and "Waits are cut to 2s per call." in text, text
    crew = crew_id_of(live, project)
    assert len(live.crew_rows("SELECT id FROM crew_claims WHERE crew_id = ? AND state = 'queued'", (crew,))) == 2
