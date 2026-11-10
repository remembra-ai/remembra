"""Review fixes for F18, F31 and F45 (handoff grading).

* F31: a command that runs a test runner without running a test (``pytest
  --version``, ``--collect-only``, ``cargo test --no-run``) or a green run with
  no sign that tests ran never erases a failing run; neither does a green run
  made in another directory. ``yarn jest`` / ``pnpm vitest run`` are test runs,
  and a failing entry a client sent is never dropped. A declared verdict is
  overridden only by a runner's summary line, not by prose.
* F18: a negation in one comma clause does not hide a deploy claim in the
  next, and an MCP close that names another project by ``git_remote`` does not
  read the checkout the session's brief remembered.
* F45: a newer idle close whose open item differs from the older one (another
  number, another word) is not a repeat. A no-change close with a summary or
  with commits made this session is stored.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory
from remembra.crew.reports import _test_passed
from remembra.relay import facts as relay_facts
from remembra.relay.handoff import (
    RELAY_ROW_SOURCE,
    assess_handoff,
    build_sections,
    check_summary_grounding,
    claims_pushed,
    repeated_idle_closes,
    sections_have_substance,
)
from remembra.relay.testruns import resolve_runs, run_verdict, runner_invocations, summary_shows_failure
from remembra.security.untrusted import unwrap_untrusted
from tests.agent_api_harness import build_api
from tests.relay_fixtures import Transcript, commit, git, make_remote_and_clones

GIT = {
    "branch": "main",
    "head_commit": "abc1234",
    "unpushed_commits": 0,
    "upstream": "origin/main",
    "commits": [{"sha": "abc1234", "subject": "s"}],
    "files_changed": ["src/a.py"],
}
RED = {
    "cmd": "pytest tests/test_a.py -q",
    "exit_code": 1,
    "output": "FAILED tests/test_a.py::test_x - assert 0\n1 failed, 3 passed in 0.10s\n",
}
RED_ALL = {"cmd": "pytest tests -q", "exit_code": 1, "output": "1 failed, 3 passed in 0.10s\n"}


def _finish(log: list[dict[str, Any]]) -> dict[str, Any]:
    out = relay_facts._finish(relay_facts.TranscriptFacts(), log, [], [])
    return {
        **GIT,
        "tests": out.tests,
        "errors": out.errors,
        "commands": [{"cmd": c["cmd"], "exit_code": c["exit_code"]} for c in log],
        "facts_source": "relay-cli:git+transcript",
    }


# ---------------------------------------------------------------------------
# F31: a red run is not erased by a command that ran no test
# ---------------------------------------------------------------------------

NOT_A_RUN = [
    [
        RED,
        {
            "cmd": "pytest tests/test_a.py --collect-only -q",
            "exit_code": 0,
            "output": "tests/test_a.py::test_x\n4 tests collected in 0.01s\n",
        },
    ],
    [RED, {"cmd": "pytest tests/test_a.py -q -x --co", "exit_code": 0, "output": "4 tests collected in 0.01s\n"}],
    [RED_ALL, {"cmd": "pytest --version", "exit_code": 0, "output": "pytest 8.3.2\n"}],
    [RED_ALL, {"cmd": "pytest --fixtures -q", "exit_code": 0, "output": "cache -- ...\n"}],
    [RED_ALL, {"cmd": "pytest -h", "exit_code": 0, "output": "usage: pytest [options]\n"}],
    [
        {"cmd": "cargo test", "exit_code": 101, "output": "test result: FAILED. 3 passed; 1 failed; 0 ignored\n"},
        {"cmd": "cargo test --no-run", "exit_code": 0, "output": "Finished test profile\n"},
    ],
    [
        {"cmd": "go test ./pkg/a", "exit_code": 1, "output": "FAIL\tx/pkg/a\t0.01s\nFAIL\n"},
        {"cmd": "go test -run NONE ./pkg/a", "exit_code": 0, "output": "ok  \tx/pkg/a\t0.01s [no tests to run]\n"},
    ],
    # A green exit with no sign that a test ran (the output was not captured).
    [RED, {"cmd": "pytest tests/test_a.py -q -x", "exit_code": 0, "output": ""}],
    [RED, {"cmd": "pytest -q -k nothing_matches", "exit_code": 0, "output": "4 deselected in 0.01s\n"}],
    # A green whole-suite run made after moving to another directory.
    [
        RED,
        {"cmd": "cd ../other-repo", "exit_code": 0, "output": ""},
        {"cmd": "pytest -q", "exit_code": 0, "output": "9 passed in 0.10s\n"},
    ],
    [
        {**RED, "cwd": "/work/shop"},
        {"cmd": "pytest tests/test_a.py -q", "exit_code": 0, "output": "4 passed in 0.10s\n", "cwd": "/work/billing"},
    ],
]


@pytest.mark.parametrize(
    "log", NOT_A_RUN, ids=[log[-1]["cmd"] + ("@" + log[-1]["cwd"] if "cwd" in log[-1] else "") for log in NOT_A_RUN]
)
def test_a_failing_run_is_not_erased_by_a_command_that_ran_no_test_of_it(log):
    facts = _finish(log)
    red = log[0]["cmd"]
    assert (red, False) in [(t["cmd"], t["passed"]) for t in facts["tests"]]
    sections = build_sections(facts)
    assert any(line.startswith(f"FAILING: {red}") for line in sections["failing"])
    health = assess_handoff(facts, check_summary_grounding("All tests pass.", facts))
    assert health["status"] == "blocked" and "1 failing test run(s)" in health["missing"]


@pytest.mark.parametrize(
    "cmd",
    ["pytest --version", "pytest --collect-only -q", "pytest --fixtures", "cargo test --no-run", "tox -l", "jest --listTests"],
)
def test_a_runner_that_runs_no_test_is_not_a_passing_test_run(cmd):
    assert run_verdict(cmd, 0, "whatever\n") is None
    facts = _finish([{"cmd": cmd, "exit_code": 0, "output": "whatever\n"}])
    assert facts["tests"] == [] and not any("tests passing" in line for line in build_sections(facts)["done"])
    # The same entry from a client that still lists it: left out of a transcript-collected handoff.
    sent = {**GIT, "tests": [{"cmd": cmd, "passed": True, "summary": None}], "facts_source": "relay-cli:git+transcript"}
    assert not any("tests passing" in line for line in build_sections(sent)["done"])


def test_a_real_green_rerun_still_replaces_the_red_run():
    for green in (
        {"cmd": "pytest tests/test_a.py -q -x", "exit_code": 0, "output": "4 passed in 0.10s\n"},
        {"cmd": "pytest -q", "exit_code": 0, "output": "40 passed in 1.10s\n"},
        {"cmd": "pytest tests/test_a.py -q 2>&1 | tail -3", "exit_code": 0, "output": "4 passed in 0.10s\n"},
    ):
        facts = _finish([RED, green])
        assert [(t["cmd"], t["passed"]) for t in facts["tests"]] == [(green["cmd"], True)], green
    # The same directory named the same way, and the shell's directory unchanged between the runs.
    red = {"cmd": "cd api && pytest -q", "exit_code": 1, "output": "1 failed in 0.10s\n"}
    green = {"cmd": "cd api && pytest -q -x", "exit_code": 0, "output": "4 passed in 0.10s\n"}
    assert [t["passed"] for t in _finish([red, {"cmd": "cd /work/shop", "exit_code": 0, "output": ""}, green])["tests"]] == [
        False,
        True,
    ]
    assert [t["passed"] for t in _finish([red, green])["tests"]] == [True]
    same_dir = [
        {**RED, "cwd": "/work/shop"},
        {"cmd": "pytest -q", "exit_code": 0, "output": "9 passed in 0.1s\n", "cwd": "/work/shop"},
    ]
    assert [t["passed"] for t in _finish(same_dir)["tests"]] == [True]
    # A declared pair: the green run's own summary ("12 passed") is evidence enough.
    declared = [
        {"cmd": "pytest tests/test_a.py", "passed": False},
        {"cmd": "pytest tests/test_a.py -x", "passed": True, "summary": "12 passed"},
    ]
    assert [t["passed"] for t in resolve_runs(declared)[0]] == [True]
    unproven = [{"cmd": "pytest tests/test_a.py", "passed": False}, {"cmd": "pytest tests/test_a.py -x", "passed": True}]
    assert [t["passed"] for t in resolve_runs(unproven)[0]] == [False, True]
    # The exact command run again is its newest result, as before.
    again = [{"cmd": "make test", "passed": False}, {"cmd": "make test", "passed": True}]
    assert [t["passed"] for t in resolve_runs(again)[0]] == [True]


def test_a_run_in_another_directory_is_sent_with_its_directory(tmp_path):
    """Through the Claude Code transcript parser: the directory each command ran in is the one the
    transcript records, and a run made elsewhere is sent as ``cd <there> && <command>``."""
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    t = Transcript("sess-review", repo)
    t.bash("pytest tests/test_a.py -q", 1, "1 failed, 3 passed in 0.10s")
    t.cwd = str(repo / "web")  # the shell moved (a ``cd web`` that persisted)
    t.bash("pytest -q", 0, "9 passed in 0.10s")
    parsed = relay_facts.parse_claude_transcript(t.write(tmp_path / "t.jsonl"), root=str(repo))
    assert [(x["cmd"], x["passed"]) for x in parsed.tests] == [
        ("pytest tests/test_a.py -q", False),
        ("cd web && pytest -q", True),
    ]
    assert all(set(x) == {"cmd", "passed", "summary"} for x in parsed.tests)  # nothing new is sent
    # The server, which sees only the commands, keeps the red run too.
    facts = {**GIT, "tests": parsed.tests, "facts_source": "relay-cli:git+transcript"}
    assert build_sections(facts)["failing"] == ["FAILING: pytest tests/test_a.py -q (1 failed, 3 passed in 0.10s)"]
    # Back in the first directory, a green run of the whole suite does replace it.
    t.cwd = str(repo)
    t.bash("pytest -q", 0, "40 passed in 0.90s")
    parsed = relay_facts.parse_claude_transcript(t.write(tmp_path / "t.jsonl"), root=str(repo))
    assert [(x["cmd"], x["passed"]) for x in parsed.tests] == [("cd web && pytest -q", True), ("pytest -q", True)]


# ---------------------------------------------------------------------------
# F31: package managers running a runner; a failing entry is never dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "runner"),
    [
        ("yarn jest src", "jest src"),
        ("pnpm jest", "jest"),
        ("pnpm vitest run", "vitest run"),
        ("bun run vitest", "vitest"),
        ("yarn run jest --ci", "jest --ci"),
        ("pnpm -r test", "pnpm test"),
        ("pnpm --filter web test", "pnpm test"),
        ("yarn workspace web jest src", "jest src"),
        ("npm --prefix web test", "npm test"),
        ("cd web && CI=1 yarn jest src 2>&1 | tail -5", "jest src"),
    ],
)
def test_a_package_manager_running_a_runner_is_a_test_run(cmd, runner):
    assert [run.text for run in runner_invocations(cmd)] == [runner]


@pytest.mark.parametrize(
    "cmd", ["yarn add jest", "pnpm add -D vitest", "npm install jest", "bun add vitest", "pnpm why jest", "yarn build"]
)
def test_installing_a_runner_is_not_a_test_run(cmd):
    assert runner_invocations(cmd) == []


def test_package_manager_runs_are_graded_by_client_and_server():
    failed = _finish([{"cmd": "yarn jest src", "exit_code": 1, "output": "Tests:       3 failed, 2 passed, 5 total\n"}])
    assert [(t["cmd"], t["passed"]) for t in failed["tests"]] == [("yarn jest src", False)] and failed["errors"] == []
    assert assess_handoff(failed, None)["status"] == "blocked"
    passed = _finish([{"cmd": "pnpm vitest run", "exit_code": 0, "output": " Tests  30 passed (30)\n"}])
    assert build_sections(passed)["done"][1] == "tests passing: pnpm vitest run (Tests 30 passed (30))"
    assert assess_handoff(passed, None)["status"] == "ready"


@pytest.mark.parametrize("cmd", ["yarn jest src", "./scripts/run-tests.sh", "python manage.py test"])
def test_a_failing_run_sent_by_an_installed_client_is_never_dropped(cmd):
    """Facts as a 0.17.5 client sends them: the server keeps a failing entry whatever its command."""
    old = {
        **GIT,
        "tests": [{"cmd": cmd, "passed": False, "summary": "Tests:       3 failed, 2 passed, 5 total"}],
        "commands": [{"cmd": cmd, "exit_code": 1}],
        "errors": [],
        "facts_source": "relay-cli:git+transcript",
    }
    grounding = check_summary_grounding("All tests pass.", old)
    assert grounding["status"] == "contradicted"
    sections = build_sections(old)
    assert sections["failing"] == [f"FAILING: {cmd} (Tests: 3 failed, 2 passed, 5 total)"]
    health = assess_handoff(old, grounding)
    assert health["status"] == "blocked" and "1 failing test run(s)" in health["missing"]


# ---------------------------------------------------------------------------
# F31: prose in a summary does not override the verdict the agent declared
# ---------------------------------------------------------------------------

PROSE = [
    "214 passed after fixing the 2 failures from the first run",
    "all green; fixed 3 errors in fixtures",
    "passed (was: 1 failed)",
    "retried: 2 errors were flaky network, now 31 passed",
    "40 passed, 0 failed",
    "OK (failures=0, errors: 0)",
]
RUNNER_LINES = [
    "1 failed, 214 passed in 3.21s",
    "1 failed, 214 passed",
    "2 errors",
    "Tests:       1 failed, 10 passed, 11 total",
    "test result: FAILED. 3 passed; 1 failed; 0 ignored",
    "FAILED (failures=1)",
    "Executed 12 tests, with 2 failures (0 unexpected) in 1.2 (1.3) seconds",
]


@pytest.mark.parametrize("summary", PROSE)
def test_prose_about_earlier_failures_does_not_fail_a_declared_pass(summary):
    assert summary_shows_failure(summary) is False
    facts = {**GIT, "tests": [{"cmd": "pytest -q", "passed": True, "summary": summary}], "facts_source": "agent-declared"}
    grounding = check_summary_grounding("Tests pass.", facts)
    assert grounding["status"] == "consistent"
    health = assess_handoff(facts, grounding)
    assert health["status"] == "ready" and health["missing"] == []
    assert _test_passed({"command": "pytest -q", "passed": True, "summary": summary}) is True
    assert _test_passed({"command": "pytest -q", "passed": 214, "failed": 0, "summary": summary}) is True


@pytest.mark.parametrize("summary", RUNNER_LINES)
def test_a_runner_summary_line_that_reports_failures_still_fails_the_run(summary):
    assert summary_shows_failure(summary) is True
    facts = {**GIT, "tests": [{"cmd": "pytest -q", "passed": True, "summary": summary}], "facts_source": "agent-declared"}
    assert assess_handoff(facts, None)["status"] == "blocked"
    assert _test_passed({"command": "pytest -q", "passed": True, "summary": summary}) is False
    # Counts the agent gave explicitly win over any text.
    assert _test_passed({"command": "pytest -q", "passed": 214, "failed": 0, "summary": summary}) is True


# ---------------------------------------------------------------------------
# F18: a negation in one comma clause does not hide a claim in the next
# ---------------------------------------------------------------------------

UNPUSHED = {
    "branch": "main",
    "head_commit": "abc1234",
    "commits": [{"sha": "abc1234def", "subject": "x"}],
    "unpushed_commits": 1,
    "upstream": "origin/main",
}


@pytest.mark.parametrize(
    "summary",
    [
        "Nothing broken, deployed to production.",
        "Tests not run, deployed to prod.",
        "Never tested, shipped to prod anyway.",
        "Not done, deployed anyway",
        "pushed - no issues found",
        "Deployed: none of the checks failed",
        "Pushed: no regressions.",
    ],
)
def test_a_denial_of_something_else_does_not_hide_a_deploy_claim(summary):
    assert claims_pushed(summary) is True
    assert check_summary_grounding(summary, UNPUSHED)["status"] == "contradicted"


@pytest.mark.parametrize(
    "summary",
    [
        "Not staged, committed or deployed.",
        "Nothing committed, pushed, deployed or published.",
        "Never tested, committed nor pushed.",
        "Pushed: no",
        "Pushed - no.",
        "Deployed: not yet",
        "pushed=false, deployed=false",
        "Pushed: none",
    ],
)
def test_a_denied_list_or_label_is_still_not_a_claim(summary):
    assert claims_pushed(summary) is False


# ---------------------------------------------------------------------------
# F45: a newer idle close with a different open item is not a repeat
# ---------------------------------------------------------------------------

START = datetime.now(UTC) - timedelta(days=2)
DIFFERENT = [
    ("Review PR 41 from the vendor", "Review PR 41", "Review PR 42 from the vendor", "Review PR 42"),
    (
        "Deploy the backend to staging after QA signs off",
        "Deploy the backend to staging",
        "Deploy the frontend to staging after QA signs off",
        "Deploy the frontend to staging",
    ),
    (
        "Customer asked to raise the invoice limit to 5000",
        "Raise the invoice limit to 5000",
        "Customer asked to raise the invoice limit to 9000",
        "Raise the invoice limit to 9000",
    ),
    (
        "Do NOT rotate the API key until Friday",
        "Wait before rotating the API key",
        "Do rotate the API key before Friday",
        "Rotate the API key",
    ),
    ("Await the upgrade", "Monitor the release task", "Await the upgrade and rotate the API key", "Monitor the release task"),
]
SAME = [
    (
        "Await the full upgrade and the approved offer check",
        "Monitor the release task.",
        "await the full upgrade and approved offer check.",
        "Monitor the release task",
    ),
    ("Await run 0004c0de (checked 14:05 UTC)", "Monitor.", "Await run 0005c0de (checked 15:07 UTC)", "Monitor."),
    ("Await the upgrade (2026-11-05T14:51:00Z)", "Monitor", "Await the upgrade (2026-11-06T09:02:11Z)", "Monitor"),
]


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _close(api, n: int, facts: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = {
        "agent_id": "claude-code",
        "session_id": f"s-{n}",
        "project_id": "shop",
        "facts": facts,
        "closed_at": (START + timedelta(minutes=n)).isoformat(),
        "end_reason": "done",
        **over,
    }
    res = api["http"].post("/api/v1/session/close", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _brief(api) -> dict[str, Any]:
    res = api["http"].get("/api/v1/session/brief", params={"project_id": "shop", "agent_id": "codex"})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize(("todo1", "next1", "todo2", "next2"), DIFFERENT, ids=[p[2] for p in DIFFERENT])
def test_a_newer_idle_close_with_a_different_open_item_is_shown(api, todo1, next1, todo2, next2):
    _close(api, 1, {"todos_open": [todo1], "next_step": next1})
    newer = _close(api, 2, {"todos_open": [todo2], "next_step": next2})
    brief = _brief(api)
    assert brief["handoff"]["id"] == newer["handoff_id"] and brief["handoffs_repeated"] == 0
    last = next(line for line in brief["rendered"].splitlines() if line.startswith("Last session"))
    assert next2 in last
    assert "repeated open items" not in brief["rendered"]


@pytest.mark.parametrize(("todo1", "next1", "todo2", "next2"), SAME, ids=[p[2] for p in SAME])
def test_the_same_open_item_restated_is_still_a_repeat(api, todo1, next1, todo2, next2):
    older = _close(api, 1, {"todos_open": [todo1], "next_step": next1})
    _close(api, 2, {"todos_open": [todo2], "next_step": next2})
    brief = _brief(api)
    assert brief["handoff"]["id"] == older["handoff_id"] and brief["handoffs_repeated"] == 1


def test_repeat_comparison_keeps_numbers_and_drops_clock_times_and_ids():
    def row(ident: str, todo: str) -> dict[str, Any]:
        relay = {"agent_id": "codex", "done": [], "failing": [], "not_done": [f"TODO: {todo}"], "next": "monitor"}
        meta = {"source": "relay", "relay": relay}
        return {"id": ident, "agent_id": "codex", "content": "", "source": RELAY_ROW_SOURCE, "metadata": meta, "trust_score": 1.0}

    assert repeated_idle_closes([row("b", "wait for build 42"), row("a", "wait for build 41")]) == set()
    assert repeated_idle_closes([row("b", "Wait for build 41!"), row("a", "wait for the build 41")]) == {"b"}
    assert repeated_idle_closes([row("b", "wait for build at 15:07"), row("a", "wait for build at 14:05")]) == {"b"}
    assert repeated_idle_closes([row("b", "do rotate the key"), row("a", "do not rotate the key")]) == set()


# ---------------------------------------------------------------------------
# MCP close: another project's close does not read this session's checkout;
# a no-change close that says or did something is stored
# ---------------------------------------------------------------------------


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
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "mcp-review")
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_session_projects", {})
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(unwrap_untrusted(raw))


def test_mcp_close_for_another_repository_does_not_read_the_briefed_checkout(mcp_env, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    bare_a, clones_a = make_remote_and_clones(tmp_path / "a")
    bare_b, _ = make_remote_and_clones(tmp_path / "b")
    repo_a = clones_a["laptop"]
    other = tmp_path / "billing-service.git"
    git(tmp_path, "clone", "-q", "--bare", str(bare_b), str(other))

    briefed = _j(server.session_brief(root_path=str(repo_a), compact=True))
    sha = commit(repo_a, "secret_plan_a.py", "x = 1\n", "feat: A only work")
    (repo_a / "A_UNCOMMITTED_PRIVATE.md").write_text("x")

    out = _j(server.close_session(summary="Reviewed repo B docs.", git_remote=str(other)))
    assert out["status"] == "ok" and out["project_id"] != briefed["project_id"]
    rendered = out["rendered"]
    for leaked in ("A_UNCOMMITTED_PRIVATE", "secret_plan_a", "A only work", sha[:7], "not pushed"):
        assert leaked not in rendered
    assert "(no git info)" in rendered

    # The briefed repository named by its own remote (or by nothing) still gets its git facts.
    same = _j(server.close_session(summary="Work in A.", git_remote=str(bare_a), session_id="s-a"))
    assert same["project_id"] == briefed["project_id"]
    assert f"- {sha[:7]} feat: A only work" in same["rendered"] and "A_UNCOMMITTED_PRIVATE.md" in same["rendered"]
    plain = _j(server.close_session(summary="Work in A.", session_id="s-a2"))
    assert f"main@{sha[:7]}" in plain["rendered"]
    # Helper level: a remote that is not the remembered checkout's reads nothing; root_path always names the checkout.
    assert server._close_git_facts(None, None, str(other)) == {}
    assert server._close_git_facts(str(repo_a), None, str(other))["head_commit"] == sha


def test_mcp_no_change_close_with_a_summary_or_session_commits_is_stored(mcp_env, tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    work = clones["laptop"]
    _j(server.session_brief(root_path=str(work), compact=True))
    said = _j(
        server.close_session(
            summary="Investigated the outage: the cause is the expired certificate on host X. No code change needed.",
            end_reason="no_change",
            root_path=str(work),
        )
    )
    assert said["handoff_id"] and said.get("stored") is not False
    assert "expired certificate" in said["rendered"]

    sha = commit(work, "m.py", "x = 1\n", "feat: real work this session")
    worked = _j(server.close_session(todos_open=["Await review"], end_reason="no_change", root_path=str(work), session_id="s2"))
    assert worked["handoff_id"] and f"- {sha[:7]} feat: real work this session" in worked["rendered"]

    # Still nothing stored for a run that says and did nothing (uncommitted files in the checkout are not its work).
    (work / "left_by_someone.txt").write_text("x")
    other = tmp_path / "plain"
    other.mkdir()
    idle = _j(
        server.close_session(
            todos_open=["Await review"], next_step="Monitor.", end_reason="heartbeat", root_path=str(other), session_id="s3"
        )
    )
    assert idle["stored"] is False and idle["handoff_id"] is None
    idle_here = _j(server.close_session(todos_open=["Await review"], end_reason="idle", project_id="elsewhere", session_id="s4"))
    assert idle_here["stored"] is False


def test_a_no_change_close_with_a_summary_has_substance():
    idle = build_sections({"todos_open": ["wait"], "next_step": "monitor"}, "monitor")
    assert sections_have_substance(idle, notes="unchanged", end_reason="no_change") is False
    assert sections_have_substance(idle, summary="Found the cause: an expired certificate.", end_reason="no_change") is True
