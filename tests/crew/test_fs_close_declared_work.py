"""F17: work an agent declares at close reaches the handoff, whatever it called the keys.

Through ``FastMCP.call_tool`` and the REST route against the live API harness: ``done`` is a
real channel (facts key and tool argument), the names agents guess (``open``, ``next``,
``failing``, ``open_work``, ``project``) are read as the fields they mean, a key the handoff has
no field for is kept in the notes instead of vanishing, and a close that replaced an earlier
handoff says so.
"""

from __future__ import annotations

from typing import Any

import remembra.mcp.server as server
import tests.crew.test_mcp_crew as base
from remembra.mcp.crew import CrewHttp
from tests.crew.test_mcp_crew import _two_agents, agent, as_json, call, project_name, use

live = base.live  # the module-scoped live API and the per-test MCP bridge, as fixtures of this module
clock = base.clock

EMPTY = (
    "Done:\n- nothing recorded\nNot done / open:\n- nothing open recorded\n"
    "Failing / errors:\n- none recorded\nNext step: none recorded"
)
FIELD_FACTS = {
    "done": ["verified the live deploy", "rotated the signing key", "uploaded artifacts to the T7 drive"],
    "open": ["owner review of the concept board"],
    "failing": ["e2e checkout is flaky"],
    "next": "wait for the owner's review",
    "notes": "Release 5667b29 is live",
}


def solo(live: Any, prefix: str) -> str:
    project = project_name(prefix)
    use(agent(live, "claude-desktop", f"{project}-a", project))
    return project


def rest_close(live: Any, project: str, facts: dict[str, Any], session: str = "rest") -> Any:
    return live.api(
        "POST",
        "/session/close",
        headers={"X-API-Key": live.state["key"]},
        json={"agent_id": "codex", "session_id": f"{project}-{session}", "project_id": project, "facts": facts},
    )


def assert_field_facts_rendered(rendered: str) -> None:
    assert EMPTY not in rendered
    assert "Done:\n- verified the live deploy\n- rotated the signing key\n- uploaded artifacts to the T7 drive\n" in rendered
    assert "- TODO: owner review of the concept board" in rendered
    assert "- error: e2e checkout is flaky" in rendered
    assert "Next step (agent): wait for the owner's review" in rendered
    assert "Notes (agent): Release 5667b29 is live" in rendered


def test_facts_done_open_failing_next_reach_the_handoff(live, clock):
    """The field case: status ok, and the stored handoff said nothing was done, open or next."""
    project = solo(live, "f17facts")
    out = as_json(call("close_session", summary="release shipped", facts=dict(FIELD_FACTS), project_id=project))
    assert out["status"] == "ok", out
    assert_field_facts_rendered(out["rendered"])
    assert out["headline"].startswith("3 item(s) declared done"), out["headline"]
    assert "no commits or file changes [" not in out["headline"] and out["headline"].endswith("[1 open, 1 failing]")
    assert any(line.startswith("open: read as todos_open") for line in out["normalized"]), out["normalized"]


def test_rest_close_reads_the_same_keys_from_an_older_client(live, clock):
    res = rest_close(live, project_name("f17rest"), dict(FIELD_FACTS))
    assert_field_facts_rendered(res["rendered"])
    assert res["sections"]["done"][:3] == FIELD_FACTS["done"]


def test_done_open_work_and_project_are_tool_arguments(live, clock):
    """Top-level arguments the tool did not declare were dropped before it ran; ``project`` filed the handoff elsewhere."""
    solo(live, "f17default")
    target = project_name("f17target")
    out = as_json(
        call(
            "close_session",
            summary="s",
            done=["deployed to staging"],
            open_work=["write the migration"],
            todos_open=["update docs"],
            project=target,
        )
    )
    assert out["status"] == "ok" and out["project_id"] == target, out
    assert "- deployed to staging" in out["rendered"]
    assert "- TODO: write the migration" in out["rendered"] and "- TODO: update docs" in out["rendered"]
    # one string where a list is expected is one item, not an argument error
    one = as_json(call("close_session", done="shipped", open_work="review", errors="lint fails", project=target))
    assert "- shipped" in one["rendered"] and "- TODO: review" in one["rendered"] and "- error: lint fails" in one["rendered"]


def test_aliases_add_to_the_field_they_mean(live, clock):
    project = solo(live, "f17merge")
    out = as_json(
        call(
            "close_session",
            facts={"todos_open": ["a"], "open": ["b"], "not_done": "c", "errors": ["e1"], "unresolved_failures": ["e2"]},
            todos_open=["d"],
            project_id=project,
        )
    )
    rendered = out["rendered"]
    for item in ("TODO: a", "TODO: b", "TODO: c", "TODO: d", "error: e1", "error: e2"):
        assert f"- {item}" in rendered, (item, rendered)


