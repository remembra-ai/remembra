"""F16: ``remembra.relay.declared_facts`` is total, idempotent and never invents a verdict."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from remembra.api.v1 import relay as relay_api
from remembra.crew import schemas as S
from remembra.relay.declared_facts import normalize_close_facts, normalize_crew_evidence, valid_branch_name

ODD: list[Any] = [
    None,
    "",
    " ",
    "none",
    "x",
    0,
    1,
    -1,
    2.5,
    True,
    False,
    [],
    {},
    [None],
    [[]],
    [{}],
    {"a": 1},
    ["a", 1, None, {}],
    object(),
]


def test_absent_and_empty_facts_are_empty_and_unreported():
    assert normalize_close_facts(None) == ({}, [])
    assert normalize_close_facts({}) == ({}, [])
    assert normalize_close_facts({"tests": None, "commits": None, "errors": None, "notes": None}) == ({"notes": None}, [])
    for bad in ("text", 3, ["a"], True):
        facts, changes = normalize_close_facts(bad)
        assert facts == {} and len(changes) == 1 and "expected an object" in changes[0]


def test_a_well_formed_close_is_passed_through_untouched():
    facts = {
        "branch": "main",
        "head_commit": "abc1234",
        "commits": [{"sha": "abc1234", "subject": "fix"}],
        "tests": [{"cmd": "pytest -q", "passed": True, "summary": "170 passed"}, {"cmd": "ruff", "passed": False}],
        "commands": [{"cmd": "make", "exit_code": 2}],
        "files_changed": ["a.py"],
        "errors": ["boom"],
        "todos_open": ["later"],
        "unpushed_commits": 0,
        "notes": "  spaced  ",
        "next_step": "ship",
        "custom": {"kept": 1},
    }
    assert normalize_close_facts(facts) == (facts, [])


@pytest.mark.parametrize(
    ("entry", "passed", "summary"),
    [
        ({"command": "t", "passed": 170, "failed": 0}, True, "170 passed, 0 failed"),
        ({"command": "t", "passed": 1, "failed": 2}, False, "1 passed, 2 failed"),
        ({"cmd": "t", "passed": 0, "failed": 0}, False, "0 passed, 0 failed"),
        ({"cmd": "t", "passed": True, "failed": 3}, False, "3 failed"),
        ({"cmd": "t", "passed": "170"}, True, "170 passed, 0 failed"),
        ({"cmd": "t", "passed": "yes"}, True, None),
        ({"cmd": "t", "passed": "FAILED"}, False, None),
        ({"cmd": "t", "failed": 0}, True, "0 failed"),
        ({"cmd": "t", "exit_code": 0}, True, None),
        ({"cmd": "t", "exit_code": 1}, False, None),
        ({"cmd": "t", "passed": True, "exit_code": 2}, False, None),
        ({"name": "t", "status": "passed"}, True, None),
        ({"fingerprint": "t", "passed": 2.0}, True, "2 passed, 0 failed"),
    ],
)
def test_a_test_entry_is_read_as_a_boolean_verdict(entry, passed, summary):
    facts, changes = normalize_close_facts({"tests": [entry]})
    want = {"cmd": "t", "passed": passed, **({"summary": summary} if summary else {})}
    assert facts["tests"] == [want] and changes


@pytest.mark.parametrize(
    "entry", ["ran it", {"cmd": "t"}, {"cmd": "t", "passed": "maybe"}, {"passed": True}, {"cmd": "t", "passed": -1}]
)
def test_an_entry_with_no_command_or_no_result_is_kept_as_text_not_as_a_verdict(entry):
    facts, changes = normalize_close_facts({"tests": [entry], "notes": "mine"})
    assert facts["tests"] == []
    assert facts["notes"].startswith("mine\nTests as declared (no pass/fail result recorded): ")
    assert changes


def test_commits_unpushed_lists_and_text_fields():
    facts, changes = normalize_close_facts(
        {
            "commits": ["1ec6d03", "abc1234 fix the thing", {"hash": "ffff000", "message": "m"}, {"subject": "no id"}, 7],
            "unpushed_commits": ["a", "b"],
            "files_changed": "a.py",
            "errors": "none",
            "todos_open": ["N/A", " real ", {"task": "x"}],
            "notes": ["one", "two"],
            "next_step": ["first", "then"],
            "commands": ["make", {"command": "ls", "exit_code": "0"}, {}],
            "branch": 12,
        }
    )
    assert facts["commits"] == [
        {"sha": "1ec6d03"},
        {"sha": "abc1234", "subject": "fix the thing"},
        {"sha": "ffff000", "subject": "m"},
    ]
    assert facts["unpushed_commits"] == 2
    assert facts["files_changed"] == ["a.py"] and facts["errors"] == []
    assert facts["todos_open"] == [" real ", '{"task": "x"}']
    assert facts["next_step"] == "first; then"
    assert facts["commands"] == [{"cmd": "make"}, {"cmd": "ls", "exit_code": 0}]
    assert facts["branch"] is None
    assert facts["notes"].startswith("one; two\nCommits as declared (no commit id): ")
    assert "Branch as declared (not a branch name): 12" in facts["notes"]
    assert len(changes) >= 8


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (3, 3),
        ("4", 4),
        (2.0, 2),
        ([], 0),
        (["a"], 1),
        (True, None),
        (False, None),
        ("unknown", None),
        (-1, None),
        ({}, None),
        (1.5, None),
    ],
)
def test_unpushed_commits_is_a_count_or_not_recorded(value, want):
    facts, _ = normalize_close_facts({"unpushed_commits": value})
    assert facts["unpushed_commits"] == want and (want is None or type(facts["unpushed_commits"]) is int)


def test_an_invalid_branch_is_kept_for_the_api_and_moved_to_notes_for_the_tool():
    bad = "feat/staff-access-s1..s4"
    assert normalize_close_facts({"branch": bad}) == ({"branch": bad}, [])
    facts, changes = normalize_close_facts({"branch": bad}, lenient_branch=True)
    assert facts == {"branch": None, "notes": f"Branch as declared (not a valid git branch name): {bad}"}
    assert changes == ["branch: not a valid git branch name, kept in notes"]
    assert normalize_close_facts({"branch": "feat/x"}, lenient_branch=True) == ({"branch": "feat/x"}, [])


def test_the_branch_rule_is_the_api_rule():
    names = ["main", "feat/x", "-x", "/x", ".x", "x/", "x.", "x.lock", "@", "a..b", "a b", "a~b", "a^b", "a:b", "a?b", "a*b"]
    names += ["a[b", "a\\b", "a@{b", "a//b", "a/.b", "(detached)", "a\x7fb", "ü", "x" * 300, "a.lock/b", "@x"]
    for name in names:
        assert valid_branch_name(name) == relay_api.valid_branch_name(name), name


@pytest.mark.parametrize("lenient", [False, True])
def test_any_input_is_survived_and_normalizing_twice_changes_nothing(lenient):
    keys = [
        "tests",
        "commits",
        "commands",
        "files_changed",
        "errors",
        "todos_open",
        "notes",
        "next_step",
        "unpushed_commits",
        "branch",
    ]
    for key, value in itertools.product(keys, ODD):
        once, _ = normalize_close_facts({key: value, "other": value}, lenient_branch=lenient)
        again, changes = normalize_close_facts(once, lenient_branch=lenient)
        assert again == once and changes == [], (key, value, once, again, changes)
    for commits, tests in itertools.product(ODD, ODD):
        out_commits, out_tests, notes = normalize_crew_evidence(commits, tests)
        assert normalize_crew_evidence(out_commits, out_tests)[:2] == (out_commits, out_tests)
        assert all(isinstance(n, str) for n in notes)


def test_the_api_validates_whatever_the_normalizer_returns():
    """No odd value in a known key can fail the close (the branch rule aside)."""
    keys = ["tests", "commits", "commands", "files_changed", "errors", "todos_open", "notes", "next_step", "unpushed_commits"]
    for key, value in itertools.product([*keys, "incomplete"], ODD):
        if key == "incomplete" and value is not None:
            continue  # written by the relay CLI, not by agents: only its null is read as "not given"
        relay_api.FactsIn.model_validate({key: value})


def test_crew_evidence_fits_the_closed_contract_shape():
    commits, tests, notes = normalize_crew_evidence(
        [{"sha": "bd6ab83", "subject": "x"}, "1ec6d03", "not a sha"],
        [
            {"cmd": "pytest " + "x" * 400, "passed": True, "summary": "170 passed"},
            {"command": "npm test", "passed": 1},
            {"command": "ruff", "passed": 1, "failed": 2},
            {"cmd": "mypy", "passed": False},
            "by hand",
        ],
    )
    assert commits == ["bd6ab83", "1ec6d03"]
    assert [(t["passed"], t["failed"]) for t in tests] == [(1, 0), (1, 0), (1, 2), (0, 1)]
    assert S.validate_mcp_call("crew_checkpoint", {"files_changed": [], "commits": commits, "tests": tests}) == []
    assert len(notes) == 3 and "170 passed" in notes[1]
    assert normalize_crew_evidence(None, None) == (None, None, [])
    assert normalize_crew_evidence([], []) == ([], [], [])
