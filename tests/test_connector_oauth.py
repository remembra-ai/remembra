"""Remote MCP connector: OAuth 2.1 authorization server + /mcp, end to end.

Every test drives the production app factory (auth ON) over ASGI: dynamic
client registration -> authorize with PKCE -> dashboard login -> consent ->
token -> MCP tool calls with the bearer token -> refresh -> revoke, plus the
negative paths the specs require.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client import streamable_http as mcp_http

import remembra.mcp.server as desktop_mcp
from remembra.client.memory import Memory
from remembra.connector.policy import CHATGPT_REDIRECT_URI, CLAUDE_REDIRECT_URI
from remembra.security import state as security_state
from tests.connector_harness import PASSWORD, PUBLIC, RESOURCE, ConnectorHarness, connector_app, pkce


@pytest.fixture()
async def h(tmp_path):
    async with connector_app(tmp_path) as harness:
        yield harness


async def _alice(h: ConnectorHarness) -> str:
    return await h.create_user("alice@example.com")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def test_authorization_server_metadata(h):
    resp = await h.http.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["issuer"] == PUBLIC
    assert meta["authorization_endpoint"] == PUBLIC + "/oauth/authorize"
    assert meta["token_endpoint"] == PUBLIC + "/oauth/token"
    assert meta["registration_endpoint"] == PUBLIC + "/oauth/register"
    assert meta["revocation_endpoint"] == PUBLIC + "/oauth/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert set(meta["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    # No CIMD advertised: clients fall back to DCR (documented behaviour of both).
    assert "client_id_metadata_document_supported" not in meta


@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"])
async def test_protected_resource_metadata(h, path):
    resp = await h.http.get(path)
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["resource"] == RESOURCE
    assert meta["authorization_servers"] == [PUBLIC]
    assert set(meta["scopes_supported"]) == {"session:brief", "memory:recall", "memory:store"}


async def test_mcp_without_token_is_401_with_resource_metadata(h):
    resp = await h.mcp_post(None, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="session:brief memory:recall memory:store"' in challenge


async def test_connector_off_by_default(tmp_path):
    async with connector_app(tmp_path, connector_enabled=False) as h:
        for path in ("/.well-known/oauth-authorization-server", "/oauth/register"):
            resp = await (h.http.get(path) if path.startswith("/.well") else h.http.post(path, json={}))
            assert resp.status_code in (404, 405)
        resp = await h.mcp_post("x", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# The full happy path, with a real MCP client
# ---------------------------------------------------------------------------


def _mcp_client(h: ConnectorHarness, token: str) -> Any:
    """The MCP SDK's streamable HTTP client over the ASGI app, on any mcp 1.x.

    mcp >= 1.24 takes a ready httpx client (``streamable_http_client``); the
    1.23 line pinned in uv.lock (what the Docker image installs) takes a factory.
    """

    def factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app, client=("203.0.113.10", 1)),
            base_url=PUBLIC,
            headers={**(headers or {}), "Authorization": f"Bearer {token}"},
            timeout=30,
        )

    new_api = getattr(mcp_http, "streamable_http_client", None)
    if new_api is not None:
        return new_api(RESOURCE, http_client=factory())
    return mcp_http.streamablehttp_client(RESOURCE, httpx_client_factory=factory)


async def test_full_flow_with_real_mcp_client_refresh_and_revoke(h):
    alice = await _alice(h)
    await h.seed_memory(alice, "alpha", "Deployed the billing service to staging", memory_type="handoff")
    conn = await h.connect("alice@example.com", ["alpha", "beta"])
    assert conn.token_response["token_type"] == "Bearer"
    assert conn.token_response["expires_in"] == 3600
    assert conn.token_response["scope"] == "session:brief memory:recall memory:store"
    assert conn.access_token.startswith("rmc_at_") and conn.refresh_token.startswith("rmc_rt_")

    # Real MCP client (the SDK's streamable HTTP client) over the same ASGI app.
    async with (
        _mcp_client(h, conn.access_token) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        assert init.serverInfo.name == "remembra"
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert set(tools) == {"session_brief", "trail", "recall_memories", "send_to_inbox", "store_memory", "list_projects"}
        # Nothing destructive is exposed over the connector.
        assert all(t.annotations is not None and t.annotations.destructiveHint is False for t in tools.values())

        result = await session.call_tool("session_brief", {})
        brief = json.loads(result.content[0].text)
        assert brief["status"] == "ok"
        assert brief["project_id"] == "alpha"
        assert brief["agent_id"] == "claude-app"
        assert "billing service" in brief["handoff"]["content"]

        result = await session.call_tool("list_projects", {})
        projects = json.loads(result.content[0].text)
        assert [p["project_id"] for p in projects["projects"]] == ["alpha", "beta"]

    # Refresh: rotation returns a new pair and the new access token works.
    refreshed = await h.token({"grant_type": "refresh_token", "refresh_token": conn.refresh_token, "client_id": conn.client_id})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.headers["cache-control"] == "no-store"
    new = refreshed.json()
    assert new["refresh_token"] != conn.refresh_token and new["access_token"] != conn.access_token
    assert (await h.tool(new["access_token"], "list_projects"))["status"] == "ok"

    # Revoke the refresh token (RFC 7009): the whole connection ends.
    rev = await h.http.post("/oauth/revoke", data={"token": new["refresh_token"], "client_id": conn.client_id})
    assert rev.status_code == 200
    denied = await h.mcp_post(new["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert denied.status_code == 401
    assert 'error="invalid_token"' in denied.headers["www-authenticate"]
    again = await h.token({"grant_type": "refresh_token", "refresh_token": new["refresh_token"], "client_id": conn.client_id})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


async def test_tools_store_note_recall_and_trail(h):
    alice = await _alice(h)
    await h.seed_memory(alice, "alpha", "Checkpoint: migrated users table", memory_type="checkpoint")
    conn = await h.connect("alice@example.com", ["alpha"])

    stored = await h.tool(conn.access_token, "store_memory", {"content": "Call the printer vendor about toner", "tags": ["todo"]})
    assert stored["status"] == "ok" and stored["project_id"] == "alpha"
    row = await h.db.get_memory(stored["id"])
    assert row is not None
    assert row["user_id"] == alice and row["project_id"] == "alpha"
    assert row["memory_type"] == "observation"
    meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
    assert meta["agent_id"] == "claude-app" and meta["source"] == "connector" and meta["kind"] == "note"
    assert meta["tags"] == ["todo"]

    trail = await h.tool(conn.access_token, "trail", {})
    assert trail["status"] == "ok"
    assert [e["memory_type"] for e in trail["trail"]] == ["checkpoint"]
    assert "migrated users table" in trail["trail"][0]["content"]

    recall = await h.tool(conn.access_token, "recall_memories", {"query": "printer toner"})
    assert recall["status"] == "ok"
    assert any("printer vendor" in m["content"] for m in recall["memories"])

    # A project outside the grant is refused by the tool, before any REST call.
    other = await h.tool(conn.access_token, "store_memory", {"content": "x", "project_id": "secret-project"})
    assert other["status"] == "error" and other["code"] == 403


# ---------------------------------------------------------------------------
# Phone -> desktop: the reason this connector exists
# ---------------------------------------------------------------------------


class _BridgeClient:
    """Sync httpx-like client for the SDK that runs requests on the test's event loop."""

    def __init__(self, http: httpx.AsyncClient, headers: dict[str, str]) -> None:
        self._http = http
        self._headers = headers

    async def _request(self, method: str, url: str, json: Any, params: Any) -> httpx.Response:
        return await self._http.request(method, url, json=json, params=params, headers=self._headers)

    def request(self, method: str, url: str, json: Any = None, params: Any = None) -> httpx.Response:
        return anyio.from_thread.run(self._request, method, url, json, params)

    def close(self) -> None:
        return None


