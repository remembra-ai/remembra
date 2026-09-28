"""gatecore hardening (review wave 1): read-only forms that run code or write, git hook bypasses,
settings switch-off, whole-checkout git operations and the lexer's linear time.

Every bypass form here was first confirmed against real git (the ``real_git`` tests below run it in
a scratch repository with a failing pre-commit hook) and is then shown to be denied by the gate.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from remembra.crew import gatecore as G
from remembra.relay.crew.gate import PRETOOL_DEADLINE_S
from tests.crew.gatecore_support import HOME, MemFs, run, snapshot

MARK = "# remembra-crew"
GATE_CMD = f"python3 ~/.remembra/crew/bin/crew-gate.py {MARK}"
GATE_ENTRY = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": GATE_CMD}]}]}}
USER_SETTINGS = f"{HOME}/.claude/settings.json"
HOOKS_OFF_EDIT = {"file_path": USER_SETTINGS, "old_string": '{"hooks"', "new_string": '{"disableAllHooks": true, "hooks"'}
PY_HOOKS_OFF = "python3 -c \"import json; json.dump({'disableAllHooks': 1}, open('.claude/settings.json', 'w'))\""


def _p(cmd: str) -> dict[str, Any]:
    return G.parse_bash(cmd)


# ---------------------------------------------------------------------------
# 1. The read-only fast path no longer admits commands that run code or write files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "kinds"),
    [
        ("git -c core.fsmonitor='pkill crewd' status", ["crewd_kill"]),
        ("git -c diff.external='pkill crewd' diff", ["crewd_kill"]),
        ("git -c core.pager='pkill crewd' log", ["crewd_kill"]),
        ("git grep -O'pkill crewd' x", ["crewd_kill"]),
        ("git fetch --upload-pack='pkill crewd' origin", ["crewd_kill"]),
        ("sed '1e pkill crewd' notes.txt", ["crewd_kill"]),
        ("sed 's/x/pkill crewd/e' notes.txt", ["crewd_kill"]),
        ("sort --compress-program='pkill crewd' big.txt", ["crewd_kill"]),
        ("rg --pre 'pkill crewd' TODO src", ["crewd_kill"]),
        ("GIT_EXTERNAL_DIFF='pkill crewd' git diff", ["crewd_kill"]),
        ("PAGER='pkill crewd' git log", ["crewd_kill"]),
    ],
)
def test_read_only_commands_that_run_code_are_opaque_and_scanned(cmd: str, kinds: list[str]) -> None:
    parsed = _p(cmd)
    assert not parsed["read_only"] and parsed["opaque"] and parsed["tamper"] == kinds
    v = run("Bash", {"command": cmd})
    assert (v.rule, v.decision, v.variant) == (2, "deny", "tamper"), cmd


@pytest.mark.parametrize(
    ("cmd", "target"),
    [
        ("git diff --output=.git/hooks/pre-commit", ".git/hooks/pre-commit"),
        ("git diff --outp .git/hooks/pre-commit", ".git/hooks/pre-commit"),
        ("git log --output=~/.remembra/crew/snapshot.json", "~/.remembra/crew/snapshot.json"),
        ("git show --output .remembra/zones.yml HEAD", ".remembra/zones.yml"),
        ("git format-patch -o .git/hooks HEAD~1", ".git/hooks"),
        ("sed -n 'w .git/hooks/pre-commit' /tmp/evil", ".git/hooks/pre-commit"),
        ("sed -n '/x/W .git/hooks/pre-push' /tmp/evil", ".git/hooks/pre-push"),
        ("sed 's/a/b/w .git/hooks/pre-commit' /tmp/evil", ".git/hooks/pre-commit"),
        ("sed -e p --expression='1w .remembra/zones.yml' /tmp/evil", ".remembra/zones.yml"),
        ("uniq /tmp/evil .git/hooks/pre-commit", ".git/hooks/pre-commit"),
        ("sort -uo .git/hooks/pre-commit /tmp/evil", ".git/hooks/pre-commit"),
        ("tree -o .git/hooks/pre-commit /tmp", ".git/hooks/pre-commit"),
        ("less -o .git/hooks/pre-commit /tmp/evil", ".git/hooks/pre-commit"),
    ],
)
def test_read_only_commands_with_output_files_are_writers(cmd: str, target: str) -> None:
    parsed = _p(cmd)
    assert not parsed["read_only"] and target in parsed["writes"], parsed
    v = run("Bash", {"command": cmd})
    assert (v.rule, v.decision, v.variant) == (2, "deny", "crew_policy"), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git log --grep=--no-verify",
        "git diff --stat HEAD~1",
        "git grep -n TODO",
        "sed -n '1,20p' src/app/pos/cart.ts",
        "sed -n 's/we/us/p' notes.txt",  # a 'w' inside the pattern is not a w command
        "sed -e '/start/,/end/{s/a/b/g;p}' -n f.txt",
        "sed 'y/abc/xyz/' f.txt",
        "sed --sandbox -n '1p' x.txt",
        "uniq -c names.txt",
        "sort -k2 -t, names.txt",
        "tree -L 2 src",
        "rg --pre-glob '*.gz' TODO",
        "FOO=1 npm test",
        "grep -rn disableAllHooks docs",
    ],
)
def test_plain_reads_stay_on_the_fast_path(cmd: str) -> None:
    assert _p(cmd)["read_only"], cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git -c color.ui=always log --oneline",
        "GIT_PAGER=cat git log",
        "export GIT_PAGER=cat && git log",
        "LESSOPEN='|./x.sh %s' less notes.txt",
        "sed -f script.sed notes.txt",
        "fd -e ts -x wc -l",
        "git grep --open-files-in-pager=vim TODO",
    ],
)
def test_config_env_and_exec_options_are_never_read_only(cmd: str) -> None:
    parsed = _p(cmd)
    assert not parsed["read_only"] and parsed["opaque"], cmd
    assert run("Bash", {"command": cmd}).variant != "read_only"


def test_paused_session_cannot_run_code_through_a_read_only_git_command() -> None:
    v = run("Bash", {"command": "git -c core.fsmonitor='touch src/app/pos/cart.ts' status"}, caller="cs_p")
    assert (v.rule, v.decision) == (1, "deny")
    assert run("Bash", {"command": "git status"}, caller="cs_p").decision == "allow"  # plain reads still work


# ---------------------------------------------------------------------------
# 2. Git hook bypasses are tamper (§5.2 row 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "kind"),
    [
        ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x", "hooks_path"),
        ("GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -m x", "hooks_path"),
        ("env GIT_CONFIG_GLOBAL=/tmp/evil.cfg git commit -m x", "hooks_path"),
        ("export GIT_CONFIG_SYSTEM=/tmp/evil.cfg", "hooks_path"),
        ("git commit --no-verif -m x", "no_verify"),
        ("git commit --no-veri -m x", "no_verify"),
        ("git commit --no-v -m x", "no_verify"),
        ("git push --no-verif", "no_verify"),
        ("git merge --no-verif feature", "no_verify"),
        ("git ci --no-verif -m x", "no_verify"),  # an alias of commit
        ("git -c alias.ci='commit --no-verify' ci -m x", "no_verify"),
        ("git -c alias.ci='!git commit -n' ci -m x", "no_verify"),
        ("git --config-env=alias.ci=EVIL ci -m x", "no_verify"),
        ("git config alias.ci 'commit --no-verify'", "no_verify"),
        ("git config --global alias.ci commit", "no_verify"),
        ("git config alias.sh '!sh -c true'", "no_verify"),
        ("git commit-tree HEAD^{tree} -m x", "no_verify"),
        ("git config include.path ../evil.cfg", "hooks_path"),
        ("git config --local includeIf.gitdir:/w/.path /tmp/evil.cfg", "hooks_path"),
        ("git -c include.path=/tmp/e commit -m x", "hooks_path"),
        ("git config core.fsmonitor ./watch.sh", "hooks_path"),
        ("git config diff.external '/tmp/x.sh'", "hooks_path"),
        ("git config core.pager 'sh -c \"rm -rf .remembra\"'", "hooks_path"),
    ],
)
def test_git_hook_bypasses_are_tamper(cmd: str, kind: str) -> None:
    assert kind in _p(cmd)["tamper"], (cmd, _p(cmd))
    v = run("Bash", {"command": cmd})
    assert (v.rule, v.decision) == (2, "deny"), cmd
    assert kind in v.tamper_kinds


@pytest.mark.parametrize(
    "cmd",
    [
        "git config alias.lg 'log --oneline --graph'",
        "git config core.pager cat",
        "git config core.editor 'code --wait'",
        "git config user.email agent@example.com",
        "git config --unset alias.ci",
        "git commit -m 'mention --no-verify in the message'",
        "git log --grep=--no-verify",
    ],
)
def test_ordinary_git_config_and_commits_are_not_tamper(cmd: str) -> None:
    assert _p(cmd)["tamper"] == [], cmd


@pytest.mark.parametrize(
    "path",
    [".git/config", "/w/yaadbooks/.git/config", "/w/yaadbooks/.git/config.worktree", "~/.gitconfig", "~/.config/git/config"],
)
def test_direct_writes_to_git_config_files_are_crew_policy(path: str) -> None:
    v = run("Bash", {"command": f"printf '[core]\\n\\thooksPath=/dev/null\\n' >> {path}"})
    assert (v.rule, v.decision, v.variant) == (2, "deny", "crew_policy"), path
    v = run("Edit", {"file_path": path.replace("~", HOME), "old_string": "[core]", "new_string": "[core]\n\thooksPath=/dev/null"})
    assert (v.rule, v.decision) == (2, "deny"), path


# ---------------------------------------------------------------------------
# 3. Claude settings: removal of the directory and hooks switched off
# ---------------------------------------------------------------------------


def _settings_fs() -> MemFs:
    return MemFs(
        {f"{HOME}/.claude/settings.json": json.dumps(GATE_ENTRY), "/w/yaadbooks-c/.claude/settings.local.json": "{}"},
        dirs=[f"{HOME}/.claude", "/w/yaadbooks-c/.claude"],
    )


@pytest.mark.parametrize(
    "cmd", ["rm -rf ~/.claude", "rm -rf $HOME/.claude", "mv ~/.claude ~/.claude.bak", f"rm -r {HOME}/.claude", "rm -rf ~"]
)
def test_removing_a_directory_above_crew_hook_entries_is_tamper(cmd: str) -> None:
    v = run("Bash", {"command": cmd}, fs=_settings_fs())
    assert (v.rule, v.decision) == (2, "deny"), cmd
    assert "settings_hook_edit" in v.tamper_kinds or v.variant == "crew_policy"


def test_removing_a_settings_directory_without_crew_entries_is_allowed() -> None:
    fs = _settings_fs()
    assert run("Bash", {"command": "rm -rf .claude"}, fs=fs).decision == "allow"
    assert run("Bash", {"command": "echo '{}' > .claude/settings.local.json"}, fs=fs).decision == "allow"


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Write", {"file_path": f"{HOME}/.claude/settings.json", "content": json.dumps({**GATE_ENTRY, "disableAllHooks": True})}),
        ("Edit", HOOKS_OFF_EDIT),
        ("Write", {"file_path": "/w/yaadbooks-c/.claude/settings.local.json", "content": '{"disableAllHooks": true}'}),
        ("Write", {"file_path": "/w/yaadbooks-c/.claude/settings.json", "content": '{"allowManagedHooksOnly": true}'}),
        ("Bash", {"command": "echo '{\"disableAllHooks\": true}' > .claude/settings.local.json"}),
        ("Bash", {"command": PY_HOOKS_OFF}),
    ],
)
def test_switching_every_hook_off_is_tamper(tool: str, tool_input: dict[str, Any]) -> None:
    v = run(tool, tool_input, fs=_settings_fs())
    assert (v.rule, v.decision) == (2, "deny"), (tool, tool_input)
    assert "settings_hook_edit" in v.tamper_kinds


def test_other_settings_edits_stay_allowed() -> None:
    fs = _settings_fs()
    edit = {"file_path": f"{HOME}/.claude/settings.json", "old_string": '{"hooks"', "new_string": '{"model": "opus", "hooks"'}
    assert run("Edit", edit, fs=fs).decision == "allow"
    ok = {"file_path": "/w/yaadbooks-c/.claude/settings.local.json", "content": '{"disableAllHooks": false}'}
    assert run("Write", ok, fs=fs).decision == "allow"


def test_settings_protection_on_the_real_filesystem(tmp_path: Path) -> None:
    """OsFs against a temporary home that holds a crew hook entry (never the user's real ~/.claude)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(GATE_ENTRY))
    (home / ".remembra").mkdir()

    def ev(cmd: str) -> G.Verdict:
        return G.evaluate(
            "Bash",
            {"command": cmd},
            snapshot=snapshot(),
            caller="cs_c",
            cwd="/w/yaadbooks-c",
            home=str(home),
            now="2026-09-25T20:01:00Z",
            mode="enforce",
            fs=G.OsFs(),
        )

    denied = (f"rm -rf {home}/.claude", f"mv {home}/.claude /tmp/x", f"rm {home}/.claude/settings.json", "rm -rf $HOME/.remembra")
    for cmd in denied:
        v = ev(cmd)
        assert (v.rule, v.decision) == (2, "deny"), cmd
    assert ev(f"rm -rf {home}/.cache").decision == "allow"


def test_dollar_home_targets_resolve_like_tilde() -> None:
    assert _p("rm -rf $HOME/.remembra") == _p("rm -rf ~/.remembra") | {"argvs": _p("rm -rf $HOME/.remembra")["argvs"]}
    assert _p('rm -rf "${HOME}/.remembra"')["tamper"] == ["crew_files_removed"]
    assert _p("rm -rf $HOMEDIR/.cache")["opaque"]  # not $HOME: still unknown
    v = run("Bash", {"command": "rm -rf $X/.remembra"})
    assert (v.rule, v.decision) == (2, "deny")  # a variable target is raw-scanned


# ---------------------------------------------------------------------------
# 4. Whole-checkout git operations: worktree remove, pathspec magic, checkout -f
# ---------------------------------------------------------------------------


def test_worktree_remove_of_another_sessions_checkout_is_denied() -> None:
    for cmd in (
        "git worktree remove --force /w/yaadbooks-a",
        "git worktree remove ../yaadbooks-a",
        "git worktree move ../yaadbooks-a /tmp/a",
    ):
        v = run("Bash", {"command": cmd})
        assert v.decision == "deny" and v.rule in (2, 4), (cmd, v.rule)
    # a worktree no session uses is the caller's business
    assert run("Bash", {"command": "git worktree remove --force /w/old-spike"}).decision == "allow"


@pytest.mark.parametrize(
    "cmd",
    [
        "git checkout -- :/",
        "git checkout -- ':(top)'",
        "git restore --source=HEAD~3 :/",
        "git restore ':(top,glob)src/**'",
        "git checkout HEAD -- ':(glob)src/**'",
        "git checkout -- 'src/app/pos/*'",
        "git checkout -- ':!README.md'",
        "git checkout --pathspec-from-file=paths.txt",
    ],
)
def test_pathspec_magic_is_resolved_not_taken_literally(cmd: str) -> None:
    v = run("Bash", {"command": cmd})
    assert v.decision == "deny" and v.rule in (3, 5, 9, 13, 15), (cmd, v.rule, v.variant)


def test_top_pathspec_resolves_from_a_subdirectory() -> None:
    top = _p("cd src/app && git checkout -- :/")
    assert top["top_writes"] == ["."] and top["writes"] == []
    sub = run("Bash", {"command": "git checkout -- ."}, cwd="/w/yaadbooks-c/src/app/reports")
    whole = run("Bash", {"command": "git checkout -- :/"}, cwd="/w/yaadbooks-c/src/app/reports")
    assert whole.decision == "deny"
    assert whole.target is not None and whole.target.abs_path == "/w/yaadbooks-c"
    assert sub.target is None or sub.target.abs_path != "/w/yaadbooks-c"


@pytest.mark.parametrize(
    ("cmd", "op"),
    [
        ("git checkout -f", "reset_hard"),
        ("git checkout --force", "reset_hard"),
        ("git checkout --forc", "reset_hard"),
        ("git reset --ha", "reset_hard"),
        ("git reset --merge", "reset_hard"),
        ("git reset --keep HEAD~1", "reset_hard"),
        ("git read-tree -u -m HEAD", "reset_hard"),
        ("git checkout-index -a -f", "reset_hard"),
        ("git clean --forc -d", "clean"),
    ],
)
def test_whole_tree_discards_are_tree_wide_git_ops(cmd: str, op: str) -> None:
    assert _p(cmd)["git_tree_op"] == op
    v = run("Bash", {"command": cmd})
    assert (v.rule, v.decision) == (15, "deny"), cmd  # cs_e is live in the same checkout


# ---------------------------------------------------------------------------
# 5. Lexer in linear time (§10.3: a gate past its deadline allows the call)
# ---------------------------------------------------------------------------


def _ms(cmd: str) -> float:
    t = time.perf_counter()
    run("Bash", {"command": cmd})
    return (time.perf_counter() - t) * 1000


# §10.3: a PreToolUse gate past its deadline allows the call, so no command may take it there, on any machine.
DEADLINE_MS = PRETOOL_DEADLINE_S * 1000
# The regression budget is relative to this run's own speed (CPU, interpreter, coverage tracing), not
# wall-clock: a ~100 kB command may cost at most this many ordinary gate calls measured in the same run.
# Measured 60-130 on an M-series Mac and in python:3.11/3.12 Linux containers, with and without
# coverage; the lexer made quadratic (n^2/16 extra character scans) measured ~500. A fixed 250 ms
# (5x under the deadline on a laptop) failed on the CI runners at ~295 ms (Python 3.11 under
# coverage on a slower CPU) and let that quadratic lexer through (183 ms in a Linux container).
LONG_COMMAND_BUDGET = 300
ORDINARY = (
    "sed -i '' 's/a/b/' src/app/reports/x.ts && git add -A",
    "npm test -- --watch=false src/app/reports",
    "echo " + "[a" * 200 + " && rm -rf src/app/pos",
)


def _ordinary_ms() -> float:
    """This run's cost of one ordinary gate call (median of 21): the unit the budget is counted in."""
    return statistics.median(_ms(cmd) for _ in range(7) for cmd in ORDINARY)


def test_long_commands_stay_inside_the_deadline() -> None:
    evil = "echo " + "[a" * 8000 + " >/dev/null && rm -rf src/app/pos"
    v = run("Bash", {"command": evil})
    assert (v.rule, v.decision) == (9, "deny")  # the write after the long word is still judged
    unit = _ordinary_ms()
    for cmd in ("echo " + "[a" * 50_000, "echo " + "x" * 100_000, 'echo "' + "y" * 100_000 + '"', "echo " + "a.b," * 25_000):
        samples = sorted(_ms(cmd) for _ in range(5))
        assert samples[-1] < DEADLINE_MS, (cmd[:20], samples)  # every call, not a typical one
        assert statistics.median(samples) < LONG_COMMAND_BUDGET * unit, (cmd[:20], samples, unit)


def test_lexing_time_grows_linearly() -> None:
    small = statistics.median(_ms("echo " + "[a" * 20_000) for _ in range(3))
    large = statistics.median(_ms("echo " + "[a" * 80_000) for _ in range(3))
    assert large < max(small, 1.0) * 10  # 4x input: quadratic would be ~16x


# ---------------------------------------------------------------------------
# 6. Real git: each bypass form really skips a failing hook, and the gate denies it
# ---------------------------------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", XDG_CONFIG_HOME=str(home / ".config"))
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return env


def _scratch_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    env = _git_env(home)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env, timeout=30)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho blocked by the crew gate >&2\nexit 1\n")
    hook.chmod(0o755)
    (tmp_path / "evil.cfg").write_text("[core]\n\thooksPath = /dev/null\n")
    return repo, env


