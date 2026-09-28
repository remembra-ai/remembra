"""WP-10: ``remembra-crew connect`` (dry-run diff, TTY consent, writes, service units, uninstall), temp HOME only."""

from __future__ import annotations

import io
import json
import os
import plistlib
import pty
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from remembra.relay.crew import githooks, install, verify
from tests.crew.wp10_support import STUB_GATE, git, make_repo, tree_snapshot, make_crew_env


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    return make_crew_env(tmp_path, monkeypatch)


SRC = Path(__file__).resolve().parents[2] / "src"


class Recorder:
    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        code = 1 if any(f in argv for f in self.fail) else 0
        return subprocess.CompletedProcess(list(argv), code, "", "not loaded" if code else "")


def _tty(answer: str = "") -> tuple[io.TextIOWrapper, int]:
    master, slave = pty.openpty()
    if answer:
        os.write(master, answer.encode())
    return os.fdopen(slave, "r"), master


def _base_args(env: dict[str, Any], *extra: str) -> list[str]:
    return [
        "--gate-source",
        str(STUB_GATE),
        "--crew-command",
        "/opt/remembra/bin/remembra-crew",
        "--crewd-command",
        "/opt/remembra/bin/remembra-crewd",
        "--python",
        sys.executable,
        *extra,
    ]


def _seed_home(home: Path) -> Path:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/local/bin/remembra-relay brief --hook claude-code --agent claude-code",
                                }
                            ]
                        }
                    ],
                    "Notification": [{"hooks": [{"type": "command", "command": "say hi"}]}],
                },
            },
            indent=2,
        )
    )
    return settings


def _run(env, argv, *, stdin=None, runner=None, platform="darwin") -> tuple[int, str]:
    out = io.StringIO()
    code = install.main(
        argv, home=env["home"], stdin=stdin or io.StringIO(""), out=out, runner=runner or Recorder(), platform=platform
    )
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# consent
# ---------------------------------------------------------------------------


def test_dry_run_shows_every_file_and_writes_nothing(crew_env, tmp_path):
    home = crew_env["home"]
    _seed_home(home)
    repo = make_repo(tmp_path / "repo")
    before = tree_snapshot(tmp_path)
    code, out = _run(crew_env, _base_args(crew_env, "--git-hooks", "--repo", str(repo), "--agents-md", str(repo / "AGENTS.md")))
    assert code == 0
    assert tree_snapshot(tmp_path) == before
    for shown in (
        "crew-gate.py (new)",
        ".claude/settings.json (new)",
        "dev.remembra.crewd.plist (new)",
        ".git/hooks/pre-push (new)",
        "AGENTS.md (new)",
        "adapters.json (new)",
        "install.json (new)",
    ):
        assert shown in out, shown
    assert "will run: launchctl bootstrap" in out
    assert "Dry run: nothing was written" in out


@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_apply_refuses_without_a_tty_even_with_yes(crew_env, tmp_path, extra):
    _seed_home(crew_env["home"])
    before = tree_snapshot(tmp_path)
    code, out = _run(crew_env, _base_args(crew_env, "--apply", *extra), stdin=io.StringIO("yes\n"))
    assert code == 2
    assert "consent needs an interactive terminal" in out
    assert tree_snapshot(tmp_path) == before


