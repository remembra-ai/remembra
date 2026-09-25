"""AGT-2: the Claude Code SessionStart hook, run as a real subprocess.

A tiny HTTP server on 127.0.0.1 forwards the hook's request to the production
API routes (ASGI TestClient over real SQLite), so the hook is exercised
end-to-end: config resolution -> HTTP GET /api/v1/session/brief -> the real
brief -> the markdown printed to stdout for Claude Code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.agent_api_harness import build_api, seed

HOOK = Path(__file__).resolve().parents[1] / "integrations" / "claude-code" / "session_start.py"
SECRET = "rem_hook_test_secret_value"


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


@pytest.fixture()
def proxy(api):
    """HTTP server that forwards to the ASGI app and records requests."""
    seen: list[dict[str, Any]] = []
    state = {"force_status": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
            if state["force_status"]:
                self.send_response(state["force_status"])
                self.end_headers()
                return
            upstream = api["http"].get(self.path)
            body = upstream.content
            self.send_response(upstream.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_port}", "seen": seen, "state": state, "api": api}
    server.shutdown()
    server.server_close()


def _run(env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "REMEMBRA_HOOK_CLAUDE_CONFIG": str(tmp_path / "none.json")}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"session_id": "abc", "hook_event_name": "SessionStart", "source": "startup"}),
        capture_output=True,
        text=True,
        env={**base, **env},
        timeout=30,
    )


def _populate(api: dict[str, Any]) -> None:
    seed(
        api,
        "h1",
        "[SESSION END] Finished AGT API. Next: MCP tools. Key files: server.py",
        datetime(2026, 9, 24, 20, 0),
        memory_type="handoff",
        metadata={"agent_id": "codex"},
    )
    seed(api, "r1", "Decided to keep status upserts in SQLite", datetime(2026, 9, 25, 9, 0), project_id="clawbot")
    seed(api, "h2", "[SESSION END] clawbot handoff", datetime(2026, 9, 25, 10, 0), project_id="clawbot", memory_type="handoff")
    codex = api["make_client"](project="clawbot", agent_id="codex")
    codex.send_to_inbox(to_agent="claude-code", subject="Review the brief endpoint", body="Please review PR 12. " * 30)
    codex.store_status("deploy:api", "pushed, not deployed")


def test_hook_prints_real_brief_from_env_config(proxy, tmp_path):
    _populate(proxy["api"])
    result = _run(
        {
            "REMEMBRA_URL": proxy["url"],
            "REMEMBRA_API_KEY": SECRET,
            "REMEMBRA_PROJECT": "clawdbot",
            "REMEMBRA_PROJECT_ALIASES": "clawdbot=clawbot",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "project: clawbot, agent: claude-code" in out
    assert "[SESSION END] clawbot handoff" in out
    assert "## Inbox: 1 unread" in out and "Review the brief endpoint" in out and "from codex" in out
    assert "deploy:api: pushed, not deployed" in out
    assert "Decided to keep status upserts in SQLite" in out
    assert "Finished AGT API" not in out  # other project's handoff not shown
    assert SECRET not in out and SECRET not in result.stderr

    request = proxy["seen"][0]
    assert request["path"].startswith("/api/v1/session/brief?")
    assert "project_id=clawbot" in request["path"] and "agent_id=claude-code" in request["path"]
    assert request["headers"].get("x-api-key") == SECRET


def test_hook_falls_back_to_claude_json_mcp_env(proxy, tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remembra": {
                        "command": "remembra-mcp",
                        "env": {
                            "REMEMBRA_URL": proxy["url"],
                            "REMEMBRA_API_KEY": SECRET,
                            "REMEMBRA_PROJECT": "alpha",
                            "REMEMBRA_AGENT_ID": "claude-code",
                        },
                    }
                }
            }
        )
    )
    result = _run({"REMEMBRA_HOOK_CLAUDE_CONFIG": str(config)}, tmp_path)
    assert result.returncode == 0
    assert "project: alpha, agent: claude-code" in result.stdout
    assert proxy["seen"][0]["headers"].get("x-api-key") == SECRET


def test_hook_http_error_is_non_blocking(proxy, tmp_path):
    proxy["state"]["force_status"] = 401
    result = _run({"REMEMBRA_URL": proxy["url"], "REMEMBRA_API_KEY": SECRET}, tmp_path)
    assert result.returncode == 0
    assert "unavailable: HTTP 401" in result.stdout
    assert SECRET not in result.stdout


def test_hook_unreachable_server_is_non_blocking(tmp_path):
    result = _run({"REMEMBRA_URL": "http://127.0.0.1:9", "REMEMBRA_API_KEY": SECRET, "REMEMBRA_HOOK_TIMEOUT": "2"}, tmp_path)
    assert result.returncode == 0
    assert result.stdout.startswith("Remembra session brief unavailable:")


def test_hook_without_key_explains(tmp_path):
    result = _run({"REMEMBRA_URL": "http://127.0.0.1:9"}, tmp_path)
    assert result.returncode == 0
    assert "no REMEMBRA_API_KEY" in result.stdout
