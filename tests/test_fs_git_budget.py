"""F43: a slow or starved git probe must not turn the rest of a close's git facts into "unknown".

Every git subprocess of a close used to share one wall-clock budget with no reservation, in the
order log -> status -> ``diff --numstat`` -> upstream: the first slow probe (or a budget spent
before the first one ran) made every later probe "did not finish in time" without running it,
push state first. These tests run the real ``git`` (behind a shim that makes one probe slow).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from remembra.relay.facts import Deadline, git_facts, merge_facts, repo_info
from remembra.relay.handoff import HEALTH_INCOMPLETE, assess_handoff, build_sections
from tests.relay_fixtures import commit, git, make_remote_and_clones


def _shim(tmp_path: Path, body: str) -> str:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    script = shim_dir / "git"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(shim_dir)


def _slow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, condition: str, seconds: int = 6) -> None:
    """Put a ``git`` on PATH that sleeps when ``condition`` (a shell test over ``$*``) holds, else runs git."""
    real_git = shutil.which("git")
    shim = _shim(tmp_path, f'case " $* " in {condition}) exec sleep {seconds};; esac\nexec {real_git} "$@"\n')
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    _, clones = make_remote_and_clones(tmp_path)
    work = clones["laptop"]
    return work, git(work, "rev-parse", "HEAD")


def test_a_slow_numstat_diff_does_not_hide_push_state_and_is_not_a_gap(repo, tmp_path, monkeypatch):
    """The clawd shape: ``git diff --numstat HEAD`` needs ~10 s, everything else is fast."""
    work, start = repo
    (work / "README.md").write_text("# widget\nchanged\n")
    info = repo_info(work, Deadline(5))
    _slow(monkeypatch, tmp_path, '*" --numstat HEAD "*')
    began = time.monotonic()
    facts = git_facts(work, Deadline(3.0), start_head=start, info=info)
    assert time.monotonic() - began < 5.0
    assert facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 0
    assert facts["uncommitted_files"] == ["README.md"]
    # the file count came from the cheap `git diff --name-status HEAD`: nothing is unknown
    assert "incomplete" not in facts
    assert facts["diff_stat"].startswith("1 files changed")
    sections = build_sections(facts)
    assert not any("unknown" in line for line in sections["not_done"] + sections["done"])
    assert assess_handoff(merge_facts(facts, None, str(work)), None)["status"] != HEALTH_INCOMPLETE


def test_a_diff_that_never_answers_only_marks_the_diff(repo, tmp_path, monkeypatch):
    work, start = repo
    (work / "README.md").write_text("# widget\nchanged\n")
    info = repo_info(work, Deadline(5))
    _slow(monkeypatch, tmp_path, '*" diff "*')
    facts = git_facts(work, Deadline(3.0), start_head=start, info=info)
    assert facts["incomplete"] == ["diff"]
    assert facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 0
    assert "push state: unknown (git did not finish in time)" not in build_sections(facts)["not_done"]


def test_a_spent_budget_still_reads_commits_and_push_state(repo):
    """The budget was used up before git_facts ran (a slow transcript parse, a loaded machine)."""
    work, start = repo
    mine = commit(work, "m.py", "m\n", "feat: mine")
    info = repo_info(work, Deadline(5))
    facts = git_facts(work, Deadline(0), start_head=start, info=info)
    assert "incomplete" not in facts
    assert [c["sha"] for c in facts["commits"]] == [mine] and facts["commit_range"] == "session-reflog"
    assert facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 1


def test_a_spent_budget_without_repo_info_still_finds_the_repository(repo):
    """crewd's close calls git_facts without a RepoInfo, on a budget that started before the transcript parse."""
    work, start = repo
    mine = commit(work, "m.py", "m\n", "feat: mine")
    facts = git_facts(work, Deadline(0), start_head=start)
    assert "incomplete" not in facts
    assert facts["branch"] == "main" and facts["head_commit"] == mine
    assert [c["sha"] for c in facts["commits"]] == [mine]
    assert facts["unpushed_commits"] == 1


