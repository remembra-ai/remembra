"""remembra-install is safe by default: a dry run with the key masked, no key on the command
line, backups, 0600 files, one REMEMBRA_AGENT_ID per agent, and a clean --remove.

Every test runs the real command in a subprocess with a temporary HOME (the
agent config paths are resolved from it), so the user's real configs are
never read or written.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from remembra.tools import doctor
from remembra.tools.agents import EXIT_NOT_WRITTEN
from remembra.tools.keyinput import ARGV_KEY_WARNING, mask_key, mask_text, resolve_api_key

SRC = str(Path(__file__).resolve().parents[1] / "src")
KEY = "rem_live_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
OLD_KEY = "rem_old_" + "Z9y8X7w6V5u4T3s2R1q0"


def install(home: Path, *args: str, env: dict[str, str] | None = None, stdin: str | None = None):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC}
    base.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "remembra.tools.agents", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=base,
        cwd=str(home),
        timeout=60,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under ``root``: (mtime_ns, sha256)."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A HOME with Claude Code (existing settings), Cursor (existing MCP config) and Codex installed."""
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {}}, indent=2))
    os.chmod(h / ".claude" / "settings.json", 0o644)
    (h / ".cursor").mkdir()
    (h / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}))
    os.chmod(h / ".cursor" / "mcp.json", 0o644)
    (h / ".codex").mkdir()
    (h / ".codex" / "config.toml").write_text('model = "gpt-5"\n')
    (h / ".gemini").mkdir()  # detected, no config file yet
    return h


PATHS = {
    "claude-code": ".claude/settings.json",
    "cursor": ".cursor/mcp.json",
    "codex": ".codex/config.toml",
    "gemini": ".gemini/settings.json",
}


def test_all_without_apply_changes_no_file_and_masks_the_key(home: Path) -> None:
    before = _snapshot(home)
    time.sleep(0.02)
    result = install(home, "--all", env={"REMEMBRA_API_KEY": KEY})
    assert result.returncode == EXIT_NOT_WRITTEN, result.stderr  # non-zero: a chained `&& connect --apply` stops
    assert _snapshot(home) == before  # no file changed, none created (not even ~/.remembra/credentials)
    out = result.stdout
    assert KEY not in out + result.stderr
    assert mask_key(KEY) in out and "(from REMEMBRA_API_KEY)" in out
    assert "Dry run: nothing was written" in out
    for agent in PATHS:
        assert f"[{agent}]" in out
    assert '"REMEMBRA_AGENT_ID": "claude-code"' in out and 'REMEMBRA_AGENT_ID = "codex"' in out  # each agent's own id


