"""F12: a close_session used as a milestone must not strand the still-working session's claims.

Three behaviours, each through ``FastMCP.call_tool`` against the live API harness (``tests/crew/mcp_support.py``):

- a final close that reserves claims reserves them for the leaving session, so the same session
  gets them back when its next crew call re-joins (as the crew's own leave path does);
- ``final=False`` (or an end reason that says checkpoint / milestone) stores the handoff and keeps
  the crew seat and its claims, against a current server and against one that ignores ``leave``;
- a final close says in words that the crew session ended and what happened to the claims.
"""

from __future__ import annotations

from typing import Any

import pytest

import remembra.crew.closeout as closeout
import tests.crew.test_mcp_crew as base
from tests.crew.test_mcp_crew import _two_agents, as_json, call, callsign_in, crew_id_of, session_row, use

live = base.live  # the module-scoped live API and the per-test MCP bridge, as fixtures of this module
clock = base.clock

PATH = "docs/launch.md"


def claim_rows(live: Any, project: str) -> list[dict[str, Any]]:
    return live.crew_rows(
        "SELECT state, reserve_reason, reserved_for, holder_session_id FROM crew_claims WHERE crew_id = ? AND path_glob = ?",
        (crew_id_of(live, project), PATH),
    )


def events(live: Any, project: str, kind: str) -> list[dict[str, Any]]:
    return live.crew_rows("SELECT seq FROM crew_events WHERE crew_id = ? AND type = ?", (crew_id_of(live, project), kind))


def test_dirty_close_then_same_session_keeps_its_claims(live, clock):
    project, a, a_sign, *_ = _two_agents(live, "f12dirty")
    use(a)
    assert call("crew_claim", paths=[PATH]).startswith("GRANTED"), "claim not granted"
    closed = as_json(
        call("close_session", summary="more to do", end_reason="done", facts={"uncommitted_files": ["x.png"]}, project_id=project)
    )
    assert closed["crew"]["session_left"] is True and closed["crew"]["claims_reserved"] == 1, closed
    sid = session_row(live, project, a_sign)["id"]
    assert claim_rows(live, project) == [
        {"state": "reserved", "reserve_reason": "ended_dirty", "reserved_for": sid, "holder_session_id": sid}
    ]
    # the session keeps working: its next crew call re-joins the same crew session and re-takes the claim
    status = call("crew_status", project_id=project)
    assert callsign_in(status) == a_sign
    assert [r["state"] for r in claim_rows(live, project)] == ["active"]
    released = call("crew_claim", action="release", paths=[PATH])
    assert released.startswith("RELEASED"), released


def test_a_final_close_says_the_crew_session_ended(live, clock):
    project, a, a_sign, *_ = _two_agents(live, "f12note")
    use(a)
    call("crew_claim", paths=[PATH])
    closed = as_json(call("close_session", summary="done", end_reason="done", project_id=project))
    crew = closed["crew"]
    assert crew["session_left"] is True and crew["claims_released"] == 1
    assert "ended your crew session" in crew["note"] and "1 claim(s) released" in crew["note"], crew
    assert "final=false" in crew["note"]


def test_milestone_close_does_not_leave_the_crew(live, clock):
    project, a, a_sign, *_ = _two_agents(live, "f12mile")
    use(a)
    call("crew_claim", paths=[PATH])
    closed = as_json(
        call(
            "close_session",
            summary="milestone reached",
            end_reason="done",
            facts={"uncommitted_files": ["x.png"]},
            project_id=project,
            final=False,
        )
    )
    assert closed["status"] == "ok" and closed["handoff_id"], closed
    crew = closed["crew"]
    assert crew["session_left"] is False and crew["claims_released"] == 0 and crew["claims_reserved"] == 0, crew
    assert "kept" in crew["note"], crew
    assert session_row(live, project, a_sign)["state"] == "active"
    assert [r["state"] for r in claim_rows(live, project)] == ["active"]
    assert events(live, project, "session.left") == []
    assert len(events(live, project, "handoff.created")) == 1
    # the seat is still live: release works without a re-join
    assert call("crew_claim", action="release", paths=[PATH]).startswith("RELEASED")
    # and a later final close still ends the crew session
    final = as_json(call("close_session", summary="finished", end_reason="done", project_id=project))
    assert final["crew"]["session_left"] is True
    assert session_row(live, project, a_sign)["state"] == "ended"


@pytest.mark.parametrize("reason", ["checkpoint", "verified release milestone; autonomous goal remains active"])
def test_a_checkpoint_or_milestone_end_reason_is_not_a_final_close(live, clock, reason):
    project, a, a_sign, *_ = _two_agents(live, "f12reason")
    use(a)
    call("crew_claim", paths=[PATH])
    closed = as_json(call("close_session", summary="so far", end_reason=reason, project_id=project))
    assert closed["crew"]["session_left"] is False, closed
    assert session_row(live, project, a_sign)["state"] == "active"
    assert [r["state"] for r in claim_rows(live, project)] == ["active"]
    # an explicit final=True wins over the wording
    final = as_json(call("close_session", summary="so far", end_reason=reason, project_id=project, final=True))
    assert final["crew"]["session_left"] is True


def test_milestone_close_keeps_the_seat_on_a_server_that_ignores_leave(live, clock, monkeypatch):
    """A 0.17.5 server has no ``leave`` field: it leaves whenever the close carries the session's token."""
    real = closeout.on_relay_close

    async def old_server(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("leave", None)
        return await real(*args, **kwargs)

    monkeypatch.setattr(closeout, "on_relay_close", old_server)
    project, a, a_sign, *_ = _two_agents(live, "f12old")
    use(a)
    call("crew_claim", paths=[PATH])
    closed = as_json(call("close_session", summary="milestone", project_id=project, final=False))
    assert closed["crew"]["session_left"] is False, closed
    assert session_row(live, project, a_sign)["state"] == "active"
    assert [r["state"] for r in claim_rows(live, project)] == ["active"]


def test_rest_close_with_leave_false_records_the_handoff_only(live, clock):
    project, a, a_sign, *_ = _two_agents(live, "f12rest")
    use(a)
    call("crew_claim", paths=[PATH])
    seat = _seats()[0]
    headers = {"X-API-Key": live.state["key"], "X-Remembra-Crew-Session": seat.token, "X-Remembra-Agent-Id": "claude-desktop"}
    body = {"session_id": seat.client_session_id, "agent_id": "claude-desktop", "project_id": project, "facts": {"notes": "n"}}
    kept = live.api("POST", "/session/close", headers=headers, json={**body, "leave": False})
    assert kept["crew"]["session_left"] is False and kept["crew"]["leave_skipped"] is None, kept["crew"]
    assert session_row(live, project, a_sign)["state"] == "active"
    assert events(live, project, "session.left") == []
    # the default is unchanged: the same close without the field ends the crew session
    left = live.api("POST", "/session/close", headers=headers, json={**body, "facts": {"notes": "n2"}})
    assert left["crew"]["session_left"] is True
    assert session_row(live, project, a_sign)["state"] == "ended"


def _seats() -> list[Any]:
    import remembra.mcp.server as server

    return [seat for by_project in server._crew._seats.values() for seat in by_project.values() if not seat.left]
