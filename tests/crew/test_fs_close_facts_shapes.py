"""F16: close_session and crew_checkpoint / crew_report accept each other's vocabulary for the same facts.

Through ``FastMCP.call_tool`` and the REST route against the live API harness: the checkpoint
shape given to a close and the close shape given to a checkpoint are both recorded (not refused
with 422 / INVALID ARGUMENTS), a run with failures is never recorded as passing, the client
alone is enough against a server that does not normalize, the server alone is enough for a
client that does not, the tool publishes the shape, and a 422 reads as ``loc: msg``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import remembra.api.v1.relay as relay_api
import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from remembra.client.memory import MemoryError as ClientError
from tests.crew.test_mcp_crew import _two_agents, agent, as_json, call, crew_id_of, project_name, use

live = base.live  # the module-scoped live API and the per-test MCP bridge, as fixtures of this module
clock = base.clock

CHECKPOINT_VOCABULARY = {
    "tests": [{"command": "pytest", "passed": 170, "failed": 0}],
    "commits": ["1ec6d03"],
    "unpushed_commits": ["366bd0c", "a0fda65"],
}


def solo(live: Any, prefix: str) -> str:
    project = project_name(prefix)
    use(agent(live, "claude-desktop", f"{project}-a", project))
    return project


def rest_close(live: Any, project: str, facts: dict[str, Any], status: int = 200) -> Any:
    return live.api(
        "POST",
        "/session/close",
        headers={"X-API-Key": live.state["key"]},
        json={"agent_id": "codex", "session_id": f"{project}-rest", "project_id": project, "facts": facts},
        status=status,
    )


def test_close_session_accepts_the_checkpoint_vocabulary(live, clock):
    project = solo(live, "f16close")
    facts = {**CHECKPOINT_VOCABULARY, "branch": "feat/staff-access-s1..s4"}
    out = as_json(call("close_session", summary="s", facts=facts, project_id=project))
    assert out["status"] == "ok", out
    rendered = out["rendered"]
    assert "- 1ec6d03" in rendered
    assert "- tests passing: pytest (170 passed, 0 failed)" in rendered
    assert "2 commit(s) not pushed" in rendered
    assert "Branch as declared (not a valid git branch name): feat/staff-access-s1..s4" in rendered
    assert any("tests[0].passed: 170 read as true" in line for line in out["normalized"]), out["normalized"]
    assert any(line.startswith("unpushed_commits: a list of 2") for line in out["normalized"])


def test_close_session_normalizes_before_sending_so_an_older_server_accepts_it(live, clock, monkeypatch):
    """A 0.17.5 server validates the facts as sent: the client's rewrite alone must be enough."""
    monkeypatch.setattr(relay_api, "normalize_close_facts", lambda facts, **_: (facts, []))
    project = solo(live, "f16old")
    out = as_json(call("close_session", facts={**CHECKPOINT_VOCABULARY, "notes": ["a", "b"]}, project_id=project))
    assert out["status"] == "ok", out
    assert "- tests passing: pytest (170 passed, 0 failed)" in out["rendered"]
    assert "Notes (agent): a; b" in out["rendered"]


def test_rest_close_accepts_the_checkpoint_vocabulary_from_an_older_client(live, clock):
    project = project_name("f16rest")
    res = rest_close(live, project, CHECKPOINT_VOCABULARY)
    assert "- tests passing: pytest (170 passed, 0 failed)" in res["rendered"]
    assert "- 1ec6d03" in res["rendered"] and "2 commit(s) not pushed" in res["rendered"]
    assert any("commits[0]" in line for line in res["facts_normalized"]), res["facts_normalized"]
    # the well-formed shape is passed through and reported as untouched
    plain = rest_close(live, project_name("f16rest"), {"tests": [{"cmd": "pytest", "passed": True, "summary": "ok"}]})
    assert plain["facts_normalized"] == []
    # the branch rule (CLI-02 / CLI-07) is unchanged at the API
    refused = rest_close(live, project, {"branch": "feat/staff-access-s1..s4"}, status=422)
    assert "valid git branch name" in json.dumps(refused)


def test_a_run_with_failures_is_never_recorded_as_passing(live, clock):
    """0.17.5 coerced ``passed: 1`` to true and dropped ``failed``: a failing run went into the handoff as passing."""
    res = rest_close(live, project_name("f16green"), {"tests": [{"cmd": "pytest", "passed": 1, "failed": 2}]})
    assert "FAILING: pytest (1 passed, 2 failed)" in res["rendered"]
    assert "tests passing" not in res["rendered"]
    project = solo(live, "f16green")
    out = as_json(call("close_session", facts={"tests": [{"cmd": "pytest", "passed": True, "failed": 3}]}, project_id=project))
    assert "FAILING: pytest" in out["rendered"] and "tests passing" not in out["rendered"]


