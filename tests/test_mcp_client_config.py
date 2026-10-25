"""``remembra-mcp --client NAME``: settings a client did not pass are read from its own config file.

The Codex app's remote exec-server starts MCP servers without the ``env`` configured for them, so
the server fell back to localhost and every tool failed with "Connection refused". These tests run
the real launcher in a subprocess with an empty environment (HOME and PATH only, as that exec-server
gives it) against a stub Remembra API, and check which key, agent id and server the calls carry.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from remembra._launch import apply_client_config, parent_client, pop_client
from remembra.relay.config import DEFAULT_URL, client_mcp_env, fill_env_from_client
from remembra.tools.codex import build_codex_mcp_block

pytest.importorskip("mcp")


class _Stub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                stub.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization") or self.headers.get("X-API-Key")}
                )
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def stub():
    s = _Stub()
    yield s
    s.close()


def _codex_home(tmp_path: Path, url: str, key: str | None = "rem_codexkey_" + "x" * 24) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    block = build_codex_mcp_block(command="remembra-mcp", url=url, api_key=key, project="proj-a", user_id="u1")
    (home / ".codex" / "config.toml").write_text('model = "x"\n\n' + block + "\n")
    return home


def _run_mcp(args: list[str], env: dict[str, str], arguments: dict | None = None) -> tuple[str, str]:
    """Start the real launcher over stdio, call one tool, return (tool result text, stderr)."""
    code = "import sys; sys.argv[0] = 'remembra-mcp'; from remembra._launch import mcp; mcp()"
    p = subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert p.stdin and p.stdout

    def rpc(msg_id: int | None, method: str, params: dict | None = None) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            msg["id"] = msg_id
        if params is not None:
            msg["params"] = params
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        if msg_id is None:
            return None
        while True:
            line = p.stdout.readline()
            assert line, "the MCP server exited"
            reply = json.loads(line)
            if reply.get("id") == msg_id:
                return reply

    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        rpc(None, "notifications/initialized")
        reply = rpc(2, "tools/call", {"name": "get_inbox", "arguments": arguments or {}})
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=30)
        err = p.stderr.read() if p.stderr else ""
    content = (reply or {}).get("result", {}).get("content") or []
    return "".join(c.get("text", "") for c in content), err


def _bare_env(home: Path) -> dict[str, str]:
    # what the Codex app's remote exec-server passes: no REMEMBRA_* at all
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return env


def test_codex_started_without_its_env_uses_the_key_url_and_agent_from_its_config(tmp_path, stub):
    key = "rem_codexkey_" + "k" * 24
    home = _codex_home(tmp_path, stub.url, key)
    text, err = _run_mcp(["--client", "codex"], _bare_env(home))
    assert stub.requests, f"no request reached the configured server: {text} {err}"
    req = stub.requests[-1]
    assert req["auth"] in (key, f"Bearer {key}")
    assert "agent_id=codex" in req["path"]
    assert "Connection refused" not in text and "No Remembra settings" not in text
    assert key not in err  # names only, never the key
    assert "REMEMBRA_API_KEY" in err and "codex's config file" in err


def test_without_client_and_without_settings_the_error_says_why(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    text, err = _run_mcp([], _bare_env(home), {"agent_id": "codex"})
    assert "No Remembra settings reached this MCP server" in text, text
    assert "--client" in text
    assert "No Remembra settings" in err


def test_settings_that_arrive_in_the_env_win_over_the_file(tmp_path, stub):
    home = _codex_home(tmp_path, "http://127.0.0.1:9", "rem_filekey_" + "f" * 24)
    env = {
        **_bare_env(home),
        "REMEMBRA_URL": stub.url,
        "REMEMBRA_API_KEY": "rem_envkey_" + "e" * 24,
        "REMEMBRA_AGENT_ID": "codex",
    }
    _run_mcp(["--client", "codex"], env)
    assert stub.requests and stub.requests[-1]["auth"] in ("rem_envkey_" + "e" * 24, "Bearer rem_envkey_" + "e" * 24)


def test_a_key_from_the_file_brings_its_url_never_another_server():
    env: dict[str, str] = {"REMEMBRA_URL": "https://elsewhere.example", "HOME": "/nonexistent"}
    home_found = {
        "REMEMBRA_API_KEY": "rem_k" + "1" * 24,
        "REMEMBRA_URL": "https://api.remembra.dev",
        "REMEMBRA_AGENT_ID": "codex",
    }
    import remembra.relay.config as cfg

    orig = cfg.client_mcp_env
    cfg.client_mcp_env = lambda client, home, codex_home=None: dict(home_found)  # type: ignore[assignment]
    try:
        filled = fill_env_from_client("codex", env)
    finally:
        cfg.client_mcp_env = orig  # type: ignore[assignment]
    assert env["REMEMBRA_URL"] == "https://api.remembra.dev"
    assert env["REMEMBRA_AGENT_ID"] == "codex"
    assert set(filled) == {"REMEMBRA_API_KEY", "REMEMBRA_URL", "REMEMBRA_AGENT_ID"}


def test_a_file_key_without_a_url_means_the_default_server(tmp_path):
    home = tmp_path / "h"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"remembra": {"env": {"REMEMBRA_API_KEY": "rem_g" + "2" * 24}}}})
    )
    env = {"REMEMBRA_URL": "https://elsewhere.example"}
    fill_env_from_client("gemini", env, home)
    assert env["REMEMBRA_URL"] == DEFAULT_URL


def test_a_sandboxed_codex_block_without_a_key_still_gives_its_bridge_url(tmp_path):
    home = _codex_home(tmp_path, "http://127.0.0.1:8766", key=None)
    env: dict[str, str] = {}
    fill_env_from_client("codex", env, home)
    assert env["REMEMBRA_URL"] == "http://127.0.0.1:8766" and "REMEMBRA_API_KEY" not in env


def test_missing_or_broken_files_give_nothing(tmp_path):
    assert client_mcp_env("codex", tmp_path) == {}
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("not = [toml")
    assert client_mcp_env("codex", tmp_path) == {}
    assert client_mcp_env("nobody", tmp_path) == {}
    env: dict[str, str] = {}
    assert fill_env_from_client("codex", env, tmp_path) == [] and env == {}


def test_client_flag_forms_and_unknown_client():
    assert pop_client(["--client", "codex", "--version"]) == ("codex", ["--version"])
    assert pop_client(["--client=gemini"]) == ("gemini", [])
    assert pop_client(["--help"]) == (None, ["--help"])
    with pytest.raises(SystemExit) as exc:
        apply_client_config(["--client", "grok"], {})
    assert exc.value.code == 2


def test_the_installer_writes_the_client_flag():
    import tomllib

    block = build_codex_mcp_block(
        command="remembra-mcp", url="https://api.remembra.dev", api_key="rem_x" + "3" * 24, project="p", user_id="u"
    )
    assert tomllib.loads(block)["mcp_servers"]["remembra"]["args"] == ["--client", "codex"]


@pytest.mark.skipif(os.name != "posix", reason="parent lookup uses ps")
def test_an_older_config_without_args_is_detected_by_its_codex_parent(tmp_path):
    # a symlink keeps the system binary's signature (macOS kills a copy) and ps shows the link's name
    fake = tmp_path / "codex"
    fake.symlink_to(shutil.which("sleep") or "/bin/sleep")
    proc = subprocess.Popen([str(fake), "30"])
    try:
        assert parent_client({"HOME": "/x"}, ppid=proc.pid) == "codex"
        # any REMEMBRA_* setting means the env arrived: nothing is read from a file
        assert parent_client({"REMEMBRA_URL": "https://api.remembra.dev"}, ppid=proc.pid) is None
    finally:
        proc.kill()
    assert parent_client({"HOME": "/x"}, ppid=os.getpid()) is None


def test_a_key_from_the_env_never_takes_the_files_url(tmp_path):
    """Codex review P1: a key that arrived without a URL must not be sent to the file's server."""
    home = _codex_home(tmp_path, "https://other.example", "rem_filekey_" + "f" * 24)
    env = {"REMEMBRA_API_KEY": "rem_envkey_" + "e" * 24}
    filled = fill_env_from_client("codex", env, home)
    assert "REMEMBRA_URL" not in env and "REMEMBRA_URL" not in filled
    assert env["REMEMBRA_API_KEY"] == "rem_envkey_" + "e" * 24
    assert env["REMEMBRA_AGENT_ID"] == "codex"  # settings that are not the key or its server are still filled


def test_codex_home_is_the_codex_config_that_is_read(tmp_path):
    """Codex review P1: Codex reads $CODEX_HOME/config.toml when CODEX_HOME is set."""
    default = _codex_home(tmp_path, "https://default.example", "rem_default_" + "d" * 24)
    other = tmp_path / "other-codex"
    other.mkdir()
    block = build_codex_mcp_block(
        command="remembra-mcp", url="https://other.example", api_key="rem_other_" + "o" * 24, project="p", user_id="u"
    )
    (other / "config.toml").write_text(block + "\n")
    env = {"CODEX_HOME": str(other)}
    fill_env_from_client("codex", env, default)
    assert env["REMEMBRA_URL"] == "https://other.example" and env["REMEMBRA_API_KEY"] == "rem_other_" + "o" * 24