def _commits(repo: Path, env: dict[str, str]) -> int:
    out = subprocess.run(["git", "rev-list", "--all", "--count"], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    return int(out.stdout.strip() or 0) if out.returncode == 0 else 0


@needs_git
@pytest.mark.parametrize(
    "cmd",
    [
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -q --allow-empty -m x",
        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -q --allow-empty -m x",
        "git commit --no-veri -q --allow-empty -m x",
        "git commit --no-verif -q --allow-empty -m x",
        "git -c alias.ci='commit --no-verify' ci -q --allow-empty -m x",
        "git -c include.path={cfg} commit -q --allow-empty -m x",
        "git config include.path {cfg} && git commit -q --allow-empty -m x",
        "git config alias.ci commit && git ci -n -q --allow-empty -m x",
    ],
)
def test_real_git_bypass_forms_are_denied(tmp_path: Path, cmd: str) -> None:
    repo, env = _scratch_repo(tmp_path)
    cmd = cmd.format(cfg=tmp_path / "evil.cfg")
    control = subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, env=env, capture_output=True, text=True, timeout=30
    )
    assert control.returncode != 0 and "blocked by the crew gate" in control.stderr  # the hook is live
    subprocess.run(["bash", "-c", cmd], cwd=repo, env=env, capture_output=True, text=True, timeout=30, check=True)
    assert _commits(repo, env) == 1  # real git skipped the hook
    v = run("Bash", {"command": cmd})
    assert (v.rule, v.decision) == (2, "deny"), (cmd, v.as_dict())


@needs_git
def test_real_git_runs_fsmonitor_and_diff_output_so_they_are_not_read_only(tmp_path: Path) -> None:
    repo, env = _scratch_repo(tmp_path)
    fsmonitor = ["git", "-c", "core.fsmonitor=touch fsmonitor-ran; false", "status"]
    subprocess.run(fsmonitor, cwd=repo, env=env, capture_output=True, timeout=30)
    assert (repo / "fsmonitor-ran").exists()  # git ran the -c value as a command during `status`
    assert not _p("git -c core.fsmonitor='touch fsmonitor-ran; false' status")["read_only"]
    (repo / "a.txt").write_text("1\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, env=env, check=True, timeout=30)
    (repo / "a.txt").write_text("2\n")
    subprocess.run(["git", "diff", "--output=written.patch"], cwd=repo, env=env, check=True, timeout=30)
    assert (repo / "written.patch").read_text().startswith("diff --git")
    assert _p("git diff --output=written.patch")["writes"] == ["written.patch"]
