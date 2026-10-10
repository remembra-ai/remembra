"""Integration of WP-9 (gate, crewd, CLI) with WP-10 (hook entries, git gates, ``connect``/``verify``).

Everything runs in a temp HOME with temp git repos; nothing touches the real account.
"""

from __future__ import annotations

import io
import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

from remembra.relay.adapters import crew_hooks
from remembra.relay.crew import cli, crewd, gate, githooks, install
from tests.crew.wp10_support import make_crew_env, make_repo, tree_snapshot


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    return make_crew_env(tmp_path, monkeypatch)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def _tty(answer: str = ""):
    master, slave = pty.openpty()
    if answer:
        os.write(master, answer.encode())
    return os.fdopen(slave, "r"), master


def _connect(env, *extra: str, apply: bool = True) -> tuple[int, str]:
    argv = [
        "--no-service",
        "--python",
        sys.executable,
        "--crew-command",
        "/opt/remembra/bin/remembra-crew",
        "--crewd-command",
        "/opt/remembra/bin/remembra-crewd",
        *extra,
    ]
    out = io.StringIO()
    if not apply:
        code = install.main(argv, home=env["home"], stdin=io.StringIO(""), out=out, runner=Recorder(), platform="darwin")
        return code, out.getvalue()
    stdin, master = _tty()
    try:
        code = install.main(
            [*argv, "--apply", "--yes"], home=env["home"], stdin=stdin, out=out, runner=Recorder(), platform="darwin"
        )
    finally:
        stdin.close()
        os.close(master)
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# Verbs the installer wires are verbs the runtime serves
# ---------------------------------------------------------------------------


def test_every_installed_verb_is_served_by_the_runtime():
    for verb in crew_hooks.GATE_VERBS:
        assert verb in gate.HOOK_COMMANDS, verb
    assert gate.HOOK_COMMANDS["rewake"] is gate.cmd_wake
    for verb in crew_hooks.CLI_VERBS:
        assert verb in cli.HOOK_COMMANDS, verb
    assert set(githooks.GATE_VERBS.values()) == {"precommit", "trailer", "prepush"}


def test_rewake_verb_runs_the_wake_waiter(tmp_path, monkeypatch):
    """``crew-gate.py rewake`` (the asyncRewake hook ``connect`` installs) reaches ``cmd_wake``."""
    layout = gate.Layout(tmp_path / "home")
    layout.ensure()
    key = gate.session_key("claude-code", "sess-rw")
    gate.write_json(layout.session_file(key), {"key": key, "inject": None})
    seen: list[str] = []
    monkeypatch.setitem(gate.HOOK_COMMANDS, "rewake", lambda ctx: seen.append(ctx.adapter) or 0)
    monkeypatch.setattr(gate, "read_stdin_ex", lambda *a, **k: (json.dumps({"session_id": "sess-rw"}), False))
    assert gate.main(["rewake", "--hook", "claude-code"], layout=layout) == 0
    assert seen == ["claude-code"]


# ---------------------------------------------------------------------------
# connect installs the real packaged gate (launcher + bin/lib + crewd.json)
# ---------------------------------------------------------------------------