def test_a_test_entry_with_no_result_goes_to_the_notes_not_to_a_verdict(live, clock):
    project = solo(live, "f16text")
    out = as_json(
        call(
            "close_session",
            facts={"tests": "ran the suite by hand", "unpushed_commits": "unknown", "commits": ["shipped the fix"]},
            project_id=project,
        )
    )
    assert out["status"] == "ok", out
    rendered = out["rendered"]
    assert "Tests as declared (no pass/fail result recorded): ran the suite by hand" in rendered
    assert "Commits as declared (no commit id): shipped the fix" in rendered
    assert "tests passing" not in rendered and "FAILING" not in rendered and "not pushed" not in rendered


def checkpoint_facts(live: Any, project: str) -> list[dict[str, Any]]:
    rows = live.crew_rows(
        "SELECT facts FROM crew_checkpoints WHERE crew_id = ? ORDER BY created_at", (crew_id_of(live, project),)
    )
    return [json.loads(r["facts"]) for r in rows]


def test_crew_checkpoint_accepts_the_close_vocabulary(live, clock):
    project, a, *_ = _two_agents(live, "f16ckp")
    use(a)
    long_cmd = "pytest -q " + "x" * 400
    out = call(
        "crew_checkpoint",
        files_changed=["src/app/pos/a.ts"],
        summary="progress",
        commits=[{"sha": "bd6ab83", "subject": "x"}, "1ec6d03 second"],
        tests=[
            {"cmd": long_cmd, "passed": True, "summary": "170 passed"},
            {"command": "npm test", "passed": 3},
            {"cmd": "ruff check", "passed": False},
            "ran the linter by hand",
        ],
    )
    assert out.startswith("CHECKPOINT ckp_"), out
    facts = checkpoint_facts(live, project)[-1]
    assert facts["commits"] == [{"sha": "bd6ab83"}, {"sha": "1ec6d03"}]
    assert facts["tests"] == [
        {"command": long_cmd[:256], "passed": 1, "failed": 0},
        {"command": "npm test", "passed": 3, "failed": 0},
        {"command": "ruff check", "passed": 0, "failed": 1},
    ]
    assert facts["notes"].startswith("progress")
    assert "170 passed" in facts["notes"] and "ran the linter by hand" in facts["notes"]


def test_crew_checkpoint_in_its_own_vocabulary_is_unchanged(live, clock):
    project, a, *_ = _two_agents(live, "f16same")
    use(a)
    out = call(
        "crew_checkpoint",
        files_changed=["src/app/pos/a.ts"],
        commits=["abc1234"],
        tests=[{"command": "npm test", "passed": 4, "failed": 1}],
    )
    assert out.startswith("CHECKPOINT ckp_"), out
    facts = checkpoint_facts(live, project)[-1]
    assert facts == {
        "files_changed": ["src/app/pos/a.ts"],
        "commits": [{"sha": "abc1234"}],
        "tests": [{"command": "npm test", "passed": 4, "failed": 1}],
    }


def test_crew_report_accepts_the_close_vocabulary(live, clock):
    project, a, *_ = _two_agents(live, "f16rpt")
    use(a)
    call("crew_task", action="create", title="Export", zones=["reports"])
    call("crew_task", action="start", task="T-1")
    out = call(
        "crew_report",
        task="T-1",
        sections={"done": ["export works"]},
        commits=[{"sha": "bd6ab83", "subject": "export"}],
        tests=[{"cmd": "npm test -- export", "passed": True, "summary": "4 passed"}],
        summary="export done",
    )
    assert out.startswith("REPORT rpt_"), out
    rows = live.crew_rows("SELECT commits, tests FROM crew_reports WHERE crew_id = ?", (crew_id_of(live, project),))
    assert "bd6ab83" in rows[-1]["commits"]
    recorded = json.loads(rows[-1]["tests"])
    assert len(recorded) == 1 and "npm test -- export" in json.dumps(recorded[0]), recorded


def test_the_close_session_tool_publishes_the_facts_shape():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    facts = tools["close_session"].inputSchema["properties"]["facts"]
    text = json.dumps(facts)
    for key in ("tests", "commits", "unpushed_commits", "cmd", "passed", "sha"):
        assert f'"{key}"' in text, (key, facts)
    assert "boolean" in text and "integer" in text
    checkpoint = json.dumps(tools["crew_checkpoint"].inputSchema["properties"])
    assert '"command"' in checkpoint and '"failed"' in checkpoint


def test_a_422_reads_as_loc_and_message_not_a_python_list(live, clock):
    client = agent(live, "codex", "f16-422", project_name("f16err"))
    with pytest.raises(ClientError) as err:
        client.close_session(facts={"no_upstream": "perhaps"})
    text = str(err.value)
    assert err.value.status_code == 422
    assert "[{" not in text and "'loc'" not in text, text
    assert text.startswith("Request failed: facts.no_upstream: "), text
    # the MCP tool adds the expected shape to a refused close
    use(client)
    out = json.loads(call("close_session", facts={"no_upstream": "perhaps"}))
    assert out["status"] == "error" and out["code"] == 422
    assert "facts.no_upstream" in out["error"] and '"cmd"' in out["expected_facts"], out
