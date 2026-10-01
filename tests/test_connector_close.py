"""Structured chat close through actual OAuth, MCP, REST, SQLite and desktop pickup."""

import json

import pytest

from remembra.security.untrusted import DATA_OPEN
from tests.connector_harness import connector_app


@pytest.fixture
async def h(tmp_path):
    async with connector_app(tmp_path) as harness:
        yield harness


async def test_chat_close_is_typed_bound_scoped_and_updates_one_handoff(h):
    uid = await h.create_user("closer@example.com")
    await h.seed_memory(uid, "alpha", "Project exists")
    conn = await h.connect("closer@example.com", ["alpha"], scope="session:brief session:close")
    facts = {
        "files_changed": ["src/sample.py"],
        "todos_open": ["Real device validation"],
        "errors": ["Provider test failed"],
        "next_step": "Verify the provider response",
        "notes": "Unfinished work remains",
        "facts_source": "server-inferred",
    }
    first = await h.tool(conn.access_token, "close_session", {"session_id": "chat-1", "project_id": "alpha", "facts": facts})
    assert first["handoff_id"] and first["changed"] is True
    assert DATA_OPEN in h.last_tool_text
    # A chat cannot represent its assertions as independently observed facts.
    row = await h.db.get_memory(first["handoff_id"])
    meta = json.loads(row["metadata"])
    assert meta["agent_id"] == "claude-app"
    assert meta["relay"]["facts_source"] == "agent-declared"
    key = await h.api_key(uid)
    desktop = await h.http.get(
        "/api/v1/session/brief", headers={"X-API-Key": key}, params={"project_id": "alpha", "agent_id": "codex"}
    )
    assert desktop.status_code == 200
    assert "Real device validation" in desktop.json()["rendered"]
    assert "Provider test failed" in desktop.json()["rendered"]
    same = await h.tool(conn.access_token, "close_session", {"session_id": "chat-1", "facts": facts})
    assert same["handoff_id"] == first["handoff_id"] and same["changed"] is False
    updated = await h.tool(
        conn.access_token, "close_session", {"session_id": "chat-1", "facts": {**facts, "todos_open": ["Updated next work"]}}
    )
    assert updated["changed"] is True
    trail = await h.tool(conn.access_token, "trail", {"project_id": "alpha"})
    assert "Updated next work" in json.dumps(trail)
    assert trail["total"] == 1
    refused = await h.mcp_post(
        conn.access_token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "store_memory", "arguments": {"content": "cannot use notes"}},
        },
    )
    assert refused.status_code == 403


async def test_old_note_grant_does_not_acquire_new_close_permission(h):
    uid = await h.create_user("oldgrant@example.com")
    await h.seed_memory(uid, "alpha", "Project exists")
    conn = await h.connect("oldgrant@example.com", ["alpha"], scope="session:brief memory:recall memory:store")
    result = await h.mcp_post(
        conn.access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "close_session", "arguments": {"session_id": "chat-1", "facts": {"notes": "Not authorized"}}},
        },
    )
    assert result.status_code == 403 and "session:close" in result.headers["www-authenticate"]
    assert (await h.tool(conn.access_token, "trail", {"project_id": "alpha"}))["total"] == 0


async def test_close_cannot_cross_project_or_account_and_revocation_is_immediate(h):
    uid = await h.create_user("scoped@example.com")
    other = await h.create_user("other@example.com")
    await h.seed_memory(uid, "alpha", "Owner alpha")
    await h.seed_memory(other, "private", "Other tenant")
    conn = await h.connect("scoped@example.com", ["alpha"], scope="session:brief session:close")
    refused = await h.tool(
        conn.access_token, "close_session", {"session_id": "chat-1", "project_id": "private", "facts": {"notes": "Denied"}}
    )
    assert refused["code"] == 403
    accepted = await h.tool(conn.access_token, "close_session", {"session_id": "chat-1", "facts": {"todos_open": ["Keep open"]}})
    assert accepted["handoff_id"]
    revoked = await h.http.post("/oauth/revoke", data={"token": conn.access_token, "client_id": conn.client_id})
    assert revoked.status_code == 200
    response = await h.mcp_post(
        conn.access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "close_session", "arguments": {"session_id": "chat-2", "facts": {"notes": "Denied after revoke"}}},
        },
    )
    assert response.status_code == 401
    cursor = await h.db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (other,))
    assert (await cursor.fetchone())[0] == 1


async def test_close_rejects_empty_session_and_invalid_fact_types(h):
    uid = await h.create_user("schema@example.com")
    await h.seed_memory(uid, "alpha", "Project exists")
    conn = await h.connect("schema@example.com", ["alpha"], scope="session:brief session:close")
    refused = await h.tool(conn.access_token, "close_session", {"session_id": " ", "facts": {}})
    assert refused["code"] == 400
    response = await h.mcp_post(
        conn.access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "close_session", "arguments": {"session_id": "chat-1", "facts": {"notes": ["wrong type"]}}},
        },
    )
    assert response.status_code == 200 and response.json()["result"]["isError"] is True
    assert (await h.tool(conn.access_token, "trail", {"project_id": "alpha"}))["total"] == 0