def test_connect_installs_the_packaged_gate_that_verifies_and_runs(crew_env):
    home: Path = crew_env["home"]
    layout = gate.Layout(home)
    (home / ".claude").mkdir()  # Claude Code installed: connect writes an agent's hooks only when it is detected

    code, out = _connect(crew_env, apply=False)
    assert code == 0
    assert "package source files" in out and "add: " + str(layout.lib / "remembra/crew/gatecore.py") in out
    assert "def evaluate" not in out  # package sources are listed, not diffed
    assert not layout.gate_script.exists()

    code, out = _connect(crew_env)
    assert code == 0, out
    assert layout.gate_script.read_text(encoding="utf-8") == gate.vendored_source()
    check = gate.verify_gate(layout)
    assert check.installed and check.ok, check
    assert json.loads(layout.crewd_cmd.read_text()) == {
        "argv": ["/opt/remembra/bin/remembra-crewd"],
        "python": sys.executable,
    }
    assert (layout.lib.stat().st_mode & 0o777) == 0o700
    assert (layout.lib / "remembra/crew/gatecore.py").stat().st_mode & 0o777 == 0o600

    # The installed launcher runs isolated (-I) from bin/lib alone.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-I", str(layout.gate_script), "version"], capture_output=True, text=True, env=env, timeout=30
    )
    assert proc.returncode == 0 and proc.stdout.strip() == f"crew-gate v{gate.GATE_VERSION}"
    # The installed Claude Code rewake hook: not a crew session, so the fast exit.
    proc = subprocess.run(
        [sys.executable, "-I", str(layout.gate_script), "rewake", "--hook", "claude-code"],
        input=json.dumps({"session_id": "never-joined"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0 and proc.stdout == ""

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    commands = [h["command"] for groups in settings["hooks"].values() for g in groups for h in g["hooks"]]
    assert any(f"{layout.gate_script} rewake --hook claude-code" in c for c in commands)

    # Idempotent: a second run has nothing to write.
    code, out = _connect(crew_env, apply=False)
    assert code == 0 and "Already up to date" in out

    # A stale file under bin/lib is removed on the next connect (the sha covers exactly what runs).
    stale = layout.lib / "remembra" / "stale.py"
    stale.write_text("x = 1\n")
    assert not gate.verify_gate(layout).ok
    assert _connect(crew_env)[0] == 0
    assert not stale.exists() and gate.verify_gate(layout).ok


def test_uninstall_removes_the_gate_bundle(crew_env):
    home: Path = crew_env["home"]
    layout = gate.Layout(home)
    assert _connect(crew_env)[0] == 0
    code, out = _connect(crew_env, "--uninstall")
    assert code == 0, out
    assert not layout.gate_script.exists()
    assert not layout.crewd_cmd.exists()
    assert not list(layout.lib.rglob("*.py"))


# ---------------------------------------------------------------------------
# remembra-crew connect / verify
# ---------------------------------------------------------------------------


def test_cli_dispatches_connect_and_verify(crew_env, monkeypatch, capsys, tmp_path):
    before = tree_snapshot(tmp_path)
    code = cli.main(["connect", "--no-service", "--crewd-command", "/opt/remembra/bin/remembra-crewd"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing was written" in out
    assert tree_snapshot(tmp_path) == before

    assert cli.main(["verify", "--agent", "nope"]) == 2
    assert "unknown agent" in capsys.readouterr().err

    assert cli.main(["connect", "--bogus-flag"]) == 2
    help_text = cli.build_parser().format_help()
    assert "connect" in help_text and "verify" in help_text


# ---------------------------------------------------------------------------
# crewd repairs a wiped git hook only where the owner consented (§8.4)
# ---------------------------------------------------------------------------


def test_crewd_repairs_wiped_git_hooks_only_with_consent(crew_env, tmp_path):
    home: Path = crew_env["home"]
    consented = make_repo(tmp_path / "consented")
    other = make_repo(tmp_path / "other")
    code, out = _connect(crew_env, "--git-hooks", "--repo", str(consented))
    assert code == 0, out

    top = str(consented.resolve())
    assert crewd.githook_state(top)[0] in ("ok", "chained")
    hook = Path(
        subprocess.run(
            ["git", "-C", top, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-commit"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    hook.unlink()
    state, missing = crewd.githook_state(top)
    assert state == "missing" and missing == ["pre-commit"]

    state, missing = crewd.githook_state_repaired(home, top)
    assert state in ("ok", "chained") and missing == []
    assert hook.exists() and os.access(hook, os.X_OK)
    assert str(gate.Layout(home).gate_script) in hook.read_text()

    # No consent recorded for this repo: nothing is written, the hooks stay missing.
    other_top = str(other.resolve())
    hooks_before = sorted(p.name for p in (other / ".git" / "hooks").iterdir())
    state, missing = crewd.githook_state_repaired(home, other_top)
    assert state == "missing" and set(missing) == {"pre-commit", "prepare-commit-msg", "pre-push"}
    assert sorted(p.name for p in (other / ".git" / "hooks").iterdir()) == hooks_before


def test_crewd_repair_failure_is_not_fatal(crew_env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")

    def boom(home, repo):
        raise OSError("disk full")

    monkeypatch.setattr(install, "ensure_git_hooks", boom)
    state, missing = crewd.githook_state_repaired(crew_env["home"], str(repo.resolve()))
    assert state == "missing" and len(missing) == 3
