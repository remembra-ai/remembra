"""F31: a handoff must not list a failing test run, or a command that only mentions a
test runner, under "tests passing".

The relay client decides a run's verdict from the runner's own summary when the shell's
exit code is not the runner's (``pytest ... | tail -3``), counts a command as a test run
only when a runner is actually run, and the server re-checks every verdict it is sent, so
handoffs from clients that still send the shell's exit code are corrected too. A later
green run of the same tests replaces an earlier red one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remembra.crew.reports import _test_passed
from remembra.relay.facts import parse_claude_transcript
from remembra.relay.handoff import HEALTH_BLOCKED, HEALTH_WARNINGS, assess_handoff, build_sections, check_summary_grounding
from remembra.relay.testruns import run_verdict, runner_invocations, summary_shows_failure
from tests.relay_fixtures import Transcript

KILL = 'kill -TERM 64445 64831 2>&1; sleep 2; ps aux | grep -i "pytest" | grep -v grep || echo "all pytest processes clear"'
HEREDOC = "python3 - <<'PY'\nimport pathlib, shutil\n# run cargo test and pytest later\nprint('ok')\nPY"


def _parse(tmp_path: Path, *runs: tuple[str, int, str]):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    t = Transcript("sess-f31", repo)
    for cmd, code, output in runs:
        t.bash(cmd, code, output)
    return parse_claude_transcript(t.write(tmp_path / "t.jsonl"), root=str(repo))


@pytest.mark.parametrize(
    ("cmd", "output", "summary"),
    [
        (
            "python3 -m pytest -q tests/pxread tests/x 2>&1 | tail -60",
            "FAILED tests/x::t\n1 failed, 214 passed in 1.97s",
            "1 failed, 214 passed in 1.97s",
        ),
        (".venv/bin/pytest -q tests/crew 2>&1 | tail -3", "=========== 12 errors in 0.63s ===========", "12 errors in 0.63s"),
        (
            "npx vitest run 2>&1 | tail -5",
            " Tests  1 failed | 1994 passed | 51 skipped (2046)",
            "Tests 1 failed | 1994 passed | 51 skipped (2046)",
        ),
        ("xcodebuild -scheme App test 2>&1 | tail -2", "** TEST FAILED **", "** TEST FAILED **"),
        ("python -m unittest discover 2>&1 | tail -3", "Ran 4 tests in 0.01s\n\nFAILED (failures=1)", "FAILED (failures=1)"),
        ("npx mocha 2>&1 | tail -4", "  3 passing (12ms)\n  2 failing", "2 failing"),
        ("dotnet test 2>&1 | tail -1", "Failed!  - Failed:     1, Passed:     3, Skipped:     0, Total:     4", None),
        ("pytest -q tests/a.py; git stash pop -q", "1 failed in 41.59s", "1 failed in 41.59s"),
    ],
)
def test_a_piped_run_whose_summary_shows_failures_is_a_failing_run(tmp_path, cmd, output, summary):
    parsed = _parse(tmp_path, (cmd, 0, output))  # the shell reports tail's / the last command's exit code: 0
    assert len(parsed.tests) == 1 and parsed.tests[0]["passed"] is False
    if summary:
        assert parsed.tests[0]["summary"] == summary
    sections = build_sections({"tests": parsed.tests, "facts_source": "relay-cli:git+transcript"})
    assert not any(item.startswith("tests passing") for item in sections["done"])
    assert len(sections["failing"]) == 1 and sections["failing"][0].startswith("FAILING: ")
    assert assess_handoff({"tests": parsed.tests}, None)["missing"][0] == "1 failing test run(s)"


@pytest.mark.parametrize(
    "cmd",
    [
        KILL,
        HEREDOC,
        "pip install pytest",
        'git commit -m "run pytest in ci"',
        "which pytest",
        "ps aux | grep -i pytest | grep -v grep",
        "pkill -f pytest",
        'pkill -f "pytest -q tests/crew"',
        "ls node_modules/.bin/jest",
        "uv pip install pytest tox",
        "echo run go test next && cat Makefile",
        "grep -rn 'cargo test' docs",
    ],
)
def test_a_command_that_only_mentions_a_runner_is_not_a_test_run(tmp_path, cmd):
    assert runner_invocations(cmd) == []
    parsed = _parse(tmp_path, (cmd, 0, "ok"))
    assert parsed.tests == []
    # ... and when it fails it is a failed command, never a FAILING test.
    failed = _parse(tmp_path, ("pip install pytest", 1, "ERROR: No matching distribution"))
    assert failed.tests == [] and failed.errors == ["`pip install pytest` exited 1: ERROR: No matching distribution"]


@pytest.mark.parametrize(
    ("cmd", "output"),
    [
        ("npm test", "Tests:       12 passed, 12 total"),
        ("cargo test 2>&1 | tail -2", "test result: ok. 39 passed; 0 failed; 0 ignored"),
        ("swift test 2>&1 | tail -1", "Executed 3 tests, with 0 failures (0 unexpected) in 0.1 seconds"),
        ("pytest -q 2>&1 | tail -1", "1 xfailed, 3 passed in 0.10s"),
        ("cd repo && FOO=1 .venv/bin/python -m pytest -q", ""),
        ("pytest -q --lf 2>&1 | tail -4", "run-last-failure: rerun previous 1 failure (skipped 12 files)\n4 passed in 0.2s"),
        ('ssh build "cd app && pytest -q"', "5 passed in 1.0s"),
        ("docker compose exec app pytest -q", "5 passed in 1.0s"),
        ("bash -lc 'uv run pytest -q tests/x'", "5 passed in 1.0s"),
        ("timeout 600 uv run --frozen pytest -q", "5 passed in 1.0s"),
        ("coverage run -m pytest", "5 passed in 1.0s"),
        ("(cd pkg && go test ./...)", "ok  \texample.com/pkg\t0.2s"),
    ],
)
def test_real_runs_that_passed_stay_passing(tmp_path, cmd, output):
    assert runner_invocations(cmd)
    parsed = _parse(tmp_path, (cmd, 0, output))
    assert len(parsed.tests) == 1 and parsed.tests[0]["passed"] is True
    assert build_sections({"tests": parsed.tests})["failing"] == []


def test_a_piped_run_with_no_summary_is_not_reported_as_passing(tmp_path):
    parsed = _parse(tmp_path, ("pytest -q tests/x | grep -c PASSED", 0, "12"), ("pytest -q tests/y || true", 0, ""))
    assert parsed.tests == []  # the exit code is grep's / true's and the runner's verdict was not captured
    # The runner's own exit code still decides when nothing masks it.
    plain = _parse(tmp_path, ("pytest -q tests/x", 0, ""), ("pytest -q tests/y && echo done", 2, ""))
    assert [(t["cmd"], t["passed"]) for t in plain.tests] == [
        ("pytest -q tests/x", True),
        ("pytest -q tests/y && echo done", False),
    ]


def test_a_later_green_run_of_the_same_tests_replaces_an_earlier_red_one(tmp_path):
    parsed = _parse(
        tmp_path,
        ("git stash -q; pytest -q tests/test_a.py 2>&1 | tail -3; git stash pop -q", 0, "1 failed in 41.59s"),  # red control
        ("pytest -q tests/test_b.py::test_one -x 2>&1 | tail -3", 0, "1 failed in 0.20s"),
        ("pytest -q tests/test_c.py 2>&1 | tail -3", 0, "2 failed in 0.20s"),
        (".venv/bin/python -m pytest tests/test_a.py -q 2>&1 | tail -1", 0, "4 passed in 0.31s"),
        ("pytest -q tests 2>&1 | tail -1", 0, "1 failed, 90 passed in 9.31s"),
        ("pytest -q tests/test_b.py 2>&1 | tail -1", 0, "7 passed in 0.31s"),
    )
    verdicts = {t["cmd"]: t["passed"] for t in parsed.tests}
    assert verdicts == {
        "pytest -q tests/test_c.py 2>&1 | tail -3": False,  # never re-run green
        ".venv/bin/python -m pytest tests/test_a.py -q 2>&1 | tail -1": True,
        "pytest -q tests 2>&1 | tail -1": False,  # the newest whole-suite run
        "pytest -q tests/test_b.py 2>&1 | tail -1": True,  # covers the earlier ::test_one run
    }
    sections = build_sections({"tests": parsed.tests})
    assert [f.split(" (")[0] for f in sections["failing"]] == [
        "FAILING: pytest -q tests 2>&1 | tail -1",  # newest first
        "FAILING: pytest -q tests/test_c.py 2>&1 | tail -3",
    ]
    assert sections["next"] == "fix the failing run: pytest -q tests 2>&1 | tail -1"
    # A later green run of the whole suite clears every earlier red run under it.
    green = _parse(
        tmp_path,
        ("pytest -q tests/test_c.py 2>&1 | tail -3", 0, "2 failed in 0.20s"),
        ("pytest -q 2>&1 | tail -1", 0, "97 passed in 9.31s"),
    )
    assert [(t["cmd"], t["passed"]) for t in green.tests] == [("pytest -q 2>&1 | tail -1", True)]


# ---------------------------------------------------------------------------
# The server re-checks what it is sent (clients that still send the shell's exit code)
# ---------------------------------------------------------------------------

OLD_CLIENT = {
    "facts_source": "relay-cli:git+transcript",
    "branch": "main",
    "head_commit": "a" * 40,
    "files_changed": ["src/x.py"],
    "tests": [
        {"cmd": "python3 -m pytest -q tests/pxread 2>&1 | tail -60", "passed": True, "summary": "1 failed, 214 passed in 1.97s"},
        {"cmd": KILL, "passed": True, "summary": None},
        {"cmd": HEREDOC, "passed": True, "summary": None},
        {"cmd": "pip install pytest", "passed": False, "summary": None},
        {"cmd": "pytest -q tests/y | grep -c PASSED", "passed": True, "summary": None},
        {"cmd": "npm test", "passed": True, "summary": "Tests:       12 passed, 12 total"},
    ],
    "commands": [{"cmd": "pip install pytest", "exit_code": 1}],
}


def test_server_corrects_the_verdicts_of_a_client_that_trusts_the_exit_code():
    sections = build_sections(OLD_CLIENT)
    assert [d for d in sections["done"] if d.startswith("tests passing")] == [
        "tests passing: npm test (Tests: 12 passed, 12 total)"
    ]
    assert sections["failing"] == [
        "FAILING: python3 -m pytest -q tests/pxread 2>&1 | tail -60 (1 failed, 214 passed in 1.97s)",
        "`pip install pytest` exited 1",
    ]
    health = assess_handoff(OLD_CLIENT, None)
    assert health["status"] == HEALTH_BLOCKED and health["missing"][0] == "1 failing test run(s)"
    assert "1 failed command(s)" in health["missing"]
    grounding = check_summary_grounding("All tests pass.", OLD_CLIENT)
    assert grounding["status"] == "contradicted"
    assert grounding["issues"][0].startswith("claims tests pass, but 1 test run(s) failed: python3 -m pytest -q tests/pxread")


def test_commands_that_are_not_test_runs_do_not_hide_tests_not_run():
    facts = {**OLD_CLIENT, "tests": [{"cmd": "pkill -f pytest", "passed": True, "summary": None}], "commands": []}
    health = assess_handoff(facts, None)
    assert health["status"] == HEALTH_WARNINGS and "tests not run" in health["missing"]


def test_a_verdict_the_agent_declared_is_kept_unless_its_own_summary_shows_failures():
    declared = {
        "facts_source": "agent-declared",
        "tests": [
            {"cmd": "full suite", "passed": True, "summary": None},  # not a command line: the agent's own words
            {"cmd": "e2e", "passed": True, "summary": "2 failed, 40 passed"},
        ],
    }
    sections = build_sections(declared)
    assert "tests passing: full suite" in sections["done"]
    assert sections["failing"] == ["FAILING: e2e (2 failed, 40 passed)"]
    assert _test_passed({"cmd": "e2e", "passed": True, "summary": "2 failed, 40 passed"}) is False
    assert _test_passed({"cmd": "e2e", "passed": True, "summary": "42 passed"}) is True
    assert _test_passed({"command": "pytest", "passed": 3, "failed": 0}) is True


@pytest.mark.parametrize(
    "line",
    [
        "1 failed, 214 passed in 1.97s",
        "12 errors in 0.63s",
        "Tests  1 failed | 1994 passed | 51 skipped (2046)",
        "** TEST FAILED **",
        "test result: FAILED. 0 passed; 2 failed",
        "FAILED (failures=1)",
        "FAILED (errors=2)",
        "2 failing",
        "Tests: 5, Assertions: 5, Failures: 1.",
        "Failed!  - Failed:     1, Passed:     3",
        "# fail 1",
        "not ok 3 - parses the header",
        "FAIL\texample.com/pkg\t0.2s",
        "3 examples, 1 failure",
    ],
)
def test_failure_summaries_of_the_detected_runners_are_recognised(line):
    assert summary_shows_failure(line) is True


@pytest.mark.parametrize(
    "line",
    [
        None,
        "",
        "39 passed, 0 failed",
        "Executed 3 tests, with 0 failures",
        "1 xfailed, 3 passed in 0.10s",
        "test result: ok. 39 passed; 0 failed; 0 ignored",
        "Tests:       12 passed, 12 total",
        "# fail 0",
        "ok 3 - parses the header",
        "Passed!  - Failed:     0, Passed:     3",
        "OK (5 tests, 5 assertions)",
    ],
)
def test_passing_summaries_are_not_failures(line):
    assert summary_shows_failure(line) is False


def test_verdict_edge_cases():
    assert run_verdict("", 0, "") is None and run_verdict("ls", 0, "1 failed") is None
    assert run_verdict("pytest -q", 0, "") == (True, None)
    assert run_verdict("pytest -q", 1, "") == (False, None)
    # Coverage below the threshold: every test passed, the runner still failed.
    assert run_verdict("pytest --cov --cov-fail-under=90", 2, "5 passed in 1.0s") == (False, "5 passed in 1.0s")
    # Two suites in one command: the first one's failure is not lost behind the second's pass.
    out = "1 failed, 2 passed in 0.1s\n8 passed in 0.2s"
    assert run_verdict("pytest -q tests/a | tail -1; pytest -q tests/b | tail -1", 0, out) == (
        False,
        "1 failed, 2 passed in 0.1s",
    )


def test_a_red_run_is_cleared_once_every_test_it_named_as_failed_ran_green(tmp_path):
    red = (
        "FAILED tests/test_a.py::test_one - AssertionError\n"
        "FAILED tests/crew/test_b.py::test_two - KeyError\n"
        "2 failed, 730 passed in 31.5s"
    )
    parsed = _parse(
        tmp_path,
        ("pytest -q tests/test_a.py tests/test_c.py tests/crew 2>&1 | tail -4", 0, red),
        ("pytest -q tests/test_a.py 2>&1 | tail -1", 0, "9 passed in 0.31s"),
    )
    # tests/crew/test_b.py::test_two has not been seen green yet.
    assert [t["passed"] for t in parsed.tests] == [False, True]
    assert set(parsed.tests[0]) == {"cmd", "passed", "summary"}  # nothing new on the wire
    parsed = _parse(
        tmp_path,
        ("pytest -q tests/test_a.py tests/test_c.py tests/crew 2>&1 | tail -4", 0, red),
        ("pytest -q tests/test_a.py 2>&1 | tail -1", 0, "9 passed in 0.31s"),
        ("pytest -q tests/crew/test_b*.py 2>&1 | tail -1", 0, "12 passed in 0.31s"),
    )
    assert [t["passed"] for t in parsed.tests] == [True, True]
    # When the output does not name every failure (tail cut them off), the whole run has to be seen green.
    cut = _parse(
        tmp_path,
        ("pytest -q tests/test_a.py tests/test_c.py 2>&1 | tail -2", 0, "FAILED tests/test_a.py::test_one - x\n2 failed in 1.5s"),
        ("pytest -q tests/test_a.py 2>&1 | tail -1", 0, "9 passed in 0.31s"),
    )
    assert [t["passed"] for t in cut.tests] == [False, True]


def test_a_long_command_is_recorded_and_shown_as_the_runner_it_runs(tmp_path):
    script = "cd src/.. && python3 - <<'EOF'\n" + "\n".join(f"line_{n} = {n}" for n in range(80)) + "\nEOF\n"
    cmd = script + ".venv/bin/python -m pytest -q tests/test_widget.py 2>&1 | tail -3"
    parsed = _parse(tmp_path, (cmd, 0, "1 failed, 3 passed in 0.21s"))
    assert parsed.tests == [{"cmd": "pytest -q tests/test_widget.py", "passed": False, "summary": "1 failed, 3 passed in 0.21s"}]
    # A client that sent the first 300 characters: the server shows the runner when it can still see it.
    old = {
        "cmd": "cd ~/proj && " + "x=1; " * 30 + "pytest -q tests/test_widget.py | tail -3",
        "passed": True,
        "summary": "3 passed",
    }
    assert build_sections({"tests": [old]})["done"] == ["tests passing: pytest -q tests/test_widget.py (3 passed)"]