async def test_phone_inbox_instruction_reaches_desktop_session_brief(h, monkeypatch):
    alice = await _alice(h)
    phone = await h.connect("alice@example.com", ["alpha"], agent="claude-app")

    sent = await h.tool(
        phone.access_token,
        "send_to_inbox",
        {"to_agent": "claude-code", "subject": "Fix the flaky login test", "body": "It fails on CI since yesterday."},
    )
    assert sent["status"] == "ok" and sent["from_agent"] == "claude-app"

    # The desktop agent: the real stdio MCP server tool, with its own API key.
    key = await h.api_key(alice)
    memory = Memory(base_url=PUBLIC, api_key=key, project="alpha", agent_id="claude-code")
    memory._client.close()
    memory._client = _BridgeClient(h.http, memory._headers)  # type: ignore[assignment]
    monkeypatch.setattr(desktop_mcp, "REMEMBRA_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(desktop_mcp, "_client", memory)
    monkeypatch.setattr(desktop_mcp, "REMEMBRA_AGENT_ID", "claude-code")

    brief = json.loads(await anyio.to_thread.run_sync(lambda: desktop_mcp.session_brief()))
    assert brief["status"] == "ok", brief
    inbox = brief["inbox"]
    assert inbox["unread_count"] == 1
    item = inbox["items"][0]
    assert item["subject"] == "Fix the flaky login test"
    assert item["from_agent"] == "claude-app"


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


async def test_wrong_code_verifier_rejected(h):
    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    _verifier, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    await h.login(rid, "alice@example.com")
    code = h.redirect_params(await h.consent(rid, ["alpha"]))["code"]
    other_verifier, _ = pkce()
    resp = await h.token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": other_verifier,
        }
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_code_replay_revokes_the_connection(h):
    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    verifier, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    await h.login(rid, "alice@example.com")
    code = h.redirect_params(await h.consent(rid, ["alpha"]))["code"]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    first = await h.token(body)
    assert first.status_code == 200
    replay = await h.token(body)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    # The tokens minted from the leaked code are dead too.
    resp = await h.mcp_post(first.json()["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401


async def test_refresh_token_reuse_revokes_the_connection(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    body = {"grant_type": "refresh_token", "refresh_token": conn.refresh_token, "client_id": conn.client_id}
    rotated = await h.token(body)
    assert rotated.status_code == 200
    store = h.app.state.connector_store
    real_clock = store._clock
    store._clock = lambda: real_clock() + 31  # past the retry grace window
    try:
        reuse = await h.token(body)
        assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
        resp = await h.mcp_post(rotated.json()["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 401
    finally:
        store._clock = real_clock


async def test_refresh_retry_within_grace_does_not_end_the_connection(h):
    """Claude refreshes proactively and reactively; a quick duplicate is a retry."""
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    body = {"grant_type": "refresh_token", "refresh_token": conn.refresh_token, "client_id": conn.client_id}
    first = await h.token(body)
    retry = await h.token(body)
    assert first.status_code == 200 and retry.status_code == 200
    assert retry.json()["access_token"] != first.json()["access_token"]
    for pair in (first.json(), retry.json()):
        assert (await h.tool(pair["access_token"], "list_projects"))["status"] == "ok"


async def test_login_is_rate_limited(h):
    from remembra.core.limiter import limiter

    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    previous = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        # Distinct unknown emails: no per-account lockout, only the IP limit applies.
        codes = [(await h.login(rid, f"nobody{i}@example.com")).status_code for i in range(11)]
    finally:
        limiter.reset()
        limiter.enabled = previous
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


async def test_failed_connector_logins_lock_the_dashboard_login_too(h):
    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    codes = [(await h.login(rid, "alice@example.com", password="wrong-password-x")).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    # Right password, still locked, and the dashboard login shares the lock.
    assert (await h.login(rid, "alice@example.com")).status_code == 429
    dash = await h.http.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert dash.status_code == 429


async def test_unregistered_redirect_uri_rejected_at_registration(h):
    resp = await h.register(redirect_uri="https://evil.example.net/callback")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


async def test_authorize_with_unregistered_redirect_never_redirects(h):
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    resp = await h.authorize(client_id, challenge, redirect_uri="https://evil.example.net/callback")
    assert resp.status_code == 400
    assert "location" not in resp.headers
    unknown = await h.authorize("rmc_client_nope", challenge)
    assert unknown.status_code == 400 and "location" not in unknown.headers


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"scope": "memory:delete"}, "invalid_scope"),
        ({"resource": "https://other.example.com/mcp"}, "invalid_target"),
    ],
)
async def test_authorize_errors_redirect_with_iss(h, overrides, error):
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    resp = await h.authorize(client_id, challenge, **overrides)
    params = h.redirect_params(resp)
    assert params["error"] == error
    assert params["iss"] == PUBLIC
    assert params["state"] == "st-123"


async def test_expired_access_token_rejected(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    store = h.app.state.connector_store
    real_clock = store._clock
    store._clock = lambda: real_clock() + 3601
    try:
        resp = await h.mcp_post(conn.access_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        store._clock = real_clock
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]


async def test_cross_tenant_isolation(h):
    alice = await _alice(h)
    await h.create_user("bob@example.com")
    await h.seed_memory(alice, "alpha", "Alice private: the vault code is in the blue binder", memory_type="handoff")
    bob = await h.connect("bob@example.com", ["alpha"])  # same project NAME, different account

    brief = await h.tool(bob.access_token, "session_brief", {})
    assert brief["status"] == "ok"
    assert brief["handoff"] is None
    assert brief["recent"] == []
    recall = await h.tool(bob.access_token, "recall_memories", {"query": "vault code blue binder"})
    assert recall["memories"] == []
    trail = await h.tool(bob.access_token, "trail", {})
    assert trail["trail"] == []

    # Bob cannot see or disconnect Alice's connections.
    alice_conn = await h.connect("alice@example.com", ["alpha"])
    grants = await h.http.get("/api/v1/connector/connections", headers=h.jwt(alice, "alice@example.com"))
    alice_grant = grants.json()["connections"][0]["connection_id"]
    bob_id = (await h.db.get_user_by_email("bob@example.com"))["id"]
    denied = await h.http.delete(f"/api/v1/connector/connections/{alice_grant}", headers=h.jwt(bob_id, "bob@example.com"))
    assert denied.status_code == 404
    assert (await h.tool(alice_conn.access_token, "list_projects"))["status"] == "ok"


async def test_insufficient_scope_is_403_with_challenge(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"], scope="session:brief")
    assert conn.token_response["scope"] == "session:brief"
    assert (await h.tool(conn.access_token, "session_brief"))["status"] == "ok"
    resp = await h.mcp_post(
        conn.access_token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "store_memory", "arguments": {"content": "x"}}},
    )
    assert resp.status_code == 403
    challenge = resp.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge and "memory:store" in challenge


async def test_oauth_token_is_not_a_rest_credential(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    resp = await h.http.get("/api/v1/session/brief", headers={"Authorization": f"Bearer {conn.access_token}"})
    assert resp.status_code == 401


async def test_password_change_style_invalidation_ends_connections(h):
    alice = await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    await security_state.invalidate_user_sessions(h.db, alice)
    resp = await h.mcp_post(conn.access_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    refresh = await h.token({"grant_type": "refresh_token", "refresh_token": conn.refresh_token, "client_id": conn.client_id})
    assert refresh.status_code == 400 and refresh.json()["error"] == "invalid_grant"


async def test_user_can_list_and_disconnect_connections(h):
    alice = await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha", "beta"])
    listed = await h.http.get("/api/v1/connector/connections", headers=h.jwt(alice, "alice@example.com"))
    assert listed.status_code == 200
    item = listed.json()["connections"][0]
    assert item["client_name"] == "Claude" and item["agent_id"] == "claude-app" and item["project_ids"] == ["alpha", "beta"]
    gone = await h.http.delete(
        f"/api/v1/connector/connections/{item['connection_id']}", headers=h.jwt(alice, "alice@example.com")
    )
    assert gone.status_code == 200
    resp = await h.mcp_post(conn.access_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
    assert (await h.http.get("/api/v1/connector/connections", headers=h.jwt(alice, "alice@example.com"))).json()["count"] == 0


# ---------------------------------------------------------------------------
# Browser pages: login, consent, CSRF, deny
# ---------------------------------------------------------------------------


async def test_consent_without_csrf_cookie_rejected(h):
    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    await h.login(rid, "alice@example.com")
    h.http.cookies.clear()
    resp = await h.consent(rid, ["alpha"])
    assert resp.status_code == 403
    assert "location" not in resp.headers


async def test_wrong_password_and_deny(h):
    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    bad = await h.login(rid, "alice@example.com", password="nope-nope-nope")
    assert bad.status_code == 401 and "Invalid email or password" in bad.text
    # Consent before a successful login is refused.
    early = await h.consent(rid, ["alpha"])
    assert early.status_code == 403
    denied = await h.consent(rid, ["alpha"], decision="deny")
    params = h.redirect_params(denied)
    assert params["error"] == "access_denied" and params["state"] == "st-123" and params["iss"] == PUBLIC


async def test_consent_validates_projects_and_agent_label(h):
    await _alice(h)
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    await h.login(rid, "alice@example.com")
    none = await h.consent(rid, [])
    assert none.status_code == 400 and "Choose at least one project" in none.text
    bad_agent = await h.consent(rid, ["alpha"], agent="Bad Agent!")
    assert bad_agent.status_code == 400 and "Agent label" in bad_agent.text
    ok = await h.consent(rid, [], new_project="new work")
    assert h.redirect_params(ok)["code"].startswith("rmc_ac_")
    # A consumed request can't be approved twice.
    twice = await h.consent(rid, ["alpha"])
    assert twice.status_code == 400


async def test_pages_carry_their_own_csp(h):
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    page = await h.authorize(client_id, challenge)
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' https://claude.ai" in csp
    assert re.search(r"style-src 'nonce-[^']+'", csp)
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    cookie = page.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    # JSON APIs keep the strict default.
    api = await h.http.get("/.well-known/oauth-authorization-server")
    assert "form-action 'none'" in api.headers["content-security-policy"]


async def test_two_factor_required_when_enabled(h):
    import pyotp

    from remembra.auth.users import UserManager

    alice = await _alice(h)
    users = UserManager(h.db, h.settings.jwt_secret)
    secret, _uri, err = await users.setup_totp(alice)
    assert err is None and secret
    ok, err = await users.enable_totp(alice, pyotp.TOTP(secret).now())
    assert ok, err
    client_id = (await h.register()).json()["client_id"]
    _v, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    missing = await h.login(rid, "alice@example.com")
    assert missing.status_code == 401 and "6-digit code" in missing.text
    wrong = await h.login(rid, "alice@example.com", totp_code="000000")
    assert wrong.status_code == 401


# ---------------------------------------------------------------------------
# Clients: ChatGPT, Claude Code loopback, confidential clients
# ---------------------------------------------------------------------------


async def test_chatgpt_redirects_accepted(h):
    for uri in (CHATGPT_REDIRECT_URI, "https://chatgpt.com/connector/oauth/abc_123-XYZ"):
        resp = await h.register(redirect_uri=uri)
        assert resp.status_code == 201, resp.text


async def test_loopback_redirect_matches_any_port(h):
    reg = await h.register(redirect_uri="http://localhost/callback")
    client_id = reg.json()["client_id"]
    _v, challenge = pkce()
    page = await h.authorize(client_id, challenge, redirect_uri="http://localhost:53682/callback")
    assert page.status_code == 200
    assert "localhost" in page.text and "on your own computer" in page.text
    wrong_path = await h.authorize(client_id, challenge, redirect_uri="http://localhost:53682/other")
    assert wrong_path.status_code == 400


async def test_loopback_disabled_by_setting(tmp_path):
    async with connector_app(tmp_path, connector_allow_loopback_redirects=False) as h2:
        resp = await h2.register(redirect_uri="http://127.0.0.1:3000/callback")
        assert resp.status_code == 400


async def test_confidential_client_needs_its_secret(h):
    await _alice(h)
    reg = await h.register(method="client_secret_basic")
    assert reg.status_code == 201
    client_id, secret = reg.json()["client_id"], reg.json()["client_secret"]
    verifier, challenge = pkce()
    rid = h.request_id(await h.authorize(client_id, challenge))
    await h.login(rid, "alice@example.com")
    code = h.redirect_params(await h.consent(rid, ["alpha"]))["code"]
    body = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_REDIRECT_URI, "code_verifier": verifier}
    no_secret = await h.token({**body, "client_id": client_id})
    assert no_secret.status_code == 401 and no_secret.json()["error"] == "invalid_client"
    ok = await h.token(body, auth=(client_id, secret))
    assert ok.status_code == 200, ok.text


async def test_token_endpoint_requires_form_encoding(h):
    resp = await h.http.post("/oauth/token", json={"grant_type": "refresh_token"})
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_request"
    bad_grant = await h.token({"grant_type": "password", "client_id": (await h.register()).json()["client_id"]})
    assert bad_grant.status_code == 400 and bad_grant.json()["error"] == "unsupported_grant_type"


async def test_secrets_are_stored_hashed(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    for table in ("oauth_tokens", "oauth_codes", "oauth_clients", "oauth_auth_requests", "oauth_grants"):
        cursor = await h.db.conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
        dump = json.dumps([list(r) for r in await cursor.fetchall()], default=str)
        assert conn.access_token not in dump and conn.refresh_token not in dump


# ---------------------------------------------------------------------------
# Defense in depth: the in-process principal
# ---------------------------------------------------------------------------


async def test_rest_layer_enforces_grant_projects_and_permissions(h):
    """Even if a tool skipped its own checks, the REST routes the connector calls
    in-process enforce the grant's projects and least-privilege permissions."""
    from remembra.connector.mcp_app import ConnectorCall, ToolFailure, _rest

    alice = await _alice(h)
    await h.connect("alice@example.com", ["alpha"])
    grant = (await h.app.state.connector_store.list_grants(alice))[0]
    from remembra.connector.store import Grant

    call = ConnectorCall(
        grant=Grant(
            grant_id=grant["connection_id"],
            user_id=alice,
            client_id=grant["client_id"],
            scopes=grant["scopes"],
            resource=RESOURCE,
            project_ids=["alpha"],
            agent_id="claude-app",
            created_at_ms=0,
        ),
        app=h.app,
        client_ip="198.51.100.7",
    )
    with pytest.raises(ToolFailure) as outside:
        await _rest(call, "GET", "/api/v1/session/brief", permissions=["memory:recall"], params={"project_id": "other"})
    assert outside.value.code == 403
    with pytest.raises(ToolFailure) as no_perm:
        await _rest(
            call, "POST", "/api/v1/memories", permissions=["memory:recall"], json_body={"content": "x", "project_id": "alpha"}
        )
    assert no_perm.value.code == 403
    ok = await _rest(call, "GET", "/api/v1/session/brief", permissions=["memory:recall"], params={"project_id": "alpha"})
    assert ok["project_id"] == "alpha"


async def test_principal_does_not_leak_past_a_tool_call(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    assert (await h.tool(conn.access_token, "session_brief"))["status"] == "ok"
    resp = await h.http.get("/api/v1/session/brief", params={"project_id": "alpha"})
    assert resp.status_code == 401


async def test_connector_writes_are_audited_to_the_connection(h):
    alice = await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    stored = await h.tool(conn.access_token, "store_memory", {"content": "Audit me"})
    assert stored["status"] == "ok"
    grant_id = (await h.app.state.connector_store.list_grants(alice))[0]["connection_id"]
    cursor = await h.db.conn.execute("SELECT api_key_id, user_id FROM audit_log WHERE action = 'memory_store'")
    rows = [tuple(r) for r in await cursor.fetchall()]
    assert (f"oauth:{grant_id}", alice) in rows


async def test_oversized_mcp_body_rejected(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    big = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "store_memory", "arguments": {"content": "x" * 1_100_000}},
    }
    resp = await h.mcp_post(conn.access_token, big)
    assert resp.status_code == 413


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ([1, 2], "invalid_client_metadata"),
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"redirect_uris": "https://claude.ai/api/mcp/auth_callback"}, "invalid_redirect_uri"),
        ({"redirect_uris": [CLAUDE_REDIRECT_URI] * 11}, "invalid_redirect_uri"),
        ({"redirect_uris": [CLAUDE_REDIRECT_URI], "token_endpoint_auth_method": "private_key_jwt"}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_REDIRECT_URI], "grant_types": ["client_credentials"]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_REDIRECT_URI], "response_types": ["token"]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_REDIRECT_URI], "scope": "memory:delete"}, "invalid_client_metadata"),
    ],
)
async def test_registration_validates_client_metadata(h, body, error):
    resp = await h.http.post("/oauth/register", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == error


async def test_registration_defaults_follow_rfc7591(h):
    resp = await h.http.post("/oauth/register", json={"redirect_uris": [CLAUDE_REDIRECT_URI], "client_name": "  Bad\x00Name  "})
    assert resp.status_code == 201
    body = resp.json()
    # RFC 7591 default auth method is client_secret_basic, so a secret is issued.
    assert body["token_endpoint_auth_method"] == "client_secret_basic" and body["client_secret"]
    assert body["client_name"] == "BadName"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]