def test_a_head_that_was_not_read_in_time_is_not_reported_as_an_empty_repository(repo, tmp_path, monkeypatch):
    """repo_info ran out of time after the branch: HEAD is unknown, which is not "no commits yet"."""
    work, start = repo
    mine = commit(work, "m.py", "m\n", "feat: mine")
    _slow(monkeypatch, tmp_path, '*" remote "*')
    info = repo_info(work, Deadline(0.8))
    assert info.is_git and info.branch == "main" and info.head_commit is None
    assert info.head_unknown is True
    monkeypatch.undo()
    facts = git_facts(work, Deadline(0), start_head=start, info=info)
    assert facts["commit_range"] != "empty-repo"
    assert facts["head_commit"] == mine and [c["sha"] for c in facts["commits"]] == [mine]


def test_a_head_that_stays_unknown_marks_the_log_and_never_says_empty(repo, tmp_path, monkeypatch):
    work, _ = repo
    info = repo_info(work, Deadline(5))
    info.head_commit, info.head_unknown = None, True
    _slow(monkeypatch, tmp_path, '*" rev-parse HEAD "*')
    facts = git_facts(work, Deadline(0), info=info)
    assert facts["commit_range"] == "unknown" and "log" in facts["incomplete"]
    assert "no_upstream" not in facts
    assert "commits: unknown (git log did not finish in time)" in build_sections(facts)["done"]


def test_the_overrun_past_a_spent_budget_is_bounded(repo, tmp_path, monkeypatch):
    """git hangs on every call: the close gives up after the grace, with everything marked unknown."""
    work, _ = repo
    info = repo_info(work, Deadline(5))
    _slow(monkeypatch, tmp_path, "*", seconds=20)
    began = time.monotonic()
    facts = git_facts(work, Deadline(0), start_head="0" * 40, info=info)
    assert time.monotonic() - began < 3.5
    assert {"log", "status", "upstream"} <= set(facts["incomplete"])
    began = time.monotonic()
    assert git_facts(work, Deadline(0)) == {"incomplete": ["repo"]}
    assert time.monotonic() - began < 3.5


def test_no_grace_keeps_the_strict_budget(repo):
    """A caller that has no time to give (``grace=0``) gets the old behaviour: nothing runs past the deadline."""
    work, start = repo
    info = repo_info(work, Deadline(5))
    facts = git_facts(work, Deadline(0), start_head=start, info=info, grace=0)
    assert facts["incomplete"] == ["status", "diff", "upstream"]
    assert git_facts(work, Deadline(0), grace=0) == {"incomplete": ["repo"]}
    assert repo_info(work, Deadline(0)).is_git is False  # repo_info alone never overruns


def test_the_cli_takes_the_grace_out_of_spare_time_never_out_of_the_send_reserve():
    from remembra.relay import cli
    from remembra.relay import facts as factlib

    assert cli.git_time(9.5) == (cli.GIT_BUDGET_SECONDS, factlib.GIT_GRACE_SECONDS)
    budget, grace = cli.git_time(7.0)  # 4 s of git, 2 s to send: 1 s to spare
    assert (budget, round(grace, 6)) == (4.0, 1.0)
    assert cli.git_time(5.0) == (3.0, 0.0)
    assert cli.git_time(0.3) == (0.5, 0.0)  # nothing left: the old floor, and no overrun


def test_a_close_logs_which_git_probe_ran_out_of_time(repo, tmp_path, monkeypatch, capsys):
    """Through the real `remembra-relay close`: the handoff is sent with push state, and relay.log names the gap."""
    import httpx

    from remembra.relay import cli

    work, _ = repo
    (work / "README.md").write_text("# widget\nchanged\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_git_budget_test")
    monkeypatch.setenv("REMEMBRA_URL", "http://relay.test")
    for var in ("REMEMBRA_AGENT_ID", "REMEMBRA_PROJECT", "REMEMBRA_RELAY_PROJECT", "REMEMBRA_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/api/v1/session/close":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"handoff_id": "h1", "project_id": "widget", "headline": "x"})
        return httpx.Response(404, json={"detail": "nope"})

    monkeypatch.setattr(
        cli.Context,
        "client",
        lambda self, config=None, agent=None, budget=None: httpx.Client(
            base_url="http://relay.test", transport=httpx.MockTransport(handle)
        ),
    )
    _slow(monkeypatch, tmp_path, '*" diff "*')
    assert cli.main(["close", "--agent", "claude-code", "--cwd", str(work), "--session-id", "s-slow-diff"]) == 0
    capsys.readouterr()
    facts = sent[0]["facts"]
    assert facts["incomplete"] == ["diff"] and facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 0
    log = (home / ".remembra" / "relay" / "relay.log").read_text()
    assert "close: git did not finish in time for claude-code session s-slow-diff (diff)" in log
