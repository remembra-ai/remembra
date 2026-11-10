"""F16 review: file names are kept as written, no number breaks the normalizer, a git ref is a commit.

Unit tests of ``remembra.relay.declared_facts`` for what the review of the first fix found:
a file called ``NA`` vanished from the handoff, an infinite count raised, a commit given as
``HEAD`` or a tag was moved to the notes, and a non-boolean ``no_upstream`` failed the close.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from remembra.api.v1 import relay as relay_api
from remembra.relay.declared_facts import normalize_close_facts, normalize_crew_evidence

PLACEHOLDERS = ["none", "None", "null", "NA", "n/a", "nil", "nothing", "-"]
NOT_FINITE = [float("inf"), float("-inf"), float("nan")]


@pytest.mark.parametrize("key", ["files_changed", "uncommitted_files"])
def test_a_file_named_like_a_placeholder_is_kept(key):
    """These lists hold paths (the relay CLI reads them from git): ``NA`` and ``null`` are file names."""
    given = [*PLACEHOLDERS, "src/a.py"]
    facts, changes = normalize_close_facts({key: list(given)})
    assert facts[key] == given and changes == []
    facts, changes = normalize_close_facts({key: ["NA", "", "  ", None]})
    assert facts[key] == ["NA"] and changes  # only what cannot be a path is dropped
    assert normalize_close_facts({key: "NA"})[0][key] == ["NA"]


@pytest.mark.parametrize("key", ["errors", "todos_open", "done"])
def test_a_placeholder_in_a_prose_list_still_means_nothing(key):
    facts, _ = normalize_close_facts({key: [*PLACEHOLDERS, "a real item"]})
    assert facts[key] == ["a real item"]


@pytest.mark.parametrize("value", NOT_FINITE)
def test_a_number_that_is_not_finite_is_survived(value):
    for key in ("passed", "failed", "exit_code"):
        entry = {"cmd": "pytest", "passed": True, key: value}
        facts, _ = normalize_close_facts({"tests": [entry]})
        relay_api.FactsIn.model_validate({"tests": [entry]})
        assert all(isinstance(t["passed"], bool) for t in facts["tests"])
        _, tests, notes = normalize_crew_evidence(None, [{"command": "pytest", "failed": 0, key: value}])
        assert all(type(t["passed"]) is int and type(t["failed"]) is int for t in tests or [])
        assert all(isinstance(n, str) for n in notes)
    facts, changes = normalize_close_facts({"unpushed_commits": value})
    assert facts["unpushed_commits"] is None and changes
    for key in ("commits", "commands", "files_changed", "errors", "notes", "next_step", "branch", "no_upstream"):
        once, _ = normalize_close_facts({key: value})
        assert normalize_close_facts(once) == (once, [])
        relay_api.FactsIn.model_validate({key: value})
    for commits, tests in itertools.product([value, [value]], repeat=2):
        normalize_crew_evidence(commits, tests)


def test_a_failure_count_that_is_not_finite_is_not_a_passing_count():
    facts, _ = normalize_close_facts({"tests": [{"cmd": "pytest", "passed": float("inf")}]})
    assert facts["tests"] == [] and "Tests as declared" in facts["notes"]


def test_a_commit_given_as_a_git_ref_stays_a_commit_for_the_crew():
    """``commits: list[str]`` took any string before: ``HEAD``, a tag and a 3-character sha are commits."""
    commits, _, notes = normalize_crew_evidence(["HEAD", "abc", "v1.2.0", " 1ec6d03 ", "origin/main"], None)
    assert commits == ["HEAD", "abc", "v1.2.0", "1ec6d03", "origin/main"] and notes == []
    # what did need rewriting still is: an object, "sha subject" text; prose is not a commit
    commits, _, notes = normalize_crew_evidence([{"sha": "bd6ab83", "subject": "x"}, "1ec6d03 fix it", "shipped the fix"], None)
    assert commits == ["bd6ab83", "1ec6d03"]
    assert notes == ["commit as declared (no commit id): shipped the fix"]
    assert normalize_crew_evidence("HEAD", None)[0] == ["HEAD"]


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (True, True),
        (False, False),
        ("yes", True),
        ("true", True),
        ("False", False),
        (" no ", False),
        (1, True),
        (0, False),
        ("1", True),
        ("0", False),
    ],
)
def test_no_upstream_is_read_as_a_boolean(value: Any, want: bool):
    facts, changes = normalize_close_facts({"no_upstream": value})
    assert facts["no_upstream"] is want
    assert bool(changes) is (not isinstance(value, bool))


@pytest.mark.parametrize("value", ["unknown", "perhaps", "", 2, -1, 1.5, [], ["a"], {}, {"a": 1}, None, object()])
def test_a_no_upstream_that_says_neither_is_left_unrecorded(value: Any):
    facts, changes = normalize_close_facts({"no_upstream": value})
    assert "no_upstream" not in facts
    assert bool(changes) is (value is not None)
    assert relay_api.FactsIn.model_validate({"no_upstream": value}).no_upstream is False
    assert normalize_close_facts(facts) == (facts, [])