async def test_revocation_endpoint_edges(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    unknown_client = await h.http.post("/oauth/revoke", data={"token": conn.refresh_token, "client_id": "rmc_client_x"})
    assert unknown_client.status_code == 401 and unknown_client.json()["error"] == "invalid_client"
    missing = await h.http.post("/oauth/revoke", data={"client_id": conn.client_id})
    assert missing.status_code == 400
    # Another client's token is ignored (200, no effect).
    other = (await h.register()).json()["client_id"]
    ignored = await h.http.post("/oauth/revoke", data={"token": conn.access_token, "client_id": other})
    assert ignored.status_code == 200
    assert (await h.tool(conn.access_token, "list_projects"))["status"] == "ok"
    # Revoking just the access token leaves the refresh token usable.
    done = await h.http.post("/oauth/revoke", data={"token": conn.access_token, "client_id": conn.client_id})
    assert done.status_code == 200
    assert (await h.mcp_post(conn.access_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 401
    refreshed = await h.token({"grant_type": "refresh_token", "refresh_token": conn.refresh_token, "client_id": conn.client_id})
    assert refreshed.status_code == 200


async def test_inbox_expiry_and_validation(h):
    await _alice(h)
    conn = await h.connect("alice@example.com", ["alpha"])
    bad = await h.tool(
        conn.access_token, "send_to_inbox", {"to_agent": "codex", "subject": "s", "body": "b", "expires_in": "soon"}
    )
    assert bad["status"] == "error" and bad["code"] == 400
    blank = await h.tool(conn.access_token, "send_to_inbox", {"to_agent": "  ", "subject": "s", "body": "b"})
    assert blank["status"] == "error"
    ok = await h.tool(conn.access_token, "send_to_inbox", {"to_agent": "codex", "subject": "s", "body": "b", "expires_in": "2d"})
    assert ok["status"] == "ok"
    rows = await h.app.state.inbox_manager.get_for_agent(
        owner_user_id=(await h.db.get_user_by_email("alice@example.com"))["id"], agent_id="codex", status="unread", limit=5
    )
    assert rows[0]["expires_at"] and rows[0]["metadata"]["project_id"] == "alpha"
