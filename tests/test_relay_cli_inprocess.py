"""remembra-relay in-process: config discovery, payload mapping, adapters, CLI against the ASGI app.

The subprocess tests in ``test_relay_cli_e2e`` prove the real entry point;
these run the same functions in-process (measured by coverage) with the
CLI's HTTP client routed into the production app.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from remembra.relay import cli
from remembra.relay.adapters import REGISTRY, get_adapter
from remembra.relay.adapters.base import PayloadMap
from remembra.relay.config import load_config
from tests.agent_api_harness import build_api
from tests.relay_fixtures import Transcript, commit, git, make_remote_and_clones


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_env_wins_and_url_comes_from_the_key_source(tmp_path):
    _write(
        tmp_path / ".claude.json",
        json.dumps(
            {"mcpServers": {"remembra": {"env": {"REMEMBRA_API_KEY": "rem_claude", "REMEMBRA_URL": "https://claude.example"}}}}
        ),
    )
    env_cfg = load_config(environ={"REMEMBRA_API_KEY": "rem_env", "REMEMBRA_URL": "https://env.example/"}, home=tmp_path)
    assert (env_cfg.api_key, env_cfg.url, env_cfg.source) == ("rem_env", "https://env.example", "env")
    # An env URL without an env key never receives the file's key.
    file_cfg = load_config(environ={"REMEMBRA_URL": "https://evil.example"}, home=tmp_path)
    assert (file_cfg.api_key, file_cfg.url) == ("rem_claude", "https://claude.example")
    assert "rem_claude" not in json.dumps(file_cfg.redacted())


def test_config_project_entries_codex_credentials_and_preference(tmp_path):
    _write(
        tmp_path / ".claude.json",
        json.dumps(
            {
                "projects": {
                    "/x": {"mcpServers": {"remembra": {"env": {"REMEMBRA_API_KEY": "rem_proj", "REMEMBRA_AGENT_ID": "cc"}}}}
                }
            }
        ),
    )
    _write(
        tmp_path / ".codex" / "config.toml",
        '[mcp_servers.remembra]\ncommand = "remembra-mcp"\n\n[mcp_servers.remembra.env]\n'
        'REMEMBRA_API_KEY = "rem_codex"\nREMEMBRA_URL = "https://codex.example"\nREMEMBRA_AGENT_ID = "codex"\n',
    )
    assert load_config(environ={}, home=tmp_path).api_key == "rem_proj"
    preferred = load_config(environ={}, home=tmp_path, prefer="codex")
    assert (preferred.api_key, preferred.url, preferred.agent_id) == ("rem_codex", "https://codex.example", "codex")
    assert load_config(agent="explicit", environ={"REMEMBRA_AGENT_ID": "env"}, home=tmp_path).agent_id == "explicit"

    creds_home = tmp_path / "other"
    _write(creds_home / ".remembra" / "credentials", json.dumps({"api_key": "rem_creds", "project": "clawdbot"}))
    creds = load_config(environ={}, home=creds_home)
    assert (creds.api_key, creds.project, creds.url) == ("rem_creds", "clawdbot", "http://localhost:8787")
    empty = load_config(environ={}, home=tmp_path / "nothing")
    assert empty.api_key is None and empty.source == "none"
    _write(tmp_path / "bad" / ".claude.json", "{not json")
    assert load_config(environ={}, home=tmp_path / "bad").api_key is None


def test_payload_maps():
    assert PayloadMap().extract({"session_id": "s", "cwd": "/w", "transcript_path": "/t", "reason": "exit"}, {}) == {
        "session_id": "s",
        "cwd": "/w",
        "transcript": "/t",
        "reason": "exit",
    }
    cursor = get_adapter("cursor")
    assert cursor is not None
    assert cursor.spec.payload.extract({"conversation_id": "c", "workspace_roots": ["/a", "/b"]}, {})["cwd"] == "/a"
    gemini = get_adapter("Gemini")
    assert gemini is not None
    assert gemini.spec.payload.extract({}, {"GEMINI_SESSION_ID": "g", "GEMINI_PROJECT_DIR": "/p"}) == {
        "session_id": "g",
        "cwd": "/p",
        "transcript": None,
        "reason": None,
    }
    assert get_adapter("nope") is None and get_adapter(None) is None


def test_every_adapter_plans_idempotently(tmp_path):
    for name, adapter in REGISTRY.items():
        first = adapter.plan(tmp_path, "/bin/relay")
        assert first.changed and first.summary, name
        assert "brief --hook " + name in first.after and "close --hook " + name in first.after
        _write(first.path, first.after)
        assert not adapter.plan(tmp_path, "/bin/relay").changed, name
        moved = adapter.plan(tmp_path, "/new/relay")  # relay moved: our entry is replaced, not duplicated
        assert moved.changed and moved.after.count("/new/relay") == 2 and "/bin/relay" not in moved.after, name


def test_json_adapter_rejects_non_object_config(tmp_path):
    adapter = REGISTRY["claude-code"]
    _write(tmp_path / ".claude" / "settings.json", "[1, 2]")
    with pytest.raises(ValueError):
        adapter.plan(tmp_path, "/bin/relay")


# ---------------------------------------------------------------------------
# CLI in-process against the ASGI app
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


@pytest.fixture()
def wired(api, monkeypatch, tmp_path):
    """CLI with its HTTP client routed into the app; isolated HOME and env."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REMEMBRA_API_KEY", "rem_inprocess")
    monkeypatch.setenv("REMEMBRA_URL", "http://testserver")
    for var in ("REMEMBRA_AGENT_ID", "REMEMBRA_PROJECT", "REMEMBRA_RELAY_PROJECT", "REMEMBRA_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    http = api["http"]

    def client(self: cli.Context) -> httpx.Client:
        http.headers.update({"X-API-Key": self.config.api_key or ""})
        if self.agent:
            http.headers["X-Remembra-Agent-Id"] = self.agent
        return _NoClose(http)

    monkeypatch.setattr(cli.Context, "client", client)
    return {"api": api, "home": home}


class _NoClose:
    def __init__(self, http):
        self.http = http

    def __enter__(self):
        return self.http

    def __exit__(self, *exc):
        self.http.headers.pop("X-Remembra-Agent-Id", None)
        return False


def _run(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_close_brief_trail_resolve_in_process(wired, monkeypatch, capsys, tmp_path):
    _, clones = make_remote_and_clones(tmp_path, ("a", "b"))
    a, b = clones["a"], clones["b"]
    git(a, "remote", "set-url", "origin", "https://github.com/acme/inproc.git")
    git(b, "remote", "set-url", "origin", "git@github.com:acme/inproc")

    hook = json.dumps({"session_id": "S1", "cwd": str(a)})
    code, out, err = _run(monkeypatch, capsys, ["brief", "--hook", "claude-code", "--agent", "claude-code"], hook)
    assert code == 0 and "Last session: none recorded" in out, err
    sha = commit(a, "x.py", "x\n", "feat: in-process")
    t = Transcript("S1", a)
    t.bash("pytest -q", 1, "=== 1 failed in 0.1s ===")
    transcript = t.write(tmp_path / "S1.jsonl")
    end = json.dumps({"session_id": "S1", "cwd": str(a), "transcript_path": str(transcript), "reason": "clear"})
    code, out, err = _run(
        monkeypatch, capsys, ["close", "--hook", "claude-code", "--agent", "claude-code", "--next", "fix it"], end
    )
    assert code == 0 and "Remembra handoff" not in out and err == ""  # hooks keep stdout clean

    code, out, _ = _run(monkeypatch, capsys, ["brief", "--agent", "codex", "--cwd", str(b)])
    lines = out.splitlines()  # in-process, server log lines share stdout: anchor on the header
    last = lines[lines.index("# Remembra brief · project inproc · you are codex") + 1]
    assert last.startswith(f"Last session: claude-code, just now, on main@{sha[:7]}")
    assert "failing: FAILING: pytest -q (1 failed in 0.1s)" in last and "next: fix it" in last

    code, out, _ = _run(monkeypatch, capsys, ["brief", "--agent", "codex", "--cwd", str(b), "--format", "json"])
    assert json.loads(out.splitlines()[-1])["project_id"] == "inproc"
    code, out, _ = _run(monkeypatch, capsys, ["trail", "--cwd", str(b), "--format", "json"])
    assert json.loads(out[out.index("{\n") :])["items"][0]["agent_id"] == "claude-code"
    code, out, _ = _run(monkeypatch, capsys, ["trail", "--cwd", str(b)])
    assert "Trail · project inproc · 1 entries" in out

    code, out, _ = _run(monkeypatch, capsys, ["resolve", "--cwd", str(b), "--project", "clawdbot", "--bind"])
    assert code == 0 and json.loads(out[out.index("{\n") :])["project_id"] == "clawdbot"
    code, out, _ = _run(monkeypatch, capsys, ["close", "--agent", "codex", "--cwd", str(b), "--todo", "t1", "--summary", "done"])
    assert "project clawdbot" in out

    code, out, err = _run(monkeypatch, capsys, ["close", "--agent", "bad agent", "--cwd", str(b)])
    assert code == 0 and "close failed: HTTP 400" in err

    state_files = list((wired["home"] / ".remembra" / "relay" / "sessions").glob("*.json"))
    assert len(state_files) == 1 and json.loads(state_files[0].read_text())["head"] != sha


def test_cli_connect_in_process(wired, monkeypatch, capsys):
    home = wired["home"]
    _write(home / ".claude" / "settings.json", "{}")
    code, out, _ = _run(monkeypatch, capsys, ["connect", "--relay-command", "/r"])
    assert code == 0 and "[claude-code] Claude Code (verified)" in out and "dry run" in out
    code, out, _ = _run(monkeypatch, capsys, ["connect", "--agent", "bogus"])
    assert code == 2
    _write(home / ".claude" / "settings.json", "{broken")
    code, out, _ = _run(monkeypatch, capsys, ["connect", "--agent", "claude-code", "--relay-command", "/r"])
    assert code == 1 and "cannot read" in out
    assert cli.main(["brief", "--bogus"]) == 0
    assert cli.main(["connect", "--bogus"]) == 2
