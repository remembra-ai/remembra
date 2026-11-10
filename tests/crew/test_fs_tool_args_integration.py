"""Integration of the tool-arguments guard (F22, F39) with the per-call project of the crew tools (F5, F6)
and the isError flag of failed calls (F23).

- an unknown key that resembles a declared argument names it;
- a remote on a crew call is checked against the project that call names, not the session's crew;
- a crew failure met while checking is an error result, like every other crew failure;
- ack_inbox(inbox_ids=[...]) with a row that was not acknowledged is an error result carrying the same JSON.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from remembra.mcp.crew import CrewUsageError
from tests.crew.test_mcp_crew import agent, crew_id_of, project_name, use

# the live API and the fresh crew bridge, shared with the WP-11 suite
live = base.live
clock = base.clock


def call(name: str, **args: Any) -> str:
    try:
        return base.call(name, **args)
    except ToolError as e:
        return str(e)


def session(*calls: tuple[str, dict[str, Any]]) -> list[tuple[bool, str]]:
    """``(isError, text)`` of each call over a real in-memory MCP client session."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async def run() -> list[tuple[bool, str]]:
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
            out = []
            for name, args in calls:
                res = await client.call_tool(name, args)
                out.append((bool(res.isError), str(res.content[0].text)))
            return out

    return asyncio.run(run())


def _claims(live: Any, project: str) -> list[str]:
    rows = live.crew_rows("SELECT resource FROM crew_claims WHERE crew_id = ? ORDER BY created_at", (crew_id_of(live, project),))
    return [r["resource"] for r in rows]


def _on_record(live: Any, *projects: str) -> None:
    for name in projects:
        live.api(
            "POST",
            "/projects/resolve",
            headers={"X-API-Key": live.state["key"]},
            json={"git_remote": f"https://github.com/acme/{name}.git", "hint_project": name},
        )


def test_an_unknown_key_close_to_a_declared_argument_names_it(live, clock):
    project = project_name("meant")
    use(agent(live, "claude-desktop", f"{project}-s", project))
    assert call("crew_status", project_id=project).startswith(f"CREW {project} ")
    out = call("crew_checkpoint", summary="x", files_change=["a.py"])
    assert out.startswith("INVALID ARGUMENTS: crew_checkpoint does not take files_change. Did you mean files_changed?"), out
    assert "Nothing was done." in out and "a.py" not in out, out
    assert live.crew_rows("SELECT id FROM crew_checkpoints WHERE crew_id = ?", (crew_id_of(live, project),)) == []
    # a key that resembles nothing gets no guess
    assert "Did you mean" not in call("crew_checkpoint", summary="x", zzz=1)


def test_a_remote_on_a_crew_call_is_checked_against_the_project_that_call_names(live, clock):
    project, other = project_name("percall"), project_name("percallother")
    _on_record(live, project, other)
    use(agent(live, "codex", f"{project}-s", "default"))
    assert call("crew_status", project_id=project).startswith(f"CREW {project} ")
    # the remote is the named project's: the call runs there, for this call
    fits = call("crew_claim", zone="schema:a", project_id=other, git_remote=f"git@github.com:acme/{other}.git")
    assert fits.startswith("GRANTED schema:a") and f"(in CREW {other} as " in fits, fits
    # the remote is another project's than the one the call names: refused, although it is the session's crew
    out = call("crew_claim", zone="schema:b", project=other, git_remote=f"git@github.com:acme/{project}.git")
    assert out.startswith(f"INVALID ARGUMENTS: crew_claim acts on the project this call names, which is {other};"), out
    assert f"you named {project}" in out and "Nothing was done." in out, out
    assert f"Leave the location out to act on {other}." in out, out
    assert _claims(live, other) == ["schema:a"] and _claims(live, project) == []


def test_a_crew_failure_met_while_checking_a_location_is_an_error_result(live, clock, monkeypatch):
    project = project_name("checkfails")
    use(agent(live, "claude-desktop", f"{project}-s", project))
    assert call("crew_status", project_id=project).startswith(f"CREW {project} ")

    def refuse() -> Any:
        raise CrewUsageError("this agent has no id")

    monkeypatch.setattr(server, "_crew_caller", refuse)
    ((is_error, text),) = session(("crew_say", {"body": "x", "git_remote": "git@github.com:acme/whatever.git"}))
    assert is_error is True and text == "CREW: this agent has no id", (is_error, text)


def test_ack_inbox_with_a_row_that_was_not_acknowledged_is_an_error_result(live, clock):
    project = project_name("ackpart")
    use(agent(live, "claude-desktop", f"{project}-s", project))
    sent = json.loads(call("send_to_inbox", to_agent="gemini", subject="s", body="b"))["inbox_id"]
    (all_ok, text_ok), (part, text_part) = session(
        ("ack_inbox", {"inbox_ids": [sent], "result": "done"}),
        ("ack_inbox", {"inbox_ids": [sent, "inb_does_not_exist"], "result": "done"}),
    )
    assert all_ok is False and json.loads(text_ok)["ok"] is True, text_ok
    answer = json.loads(text_part)
    assert part is True and answer["ok"] is False and (answer["count"], answer["failed"]) == (1, 1), text_part
    assert [i["ok"] for i in answer["items"]] == [True, False] and answer["items"][1]["inbox_id"] == "inb_does_not_exist"
