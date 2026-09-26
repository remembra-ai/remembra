"""Tests for universal agent installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from remembra.tools.agents import (
    AGENT_CONFIGS,
    build_mcp_server_config,
    detect_agents,
    install_agent_config,
    install_all_agents,
    upsert_mcp_config,
)


# ============================================================================
# MCP Server Config Tests
# ============================================================================


def test_build_mcp_server_config_creates_valid_config() -> None:
    config = build_mcp_server_config(
        command="remembra-mcp",
        url="https://api.remembra.dev",
        api_key="rem_test",
        project="clawdbot",
        user_id="user_123",
    )

    assert config["command"] == "remembra-mcp"
    assert config["env"]["REMEMBRA_URL"] == "https://api.remembra.dev"
    assert config["env"]["REMEMBRA_API_KEY"] == "rem_test"
    assert config["env"]["REMEMBRA_PROJECT"] == "clawdbot"
    assert config["env"]["REMEMBRA_USER_ID"] == "user_123"


def test_build_mcp_server_config_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        build_mcp_server_config(
            command="remembra-mcp",
            url="https://api.remembra.dev",
            api_key="",
            project="default",
            user_id="default",
        )


def test_build_mcp_server_config_requires_project() -> None:
    with pytest.raises(ValueError, match="project is required"):
        build_mcp_server_config(
            command="remembra-mcp",
            url="https://api.remembra.dev",
            api_key="rem_test",
            project="",
            user_id="default",
        )


def test_build_mcp_server_config_requires_user_id() -> None:
    with pytest.raises(ValueError, match="user_id is required"):
        build_mcp_server_config(
            command="remembra-mcp",
            url="https://api.remembra.dev",
            api_key="rem_test",
            project="default",
            user_id="",
        )


# ============================================================================
# Upsert Config Tests
# ============================================================================


def test_upsert_mcp_config_creates_new_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    server_config = build_mcp_server_config(
        command="remembra-mcp",
        url="https://api.remembra.dev",
        api_key="rem_test",
        project="default",
        user_id="user_123",
    )

    config, created, updated = upsert_mcp_config(config_path, server_config)

    assert created is True
    assert updated is False
    assert config["mcpServers"]["remembra"] == server_config


def test_upsert_mcp_config_preserves_existing_servers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    existing_config = {
        "mcpServers": {
            "other-server": {"command": "other-cmd"},
        },
        "otherSetting": True,
    }
    config_path.write_text(json.dumps(existing_config))

    server_config = build_mcp_server_config(
        command="remembra-mcp",
        url="https://api.remembra.dev",
        api_key="rem_test",
        project="default",
        user_id="user_123",
    )

    config, created, updated = upsert_mcp_config(config_path, server_config)

    assert created is False
    assert updated is False
    assert "other-server" in config["mcpServers"]
    assert "remembra" in config["mcpServers"]
    assert config["otherSetting"] is True


def test_upsert_mcp_config_updates_existing_remembra(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    existing_config = {
        "mcpServers": {
            "remembra": {"command": "old-command"},
        },
    }
    config_path.write_text(json.dumps(existing_config))

    server_config = build_mcp_server_config(
        command="remembra-mcp",
        url="https://api.remembra.dev",
        api_key="rem_new",
        project="default",
        user_id="user_123",
    )

    config, created, updated = upsert_mcp_config(config_path, server_config)

    assert created is False
    assert updated is True
    assert config["mcpServers"]["remembra"]["command"] == "remembra-mcp"


# ============================================================================
# Install Agent Config Tests
# ============================================================================


def test_install_agent_config_creates_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"

    result = install_agent_config(
        "claude-desktop",
        config_path,
        api_key="rem_test",
        project="clawdbot",
        user_id="user_123",
        url="http://127.0.0.1:9819",
    )

    assert result.agent == "claude-desktop"
    assert result.config_path == config_path
    assert result.created is True
    assert config_path.exists()

    content = json.loads(config_path.read_text())
    assert content["mcpServers"]["remembra"]["command"] == "remembra-mcp"
    assert content["mcpServers"]["remembra"]["env"]["REMEMBRA_URL"] == "http://127.0.0.1:9819"


def test_install_agent_config_unknown_agent_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown agent"):
        install_agent_config(
            "unknown-agent",
            api_key="rem_test",
            project="default",
            user_id="user_123",
        )


# ============================================================================
# Install All Agents Tests
# ============================================================================


def test_install_all_agents_installs_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Create fake agent directories
    claude_dir = tmp_path / "Claude"
    claude_dir.mkdir()
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()

    # Patch the config paths
    monkeypatch.setitem(AGENT_CONFIGS, "claude-desktop", claude_dir / "claude_desktop_config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "cursor", cursor_dir / "mcp.json")
    # Remove other agents by setting non-existent paths
    monkeypatch.setitem(AGENT_CONFIGS, "claude-code", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "gemini", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "windsurf", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "codex", tmp_path / "nonexistent" / "config.toml")

    results = install_all_agents(
        api_key="rem_test",
        project="default",
        user_id="user_123",
    )

    assert len(results) == 2
    agents = {r.agent for r in results}
    assert agents == {"claude-desktop", "cursor"}


# ============================================================================
# Detect Agents Tests
# ============================================================================


def test_detect_agents_finds_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Create fake agent directory
    claude_dir = tmp_path / "Claude"
    claude_dir.mkdir()

    monkeypatch.setitem(AGENT_CONFIGS, "claude-desktop", claude_dir / "claude_desktop_config.json")
    # Others don't exist
    monkeypatch.setitem(AGENT_CONFIGS, "claude-code", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "gemini", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "cursor", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "windsurf", tmp_path / "nonexistent" / "config.json")
    monkeypatch.setitem(AGENT_CONFIGS, "codex", tmp_path / "nonexistent" / "config.toml")

    detected = detect_agents()

    assert detected == ["claude-desktop"]


def test_detect_agents_empty_when_none_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # All paths nonexistent
    for agent in AGENT_CONFIGS:
        monkeypatch.setitem(AGENT_CONFIGS, agent, tmp_path / "nonexistent" / f"{agent}.json")

    detected = detect_agents()

    assert detected == []


# ============================================================================
# Codex (TOML config) Tests
# ============================================================================


def test_codex_is_one_of_the_agents_all_configures() -> None:
    assert "codex" in AGENT_CONFIGS
    assert AGENT_CONFIGS["codex"].name == "config.toml"


def test_install_agent_config_writes_codex_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A sandboxed shell must not flip remembra-install into bridge mode.
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('model = "gpt-5"\n')

    result = install_agent_config(
        "codex",
        config_path,
        api_key="rem_test",
        project="invoices",
        user_id="user_123",
        url="https://api.remembra.dev",
    )

    content = config_path.read_text()
    assert content.startswith('model = "gpt-5"')
    assert "[mcp_servers.remembra]" in content
    assert 'command = "remembra-mcp"' in content
    assert 'REMEMBRA_URL = "https://api.remembra.dev"' in content
    assert 'REMEMBRA_API_KEY = "rem_test"' in content
    assert 'REMEMBRA_PROJECT = "invoices"' in content
    assert result.agent == "codex"
    assert result.url == "https://api.remembra.dev"
    assert result.created is False
    assert result.updated is False

    again = install_agent_config("codex", config_path, api_key="rem_new", project="invoices", user_id="user_123")
    content = config_path.read_text()
    assert again.updated is True
    assert content.count("[mcp_servers.remembra]") == 1
    assert 'REMEMBRA_API_KEY = "rem_new"' in content
    assert "rem_test" not in content


def test_install_agent_config_codex_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="api_key"):
        install_agent_config("codex", tmp_path / "config.toml", api_key="", project="default", user_id="u")


def test_install_all_agents_includes_codex_when_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    for agent in AGENT_CONFIGS:
        monkeypatch.setitem(AGENT_CONFIGS, agent, tmp_path / "nonexistent" / f"{agent}.json")
    monkeypatch.setitem(AGENT_CONFIGS, "codex", codex_dir / "config.toml")

    results = install_all_agents(api_key="rem_test", project="default", user_id="user_123")

    assert [r.agent for r in results] == ["codex"]
    assert results[0].created is True
    assert "[mcp_servers.remembra]" in (codex_dir / "config.toml").read_text()
    assert detect_agents() == ["codex"]
