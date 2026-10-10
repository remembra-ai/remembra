"""A close with no known session start reports a bounded window, never the whole branch.

``remembra-relay close`` run by hand (no ``brief`` first, no transcript) used to list every commit since the
branch left the default branch, and ``--hours`` was ignored whenever a default branch existed. On a long-lived
feature branch that credited weeks of other people's commits to the closing session: one real close listed 30
commits and 44 files, 29 of the commits made by another agent nine days earlier.

Now the branch range is always limited to the last ``hours``, and the evidence label says so. A session whose
start is known (``brief`` first, or a transcript) is unchanged.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remembra.relay.facts import Deadline, git_facts, repo_info
from remembra.relay.handoff import _window_evidence
from tests.relay_fixtures import GIT_ENV, commit, git, make_remote_and_clones


def commit_at(repo: Path, path: str, message: str, when: datetime) -> str:
    """A commit whose author and committer time are ``when``."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(message + "\n")
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {**os.environ, **GIT_ENV, "HOME": str(repo), "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    env.pop("GIT_DIR", None)
    subprocess.run(["git", "add", path], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, env=env, check=True, capture_output=True)
    return git(repo, "rev-parse", "HEAD")


def long_lived_branch(tmp_path: Path) -> tuple[Path, list[str], str]:
    """A teammate's nine-day-old feature branch on origin; this checkout branches from it and adds one commit now.

    Returns (this checkout's worktree, the teammate's commits, this checkout's commit).
    """
    _, clones = make_remote_and_clones(tmp_path, names=("teammate", "laptop"))
    old = datetime.now(UTC) - timedelta(days=9)
    git(clones["teammate"], "checkout", "-q", "-b", "feat/long")
    theirs = [commit_at(clones["teammate"], f"t{i}.py", f"feat: teammate work {i}", old) for i in range(3)]
    git(clones["teammate"], "push", "-q", "origin", "feat/long")
    git(clones["laptop"], "fetch", "-q", "origin")
    wt = tmp_path / "wt-mine"
    git(clones["laptop"], "worktree", "add", "-q", "-b", "fix/mine", str(wt), "origin/feat/long")
    mine = commit(wt, "mine.py", "mine\n", "fix: my one commit")
    return wt, theirs, mine


def adhoc_facts(repo: Path, hours: float) -> dict:
    return git_facts(repo, Deadline(10), hours=hours, info=repo_info(repo, Deadline(10)))


@pytest.mark.parametrize("reflog", [True, False])
def test_adhoc_close_on_a_long_lived_branch_lists_only_the_window(tmp_path, reflog):
    wt, theirs, mine = long_lived_branch(tmp_path)
    if not reflog:
        git(wt, "reflog", "expire", "--expire=now", "--all")
    facts = adhoc_facts(wt, hours=2)
    listed = [c["sha"] for c in facts["commits"]]
    assert listed == [mine], f"listed {len(listed)} commits; {len(set(listed) & set(theirs))} are the teammate's old ones"
    assert facts["files_changed"] == ["mine.py"]
    assert facts["commit_range"] == "merge-base:origin/main:last-2h"
    assert _window_evidence(facts["commit_range"])  # the brief still says: not necessarily by this agent


def test_hours_bounds_the_branch_range(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "checkout", "-q", "-b", "feat/x")
    earlier = commit_at(repo, "early.py", "feat: five hours ago", datetime.now(UTC) - timedelta(hours=5))
    recent = commit(repo, "late.py", "late\n", "feat: just now")
    assert [c["sha"] for c in adhoc_facts(repo, hours=2)["commits"]] == [recent]
    wide = adhoc_facts(repo, hours=12)
    assert [c["sha"] for c in wide["commits"]] == [earlier, recent]
    assert wide["commit_range"] == "merge-base:origin/main:last-12h"


def test_on_the_default_branch_the_window_is_unchanged(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    recent = commit(repo, "m.py", "m\n", "feat: on main")
    facts = adhoc_facts(repo, hours=12)
    assert facts["commit_range"] == "last-12h"
    assert recent in [c["sha"] for c in facts["commits"]]


def test_a_known_session_start_is_unchanged(tmp_path):
    wt, _theirs, mine = long_lived_branch(tmp_path)
    start = git(wt, "rev-parse", "HEAD")
    later = commit(wt, "later.py", "later\n", "fix: after the brief")
    info = repo_info(wt, Deadline(10))
    facts = git_facts(wt, Deadline(10), start_head=start, hours=2, info=info)
    assert [c["sha"] for c in facts["commits"]] == [later] and facts["commit_range"] == "session-reflog"
    nothing = git_facts(wt, Deadline(10), start_head=later, hours=2, info=info)
    assert nothing["commits"] == [] and nothing["commit_range"] == "session-start"
    assert mine not in [c["sha"] for c in facts["commits"]]
