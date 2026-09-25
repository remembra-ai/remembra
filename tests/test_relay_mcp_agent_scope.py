"""Relay over MCP (close_session / resolve_project / compact session_brief) and agent-scoped keys."""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

import remembra.mcp.server as server
from remembra.auth.keys import APIKeyManager, _key_cache
from remembra.auth.middleware import authenticate_api_key
from remembra.auth.rbac import RoleManager
from remembra.client.memory import Memory
from remembra.storage.database import Database
from tests.agent_api_harness import build_api


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


@pytest.fixture()
def mcp_env(api, monkeypatch):
    http = api["http"]

    class ASGIMemory(Memory):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)
            self._client.close()
            self._client = http

    monkeypatch.setattr(server, "Memory", ASGIMemory)
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "kimi")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT", "alpha")
    monkeypatch.setattr(server, "REMEMBRA_PROJECT_ALIASES", {})
    monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "mcp-sess-1")
    monkeypatch.setattr(server, "_client", None)
    yield api
    server._client = None


def _j(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def test_instructions_tell_every_agent_to_brief_and_close():
    text = server.mcp.instructions or ""
    assert "session_brief" in text and "close_session" in text
    tools = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert {"session_brief", "close_session", "resolve_project"} <= tools


def test_mcp_close_then_compact_brief_for_hookless_agent(mcp_env):
    out = _j(
        server.close_session(
            summary="Refactored the parser; tests pass.",
            next_step="port the tokenizer",
            todos_open=["tokenizer port"],
            errors=["mypy: 2 errors in parser.py"],
            facts={"branch": "dev", "tests": [{"cmd": "pytest", "passed": False, "summary": "3 failed"}]},
            git_remote="https://github.com/acme/parser.git",
        )
    )
    assert out["status"] == "ok" and out["project_id"] == "parser"
    assert out["grounding"]["status"] == "contradicted"
    assert "Next step (agent): port the tokenizer" in out["rendered"]
    again = _j(
        server.close_session(next_step="port the tokenizer", facts={"branch": "dev"}, git_remote="git@github.com:acme/parser")
    )
    assert again["handoff_id"] != out["handoff_id"]  # same session, new facts: superseded, not duplicated
    trail = mcp_env["http"].get("/api/v1/trail", params={"project_id": "parser"}).json()
    assert trail["total"] == 1 and trail["items"][0]["agent_id"] == "kimi"

    brief = _j(server.session_brief(git_remote="git@github.com:ACME/parser.git"))
    assert brief["project_id"] == "parser" and brief["handoff_id"] == again["handoff_id"]
    assert set(brief) == {"status", "project_id", "agent_id", "brief", "handoff_id", "inbox_unread", "warnings"}
    assert brief["brief"].splitlines()[1].startswith("Last session: kimi, just now, on dev: ")


def test_mcp_resolve_project_and_bind(mcp_env):
    first = _j(server.resolve_project(git_remote="https://github.com/acme/thing"))
    assert first["project_id"] == "thing" and first["created"] is True
    bound = _j(server.resolve_project(git_remote="git@github.com:acme/thing.git", hint_project="clawdbot", bind=True))
    assert bound["project_id"] == "clawdbot"


def test_sdk_sends_agent_header_and_relay_methods(mcp_env):
    client = mcp_env["make_client"](project="alpha", agent_id="qwen")
    result = client.close_session(facts={"todos_open": ["x"]}, session_id="s1")
    assert result["agent_id"] == "qwen"
    assert client.trail()["items"][0]["agent_id"] == "qwen"
    client.link_projects("alpha", "beta", relation="depends_on")
    assert client.project_links()[0]["project_id"] == "beta"
    assert client.resolve_project(root_path="/tmp/x", host="h", hint_project="alpha")["project_id"] == "alpha"


# ---------------------------------------------------------------------------
# Agent-scoped API keys (real key storage + validation + auth)
# ---------------------------------------------------------------------------


@pytest.fixture
async def keydb():
    tmp = tempfile.mktemp(suffix=".db")
    database = Database(f"sqlite+aiosqlite:///{tmp}")
    await database.connect()
    await database.init_schema()
    await RoleManager(database).init_schema()
    _key_cache.clear()
    yield database
    _key_cache.clear()
    await database.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass


async def test_agent_scoped_key_round_trip(keydb):
    mgr = APIKeyManager(keydb)
    scoped = await mgr.create_key(user_id="u1", name="codex key", agent_id="codex")
    plain = await mgr.create_key(user_id="u1", name="plain")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(api_key_manager=mgr, db=keydb)))

    user = await authenticate_api_key(request, scoped.key)  # type: ignore[arg-type]
    assert user is not None and user.agent_id == "codex"
    _key_cache.clear()
    assert (await authenticate_api_key(request, scoped.key)).agent_id == "codex"  # type: ignore[arg-type,union-attr]
    assert (await authenticate_api_key(request, plain.key)).agent_id is None  # type: ignore[arg-type,union-attr]
