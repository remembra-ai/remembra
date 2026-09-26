"""WP-10: crew git gates (spec §8.4) installed into real temp repositories and run by real git."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from remembra.crew import gatecore
from remembra.crew.schemas import CREW_HOOK_MARKER
from remembra.relay.crew import githooks
from tests.crew.wp10_support import gate_command, git, make_repo, stub_log, verbs, make_crew_env


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    return make_crew_env(tmp_path, monkeypatch)


pytestmark = pytest.mark.skipif(os.name == "nt", reason="git hooks are POSIX shell scripts")


def _install(repo: Path, env, hooks=githooks.HOOKS) -> githooks.HookPlan:
    plan = githooks.plan_install(repo, gate_command(env), hooks=hooks)
    githooks.apply_plan(plan)
    return plan


def _commit(repo: Path, rel: str, text: str = "x\n", *extra: str) -> object:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", rel)
    return git(repo, "commit", "-m", f"add {rel}", *extra, check=False)


def _bare_remote(env, repo: Path) -> Path:
    remote = env["tmp"] / "remote.git"
    git(env["tmp"], "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    return remote


# ---------------------------------------------------------------------------
# plain install
# ---------------------------------------------------------------------------


def test_plain_install_gates_commits_and_adds_trailer(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    plan = _install(repo, crew_env)
    assert plan.methods == dict.fromkeys(githooks.HOOKS, "plain")
    for hook in githooks.HOOKS:
        path = repo / ".git" / "hooks" / hook
        assert path.stat().st_mode & stat.S_IXUSR
        text = path.read_text()
        assert CREW_HOOK_MARKER in text and githooks.is_crew_script(text)
    assert githooks.status(repo) == dict.fromkeys(githooks.HOOKS, "ok")

    ok = _commit(repo, "src/app.ts")
    assert ok.returncode == 0, ok.stderr
    message = git(repo, "log", "-1", "--format=%B").stdout
    assert "Remembra-Member: stub-member" in message  # prepare-commit-msg -> gate trailer
    assert verbs(crew_env)[:2] == ["precommit", "trailer"]
    trailer_call = stub_log(crew_env)[1]
    assert trailer_call["argv"][1].endswith("COMMIT_EDITMSG")

    head = git(repo, "rev-parse", "HEAD").stdout
    denied = _commit(repo, "held/cart.ts")
    assert denied.returncode != 0
    assert "BLOCKED by Remembra Crew (stub)" in denied.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == head  # no commit was created


def test_pre_push_catches_a_no_verify_commit(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    _bare_remote(crew_env, repo)
    _install(repo, crew_env)
    assert git(repo, "push", "-q", "origin", "main", check=False).returncode == 0
    sneaky = _commit(repo, "held/cart.ts", "x\n", "--no-verify")
    assert sneaky.returncode == 0  # pre-commit skipped: that is why pre-push exists (D22)
    push = git(repo, "push", "origin", "main", check=False)
    assert push.returncode != 0
    assert "pushed commits touch held/" in push.stderr
    prepush = [e for e in stub_log(crew_env) if e["verb"] == "prepush"][-1]
    assert prepush["argv"][1:] == ["origin", str(crew_env["tmp"] / "remote.git")]
    assert "refs/heads/main" in prepush["stdin"]


def test_existing_hook_is_kept_and_runs_first(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    marker = tmp_path / "user-hook.log"
    user = repo / ".git" / "hooks" / "pre-commit"
    user.write_text(f'#!/bin/sh\necho "user hook ran" >> {marker}\n[ ! -f "$(git rev-parse --show-toplevel)/veto" ]\n')
    user.chmod(0o755)
    original = user.read_text()
    plan = _install(repo, crew_env)
    assert plan.methods["pre-commit"] == "delegate"
    prev = repo / ".git" / "hooks" / f"pre-commit{githooks.PREV_SUFFIX}"
    assert prev.read_text() == original and prev.stat().st_mode & stat.S_IXUSR
    assert githooks.status(repo)["pre-commit"] == "chained"

    assert _commit(repo, "a.txt").returncode == 0
    assert marker.read_text().count("user hook ran") == 1
    assert "precommit" in verbs(crew_env)

    # The user's hook fails first: the gate is not even asked.
    (repo / "veto").write_text("1")
    before = verbs(crew_env).count("precommit")
    assert _commit(repo, "b.txt").returncode != 0
    assert verbs(crew_env).count("precommit") == before

    # Re-running is a no-op.
    again = githooks.plan_install(repo, gate_command(crew_env))
    assert not again.changed


def test_pre_push_prev_and_gate_both_get_stdin(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    _bare_remote(crew_env, repo)
    seen = tmp_path / "prev-stdin.txt"
    user = repo / ".git" / "hooks" / "pre-push"
    user.write_text(f"#!/bin/sh\ncat > {seen}\n")
    user.chmod(0o755)
    _install(repo, crew_env)
    _commit(repo, "ok.txt")
    push = git(repo, "push", "origin", "main", check=False)
    assert push.returncode == 0, push.stderr
    gate_stdin = [e for e in stub_log(crew_env) if e["verb"] == "prepush"][-1]["stdin"]
    assert seen.read_text() == gate_stdin
    assert "refs/heads/main" in gate_stdin


def test_missing_gate_fails_open_with_a_note(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    gate = tmp_path / "gone" / "crew-gate.py"
    plan = githooks.plan_install(repo, githooks.GateCommand(python=os.sys.executable, gate=str(gate)))
    githooks.apply_plan(plan)
    result = _commit(repo, "held/x.ts")
    assert result.returncode == 0
    assert "crew gate not found" in result.stderr


def test_gate_exit_code_is_propagated(crew_env, tmp_path):
    import subprocess

    repo = make_repo(tmp_path / "repo")
    _install(repo, crew_env, hooks=("pre-commit",))
    (repo / "fail-precommit").write_text("1")
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert subprocess.run([str(hook)], cwd=repo).returncode == 3
    assert _commit(repo, "a.txt").returncode != 0


# ---------------------------------------------------------------------------
# managers: Husky v9 and lefthook
# ---------------------------------------------------------------------------

HUSKY_H = """#!/usr/bin/env sh
n=$(basename "$0")
s=$(dirname "$(dirname "$0")")/$n
[ ! -f "$s" ] && exit 0
[ "${HUSKY-}" = "0" ] && exit 0
sh -e "$s" "$@"
c=$?
[ $c != 0 ] && echo "husky - $n script failed (code $c)"
exit $c
"""


def _husky(repo: Path) -> None:
    """What ``husky`` v9 generates: .husky/_/h plus one runner per hook, and core.hooksPath=.husky/_."""
    d = repo / ".husky" / "_"
    if d.exists():
        for p in d.iterdir():
            p.unlink()
    d.mkdir(parents=True, exist_ok=True)
    (d / "h").write_text(HUSKY_H)
    (d / ".gitignore").write_text("*\n")
    for hook in ("pre-commit", "prepare-commit-msg", "pre-push", "commit-msg"):
        (d / hook).write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n')
        (d / hook).chmod(0o755)
    (d / "h").chmod(0o755)
    git(repo, "config", "core.hooksPath", ".husky/_")


def test_husky_v9_tracked_script_uses_runner_delegation_untracked_uses_include(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    log = tmp_path / "husky.log"
    (repo / ".husky").mkdir()
    (repo / ".husky" / "pre-commit").write_text(f"echo husky-pre-commit >> {log}\n")
    git(repo, "add", ".husky/pre-commit")
    git(repo, "commit", "-qm", "husky")
    _husky(repo)
    _bare_remote(crew_env, repo)

    plan = _install(repo, crew_env)
    # tracked .husky/pre-commit is never modified: the runner in .husky/_ is chained instead
    assert plan.methods["pre-commit"] == "delegate"
    assert plan.methods["pre-push"] == "husky" and plan.methods["prepare-commit-msg"] == "husky"
    assert git(repo, "status", "--porcelain").stdout == ""  # nothing untracked or modified shows up
    assert CREW_HOOK_MARKER not in (repo / ".husky" / "pre-commit").read_text()
    assert githooks.husky_line("pre-push") in (repo / ".husky" / "pre-push").read_text()
    assert (repo / ".git" / "hooks" / githooks.INCLUDE_DIRNAME / "pre-push").is_file()
    assert set(githooks.status(repo).values()) == {"chained"}

    assert _commit(repo, "a.txt").returncode == 0
    assert "husky-pre-commit" in log.read_text()  # the repo's own hook still ran
    assert "Remembra-Member: stub-member" in git(repo, "log", "-1", "--format=%B").stdout
    assert _commit(repo, "held/b.txt").returncode != 0
    _commit(repo, "held/c.txt", "x\n", "--no-verify")
    push = git(repo, "push", "origin", "main", check=False)
    assert push.returncode != 0 and "pushed commits touch held/" in (push.stdout + push.stderr)

    # `npm install` re-runs husky, which rewrites .husky/_: the delegated pre-commit is gone,
    # the include-based hooks survive (the include lives in .git, the line in the untracked script).
    _husky(repo)
    assert githooks.status(repo) == {"pre-commit": "missing", "prepare-commit-msg": "chained", "pre-push": "chained"}
    assert githooks.ensure(repo, gate_command(crew_env), consented=False)["pre-commit"] == "missing"
    repaired = githooks.ensure(repo, gate_command(crew_env), consented=True)
    assert repaired["pre-commit"] == "chained"
    assert _commit(repo, "held/d.txt").returncode != 0


def test_lefthook_gets_local_commands(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "lefthook.yml").write_text("pre-commit:\n  commands:\n    lint:\n      run: echo lint\n")
    runner = '#!/bin/sh\n# lefthook\nexec lefthook run "pre-commit" "$@"\n'
    for hook in ("pre-commit", "pre-push"):
        path = repo / ".git" / "hooks" / hook
        path.write_text(runner.replace("pre-commit", hook))
        path.chmod(0o755)
    (repo / "lefthook-local.yml").write_text("pre-commit:\n  commands:\n    mine:\n      run: echo mine\n")
    plan = _install(repo, crew_env)
    assert plan.methods == {"pre-commit": "lefthook", "prepare-commit-msg": "plain", "pre-push": "lefthook"}

    import yaml

    local = yaml.safe_load((repo / "lefthook-local.yml").read_text())
    assert local["pre-commit"]["commands"]["mine"] == {"run": "echo mine"}  # kept
    crew = local["pre-commit"]["commands"][githooks.LEFTHOOK_COMMAND]
    assert CREW_HOOK_MARKER in crew["run"] and " precommit {0} " in crew["run"]
    assert local["pre-push"]["commands"][githooks.LEFTHOOK_COMMAND]["use_stdin"] is True
    assert githooks.status(repo) == {"pre-commit": "chained", "prepare-commit-msg": "ok", "pre-push": "chained"}
    # the gate's tamper protection recognises the crew lines in lefthook config (D28)
    assert gatecore.marker_entries((repo / "lefthook-local.yml").read_text())

    # The command lefthook would run really runs the gate and fails closed on a held file.
    run = crew["run"].replace("{0}", "")
    (repo / "held").mkdir()
    (repo / "held" / "x.ts").write_text("x")
    git(repo, "add", "held/x.ts")
    proc = __import__("subprocess").run(["sh", "-c", run], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 1

    githooks.apply_plan(githooks.plan_uninstall(repo))
    after = yaml.safe_load((repo / "lefthook-local.yml").read_text())
    assert after == {"pre-commit": {"commands": {"mine": {"run": "echo mine"}}}}


def test_lefthook_local_file_created_and_removed_when_only_ours(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "lefthook.yml").write_text("pre-commit:\n  commands:\n    lint:\n      run: echo lint\n")
    git(repo, "add", "lefthook.yml")
    git(repo, "commit", "-qm", "lefthook")
    path = repo / ".git" / "hooks" / "pre-commit"
    path.write_text('#!/bin/sh\nlefthook run pre-commit "$@"\n')
    path.chmod(0o755)
    _install(repo, crew_env, hooks=("pre-commit",))
    assert (repo / "lefthook-local.yml").is_file()
    assert git(repo, "status", "--porcelain").stdout == ""  # excluded via .git/info/exclude
    githooks.apply_plan(githooks.plan_uninstall(repo))
    assert not (repo / "lefthook-local.yml").exists()
    assert githooks.EXCLUDE_BEGIN not in (repo / ".git" / "info" / "exclude").read_text()


# ---------------------------------------------------------------------------
# never touch what is not ours
# ---------------------------------------------------------------------------


def test_global_hooks_path_is_never_written(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    global_hooks = tmp_path / "global-hooks"
    global_hooks.mkdir()
    (global_hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    with crew_env["gitconfig"].open("a") as fh:
        fh.write(f"[core]\n\thooksPath = {global_hooks}\n")
    plan = githooks.plan_install(repo, gate_command(crew_env))
    assert set(plan.methods.values()) == {"manual"}
    assert all(not str(op.path).startswith(str(global_hooks)) for op in plan.ops)
    assert any("outside this repository" in n for n in plan.notes)
    githooks.apply_plan(plan)
    assert sorted(p.name for p in global_hooks.iterdir()) == ["pre-commit"]
    assert set(githooks.status(repo).values()) == {"missing"}


def test_tracked_hook_path_is_never_modified(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    hooks = repo / "githooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (hooks / "pre-commit").chmod(0o755)
    git(repo, "add", "githooks")
    git(repo, "commit", "-qm", "hooks")
    git(repo, "config", "core.hooksPath", "githooks")
    plan = githooks.plan_install(repo, gate_command(crew_env))
    assert plan.methods["pre-commit"] == "manual"
    assert plan.methods["pre-push"] == "plain"
    githooks.apply_plan(plan)
    assert (hooks / "pre-commit").read_text() == "#!/bin/sh\nexit 0\n"
    assert git(repo, "status", "--porcelain").stdout == ""  # the new pre-push file is excluded


def test_uninstall_restores_everything(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    user = repo / ".git" / "hooks" / "pre-commit"
    user.write_text("#!/bin/sh\nexit 0\n")
    user.chmod(0o750)
    exclude_before = (repo / ".git" / "info" / "exclude").read_text()
    _install(repo, crew_env)
    githooks.apply_plan(githooks.plan_uninstall(repo))
    assert user.read_text() == "#!/bin/sh\nexit 0\n"
    assert user.stat().st_mode & 0o777 == 0o750
    hooks = sorted(p.name for p in (repo / ".git" / "hooks").iterdir() if not p.name.endswith(".sample"))
    assert hooks == ["pre-commit"]
    assert (repo / ".git" / "info" / "exclude").read_text() == exclude_before
    assert githooks.status(repo)["pre-push"] == "missing"


def test_worktree_shares_the_hooks_of_the_main_checkout(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    plan = _install(wt, crew_env)
    assert plan.repo.hooks_dir == (repo / ".git" / "hooks").resolve()
    assert githooks.status(repo) == githooks.status(wt) == dict.fromkeys(githooks.HOOKS, "ok")
    assert _commit(wt, "held/x.ts").returncode != 0


def test_not_a_repo_is_unknown(crew_env, tmp_path):
    (tmp_path / "plain").mkdir()
    assert githooks.status(tmp_path / "plain") == dict.fromkeys(githooks.HOOKS, "unknown")
    assert githooks.overall(githooks.status(tmp_path / "plain")) == "unknown"
    assert githooks.overall({"a": "ok", "b": "chained"}) == "chained"
    assert githooks.overall({"a": "ok", "b": "missing"}) == "missing"
    with pytest.raises(githooks.GitHookError):
        githooks.plan_install(tmp_path / "plain", gate_command(crew_env))


def test_non_executable_crew_hook_counts_as_missing(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    _install(repo, crew_env)
    (repo / ".git" / "hooks" / "pre-commit").chmod(0o644)
    assert githooks.status(repo)["pre-commit"] == "missing"
    assert githooks.ensure(repo, gate_command(crew_env), consented=True)["pre-commit"] == "ok"


def test_a_replaced_crew_hook_is_rechained_without_losing_the_first_kept_hook(crew_env, tmp_path):
    repo = make_repo(tmp_path / "repo")
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\necho first\n")
    (hooks / "pre-commit").chmod(0o755)
    _install(repo, crew_env, hooks=("pre-commit",))
    # Another tool overwrites our hook with its own.
    (hooks / "pre-commit").write_text("#!/bin/sh\necho second\n")
    assert githooks.status(repo)["pre-commit"] == "missing"
    assert githooks.ensure(repo, gate_command(crew_env), consented=True)["pre-commit"] == "chained"
    assert (hooks / f"pre-commit{githooks.PREV_SUFFIX}").read_text() == "#!/bin/sh\necho second\n"
    backups = list(hooks.glob(f"pre-commit{githooks.PREV_SUFFIX}.bak-crew-*"))
    assert len(backups) == 1 and backups[0].read_text() == "#!/bin/sh\necho first\n"