def test_apply_at_a_tty_declined_writes_nothing(crew_env, tmp_path):
    _seed_home(crew_env["home"])
    before = tree_snapshot(tmp_path)
    stdin, master = _tty("no\n")
    try:
        code, out = _run(crew_env, _base_args(crew_env, "--apply"), stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
    assert code == 1 and "not confirmed" in out
    assert tree_snapshot(tmp_path) == before


def _assert_crewd_supervisor_written(home: Path, out: str) -> None:
    """The crewd supervisor a real run writes on this platform, not loaded (``--no-service-load``):
    a launchd LaunchAgent on macOS, a systemd ``--user`` unit on Linux (``install._plan_service``)."""
    plist = home / "Library" / "LaunchAgents" / "dev.remembra.crewd.plist"
    unit = home / ".config" / "systemd" / "user" / "remembra-crewd.service"
    if sys.platform == "darwin":
        body = plistlib.loads(plist.read_bytes())
        assert body["ProgramArguments"] == ["/opt/remembra/bin/remembra-crewd"] and body["KeepAlive"] is True
        assert plist.stat().st_mode & 0o777 == 0o644 and not unit.exists()
        assert f"load it with: launchctl bootstrap gui/{os.getuid()} {plist}" in out
    elif sys.platform.startswith("linux"):
        text = unit.read_text()
        assert "\nExecStart=/opt/remembra/bin/remembra-crewd\n" in text and "\nRestart=always\n" in text
        assert "\nWantedBy=default.target\n" in text
        assert unit.stat().st_mode & 0o777 == 0o644 and not plist.exists()
        assert "start it with: systemctl --user enable --now remembra-crewd.service" in out
    else:  # no supervisor elsewhere: connect says so and writes neither
        assert "crewd supervision is not supported" in out and not plist.exists() and not unit.exists()


def test_real_process_consent_at_a_pty_writes_everything(crew_env, tmp_path):
    """The real CLI in its own process: dry-run diff, typed 'yes' at a pseudo-terminal, then writes."""
    home = crew_env["home"]
    settings = _seed_home(home)
    repo = make_repo(tmp_path / "repo")
    args = [
        sys.executable,
        "-m",
        "remembra.relay.crew.install",
        *_base_args(
            crew_env, "--apply", "--git-hooks", "--repo", str(repo), "--agents-md", str(repo / "AGENTS.md"), "--no-service-load"
        ),
    ]
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    master, slave = pty.openpty()
    proc = subprocess.Popen(args, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=tmp_path, text=True)
    os.close(slave)
    os.write(master, b"yes\n")
    out, err = proc.communicate(timeout=60)
    os.close(master)
    assert proc.returncode == 0, out + err
    assert "Type 'yes' to confirm" in out and "confirmed at the terminal" in out

    crew = home / ".remembra" / "crew"
    gate = crew / "bin" / "crew-gate.py"
    assert gate.read_text() == STUB_GATE.read_text()
    assert gate.stat().st_mode & 0o777 == 0o600
    for d in (crew, crew / "bin", crew / "log"):
        assert d.stat().st_mode & 0o777 == 0o700
    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and data["hooks"]["Notification"]
    assert "remembra-relay" not in settings.read_text()
    assert f"{sys.executable} -I {gate} pretool --hook claude-code # remembra-crew" in settings.read_text()
    assert list(settings.parent.glob("settings.json.bak-crew-*"))  # backup kept
    _assert_crewd_supervisor_written(home, out)
    assert githooks.status(repo) == dict.fromkeys(githooks.HOOKS, "ok")
    assert "## Crew mode (Remembra)" in (repo / "AGENTS.md").read_text()
    adapters = verify.read_adapters(home)["adapters"]
    assert adapters["claude-code"]["enforcement"] == "enforce" and adapters["claude-code"]["installed"] is True
    manifest = install.read_manifest(home)
    assert str(repo.resolve()) in manifest["repos"] and manifest["gate"] == str(gate)
    assert manifest["agents"]["claude-code"] == [str(settings)]

    # The installed pre-commit hook runs the installed gate (the stub) for real.
    (repo / "held").mkdir()
    (repo / "held" / "x.ts").write_text("x")
    git(repo, "add", "held/x.ts")
    assert git(repo, "commit", "-m", "held", check=False).returncode != 0

    # Idempotent: a second run (on the platform the real process ran on) has nothing to write.
    code, out = _run(
        crew_env,
        _base_args(crew_env, "--git-hooks", "--repo", str(repo), "--agents-md", str(repo / "AGENTS.md"), "--no-service-load"),
        platform=sys.platform,
    )
    assert code == 0 and "Already up to date" in out


# ---------------------------------------------------------------------------
# service units
# ---------------------------------------------------------------------------


def test_launch_agent_is_loaded_after_consent(crew_env):
    stdin, master = _tty()
    runner = Recorder(fail=("bootout",))
    try:
        code, out = _run(crew_env, _base_args(crew_env, "--apply", "--yes"), stdin=stdin, runner=runner)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    uid = os.getuid()
    path = crew_env["home"] / "Library" / "LaunchAgents" / "dev.remembra.crewd.plist"
    assert runner.calls == [
        ("launchctl", "bootout", f"gui/{uid}/dev.remembra.crewd"),
        ("launchctl", "bootstrap", f"gui/{uid}", str(path)),
    ]
    if shutil.which("plutil"):
        assert subprocess.run(["plutil", "-lint", str(path)], capture_output=True).returncode == 0
    body = plistlib.loads(path.read_bytes())
    assert body["Label"] == "dev.remembra.crewd" and body["RunAtLoad"] is True
    assert body["StandardErrorPath"].startswith(str(crew_env["home"] / ".remembra" / "crew" / "log"))
    assert "REMEMBRA_API_KEY" not in path.read_text()  # keys are never written (read by crewd at run time)


def test_systemd_user_unit_on_linux(crew_env):
    stdin, master = _tty()
    runner = Recorder()
    try:
        code, _ = _run(
            crew_env,
            _base_args(crew_env, "--apply", "--yes", "--crewd-command", "'/opt/my tools/crewd' --flag"),
            stdin=stdin,
            runner=runner,
            platform="linux",
        )
    finally:
        stdin.close()
        os.close(master)
    assert code == 0
    unit = crew_env["home"] / ".config" / "systemd" / "user" / "remembra-crewd.service"
    text = unit.read_text()
    assert 'ExecStart="/opt/my tools/crewd" --flag' in text
    assert "Restart=always" in text and "WantedBy=default.target" in text
    assert runner.calls == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "remembra-crewd.service"),
        ("systemctl", "--user", "restart", "remembra-crewd.service"),
    ]
    assert not (crew_env["home"] / "Library").exists()


