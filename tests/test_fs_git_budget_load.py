"""F43 (review): the starved close on a machine where every git call is slow, and the numstat cap.

``tests/test_fs_git_budget.py`` covers one slow probe and a spent budget with a fast git. The handoff
behind the fault came from neither: the budget was spent before git was asked AND every git call took
a noticeable time, with a session of many commits. The per-commit ancestor checks then used up the
grace before push state and the working tree were asked.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from remembra.relay import facts as factlib
from remembra.relay.facts import Deadline, Git, git_facts, repo_info
from tests.relay_fixtures import commit, git, make_remote_and_clones


def _shim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> Path:
    """A ``git`` first on PATH that logs each call's arguments, runs ``body``, then the real git."""
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    calls = tmp_path / "git-calls.log"
    script = shim_dir / "git"
    script.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n{body}\nexec {real_git} "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    return calls


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture()
def busy(tmp_path: Path) -> tuple[Path, str, float, list[str]]:
    """A clone with an upstream, 30 commits made by this session and one untracked file."""
    _, clones = make_remote_and_clones(tmp_path)
    work = clones["laptop"]
    start = git(work, "rev-parse", "HEAD")
    started_at = time.time() - 5
    shas = [commit(work, f"f{i}.py", f"{i}\n", f"feat: step {i}") for i in range(30)]
    (work / "notes.txt").write_text("untracked\n")
    return work, start, started_at, shas


def test_a_starved_close_under_uniformly_slow_git_still_reads_commits_push_state_and_status(busy, tmp_path, monkeypatch):
    """crewd's shape: no RepoInfo, the budget already spent, and every git call takes 0.1 s."""
    work, start, started_at, shas = busy
    calls = _shim(monkeypatch, tmp_path, "sleep 0.1")
    began = time.monotonic()
    facts = git_facts(work, Deadline(0), start_head=start, started_at=started_at)
    took = time.monotonic() - began
    assert "incomplete" not in facts, facts.get("incomplete")
    assert facts["commit_range"] == "session-reflog"
    assert [c["sha"] for c in facts["commits"]] == shas
    assert facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 30
    assert facts["uncommitted_files"] == ["notes.txt"]
    assert took < 3.0
    # 30 commits are checked against HEAD in one call, not one call each
    assert sum(1 for line in _calls(calls) if " merge-base --is-ancestor " in f" {line} ") == 0
    assert len(_calls(calls)) <= 14


def test_push_state_is_asked_before_the_session_commits(busy, tmp_path, monkeypatch):
    """Whatever finding the commits costs, the two cheap calls a deploy decision rests on come first."""
    work, start, started_at, _ = busy
    info = repo_info(work, Deadline(5))
    calls = _shim(monkeypatch, tmp_path, "")
    git_facts(work, Deadline(5), start_head=start, started_at=started_at, info=info)
    order = _calls(calls)
    upstream = next(i for i, line in enumerate(order) if "@{u}" in line)
    reflog = next(i for i, line in enumerate(order) if " reflog " in f" {line} ")
    assert upstream < reflog


def test_slow_ancestor_checks_do_not_cost_push_state(busy, tmp_path, monkeypatch):
    """Finding the session's commits hangs (reflog and every ancestry question): only the log is unknown."""
    work, start, started_at, _ = busy
    info = repo_info(work, Deadline(5))
    _shim(monkeypatch, tmp_path, 'case " $* " in *" reflog "*|*" rev-list "*"--not"*|*" merge-base "*) exec sleep 20;; esac')
    began = time.monotonic()
    facts = git_facts(work, Deadline(0), start_head=start, started_at=started_at, info=info)
    assert time.monotonic() - began < 3.5
    assert facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 30
    assert "upstream" not in facts["incomplete"] and "log" in facts["incomplete"]


def test_ancestors_of_head_are_found_in_one_call_and_strangers_are_dropped(busy, tmp_path, monkeypatch):
    """The batched check answers exactly what the per-commit one did: short ids, a side branch, an unknown id."""
    work, _, _, shas = busy
    git(work, "checkout", "-q", "-b", "side", shas[10])
    stranger = commit(work, "side.py", "s\n", "feat: on a side branch")
    git(work, "checkout", "-q", "main")
    calls = _shim(monkeypatch, tmp_path, "")
    runner = Git(work, Deadline(5))
    asked = [shas[3][:9], stranger, shas[7], "deadbeef" * 5, shas[3], stranger[:10], shas[29][:12]]
    assert factlib._reachable(runner, asked) == [shas[3], shas[7], shas[29]]
    # all ids exist and are full: one call
    calls.write_text("")
    assert factlib._reachable(runner, [shas[1], stranger, shas[2]]) == [shas[1], shas[2]]
    assert len(_calls(calls)) == 1
    assert factlib._reachable(runner, []) == []


def test_numstat_gets_the_time_that_is_left_when_there_is_plenty(busy, tmp_path, monkeypatch):
    """A 2 s ``git diff --numstat`` under a 6 s budget used to give line counts and must still."""
    work, start, started_at, _ = busy
    (work / "f0.py").write_text("0\nmore\n")
    info = repo_info(work, Deadline(5))
    _shim(monkeypatch, tmp_path, 'case " $* " in *" --numstat HEAD "*) sleep 2;; esac')
    facts = git_facts(work, Deadline(6.0), start_head=start, started_at=started_at, info=info)
    assert "incomplete" not in facts
    assert "line counts not read" not in facts["diff_stat"]
    assert facts["diff_stat"].endswith("31 insertions(+), 0 deletions(-)")


def test_numstat_keeps_its_cap_when_time_is_short(busy, tmp_path, monkeypatch):
    work, start, started_at, _ = busy
    (work / "f0.py").write_text("0\nmore\n")
    info = repo_info(work, Deadline(5))
    _shim(monkeypatch, tmp_path, 'case " $* " in *" --numstat HEAD "*) exec sleep 20;; esac')
    began = time.monotonic()
    facts = git_facts(work, Deadline(0.3), start_head=start, started_at=started_at, info=info)
    assert time.monotonic() - began < 3.0
    assert "incomplete" not in facts
    assert "line counts not read" in facts["diff_stat"]
