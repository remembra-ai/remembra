"""F18: the handoff grader must not flag honest handoffs.

A statement that work was NOT pushed or deployed is not a deploy claim; a
commit id, file or library name that is merely absent from the recorded facts
is unverifiable, not a contradiction; Remembra's own ids are not commit ids;
and an MCP ``close_session`` given a local ``root_path`` reads git itself, so
the handoff does not say ``(no git info)`` for committed work.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.relay.handoff import (
    HEALTH_CONFLICTED,
    HEALTH_INCOMPLETE,
    assess_handoff,
    build_sections,
    check_summary_grounding,
)
from remembra.security.untrusted import unwrap_untrusted
from tests.agent_api_harness import build_api
from tests.relay_fixtures import commit, git, make_remote_and_clones

UNRECORDED_PUSH = "claims pushed/deployed; push state was not recorded (unverifiable)"

NEGATED = [
    "Dashboard files are ready for root integration, not committed or deployed by this agent.",
    "Edits are local: not staged/committed/deployed.",
    "The new route is not wired/deployed.",
    "Local files only; nothing committed, pushed, deployed or published.",
    "The fix hasn't been deployed.",
    "The build isn't deployed and the branch wasn't merged.",
    "Not live on the server yet.",
    "Committed: yes. Pushed: no. Deployed: no.",
    "state: committed=true pushed=false deployed=false",
    "Committed locally, never pushed or deployed.",
    "Nothing was pushed or deployed.",
    "Committed, not pushed.",
    "Not yet deployed.",
]

NOT_A_CLAIM = [
    "Ready to be deployed after review.",
    "The branch needs to be pushed.",
    "This will be merged once review passes.",
    "Merged main into the feature branch locally.",
]


@pytest.mark.parametrize("summary", NEGATED + NOT_A_CLAIM)
def test_a_statement_that_work_is_not_deployed_is_not_a_deploy_claim(summary):
    grounding = check_summary_grounding(summary, {})
    assert grounding["issues"] == []
    assert "pushed/deployed" not in grounding["checked"]
    assert grounding["status"] == "consistent"


@pytest.mark.parametrize(
    "summary",
    [
        "Pushed to origin and deployed to prod; docs not merged.",
        "No errors, pushed and deployed.",
        "Not tested but deployed.",
        "The hotfix is live on prod. The docs are not merged.",
        "Pushed: yes. Deployed: no.",
    ],
)
def test_a_negation_elsewhere_does_not_hide_a_real_deploy_claim(summary):
    grounding = check_summary_grounding(summary, {"unpushed_commits": 3, "upstream": "origin/main"})
    assert "claims pushed/deployed, but 3 commit(s) are not pushed to origin/main" in grounding["issues"]
    assert grounding["status"] == "contradicted"
    # With no push state recorded the same claim is unverifiable, never a contradiction.
    unknown = check_summary_grounding(summary, {})
    assert UNRECORDED_PUSH in unknown["issues"] and unknown["status"] == "consistent"


def test_a_real_claim_with_pushed_state_recorded_is_consistent():
    grounding = check_summary_grounding("Pushed and deployed.", {"unpushed_commits": 0})
    assert grounding == {"status": "consistent", "issues": [], "checked": ["pushed/deployed"]}


@pytest.mark.parametrize(
    "facts",
    [
        {},
        {"branch": "main", "head_commit": "a" * 40, "commits": [{"sha": "b" * 40, "subject": "feat: x"}]},
        {"facts_source": "relay-cli:git", "commits": [{"sha": "c" * 40, "subject": "fix: y"}], "files_changed": ["src/a.py"]},
    ],
)
def test_an_unrecorded_commit_or_file_is_unverifiable_not_contradicted(facts):
    summary = "Fixed in 4b478d5 on main; live build is 6cf1d069. See docs/audit/AUDIT-FIRM-SHORTLIST.md."
    grounding = check_summary_grounding(summary, facts)
    assert grounding["status"] == "consistent"
    assert all("(unverifiable)" in issue for issue in grounding["issues"])
    joined = " | ".join(grounding["issues"])
    assert "4b478d5" in joined and "6cf1d069" in joined and "docs/audit/AUDIT-FIRM-SHORTLIST.md" in joined
    health = assess_handoff(facts, grounding)
    assert health["status"] != HEALTH_CONFLICTED
    assert not any(w.startswith("summary contradicted") for w in health["warnings"])
    assert any(w.startswith("summary claim not checkable: mentions commit 4b478d5") for w in health["warnings"])


def test_no_git_state_grades_incomplete_not_conflicted():
    grounding = check_summary_grounding("Fixed in 4b478d5 on main.", {})
    health = assess_handoff({}, grounding)
    assert health["status"] == HEALTH_INCOMPLETE and health["missing"] == ["no git state recorded"]


@pytest.mark.parametrize(
    "summary",
    [
        "Ran deploy-release.sh to publish the build.",
        "Viewer built with Three.js and Next.js on Node.js.",
        "Branch release/3.5.93 was cut from github.com/acme/repo.git.",
        "Sent findings to inbox_43f628eeb81d4fca; handoff 7565d28a-aacf-412b-98fb-a3751a9c635d.",
        "See https://github.com/acme/repo/blob/main/docs/guide.md for the steps.",
    ],
)
def test_tool_library_branch_and_id_names_are_not_claims(summary):
    grounding = check_summary_grounding(summary, {"files_changed": ["src/app.py"], "commits": [{"sha": "d" * 40}]})
    assert grounding == {"status": "consistent", "issues": [], "checked": []}


def test_recorded_commits_and_files_are_still_checked():
    facts = {"commits": [{"sha": "abc1234" + "0" * 33, "subject": "feat"}], "files_changed": ["src/widget.py", "README.md"]}
    grounding = check_summary_grounding("Changed src/widget.py and README.md in abc1234.", facts)
    assert grounding["issues"] == []
    assert grounding["checked"] == ["commit abc1234", "file src/widget.py", "file README.md"]


def test_tests_pass_with_no_recorded_run_is_unverifiable_and_a_failed_run_still_contradicts():
    none_recorded = check_summary_grounding("All tests pass.", {})
    assert none_recorded["status"] == "consistent"
    assert none_recorded["issues"] == ["claims tests pass, but no test run was recorded (unverifiable)"]
    failed = check_summary_grounding("All tests pass.", {"tests": [{"cmd": "pytest -q", "passed": False}]})
    assert failed["status"] == "contradicted"


def test_headline_names_the_recorded_checkout_when_nothing_changed():
    assert build_sections({})["headline"] == "no commits or file changes"
    sections = build_sections({"branch": "main", "head_commit": "abc1234" + "0" * 33})
    assert sections["headline"] == "no commits or file changes (at main@abc1234)"


# ---------------------------------------------------------------------------
# MCP close_session reads git from a local root_path
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


@pytest.fixture()
def mcp_env(api, monkeypatch):
    http = api["http"]

    class ASGIMemory(Memory):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)
            self._client.close()
            self._client = http

    monkeypatch.setattr(server, "Memory", ASGIMemory)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "codex")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "default")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "mcp-f18")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    return json.loads(unwrap_untrusted(raw))


@pytest.fixture()
def work(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    return clones["laptop"]


def test_mcp_close_reads_git_from_root_path(mcp_env, work):
    brief = _j(server.session_brief(root_path=str(work), compact=True))  # remembers the session's start HEAD
    project = brief["project_id"]
    sha = commit(work, "src/widget.py", "x = 1\n", "feat: widget")
    (work / "NOTES.md").write_text("wip\n")
    out = _j(server.close_session(summary=f"Added the widget in {sha[:7]}; committed, not pushed.", root_path=str(work)))
    assert out["status"] == "ok" and out["project_id"] == project
    rendered = out["rendered"]
    assert "(no git info)" not in rendered and f"main@{sha[:7]}" in rendered
    assert f"- {sha[:7]} feat: widget" in rendered
    assert "1 commit(s) not pushed to origin/main" in rendered
    assert "uncommitted changes in 1 file(s): NOTES.md" in rendered
    assert out["headline"].startswith("1 commit(s), last: feat: widget")
    # (An abbreviated id that happens to be all digits reads as a number and is not checked.)
    checked = [] if sha[:7].isdigit() else [f"commit {sha[:7]}"]
    assert out["grounding"] == {"status": "consistent", "issues": [], "checked": checked}
    assert "Facts: declared by the agent (not checked)." in rendered  # the label an old server also knows


def test_mcp_close_uses_the_root_the_brief_remembered_and_keeps_declared_facts(mcp_env, work):
    _j(server.session_brief(root_path=str(work), compact=True))
    sha = commit(work, "src/widget.py", "x = 1\n", "feat: widget")
    git(work, "push", "-q", "origin", "main")
    out = _j(server.close_session(summary="Pushed.", facts={"branch": "declared-branch", "unpushed_commits": 2}))
    rendered = out["rendered"]
    assert f"declared-branch@{sha[:7]}" in rendered  # declared keys win; missing ones are read
    assert "2 commit(s) not pushed" in rendered
    assert out["grounding"]["status"] == "contradicted"  # judged against what the agent declared


def test_mcp_close_without_a_session_start_does_not_invent_commits(mcp_env, work):
    sha = commit(work, "src/widget.py", "x = 1\n", "feat: widget")  # made before this MCP session knew the repo
    out = _j(server.close_session(root_path=str(work)))
    rendered = out["rendered"]
    assert f"main@{sha[:7]}" in rendered and "feat: widget" not in rendered
    assert out["headline"].startswith("no commits or file changes (at main@")


def test_mcp_close_outside_git_or_on_a_remote_server_reads_nothing(mcp_env, work, tmp_path, monkeypatch):
    folder = tmp_path / "plain"
    folder.mkdir()
    out = _j(server.close_session(next_step="carry on", root_path=str(folder)))
    assert out["status"] == "ok" and "(no git info)" in out["rendered"]

    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    assert server._close_git_facts(str(work), None) == {}


def test_mcp_close_by_project_id_still_reads_the_sessions_commits(mcp_env, work):
    """A session that names its project by id (and its checkout by root_path) gets the same git facts."""
    _j(server.session_brief(project_id="widget", root_path=str(work), compact=True))
    sha = commit(work, "src/widget.py", "x = 1\n", "feat: widget")
    out = _j(server.close_session(project_id="widget", root_path=str(work)))
    assert out["project_id"] == "widget"
    assert f"- {sha[:7]} feat: widget" in out["rendered"] and f"main@{sha[:7]}" in out["rendered"]
    # Without root_path the remembered checkout belongs to the brief's project only.
    other = _j(server.close_session(project_id="another", session_id="s-other"))
    assert "(no git info)" in other["rendered"]