def test_failed_service_load_is_reported(crew_env):
    stdin, master = _tty()
    try:
        code, out = _run(crew_env, _base_args(crew_env, "--apply", "--yes"), stdin=stdin, runner=Recorder(fail=("bootstrap",)))
    finally:
        stdin.close()
        os.close(master)
    assert code == 1 and "a service command failed" in out


def test_unsupported_platform_skips_the_service(crew_env):
    code, out = _run(crew_env, _base_args(crew_env), platform="win32")
    assert code == 0 and "not supported on win32" in out


# ---------------------------------------------------------------------------
# scopes, adapters, errors
# ---------------------------------------------------------------------------


def test_project_scope_writes_only_the_repo(crew_env, tmp_path):
    home = crew_env["home"]
    settings = _seed_home(home)
    original = settings.read_text()
    a = make_repo(tmp_path / "a")
    b = make_repo(tmp_path / "b")
    (b / ".claude").mkdir()
    (b / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    git(b, "add", ".claude/settings.json")
    git(b, "commit", "-qm", "shared settings")
    stdin, master = _tty()
    try:
        code, out = _run(
            crew_env,
            _base_args(crew_env, "--apply", "--yes", "--no-service", "--scope", "project", "--repo", str(a), "--repo", str(b)),
            stdin=stdin,
        )
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    assert settings.read_text() == original  # global untouched
    assert "pretool --hook claude-code # remembra-crew" in (a / ".claude" / "settings.json").read_text()
    assert (b / ".claude" / "settings.json").read_text() == '{"permissions": {}}\n'  # tracked: never modified
    assert "pretool --hook claude-code" in (b / ".claude" / "settings.local.json").read_text()
    assert "tracked by git" in out


def test_unverified_adapters_need_the_flag_and_run_in_observe(crew_env):
    home = crew_env["home"]
    code, out = _run(crew_env, _base_args(crew_env, "--agent", "codex", "--no-service"))
    assert code == 0 and "unverified adapter, not written" in out
    stdin, master = _tty()
    try:
        code, out = _run(
            crew_env,
            _base_args(
                crew_env, "--apply", "--yes", "--no-service", "--agent", "codex", "--agent", "cursor", "--include-unverified"
            ),
            stdin=stdin,
        )
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    assert "PreToolUse" in hooks and hooks["PreToolUse"][0]["hooks"][0]["command"].endswith(
        "pretool --hook codex # remembra-crew"
    )
    assert "beforeShellExecution" in json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"]
    adapters = verify.read_adapters(home)["adapters"]
    assert adapters["codex"]["enforcement"] == "observe" and adapters["cursor"]["enforcement"] == "observe"
    assert verify.adapter_enforcement(home, "codex") == "advisory"
    assert "OBSERVE mode" in out
    assert not (home / ".claude").exists()  # only the named agents


def test_verified_locally_adapter_is_reinstalled_in_enforce(crew_env):
    home = crew_env["home"]
    adapters = verify.adapters_path(home)
    adapters.parent.mkdir(parents=True)
    adapters.write_text(verify.render_adapters(None, {"gemini": {"verified_local": True, "enforcement": "enforce"}}))
    code, out = _run(crew_env, _base_args(crew_env, "--no-service", "--agent", "gemini", "--include-unverified"))
    assert code == 0
    assert '"enforcement": "enforce"' in out and "OBSERVE" not in out


def test_missing_runtime_refuses_and_writes_nothing(crew_env, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "remembra.relay.crew.gate", None)  # WP-9 runtime absent
    before = tree_snapshot(tmp_path)
    out = io.StringIO()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    code = install.main(["--apply", "--no-service"], home=crew_env["home"], stdin=io.StringIO(), out=out, platform="darwin")
    assert code == 1 and "crew runtime is not installed" in err.getvalue()
    assert tree_snapshot(tmp_path) == before
    monkeypatch.setattr(install, "default_crewd_command", lambda: None)
    code = install.main(["--gate-source", str(STUB_GATE)], home=crew_env["home"], stdin=io.StringIO(), out=out, platform="darwin")
    assert code == 1 and "remembra-crewd is not installed" in err.getvalue()


def test_unknown_agent_is_an_error(crew_env, monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    code, _ = _run(crew_env, _base_args(crew_env, "--agent", "nope"))
    assert code == 1 and "unknown agent" in err.getvalue()
    code, _ = _run(crew_env, ["--scope", "project"])
    assert code == 2


# ---------------------------------------------------------------------------
# uninstall and crewd's repair hook
# ---------------------------------------------------------------------------


def test_uninstall_reverses_the_install(crew_env, tmp_path):
    home = crew_env["home"]
    settings = _seed_home(home)
    repo = make_repo(tmp_path / "repo")
    user_hook = repo / ".git" / "hooks" / "pre-push"
    user_hook.write_text("#!/bin/sh\nexit 0\n")
    user_hook.chmod(0o755)
    stdin, master = _tty()
    try:
        assert _run(crew_env, _base_args(crew_env, "--apply", "--yes", "--git-hooks", "--repo", str(repo)), stdin=stdin)[0] == 0
    finally:
        stdin.close()
        os.close(master)
    runner = Recorder()
    stdin, master = _tty("yes\n")
    try:
        code, out = _run(crew_env, ["--uninstall", "--apply"], stdin=stdin, runner=runner)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    text = settings.read_text()
    assert "# remembra-crew" not in text and json.loads(text)["hooks"]["Notification"]
    assert not (home / ".remembra" / "crew" / "bin" / "crew-gate.py").exists()
    assert not (home / "Library" / "LaunchAgents" / "dev.remembra.crewd.plist").exists()
    assert runner.calls == [("launchctl", "bootout", f"gui/{os.getuid()}/dev.remembra.crewd")]
    assert user_hook.read_text() == "#!/bin/sh\nexit 0\n"
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()
    manifest = install.read_manifest(home)
    assert manifest["agents"] == {} and manifest["repos"] == {}
    assert verify.read_adapters(home)["adapters"]["claude-code"]["installed"] is False
    code, out = _run(crew_env, ["--uninstall"])
    assert code == 0 and "Already up to date" in out


def test_ensure_git_hooks_repairs_only_consented_repos(crew_env, tmp_path):
    home = crew_env["home"]
    repo = make_repo(tmp_path / "repo")
    other = make_repo(tmp_path / "other")
    stdin, master = _tty()
    try:
        assert (
            _run(
                crew_env,
                _base_args(crew_env, "--apply", "--yes", "--no-service", "--git-hooks", "--repo", str(repo)),
                stdin=stdin,
            )[0]
            == 0
        )
    finally:
        stdin.close()
        os.close(master)
    (repo / ".git" / "hooks" / "pre-commit").unlink()
    assert githooks.status(repo)["pre-commit"] == "missing"
    assert install.ensure_git_hooks(home, repo)["pre-commit"] == "ok"
    assert install.ensure_git_hooks(home, other) == dict.fromkeys(githooks.HOOKS, "missing")
    assert not (other / ".git" / "hooks" / "pre-commit").exists()
    assert install.ensure_git_hooks(home, tmp_path) == dict.fromkeys(githooks.HOOKS, "unknown")


def test_partial_uninstall_keeps_the_shared_runtime(crew_env):
    home = crew_env["home"]
    stdin, master = _tty()
    try:
        args = _base_args(crew_env, "--apply", "--yes", "--agent", "claude-code", "--agent", "codex", "--include-unverified")
        assert _run(crew_env, args, stdin=stdin)[0] == 0
    finally:
        stdin.close()
        os.close(master)
    runner = Recorder()
    stdin, master = _tty()
    try:
        code, out = _run(crew_env, ["--uninstall", "--agent", "codex", "--apply", "--yes"], stdin=stdin, runner=runner)
    finally:
        stdin.close()
        os.close(master)
    assert code == 0, out
    assert "# remembra-crew" not in (home / ".codex" / "hooks.json").read_text()
    assert "# remembra-crew" in (home / ".claude" / "settings.json").read_text()
    assert (home / ".remembra" / "crew" / "bin" / "crew-gate.py").exists()
    assert (home / "Library" / "LaunchAgents" / "dev.remembra.crewd.plist").exists()
    assert runner.calls == []
    assert list(install.read_manifest(home)["agents"]) == ["claude-code"]


def test_reinstall_with_a_new_crewd_rewrites_the_plist_without_a_backup_next_to_it(crew_env):
    for crewd in ("/opt/a/remembra-crewd", "/opt/b/remembra-crewd"):
        stdin, master = _tty()
        try:
            args = _base_args(crew_env, "--apply", "--yes") + ["--crewd-command", crewd]
            assert _run(crew_env, args, stdin=stdin)[0] == 0
        finally:
            stdin.close()
            os.close(master)
    agents = crew_env["home"] / "Library" / "LaunchAgents"
    assert sorted(p.name for p in agents.iterdir()) == ["dev.remembra.crewd.plist"]
    assert plistlib.loads((agents / "dev.remembra.crewd.plist").read_bytes())["ProgramArguments"] == ["/opt/b/remembra-crewd"]
    assert not list((crew_env["home"] / ".remembra" / "crew").glob("*.bak-crew-*"))


def test_a_write_error_stops_cleanly(crew_env):
    home = crew_env["home"]
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    (home / ".claude").chmod(0o500)  # read-only directory: the settings write fails
    stdin, master = _tty()
    try:
        code, out = _run(crew_env, _base_args(crew_env, "--apply", "--yes", "--no-service"), stdin=stdin)
    finally:
        stdin.close()
        os.close(master)
        (home / ".claude").chmod(0o700)
    assert code == 1 and "Stopped:" in out
    assert (home / ".claude" / "settings.json").read_text() == "{}"
