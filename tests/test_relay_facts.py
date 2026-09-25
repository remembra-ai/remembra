"""Relay client-side facts: remote normalization, git facts in real repos, transcript parsing."""

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime

import pytest

from remembra.relay.facts import Deadline, git_facts, merge_facts, parse_claude_transcript, repo_info
from remembra.relay.handoff import build_sections, check_summary_grounding, render_brief
from remembra.relay.identity import ProjectLocator, normalize_git_remote, normalize_root_commit, slugify_project
from tests.relay_fixtures import Transcript, commit, git, make_remote_and_clones

SECRET = "sk-proj-" + "Zx9Qw3Er7Ty1Ui5Op2As8Df4Gh6Jk0Lz"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Acme/Widget.git",
        "http://github.com/acme/widget",
        "git@github.com:acme/widget.git",
        "github.com:Acme/Widget",
        "ssh://git@github.com/acme/widget",
        "ssh://git@github.com:22/acme/widget.git/",
        "git+ssh://git@github.com/acme/widget.git",
        "https://oauth2:ghp_token@github.com/acme/widget",
        "https://www.github.com/acme//widget.git",
    ],
)
def test_remote_variants_normalize_to_one_fingerprint(url):
    assert normalize_git_remote(url) == "github.com/acme/widget"


def test_remote_edge_cases():
    assert normalize_git_remote("") is None
    assert normalize_git_remote("https://github.com/") is None
    assert normalize_git_remote("/srv/git/Widget.git") == "local:/srv/git/widget"
    assert normalize_git_remote("git@gitlab.com:group/sub/proj.git") == "gitlab.com/group/sub/proj"
    assert normalize_root_commit("A" * 40) == "a" * 40
    assert normalize_root_commit("abc") is None
    assert slugify_project("My Repo!!") == "my-repo"
    assert slugify_project("") == "project"
    loc = ProjectLocator(git_remote="git@github.com:acme/widget.git", root_commit="b" * 40, root_path="/x/y", host="Mac")
    assert [f.key for f in loc.fingerprints()] == ["git:github.com/acme/widget", "root:" + "b" * 40, "path:mac:/x/y"]
    assert loc.display_name() == "widget"