def test_a_key_the_handoff_has_no_field_for_is_kept_in_the_notes(live, clock):
    project = solo(live, "f17other")
    out = as_json(
        call(
            "close_session",
            facts={"committed": True, "pushed": False, "deployed": "not yet", "live_verified": None, "notes": "mine"},
            project_id=project,
        )
    )
    assert "Notes (agent): mine Other declared facts: committed: true; pushed: false; deployed: not yet" in out["rendered"]
    assert any(line.startswith("committed, pushed, deployed: no handoff field") for line in out["normalized"]), out["normalized"]
    # the relay CLI's own extra keys (git-read facts) are not agent prose: they stay out of the notes
    res = rest_close(
        live, project_name("f17cli"), {"facts_source": "relay-cli:git", "commit_range": "session-reflog", "branch": "main"}
    )
    assert "Other declared facts" not in res["rendered"] and "commit_range" not in res["rendered"]


def test_declared_work_and_commits_both_show_in_the_brief(live, clock):
    project = solo(live, "f17brief")
    commits = [{"sha": f"{i}abc12{i}", "subject": f"commit number {i}"} for i in range(1, 6)]
    call("close_session", facts={"done": ["verified the live deploy", "b", "c"], "commits": commits}, project_id=project)
    brief = as_json(call("session_brief", project_id=project))["brief"]
    line = next(text for text in brief.splitlines() if text.startswith("Last session:"))
    assert "done: verified the live deploy; b; 1abc121 commit number 1; 2abc122 commit number 2 /" in line, line


def test_a_close_that_replaced_an_earlier_handoff_says_so(live, clock):
    project = solo(live, "f17super")
    first = as_json(call("close_session", done=["one"], project_id=project))
    assert "superseded" not in first
    second = as_json(call("close_session", done=["two"], project_id=project))
    assert second["superseded"] == [first["handoff_id"]], second
    assert "replaced" in second["note"]


def _as_old_server(monkeypatch: Any) -> list[dict[str, Any]]:
    """The wire as a 0.17.5 server leaves it: ``facts.done`` is an unknown key (dropped) and the
    response has no ``facts_normalized``. Returns the request bodies that were sent."""
    sent: list[dict[str, Any]] = []
    real_request = server.Memory._request
    real_call = CrewHttp.call

    def old(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {**payload, "facts": {k: v for k, v in payload["facts"].items() if k != "done"}}

    def request(self: Any, method: str, path: str, json: Any = None, **kw: Any) -> Any:
        if path != "/api/v1/session/close":
            return real_request(self, method, path, json=json, **kw)
        body = real_request(self, method, path, json=old(json), **kw)
        body.pop("facts_normalized", None)
        return body

    def crew_call(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path != "/session/close":
            return real_call(self, method, path, **kw)
        status, body, headers = real_call(self, method, path, **{**kw, "json": old(kw["json"])})
        body.pop("facts_normalized", None)
        return status, body, headers

    monkeypatch.setattr(server.Memory, "_request", request)
    monkeypatch.setattr(CrewHttp, "call", crew_call)
    return sent


def test_declared_work_survives_a_server_with_no_done_field(live, clock, monkeypatch):
    sent = _as_old_server(monkeypatch)
    project = solo(live, "f17old")
    out = as_json(
        call("close_session", done=["verified the live deploy", "rotated the key"], facts={"notes": "n"}, project_id=project)
    )
    assert out["status"] == "ok", out
    assert "Notes (agent): Done (declared by the agent): verified the live deploy; rotated the key n" in out["rendered"]
    assert len(sent) == 2 and "done" not in sent[1]["facts"]
    assert "superseded" not in out  # the first copy, stored without the declared work, is not the agent's earlier handoff
    assert "done: this server has no field for declared work, kept in notes" in out["normalized"]
    # nothing declared: one request, as before
    sent.clear()
    as_json(call("close_session", facts={"notes": "second"}, project_id=project))
    assert len(sent) == 1


def test_declared_work_survives_an_old_server_with_a_crew_seat(live, clock, monkeypatch):
    project, a, a_sign, *_ = _two_agents(live, "f17oldcrew")
    use(a)
    call("crew_claim", zone="billing")
    sent = _as_old_server(monkeypatch)
    out = as_json(call("close_session", done=["shipped billing"], end_reason="done", project_id=project))
    assert "Done (declared by the agent): shipped billing" in out["rendered"]
    assert len(sent) == 2
    # the crew result is the first close's: the session left once and its claim was released
    assert out["crew"]["session_left"] is True and out["crew"]["claims_released"] == 1, out["crew"]
