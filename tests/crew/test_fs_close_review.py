"""F12 / F16 / F17 review: what the review of the first fixes found, through the real routes.

Through ``FastMCP.call_tool`` and the REST route against the live API harness:

- a dirty close whose only uncommitted file is called ``NA`` still reserves the claim (F16);
- an infinite count does not fail the close or the checkpoint (F16);
- ``HEAD`` or a tag given as a checkpoint commit is recorded as a commit (F16);
- one item given bare where a list is expected, a text ``facts`` and a non-boolean
  ``no_upstream`` are read, not answered with a raw validation error (F16);
- an end reason that says the session is over ends it, whatever else it says (F12);
- a server with no field for declared work is asked twice only once per client (F17).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from tests.crew.test_fs_close_declared_work import _as_old_server
from tests.crew.test_mcp_crew import _two_agents, agent, as_json, call, crew_id_of, project_name, session_row, use

live = base.live  # the module-scoped live API and the per-test MCP bridge, as fixtures of this module
clock = base.clock

PATH = "docs/launch.md"


def solo(live: Any, prefix: str) -> str:
    project = project_name(prefix)
    use(agent(live, "claude-desktop", f"{project}-a", project))
    return project


def claim_states(live: Any, project: str) -> list[str]:
    rows = live.crew_rows("SELECT state FROM crew_claims WHERE crew_id = ? AND path_glob = ?", (crew_id_of(live, project), PATH))
    return [str(r["state"]) for r in rows]


def last_checkpoint(live: Any, project: str) -> dict[str, Any]:
    rows = live.crew_rows(
        "SELECT facts, trigger FROM crew_checkpoints WHERE crew_id = ? ORDER BY created_at DESC LIMIT 1",
        (crew_id_of(live, project),),
    )
    return {"facts": json.loads(rows[0]["facts"]), "trigger": rows[0]["trigger"]}


def test_a_dirty_close_whose_file_is_named_na_reserves_the_claim(live, clock):
    """The relay CLI's git-read facts through the REST route, as a 0.17.5 client sends them."""
    project, a, *_ = _two_agents(live, "rvna")
    use(a)
    assert call("crew_claim", paths=[PATH]).startswith("GRANTED")
    seat = next(s for by_project in server._crew._seats.values() for s in by_project.values() if not s.left)
    res = live.api(
        "POST",
        "/session/close",
        headers={"X-API-Key": live.state["key"], "X-Remembra-Crew-Session": seat.token, "X-Remembra-Agent-Id": "claude-desktop"},
        json={
            "session_id": seat.client_session_id,
            "agent_id": "claude-desktop",
            "project_id": project,
            "facts": {
                "facts_source": "relay-cli:claude",
                "uncommitted_files": ["NA"],
                "files_changed": ["null", "none", "src/a.py"],
            },
        },
    )
    assert len(res["crew"]["claims_reserved"]) == 1 and res["crew"]["claims_released"] == [], res["crew"]
    assert claim_states(live, project) == ["reserved"]
    assert "changed 3 file(s): null, none, src/a.py" in res["rendered"]
    assert "uncommitted changes in 1 file(s): NA" in res["rendered"]
    assert res["facts_normalized"] == []