def test_git_facts_session_commits_uncommitted_and_push_state(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    start = git(repo, "rev-parse", "HEAD")
    commit(repo, "src/widget.py", "x = 1\n", "feat: widget api")
    commit(repo, "tests/test_widget.py", "def test(): pass\n", "test: widget")
    (repo / "README.md").write_text("# widget\nchanged\n")
    (repo / "scratch.txt").write_text("untracked\n")

    info = repo_info(repo, Deadline(5))
    assert info.is_git and info.branch == "main" and info.repo_name == "widget"
    assert info.root_commit == git(repo, "rev-list", "--max-parents=0", "HEAD")
    facts = git_facts(repo, Deadline(5), start_head=start, info=info)
    assert [c["subject"] for c in facts["commits"]] == ["feat: widget api", "test: widget"]
    assert facts["commit_range"] == "session-reflog"
    assert facts["uncommitted_files"] == ["README.md", "scratch.txt"]
    assert set(facts["files_changed"]) == {"src/widget.py", "tests/test_widget.py", "README.md", "scratch.txt"}
    assert facts["upstream"] == "origin/main" and facts["unpushed_commits"] == 2
    assert "files changed" in facts["diff_stat"]


def test_git_facts_worktree_and_no_upstream(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    wt = tmp_path / "wt-feature"
    git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    commit(wt, "a.py", "a\n", "feat: in worktree")
    info = repo_info(wt, Deadline(5))
    assert info.repo_name == "widget"  # main checkout's name, not the worktree dir
    assert info.branch == "feat/x"
    facts = git_facts(wt, Deadline(5), info=info)
    assert facts.get("no_upstream") is True and "unpushed_commits" not in facts
    assert [c["subject"] for c in facts["commits"]] == ["feat: in worktree"]
    assert facts["commit_range"].startswith(("merge-base", "last-"))


def test_non_git_directory_yields_no_git_facts(tmp_path):
    info = repo_info(tmp_path, Deadline(5))
    assert info.is_git is False
    assert git_facts(tmp_path, Deadline(5), info=info) == {}
    assert info.locator(tmp_path, "host1")["root_path"] == str(tmp_path)


def test_expired_deadline_never_runs_git(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    info = repo_info(clones["laptop"], Deadline(0))
    assert info.is_git is False  # nothing ran; callers degrade to path identity


def _transcript(tmp_path, cwd):
    t = Transcript("sess-A", cwd)
    t.bash("ls src", 0, "widget.py")
    t.bash("grep -rn TODO src", 1, "")  # probe: no match, not an error
    t.bash("pytest -q tests/test_widget.py", 1, "F...\n=== 1 failed, 3 passed in 0.21s ===")
    t.bash("npm run build", 2, "Error: Cannot find module 'left-pad'")
    t.bash("npm run build", 0, "built")  # fixed later: not an error
    t.bash("make deploy", 2, "deploy: permission denied")
    t.bash(f"curl -H 'Authorization: Bearer {SECRET}' https://api.example.com", 0, "ok")
    t.bash("npm test", 0, "Tests:       12 passed, 12 total")
    t.bash("cd /somewhere/else && pytest -q", 1, "=== 9 failed in 1s ===")  # another repo: not ours
    t.bash('git add -A && git commit -m "feat: widget api"', 0, "[main 1a2b3c4] feat: widget api\n 2 files changed")
    t.tool("Bash", {"command": "sleep 100", "run_in_background": True}, "Command running in background with ID: b1")
    t.tool("Bash", {"command": "rm -rf /"}, "The user doesn't want to proceed with this tool use.", is_error=True)
    t.tool("Edit", {"file_path": str(cwd / "src" / "widget.py"), "old_string": "a", "new_string": "b"}, "ok")
    t.tool("Write", {"file_path": "/etc/elsewhere.conf", "content": "x"}, "ok")
    t.tool(
        "Edit",
        {"file_path": str(cwd / "missing.py"), "old_string": "a", "new_string": "b"},
        "File does not exist.",
        is_error=True,
    )
    t.tool(
        "TodoWrite",
        {
            "todos": [
                {"content": "build widget api", "status": "completed", "activeForm": "x"},
                {"content": "wire the codex adapter", "status": "in_progress", "activeForm": "x"},
            ]
        },
        "ok",
    )
    t.tool("TaskCreate", {"subject": "write docs"}, "Task #1 created", tur={"task": {"id": "1", "subject": "write docs"}})
    t.tool("TaskCreate", {"subject": "ship it"}, "Task #2 created", tur={"task": {"id": "2", "subject": "ship it"}})
    t.tool("TaskUpdate", {"taskId": "2", "status": "completed"}, "ok")
    return t.write(tmp_path / "sess-A.jsonl")


def test_parse_claude_transcript(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    facts = parse_claude_transcript(_transcript(tmp_path, repo), root=str(repo))
    assert facts.session_id == "sess-A"
    assert facts.commit_shas == ["1a2b3c4"]
    assert not any("somewhere" in c["cmd"] for c in facts.commands + facts.tests)
    assert facts.started_at == datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    tests = {t["cmd"]: t for t in facts.tests}
    assert tests["pytest -q tests/test_widget.py"] == {
        "cmd": "pytest -q tests/test_widget.py",
        "passed": False,
        "summary": "1 failed, 3 passed in 0.21s",
    }
    assert tests["npm test"]["passed"] is True and tests["npm test"]["summary"].startswith("Tests:")
    assert facts.errors == ["`make deploy` exited 2: deploy: permission denied"]
    assert all("sleep 100" not in c["cmd"] and "rm -rf" not in c["cmd"] for c in facts.commands)
    assert {"cmd": "npm run build", "exit_code": 2} in facts.commands
    assert facts.files == [str(repo / "src" / "widget.py"), "/etc/elsewhere.conf"]
    assert facts.todos_open == ["wire the codex adapter", "write docs"]
    assert SECRET not in str(facts.commands)
    assert any("[REDACTED:" in c["cmd"] for c in facts.commands)


def test_merge_facts_relativizes_paths_and_feeds_sections(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    transcript = parse_claude_transcript(_transcript(tmp_path, repo), root=str(repo))
    merged = merge_facts({"branch": "main", "files_changed": ["README.md"], "commit_range": "x"}, transcript, str(repo))
    assert "commit_range" not in merged
    assert merged["files_changed"] == ["README.md", "src/widget.py"]  # /etc/elsewhere.conf is outside the repo
    sections = build_sections(merged)
    assert sections["failing"][0] == "FAILING: pytest -q tests/test_widget.py (1 failed, 3 passed in 0.21s)"
    # Described once (by the error entry), never twice; probes and later-fixed commands are not failures.
    assert "`make deploy` exited 2" not in sections["failing"]
    assert not any("grep" in f or "npm run build" in f for f in sections["failing"])
    assert "error: `make deploy` exited 2: deploy: permission denied" in sections["failing"]
    assert sections["next"] == "fix the failing run: pytest -q tests/test_widget.py"


def test_transcript_garbage_lines_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        'not json\n{"type": "user"}\n[1,2]\n{"message": {"content": [{"type": "tool_result", "tool_use_id": "nope"}]}}\n'
    )
    facts = parse_claude_transcript(path)
    assert facts.commands == [] and facts.tests == [] and facts.todos_open == []


def test_grounding_and_brief_render_are_pure():
    facts = {"commits": [{"sha": "abc1234" + "0" * 33, "subject": "x"}], "tests": [], "files_changed": ["a/b.py"]}
    assert check_summary_grounding(None, facts)["status"] == "none"
    ok = check_summary_grounding("Changed a/b.py in abc1234; not pushed yet.", facts)
    assert ok["status"] == "consistent" and ok["issues"] == []
    bad = check_summary_grounding("All tests pass, nothing failing.", {**facts, "errors": ["boom"]})
    assert bad["status"] == "contradicted"
    unverifiable = check_summary_grounding("Deployed to prod.", facts)
    assert unverifiable["status"] == "consistent" and "unverifiable" in unverifiable["issues"][0]
    text = render_brief({"project_id": "p", "agent_id": "a", "handoff": None, "recent": []})
    assert text.splitlines()[3] == "Last session: none recorded for this project."


def test_failing_commands_from_mcp_agents_skip_probes_and_fixed_runs():
    sections = build_sections(
        {
            "commands": [
                {"cmd": "grep -rn foo src", "exit_code": 1},
                {"cmd": "cargo build", "exit_code": 101},
                {"cmd": "cargo build", "exit_code": 0},
                {"cmd": "./deploy.sh", "exit_code": 3},
                {"cmd": "FOO=1 test -f x", "exit_code": 1},
            ]
        }
    )
    assert sections["failing"] == ["`./deploy.sh` exited 3"]
    assert sections["next"] == "resolve: `./deploy.sh` exited 3"


def test_transcript_commit_evidence_limits_commits_to_this_agent(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    other = commit(repo, "other.py", "x\n", "feat: someone else's work")
    mine = commit(repo, "mine.py", "y\n", "feat: my work")
    info = repo_info(repo, Deadline(5))
    facts = git_facts(repo, Deadline(5), session_commits=[mine[:7], "deadbee"], info=info)
    assert facts["commit_range"] == "transcript-commits"
    assert [c["sha"] for c in facts["commits"]] == [mine]
    assert other not in str(facts["commits"])
    assert facts["files_changed"] == ["mine.py"]
    nothing = git_facts(repo, Deadline(5), session_commits=[], info=info)
    assert nothing["commits"] == [] and nothing["commit_range"] == "transcript-no-commits"
    # A recorded session start wins over transcript evidence (the reflog shows what this checkout committed).
    ranged = git_facts(repo, Deadline(5), start_head=other, session_commits=[], info=info)
    assert [c["sha"] for c in ranged["commits"]] == [mine] and ranged["commit_range"] == "session-reflog"


# ---------------------------------------------------------------------------
# Review fixes: only this checkout's commits, directory-aware transcripts,
# remote choice / ssh aliases / relative remotes, git timeouts
# ---------------------------------------------------------------------------


def _teammate_pushes(tmp_path, clones, n=2):
    mate = clones["mate"]
    shas = [commit(mate, f"mate{i}.py", "m\n", f"OTHER PERSON: teammate change {i}") for i in range(n)]
    git(mate, "push", "-q", "origin", "main")
    return shas


def test_pulled_commits_are_not_this_sessions_work(tmp_path):
    _, clones = make_remote_and_clones(tmp_path, ("agent", "mate"))
    repo = clones["agent"]
    start, started = git(repo, "rev-parse", "HEAD"), time.time()
    time.sleep(1.1)  # reflog timestamps have one-second resolution
    mates = _teammate_pushes(tmp_path, clones)
    git(repo, "pull", "-q", "--ff-only", "origin", "main")
    mine = commit(repo, "mine.py", "one\ntwo\n", "agent: my work")

    info = repo_info(repo, Deadline(5))
    for started_at in (started, None):  # with the recorded start time, and walking back to the start HEAD
        facts = git_facts(repo, Deadline(5), start_head=start, info=info, started_at=started_at)
        assert facts["commit_range"] == "session-reflog"
        assert [c["sha"] for c in facts["commits"]] == [mine], started_at
        assert not set(mates) & {c["sha"] for c in facts["commits"]}
        assert facts["files_changed"] == ["mine.py"]
        assert facts["diff_stat"] == "1 files changed, 2 insertions(+), 0 deletions(-)"


def test_merged_commits_are_not_this_sessions_work(tmp_path):
    _, clones = make_remote_and_clones(tmp_path, ("agent", "mate"))
    repo = clones["agent"]
    git(repo, "checkout", "-q", "-b", "feat/x")
    start, started = git(repo, "rev-parse", "HEAD"), time.time()
    time.sleep(1.1)
    first = commit(repo, "a.py", "a\n", "agent: first")
    _teammate_pushes(tmp_path, clones)
    git(repo, "fetch", "-q", "origin")
    git(repo, "merge", "-q", "--no-edit", "origin/main")
    second = commit(repo, "b.py", "b\n", "agent: second")
    amended = commit(repo, "c.py", "c\n", "agent: third")
    git(repo, "commit", "-q", "--amend", "-m", "agent: third (amended)")
    final = git(repo, "rev-parse", "HEAD")
    facts = git_facts(repo, Deadline(5), start_head=start, info=repo_info(repo, Deadline(5)), started_at=started)
    assert [c["sha"] for c in facts["commits"]] == [first, second, final]
    assert amended not in str(facts["commits"])
    assert [c["subject"] for c in facts["commits"]][-1] == "agent: third (amended)"


def test_commands_run_in_other_directories_are_not_this_projects(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    other = tmp_path / "pull" / "a"
    other.mkdir(parents=True)
    t = Transcript("sess-dirs", repo)
    t.bash("cd ../../pull/a && pytest", 1, "=== 3 failed in 0.1s ===")  # relative to the repo
    t.bash(f"(cd {other} && npm test)", 1, "npm ERR! Test failed.")
    t.bash(f"git -C {other} push", 1, "! [rejected] main -> main (fetch first)")
    t.bash(f"cd {other}", 0, "")
    t.cwd = str(other)  # Claude Code records the shell's new directory on the following lines
    t.bash("cargo test", 101, "test result: FAILED. 0 passed; 2 failed")
    t.bash("make deploy", 2, "no rule")
    t.cwd = str(repo)
    t.bash("pytest -q", 1, "=== 1 failed in 0.1s ===")
    t.bash("./scripts/check.sh", 3, "check failed")
    parsed = parse_claude_transcript(t.write(tmp_path / "dirs.jsonl"), root=str(repo))
    assert [x["cmd"] for x in parsed.tests] == ["pytest -q"]
    assert parsed.errors == ["`./scripts/check.sh` exited 3: check failed"]
    assert {c["cmd"] for c in parsed.commands} == {"pytest -q", "./scripts/check.sh"}


def test_remote_choice_prefers_origin_then_the_tracked_remote(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "remote", "rename", "origin", "mine")
    git(repo, "remote", "set-url", "mine", "https://github.com/mani/widget")
    git(repo, "remote", "add", "acme", "https://github.com/acme/widget")
    assert repo_info(repo, Deadline(5)).git_remote == "https://github.com/mani/widget"  # main tracks mine
    git(repo, "remote", "add", "origin", "https://github.com/org/widget")
    assert repo_info(repo, Deadline(5)).git_remote == "https://github.com/org/widget"


def test_relative_local_remote_is_made_absolute(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "remote", "set-url", "origin", "../upstream")
    info = repo_info(repo, Deadline(5))
    assert info.toplevel and info.git_remote == os.path.normpath(os.path.join(info.toplevel, "..", "upstream"))
    assert os.path.isabs(info.git_remote) and normalize_git_remote(info.git_remote).startswith("local:/")
    assert normalize_git_remote("../upstream") is None and normalize_git_remote("./x") is None


def _shim(tmp_path, name: str, body: str) -> str:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    script = shim_dir / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(shim_dir)


def test_ssh_host_alias_resolves_to_the_real_host(tmp_path, monkeypatch):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "remote", "set-url", "origin", "git@github-work:mani/widget.git")
    shim = _shim(
        tmp_path,
        "ssh",
        'if [ "$1" = "-G" ] && [ "$2" = "github-work" ]; then printf "user git\\nhostname github.com\\nport 22\\n"; exit 0; fi\n'
        "exit 255\n",
    )
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    info = repo_info(repo, Deadline(5))
    assert info.git_remote == "git@github.com:mani/widget.git"
    assert normalize_git_remote(info.git_remote) == "github.com/mani/widget"


def test_git_timeout_is_reported_as_unknown_not_clean(tmp_path, monkeypatch):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    (repo / "dirty.txt").write_text("x\n")
    info = repo_info(repo, Deadline(5))
    real_git = shutil.which("git")
    shim = _shim(tmp_path, "git", f'for a in "$@"; do [ "$a" = "status" ] && exec sleep 5; done\nexec {real_git} "$@"\n')
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    facts = git_facts(repo, Deadline(1.5), info=info)
    assert "status" in facts["incomplete"]
    assert facts["uncommitted_files"] == []
    sections = build_sections(facts)
    assert "uncommitted changes: unknown (git status did not finish in time)" in sections["not_done"]


def test_without_a_reflog_the_session_range_falls_back_and_is_labeled(tmp_path):
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    start = git(repo, "rev-parse", "HEAD")
    mine = commit(repo, "m.py", "m\n", "feat: mine")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    facts = git_facts(repo, Deadline(5), start_head=start, info=repo_info(repo, Deadline(5)))
    assert facts["commit_range"] == "session-start-range" and [c["sha"] for c in facts["commits"]] == [mine]
    sections = build_sections({**facts, "commit_evidence": facts["commit_range"]})
    assert sections["done"][0].endswith("feat: mine")
