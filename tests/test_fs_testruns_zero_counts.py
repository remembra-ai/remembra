"""F31 review: a zero count is not a failure (``"FAIL: 0"``), a non-zero count in the same form is.

``summary_shows_failure`` took any line that starts with ``FAIL`` for a runner's failure line,
so an automake-style count ``FAIL: 0`` turned a passing run into a failing one, while the same
counts written ``failed=2`` or ``failures: 4`` were not read at all. Counts are now read as
counts, in every form: zero is never a failure, anything else is.
"""

from __future__ import annotations

import pytest

from remembra.relay.testruns import resolve_runs, run_summary, run_verdict, summary_shows_failure, summary_shows_tests_ran

# (form with {n} for the count, also a line that says tests ran when the count is zero)
FORMS = [
    ("FAIL: {n}", False),
    ("FAIL:{n}", False),
    ("FAIL : {n}", False),
    ("FAIL={n}", False),
    ("FAIL {n}", False),
    ("# FAIL:  {n}", False),
    ("FAILED: {n}", False),
    ("FAILED={n}", False),
    ("failed={n}", False),
    ("failed: {n}", False),
    ("Failed: {n}", False),
    ("{n} failed", False),
    ("{n} failures", False),
    ("{n} errors", False),
    ("failures: {n}", False),
    ("failures={n}", False),
    ("Failures: {n}.", False),
    ("errors={n}", False),
    ("errors: {n}", False),
    ("ERROR: {n}", False),
    ("# fail {n}", False),
    ("FAIL: {n}, PASS: 12", True),
    ("PASS: 12, FAIL: {n}", True),
    ("FAIL: {n}\tPASS: 4\tSKIP: 1", True),
    ("TOTAL: 5 PASS: 5 FAIL: {n}", True),
    ("PASS: 3 | FAIL: {n} | SKIP: 0", True),
    ("passed=5 failed={n} errors=0", True),
    ("passed=5 failed=0 errors={n}", True),
    ("passed: 5; failed: {n}", True),
    ("Failures: {n}, Errors: 0", False),
    ("Failures: 0, Errors: {n}", False),
    ("FAIL: 0, ERROR: {n}", False),
    ("39 passed, {n} failed", True),
    ("{n} failed, 39 passed in 3.2s", True),
    ("Tests: 5, Assertions: 5, Failures: {n}.", True),
    ("Passed!  - Failed:     {n}, Passed:     3", False),
    ("test result: ok. 39 passed; {n} failed; 0 ignored", True),
    ("Executed 3 tests, with {n} failures", True),
    ("3 examples, {n} failures", False),
]
# written by a runner only when something failed: the rest of the line says so with or without a count
ZERO_ONLY = {"test result: ok. 39 passed; {n} failed; 0 ignored": "test result: FAILED. 38 passed; {n} failed; 0 ignored"}
ZEROS = ["0", "00"]
NON_ZEROS = ["1", "3", "10", "03", "100"]


@pytest.mark.parametrize("form", [f for f, _ in FORMS])
@pytest.mark.parametrize("zero", ZEROS)
def test_a_zero_count_is_not_a_failure_in_any_form(form, zero):
    line = form.format(n=zero)
    assert summary_shows_failure(line) is False, line
    assert summary_shows_failure(f"=== {line} ===") is False, line


@pytest.mark.parametrize("form", [f for f, _ in FORMS])
@pytest.mark.parametrize("count", NON_ZEROS)
def test_a_non_zero_count_is_a_failure_in_every_form(form, count):
    line = ZERO_ONLY.get(form, form).format(n=count)
    assert summary_shows_failure(line) is True, line
    assert summary_shows_tests_ran(line) is False, line


@pytest.mark.parametrize("form", [f for f, ran in FORMS if ran])
def test_a_zero_failure_count_beside_passed_tests_says_tests_ran(form):
    assert summary_shows_tests_ran(form.format(n="0")) is True, form


@pytest.mark.parametrize(
    "line",
    [
        "FAIL",
        "FAILED",
        "FAILURES!",
        "FAILED (failures=1)",
        "FAILED (errors=2)",
        "FAILED tests/a.py::test_x - assert 0",
        "FAIL: test_zero_is_kept (tests.test_money.MoneyTest)",
        "FAIL: 0.5s",
        "FAIL\texample.com/pkg\t0.2s",
        "FAIL: 0, FAIL: 2",
        "** TEST FAILED **",
        "not ok 3 - parses the header",
        "Some tests failed.",
    ],
)
def test_a_failure_line_without_a_zero_count_still_fails(line):
    assert summary_shows_failure(line) is True, line


@pytest.mark.parametrize(
    "line",
    [
        "failed=2 in the first run, fixed since",
        "errors: 3 of them were typos in my notes",
        "214 passed after fixing the 2 failures from the first run",
        "the count is failed=2",
        "error: 1 file would be reformatted",
        "fail: 2 fast",
    ],
)
def test_prose_with_a_count_in_it_is_still_not_a_runner_line(line):
    assert summary_shows_failure(line) is False, line


def test_a_declared_pass_with_a_zero_count_stands_and_one_with_a_real_count_does_not():
    current, _ = resolve_runs(
        [
            {"cmd": "pytest -q", "passed": True, "summary": "FAIL: 0, PASS: 12"},
            {"cmd": "npm test", "passed": True, "summary": "FAIL: 2, PASS: 10"},
            {"cmd": "go test ./...", "passed": True, "summary": "failed=0"},
            {"cmd": "cargo test", "passed": True, "summary": "failed=3"},
        ]
    )
    assert [(r["cmd"], r["passed"]) for r in current] == [
        ("pytest -q", True),
        ("npm test", False),
        ("go test ./...", True),
        ("cargo test", False),
    ]


def test_a_run_whose_output_ends_in_a_zero_count_keeps_its_exit_code_verdict():
    out = "12 passed in 0.10s\nFAIL: 0\n"
    assert run_verdict("pytest -q", 0, out) == (True, run_summary(out))
    assert run_verdict("pytest -q", 1, out)[0] is False
    piped = run_verdict("pytest -q | tee log", 0, "Tests: 0 failed, 12 passed, 12 total\n")
    assert piped is not None and piped[0] is True
    piped = run_verdict("pytest -q | tee log", 0, "Tests: 2 failed, 10 passed, 12 total\n")
    assert piped is not None and piped[0] is False