def test_apply_writes_0600_files_with_backups_and_one_agent_id_each(home: Path) -> None:
    originals = {agent: (home / rel).read_text() for agent, rel in PATHS.items() if (home / rel).exists()}
    result = install(home, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert result.returncode == 0, result.stderr
    assert KEY not in result.stdout + result.stderr

    for agent, rel in PATHS.items():
        path = home / rel
        assert _mode(path) == 0o600, (agent, oct(_mode(path)))
        if agent == "codex":
            env = tomllib.loads(path.read_text())["mcp_servers"]["remembra"]["env"]
            assert tomllib.loads(path.read_text())["model"] == "gpt-5"
        else:
            data = json.loads(path.read_text())
            env = data["mcpServers"]["remembra"]["env"]
        assert env["REMEMBRA_AGENT_ID"] == agent
        assert env["REMEMBRA_API_KEY"] == KEY
        backups = list(path.parent.glob(f"{path.name}.bak-remembra-*"))
        if agent in originals:
            assert len(backups) == 1 and backups[0].read_text() == originals[agent], agent
            assert _mode(backups[0]) == 0o600
        else:
            assert backups == [], agent  # a new file has nothing to back up
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["github"] == {"command": "gh-mcp"}
    assert json.loads((home / ".claude" / "settings.json").read_text())["model"] == "opus"
    creds = home / ".remembra" / "credentials"
    assert _mode(creds) == 0o600 and json.loads(creds.read_text())["api_key"] == KEY

    # Idempotent: a second run finds nothing to do and makes no new backup.
    again = install(home, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert again.returncode == 0 and "Nothing to change." in again.stdout
    assert len(list((home / ".cursor").glob("mcp.json.bak-remembra-*"))) == 1


def test_the_saved_key_is_used_and_no_code_path_needs_it_on_argv(home: Path) -> None:
    install(home, "--agent", "cursor", "--apply", env={"REMEMBRA_API_KEY": KEY})
    # No env, no flag, no terminal: the key saved in ~/.remembra/credentials is used.
    rerun = install(home, "--agent", "codex", "--apply", env={"REMEMBRA_API_KEY": ""})
    assert rerun.returncode == 0, rerun.stderr
    assert "--api-key" not in rerun.stderr
    assert (
        tomllib.loads((home / ".codex" / "config.toml").read_text())["mcp_servers"]["remembra"]["env"]["REMEMBRA_API_KEY"] == KEY
    )

    piped = install(home, "--agent", "gemini", "--apply", "--api-key-stdin", env={"REMEMBRA_API_KEY": ""}, stdin=OLD_KEY + "\n")
    assert piped.returncode == 0 and "(from stdin)" in piped.stdout and "warning" not in piped.stderr
    assert (
        json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]["remembra"]["env"]["REMEMBRA_API_KEY"]
        == OLD_KEY
    )


def test_api_key_on_argv_still_works_but_warns(home: Path) -> None:
    result = install(home, "--agent", "cursor", "--apply", "--api-key", KEY, env={"REMEMBRA_API_KEY": ""})
    assert result.returncode == 0, result.stderr
    assert ARGV_KEY_WARNING in result.stderr
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["remembra"]["env"]["REMEMBRA_API_KEY"] == KEY
    assert "--api-key " not in install(home, "--help").stdout  # hidden from the help


def test_no_key_anywhere_is_a_clear_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    (empty / ".cursor").mkdir(parents=True)
    result = install(empty, "--all", env={"REMEMBRA_API_KEY": ""})
    assert result.returncode == 2
    assert "no API key" in result.stderr and "REMEMBRA_API_KEY" in result.stderr and "--api-key-stdin" in result.stderr
    assert not (empty / ".cursor" / "mcp.json").exists()


def test_an_invalid_config_is_reported_not_overwritten(home: Path) -> None:
    broken = home / ".cursor" / "mcp.json"
    broken.write_text("{ not json")
    result = install(home, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert result.returncode == 1
    assert "[cursor] cannot read its config" in result.stdout and "not valid JSON" in result.stdout
    assert broken.read_text() == "{ not json"
    assert "REMEMBRA_AGENT_ID" in (home / ".claude" / "settings.json").read_text()  # the others still went ahead


def test_an_unchanged_config_readable_by_others_is_tightened(home: Path) -> None:
    install(home, "--agent", "cursor", "--apply", env={"REMEMBRA_API_KEY": KEY})
    path = home / ".cursor" / "mcp.json"
    os.chmod(path, 0o644)
    dry = install(home, "--agent", "cursor", env={"REMEMBRA_API_KEY": KEY})
    assert "is readable by other users" in dry.stdout and _mode(path) == 0o644
    install(home, "--agent", "cursor", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert _mode(path) == 0o600


def test_interactive_prompt_reads_the_key_hidden_and_asks_before_writing(home: Path) -> None:
    """A real pseudo-terminal: the key is typed at a hidden prompt, then 'y' writes."""
    master, slave = pty.openpty()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC, "REMEMBRA_API_KEY": ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "remembra.tools.agents", "--agent", "cursor"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=str(home),
        start_new_session=True,  # no controlling terminal: getpass uses this pty, never the developer's
    )
    os.close(slave)
    seen = b""

    def read_until(marker: bytes, timeout: float = 20.0) -> None:
        nonlocal seen
        deadline = time.time() + timeout
        while marker not in seen:
            assert time.time() < deadline, seen.decode(errors="replace")
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # the child closed the terminal
                    chunk = b""
                assert chunk or marker in seen, seen.decode(errors="replace")
                seen += chunk

    try:
        read_until(b"Remembra API key (input hidden)")
        os.write(master, (KEY + "\n").encode())
        read_until(b"[y/N]")
        os.write(master, b"y\n")
        read_until(b"Next: remembra-relay connect")
        assert proc.wait(timeout=30) == 0
        assert b"written (0600)" in seen
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    assert KEY.encode() not in seen  # never echoed, never printed
    data = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert data["mcpServers"]["remembra"]["env"]["REMEMBRA_API_KEY"] == KEY


def test_the_key_is_saved_even_when_no_agent_config_is_detected(tmp_path: Path) -> None:
    """R-22: a Qwen-only (or Kimi-only) machine still gets ~/.remembra/credentials for the relay hooks."""
    qwen_only = tmp_path / "qwen-only"
    (qwen_only / ".qwen").mkdir(parents=True)
    creds = qwen_only / ".remembra" / "credentials"

    dry = install(qwen_only, "--all", env={"REMEMBRA_API_KEY": KEY})
    assert dry.returncode == EXIT_NOT_WRITTEN and not creds.exists()
    assert "No agent MCP config found" in dry.stdout and "save the key where remembra-relay reads it" in dry.stdout
    assert KEY not in dry.stdout + dry.stderr

    applied = install(qwen_only, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert applied.returncode == 0, applied.stderr
    assert _mode(creds) == 0o600 and json.loads(creds.read_text())["api_key"] == KEY
    assert "Restart your agents" not in applied.stdout and "Next: remembra-relay connect" in applied.stdout
    assert sorted(p.name for p in qwen_only.iterdir()) == [".qwen", ".remembra"]  # no agent config was created

    again = install(qwen_only, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    assert again.returncode == 0 and "Nothing to change." in again.stdout
    removed = install(qwen_only, "--remove", "--all", "--apply")
    assert removed.returncode == 0 and "no MCP entry to remove" in removed.stdout and creds.exists()


def test_a_run_that_writes_nothing_stops_a_chained_command(home: Path, tmp_path: Path) -> None:
    """The dashboard one-liner chains `remembra-install --all && remembra-relay connect --apply`."""
    marker = tmp_path / "connect-ran"
    chained = subprocess.run(
        ["sh", "-c", f'"{sys.executable}" -m remembra.tools.agents --all && touch "{marker}"'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC, "REMEMBRA_API_KEY": KEY},
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert chained.returncode == EXIT_NOT_WRITTEN and not marker.exists()
    assert install(home, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY}).returncode == 0
    nothing_to_do = install(home, "--all", env={"REMEMBRA_API_KEY": KEY})
    assert nothing_to_do.returncode == 0 and "Nothing to change." in nothing_to_do.stdout


def test_answering_no_at_the_prompt_writes_nothing_and_exits_3(home: Path) -> None:
    before = _snapshot(home)
    master, slave = pty.openpty()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": SRC, "REMEMBRA_API_KEY": KEY}
    proc = subprocess.Popen(
        [sys.executable, "-m", "remembra.tools.agents", "--all"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=str(home),
        start_new_session=True,
    )
    os.close(slave)
    seen = b""
    try:
        deadline = time.time() + 20
        while b"[y/N]" not in seen:
            assert time.time() < deadline, seen.decode(errors="replace")
            if select.select([master], [], [], 0.2)[0]:
                seen += os.read(master, 4096)
        os.write(master, b"n\n")
        assert proc.wait(timeout=30) == EXIT_NOT_WRITTEN
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    assert _snapshot(home) == before


def test_remove_restores_the_configs_and_is_a_dry_run_first(home: Path) -> None:
    originals = {agent: (home / rel).read_text() for agent, rel in PATHS.items() if (home / rel).exists()}
    install(home, "--all", "--apply", env={"REMEMBRA_API_KEY": KEY})
    installed = _snapshot(home)
    time.sleep(0.02)
    dry = install(home, "--remove", "--all", env={"REMEMBRA_API_KEY": ""})
    assert dry.returncode == EXIT_NOT_WRITTEN and "remove the remembra MCP server" in dry.stdout
    assert KEY not in dry.stdout
    assert _snapshot(home) == installed

    removed = install(home, "--remove", "--all", "--apply", env={"REMEMBRA_API_KEY": ""})
    assert removed.returncode == 0, removed.stderr
    for agent, rel in PATHS.items():
        path = home / rel
        if agent not in originals:
            assert not path.exists(), agent  # the file only ever held the Remembra entry
        elif agent == "codex":
            assert tomllib.loads(path.read_text()) == tomllib.loads(originals[agent])
        else:
            assert json.loads(path.read_text()) == json.loads(originals[agent]), agent
    assert (home / ".remembra" / "credentials").exists()  # kept: remembra-relay reads it
    assert "pipx uninstall remembra" in removed.stdout and "revoke the key" in removed.stdout
    assert "Nothing to change." in install(home, "--remove", "--all", "--apply").stdout


def test_resolve_api_key_order_and_prompt(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text(json.dumps({"api_key": OLD_KEY}))
    warnings: list[str] = []
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return ""

    # On a terminal, Enter keeps the saved key (the prompt shows it masked).
    key, source = resolve_api_key(cli_key=None, from_stdin=False, credentials=creds, environ={}, interactive=True, prompt=prompt)
    assert (key, source) == (OLD_KEY, str(creds)) and mask_key(OLD_KEY) in prompts[0] and OLD_KEY not in prompts[0]
    key, source = resolve_api_key(
        cli_key=None, from_stdin=False, credentials=creds, environ={}, interactive=True, prompt=lambda _: KEY
    )
    assert (key, source) == (KEY, "prompt")
    key, source = resolve_api_key(
        cli_key=None, from_stdin=False, credentials=creds, environ={"REMEMBRA_API_KEY": KEY}, interactive=True
    )
    assert source == "REMEMBRA_API_KEY"
    key, source = resolve_api_key(
        cli_key=KEY, from_stdin=False, credentials=creds, environ={}, interactive=False, warn=warnings.append
    )
    assert source == "--api-key" and warnings == [ARGV_KEY_WARNING]
    assert resolve_api_key(cli_key=None, from_stdin=False, credentials=tmp_path / "none", environ={}, interactive=False) == (
        None,
        "no key given",
    )


def test_mask_text_hides_every_key_form() -> None:
    assert mask_key(KEY) == "rem_…K1l2" and mask_key("short") == "…"
    line = f'  "REMEMBRA_API_KEY": "{KEY}", "api_key": "sk-other-secret-value", REMEMBRA_API_KEY = "plainsecret99"'
    masked = mask_text(line, (KEY,))
    assert KEY not in masked and "sk-other-secret-value" not in masked and "plainsecret99" not in masked
    assert mask_key(KEY) in masked


def test_doctor_warns_on_a_key_file_others_can_read(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remembra": {"command": "remembra-mcp", "env": {"REMEMBRA_API_KEY": KEY}}}}))
    os.chmod(path, 0o644)
    warning = doctor.check_key_file_permissions(path)
    assert warning is not None and warning.status == "warn" and "chmod 600" in warning.message and KEY not in warning.message
    os.chmod(path, 0o600)
    assert doctor.check_key_file_permissions(path) is None
    plain = tmp_path / "plain.json"
    plain.write_text('{"theme": "dark"}')
    os.chmod(plain, 0o644)
    assert doctor.check_key_file_permissions(plain) is None  # no key, nothing to warn about
    os.chmod(path, 0o644)
    results = doctor._run_doctor("cursor", path, doctor.load_cursor_target)
    assert results[-1].name == "permissions" and results[-1].status == "warn"
