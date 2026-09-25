"""Unit tests for the connector's pure OAuth rules and its settings validation."""

from __future__ import annotations

import pytest

from remembra.config import Settings
from remembra.connector.policy import (
    CHATGPT_REDIRECT_URI,
    CLAUDE_REDIRECT_URI,
    ScopeError,
    default_agent_label,
    normalize_agent_label,
    normalize_projects,
    normalize_public_url,
    parse_requested_scope,
    parse_scope,
    redirect_uri_allowed,
    redirect_uri_matches,
    resource_matches,
    s256,
    valid_code_challenge,
    verify_pkce,
)


def test_parse_scope_defaults_orders_and_rejects_unknown():
    assert parse_scope(None) == ["session:brief", "memory:recall", "memory:store"]
    assert parse_scope("offline_access") == ["session:brief", "memory:recall", "memory:store"]
    assert parse_scope("memory:store memory:recall memory:recall offline_access") == ["memory:recall", "memory:store"]
    with pytest.raises(ScopeError):
        parse_scope("memory:recall memory:delete")


def test_parse_requested_scope_keeps_supported_subset():
    assert parse_requested_scope(None) == (["session:brief", "memory:recall", "memory:store"], [])
    assert parse_requested_scope("offline_access") == (["session:brief", "memory:recall", "memory:store"], [])
    assert parse_requested_scope("claudeai memory:store session:brief") == (
        ["session:brief", "memory:store"],
        ["claudeai"],
    )
    with pytest.raises(ScopeError):
        parse_requested_scope("claudeai memory:delete")


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        (CLAUDE_REDIRECT_URI, True),
        (CHATGPT_REDIRECT_URI, True),
        ("https://chatgpt.com/connector/oauth/cb_ABC-123", True),
        ("https://chatgpt.com/connector/oauth/../../evil", False),
        ("https://claude.ai/api/mcp/auth_callback/extra", False),
        ("https://claude.ai.evil.com/api/mcp/auth_callback", False),
        ("http://claude.ai/api/mcp/auth_callback", False),
        ("http://localhost:3118/callback", True),
        ("http://127.0.0.1/callback", True),
        ("http://[::1]:9000/cb", True),
        ("http://user:pw@localhost/callback", False),
        ("https://localhost/callback", False),
        ("http://localhost.evil.com/callback", False),
        ("https://evil.example/cb", False),
        ("https://claude.ai/api/mcp/auth_callback#frag", False),
        ("", False),
    ],
)
def test_redirect_uri_allowlist(uri, allowed):
    assert redirect_uri_allowed(uri) is allowed


def test_redirect_uri_allowlist_extras_and_loopback_switch():
    assert redirect_uri_allowed("https://app.example.com/cb", extra=["https://app.example.com/cb"])
    assert not redirect_uri_allowed("http://localhost:3000/callback", allow_loopback=False)


def test_redirect_matching_is_exact_except_loopback_port():
    registered = [CLAUDE_REDIRECT_URI, "http://localhost/callback"]
    assert redirect_uri_matches(CLAUDE_REDIRECT_URI, registered)
    assert not redirect_uri_matches(CLAUDE_REDIRECT_URI + "?x=1", registered)
    assert redirect_uri_matches("http://localhost:61234/callback", registered)
    assert not redirect_uri_matches("http://localhost:61234/other", registered)
    assert not redirect_uri_matches("http://127.0.0.1:61234/callback", registered)


def test_pkce_s256_only():
    verifier = "a" * 43 + "-._~"
    challenge = s256(verifier)
    assert valid_code_challenge(challenge)
    assert verify_pkce(verifier, challenge)
    assert not verify_pkce("b" * 50, challenge)
    assert not verify_pkce(None, challenge)
    assert not verify_pkce("short", s256("short"))  # verifier under 43 chars
    assert not valid_code_challenge("plain-challenge")


def test_resource_matching_is_canonical():
    expected = "https://api.example.com/mcp"
    assert resource_matches(None, expected)
    assert resource_matches("HTTPS://API.EXAMPLE.COM/mcp/", expected)
    assert not resource_matches("https://api.example.com/other", expected)
    assert not resource_matches("https://evil.example.com/mcp", expected)


def test_consent_inputs():
    assert normalize_agent_label(" Claude-App ") == "claude-app"
    for bad in ("", "has space", "x" * 65, "-leading"):
        with pytest.raises(ValueError):
            normalize_agent_label(bad)
    assert normalize_projects(["alpha", " alpha ", "", "my project"]) == ["alpha", "my-project"]
    with pytest.raises(ValueError):
        normalize_projects(["", "  "])
    with pytest.raises(ValueError):
        normalize_projects([f"p{i}" for i in range(21)])
    with pytest.raises(ValueError):
        normalize_projects(["bad\x00id"])
    assert default_agent_label(CLAUDE_REDIRECT_URI) == "claude-app"
    assert default_agent_label(CHATGPT_REDIRECT_URI) == "chatgpt"
    assert default_agent_label("http://localhost:1/callback") == "mcp-client"


def test_public_url_rules():
    assert normalize_public_url("https://API.Example.com/") == "https://api.example.com"
    assert normalize_public_url("http://localhost:8787") == "http://localhost:8787"
    for bad in ("http://api.example.com", "https://api.example.com/sub", "ftp://x.com", "api.example.com", "https://x.com/?a=1"):
        with pytest.raises(ValueError):
            normalize_public_url(bad)


def test_settings_require_public_url_when_enabled():
    with pytest.raises(ValueError, match="public_url"):
        Settings(connector_enabled=True, public_url=None)
    with pytest.raises(ValueError, match="https"):
        Settings(connector_enabled=True, public_url="http://api.example.com")
    ok = Settings(connector_enabled=True, public_url="https://api.example.com/")
    assert ok.public_url == "https://api.example.com"
    assert Settings().connector_enabled is False


def test_connector_refuses_to_install_without_auth():
    from fastapi import FastAPI

    from remembra.connector import install_connector

    settings = Settings(connector_enabled=True, public_url="https://api.example.com", auth_enabled=False)
    with pytest.raises(RuntimeError, match="authentication"):
        install_connector(FastAPI(), settings)


def test_connector_explains_a_missing_mcp_sdk(monkeypatch):
    import sys

    from fastapi import FastAPI

    from remembra.connector import install_connector

    monkeypatch.setitem(sys.modules, "mcp", None)  # import mcp -> ImportError
    settings = Settings(connector_enabled=True, public_url="https://api.example.com", auth_enabled=True)
    with pytest.raises(RuntimeError, match=r"remembra\[server,mcp\]"):
        install_connector(FastAPI(), settings)
