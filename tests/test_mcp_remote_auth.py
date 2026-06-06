"""Security tests for the remote (HTTP) MCP transport.

The remote MCP is multi-tenant: every caller authenticates with their own
X-API-Key. These tests pin the invariants that keep that safe:
  - stdio stays single-tenant (env key).
  - remote NEVER falls back to a shared env key — no key => 401.
  - each key gets its own client (no cross-tenant bleed), and the key set by the
    auth middleware propagates to a task spawned downstream (as FastMCP dispatches
    tools), with the context reset after the request.
"""

import asyncio

import pytest

import remembra.mcp.server as server


@pytest.fixture(autouse=True)
def _clean_mcp_context():
    """Reset per-request MCP state so tests don't leak keys into each other."""
    server._request_api_key.set(None)
    server._request_project.set(None)
    server._clients_by_key.clear()
    yield
    server._request_api_key.set(None)
    server._request_project.set(None)
    server._clients_by_key.clear()


def test_extract_api_key():
    assert server._extract_api_key({"x-api-key": "rem_A"}) == "rem_A"
    assert server._extract_api_key({"authorization": "Bearer rem_B"}) == "rem_B"
    assert server._extract_api_key({"authorization": "bearer rem_C"}) == "rem_C"
    assert server._extract_api_key({}) is None
    assert server._extract_api_key({"x-api-key": ""}) is None


def test_stdio_uses_env_key(monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "REMEMBRA_API_KEY", "rem_env")
    monkeypatch.setattr(server, "_client", None)
    assert server._get_client().api_key == "rem_env"


def test_remote_requires_key_no_env_fallback(monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "REMEMBRA_API_KEY", "rem_shared_env")  # must NOT be used
    server._request_api_key.set(None)
    with pytest.raises(server.MemoryError) as exc:
        server._get_client()
    assert exc.value.status_code == 401


def test_remote_per_key_isolation(monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    server._clients_by_key.clear()

    server._request_api_key.set("rem_AAA")
    ca = server._get_client()
    assert ca.api_key == "rem_AAA"

    server._request_api_key.set("rem_BBB")
    cb = server._get_client()
    assert cb.api_key == "rem_BBB"
    assert ca is not cb  # different tenants get different clients

    # Re-selecting AAA returns AAA's cached client, never BBB's.
    server._request_api_key.set("rem_AAA")
    assert server._get_client() is ca


async def test_middleware_propagates_key_to_spawned_task(monkeypatch):
    """The crux: a task spawned by the inner app (as FastMCP dispatches a tool)
    must see the key the middleware set for THIS request, then it must reset."""
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    captured: dict[str, str | None] = {}

    async def fake_inner(scope, receive, send):
        async def tool_task():
            captured["key"] = server._request_api_key.get()
            captured["project"] = server._request_project.get()

        await asyncio.create_task(tool_task())

    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: fake_inner)
    app = server._build_remote_app("streamable-http")

    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"rem_XYZ")],
        "query_string": b"project=clawbot",
    }
    await app(scope, None, None)

    assert captured["key"] == "rem_XYZ"
    assert captured["project"] == "clawbot"
    # Context is cleaned up so it can't leak into the next request.
    assert server._request_api_key.get() is None
    assert server._request_project.get() is None


async def test_middleware_no_key_sets_none(monkeypatch):
    monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", "streamable-http")
    seen: dict[str, str | None] = {}

    async def fake_inner(scope, receive, send):
        seen["key"] = server._request_api_key.get()

    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: fake_inner)
    app = server._build_remote_app("streamable-http")
    await app({"type": "http", "headers": [], "query_string": b""}, None, None)
    assert seen["key"] is None