def test_an_infinite_count_does_not_fail_the_close(live, clock):
    project = project_name("rvinf")
    res = httpx.post(
        f"{live.url}/api/v1/session/close",
        headers={"X-API-Key": live.state["key"], "Content-Type": "application/json"},
        # written out: 1e999 is valid JSON that parses to infinity, and json.dumps cannot produce it
        content=(
            f'{{"agent_id": "codex", "session_id": "{project}-s", "project_id": "{project}", "facts": '
            '{"unpushed_commits": 1e999, "tests": [{"cmd": "pytest", "passed": true, "failed": 1e999}, '
            '{"cmd": "ruff", "passed": 1e999}]}}'
        ),
        timeout=30,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "tests passing: pytest" in body["rendered"] and "not pushed" not in body["rendered"]
    assert "Tests as declared (no pass/fail result recorded)" in body["rendered"]
    # the MCP tools: a result, not a raised tool error
    solo(live, "rvinfmcp")
    inf = float("inf")
    out = as_json(call("close_session", facts={"unpushed_commits": inf, "tests": [{"cmd": "t", "failed": inf}]}))
    assert out["status"] == "ok", out


def test_an_infinite_count_does_not_fail_the_checkpoint(live, clock):
    project, a, *_ = _two_agents(live, "rvinfck")
    use(a)
    out = call("crew_checkpoint", files_changed=["a.py"], tests=[{"command": "pytest", "passed": float("inf"), "failed": 0}])
    assert out.startswith("CHECKPOINT ckp_"), out
    assert last_checkpoint(live, project)["facts"]["tests"] == [{"command": "pytest", "passed": 1, "failed": 0}]


def test_a_git_ref_given_as_a_checkpoint_commit_is_recorded_as_a_commit(live, clock):
    project, a, *_ = _two_agents(live, "rvref")
    use(a)
    out = call("crew_checkpoint", files_changed=["a.py"], commits=["HEAD", "abc", "v1.2.0"])
    assert out.startswith("CHECKPOINT ckp_"), out
    row = last_checkpoint(live, project)
    assert row["facts"] == {"files_changed": ["a.py"], "commits": [{"sha": "HEAD"}, {"sha": "abc"}, {"sha": "v1.2.0"}]}
    assert row["trigger"] == "commit"


@pytest.mark.parametrize(
    ("args", "facts"),
    [
        ({"tests": {"command": "x", "passed": 1, "failed": 0}}, {"tests": [{"command": "x", "passed": 1, "failed": 0}]}),
        ({"tests": "pytest ok"}, {"notes": "test as declared (no result): pytest ok"}),
        ({"commits": "1ec6d03"}, {"commits": [{"sha": "1ec6d03"}]}),
        ({"commits": {"sha": "1ec6d03", "subject": "fix"}}, {"commits": [{"sha": "1ec6d03"}]}),
    ],
)
def test_one_item_given_bare_to_a_checkpoint_is_one_item(live, clock, args, facts):
    project, a, *_ = _two_agents(live, "rvbare")
    use(a)
    out = call("crew_checkpoint", files_changed=["a.py"], **args)
    assert out.startswith("CHECKPOINT ckp_"), out
    assert last_checkpoint(live, project)["facts"] == {"files_changed": ["a.py"], **facts}


def test_one_item_given_bare_to_a_report_is_one_item(live, clock):
    project, a, *_ = _two_agents(live, "rvrpt")
    use(a)
    call("crew_task", action="create", title="Export", zones=["reports"])
    call("crew_task", action="start", task="T-1")
    out = call(
        "crew_report",
        task="T-1",
        sections={"done": ["export works"]},
        commits="bd6ab83",
        tests={"command": "npm test", "passed": 4, "failed": 0},
    )
    assert out.startswith("REPORT rpt_"), out
    rows = live.crew_rows("SELECT commits, tests FROM crew_reports WHERE crew_id = ?", (crew_id_of(live, project),))
    assert "bd6ab83" in rows[-1]["commits"] and "npm test" in rows[-1]["tests"]


def test_close_session_reads_odd_arguments_instead_of_refusing_them(live, clock):
    project = solo(live, "rvodd")
    out = as_json(
        call(
            "close_session",
            todos_open=[{"task": "write the migration"}, "update docs"],
            errors=[7],
            done=[{"what": "deployed"}],
            open_work=["review", 3],
            project_id=project,
        )
    )
    assert out["status"] == "ok", out
    for line in ('- TODO: {"task": "write the migration"}', "- TODO: update docs", "- TODO: review", "- TODO: 3", "- error: 7"):
        assert line in out["rendered"], (line, out["rendered"])
    assert '- {"what": "deployed"}' in out["rendered"]
    # facts given as one text: it is what the agent knows, so it is the notes
    text = as_json(call("close_session", facts="all good, deployed to staging", project_id=project))
    assert text["status"] == "ok" and "Notes (agent): all good, deployed to staging" in text["rendered"], text
    assert any(line.startswith("facts: text read as notes") for line in text["normalized"]), text["normalized"]


def test_a_no_upstream_that_is_not_a_boolean_does_not_fail_the_close(live, clock):
    project = solo(live, "rvnoup")
    out = as_json(call("close_session", facts={"no_upstream": "unknown", "notes": "n"}, project_id=project))
    assert out["status"] == "ok" and "Notes (agent): n" in out["rendered"], out
    assert any(line.startswith("no_upstream:") for line in out["normalized"]), out["normalized"]
    # an older client: the server reads it too
    res = live.api(
        "POST",
        "/session/close",
        headers={"X-API-Key": live.state["key"]},
        json={"agent_id": "codex", "session_id": f"{project}-rest", "project_id": project, "facts": {"no_upstream": "perhaps"}},
    )
    assert any(line.startswith("no_upstream:") for line in res["facts_normalized"]), res


@pytest.mark.parametrize(
    "reason",
    [
        "final milestone shipped, session over",
        "Final checkpoint",
        "milestone reached; session ended",
        "last checkpoint, signing off",
    ],
)
def test_an_end_reason_that_says_the_session_is_over_is_a_final_close(live, clock, reason):
    project, a, a_sign, *_ = _two_agents(live, "rvfinal")
    use(a)
    call("crew_claim", paths=[PATH])
    closed = as_json(call("close_session", summary="wrapping up", end_reason=reason, project_id=project))
    assert closed["crew"]["session_left"] is True and closed["crew"]["claims_released"] == 1, closed
    assert session_row(live, project, a_sign)["state"] == "ended"


@pytest.mark.parametrize("reason", ["checkpoint", "milestone done; goal remains active", "checkpoint before the final push"])
def test_a_milestone_reason_still_keeps_the_seat(live, clock, reason):
    project, a, a_sign, *_ = _two_agents(live, "rvkeep")
    use(a)
    call("crew_claim", paths=[PATH])
    closed = as_json(call("close_session", summary="so far", end_reason=reason, project_id=project))
    assert closed["crew"]["session_left"] is False, closed
    assert claim_states(live, project) == ["active"]
    # explicit final=false wins over wording that says the session is over
    kept = as_json(call("close_session", summary="so far", end_reason="final milestone", project_id=project, final=False))
    assert kept["crew"]["session_left"] is False, kept


def test_a_server_with_no_done_field_is_asked_twice_only_once(live, clock, monkeypatch):
    """Each double send stores two handoffs and counts twice against the relay limits."""
    project = solo(live, "rvonce")
    with monkeypatch.context() as old:
        sent = _as_old_server(old)
        first = as_json(call("close_session", done=["one"], project_id=project))
        assert len(sent) == 2 and "Done (declared by the agent): one" in first["rendered"]
        sent.clear()
        second = as_json(call("close_session", done=["two", "three"], facts={"notes": "n"}, project_id=project))
        assert len(sent) == 1 and "done" not in sent[0]["facts"], sent
        assert "Notes (agent): Done (declared by the agent): two; three n" in second["rendered"]
        assert "done: this server has no field for declared work, kept in notes" in second["normalized"]
        assert second["superseded"] == [first["handoff_id"]], second
    # the server is upgraded while this MCP process lives: the next close finds out and uses the field again
    sent_new: list[dict[str, Any]] = []
    real = server.Memory._request

    def spy(self: Any, method: str, path: str, json: Any = None, **kw: Any) -> Any:
        if path == "/api/v1/session/close":
            sent_new.append(json)
        return real(self, method, path, json=json, **kw)

    monkeypatch.setattr(server.Memory, "_request", spy)
    third = as_json(call("close_session", done=["four"], project_id=project))
    assert len(sent_new) == 1 and "Done (declared by the agent): four" in third["rendered"]
    fourth = as_json(call("close_session", done=["five"], project_id=project))
    assert len(sent_new) == 2 and sent_new[1]["facts"]["done"] == ["five"], sent_new
    assert "Done:\n- five\n" in fourth["rendered"] and "kept in notes" not in json.dumps(fourth.get("normalized", []))


def test_an_old_server_learned_from_a_close_with_nothing_declared(live, clock, monkeypatch):
    project = solo(live, "rvlearn")
    sent = _as_old_server(monkeypatch)
    as_json(call("close_session", facts={"notes": "first"}, project_id=project))
    assert len(sent) == 1
    out = as_json(call("close_session", done=["shipped"], project_id=project))
    assert len(sent) == 2, sent  # one request each
    assert "Done (declared by the agent): shipped" in out["rendered"]
