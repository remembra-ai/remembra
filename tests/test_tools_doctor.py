"""Tests for doctor diagnostics across all agents."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from remembra.tools.doctor import (
    classify_http_error,
    doctor_all,
    doctor_claude_code,
    doctor_claude_desktop,
    doctor_cursor,
    doctor_gemini,
    doctor_windsurf,
    load_claude_code_target,
    load_claude_desktop_target,
    load_codex_target,
    load_cursor_target,
    load_gemini_target,
    load_json_agent_target,
    load_windsurf_target,
    run_remote_checks,
)
from remembra.tools.codex import build_codex_mcp_block


# ============================================================================
# JSON Agent Config Fixtures
# ============================================================================


def build_json_mcp_config(
    command: str = "remembra-mcp",
    url: str = "https://api.remembra.dev",
    api_key: str = "rem_test",
    project: str = "default",
    user_id: str = "user_123",
) -> str:
    """Build a JSON MCP config for testing."""
    return json.dumps(
        {
            "mcpServers": {
                "remembra": {
                    "command": command,
                    "env": {
                        "REMEMBRA_URL": url,
                        "REMEMBRA_API_KEY": api_key,
                        "REMEMBRA_PROJECT": project,
                        "REMEMBRA_USER_ID": user_id,
                    },
                }
            }
        },
        indent=2,
    )


# ============================================================================
# JSON Agent Loading Tests
# ============================================================================


@pytest.mark.parametrize(
    "agent,loader",
    [
        ("claude-desktop", load_claude_desktop_target),
        ("claude-code", load_claude_code_target),
        ("gemini", load_gemini_target),
        ("cursor", load_cursor_target),
        ("windsurf", load_windsurf_target),
    ],
)
def test_load_json_agent_target_parses_config(
    tmp_path: Path, agent: str, loader: callable
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        build_json_mcp_config(
            command="remembra-mcp",
            url="https://api.remembra.dev",
            api_key="rem_test",
            project="clawdbot",
            user_id="user_123",
        )
    )

    target = loader(config_path)

    assert target.agent == agent
    assert target.command == "remembra-mcp"
    assert target.url == "https://api.remembra.dev"
    assert target.api_key == "rem_test"
    assert target.project == "clawdbot"
    assert target.user_id == "user_123"


def test_load_json_agent_target_missing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "nonexistent.json"

    with pytest.raises(Exception) as exc_info:
        load_json_agent_target("test", config_path)

    assert "config_missing" in str(exc_info.value)


def test_load_json_agent_target_invalid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{ invalid json }")

    with pytest.raises(Exception) as exc_info:
        load_json_agent_target("test", config_path)

    assert "invalid_config" in str(exc_info.value)


def test_load_json_agent_target_missing_server(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))

    with pytest.raises(Exception) as exc_info:
        load_json_agent_target("test", config_path)

    assert "missing_server" in str(exc_info.value)


def test_load_json_agent_target_missing_command(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"remembra": {"env": {}}}})
    )

    with pytest.raises(Exception) as exc_info:
        load_json_agent_target("test", config_path)

    assert "missing_command" in str(exc_info.value)


# ============================================================================
# Doctor Function Tests
# ============================================================================


@pytest.mark.parametrize(
    "doctor_fn,config_builder",
    [
        (doctor_claude_desktop, build_json_mcp_config),
        (doctor_claude_code, build_json_mcp_config),
        (doctor_gemini, build_json_mcp_config),
        (doctor_cursor, build_json_mcp_config),
        (doctor_windsurf, build_json_mcp_config),
    ],
)
def test_doctor_json_agent_reports_missing_command(
    tmp_path: Path, doctor_fn: callable, config_builder: callable
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        config_builder(command="/does/not/exist")
    )

    results = doctor_fn(config_path)

    assert results[0].status == "pass"  # config loaded
    assert results[1].status == "fail"  # command missing
    assert "command_missing" in results[1].message


def test_doctor_codex_with_mock_transport(tmp_path: Path) -> None:
    """Verify Codex doctor still works with existing tests."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        build_codex_mcp_block(
            command="remembra-mcp",
            url="https://api.remembra.dev",
            api_key="rem_test",
            project="clawdbot",
            user_id="user_123",
        )
    )

    # Mock the remembra-mcp command
    fake_command = tmp_path / "remembra-mcp"
    fake_command.write_text("#!/bin/sh\necho 'mock'")
    fake_command.chmod(0o755)

    # Update config to use fake command
    config_path.write_text(
        build_codex_mcp_block(
            command=str(fake_command),
            url="https://api.remembra.dev",
            api_key="rem_test",
            project="clawdbot",
            user_id="user_123",
        )
    )

    target = load_codex_target(config_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/memories/recall":
            return httpx.Response(200, json={"context": "", "memories": [], "entities": []})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    results = run_remote_checks(target, transport=transport)

    assert [result.status for result in results] == ["pass", "pass"]


# ============================================================================
# Remote Checks Tests
# ============================================================================


def test_run_remote_checks_handles_auth_failure(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(build_json_mcp_config())

    target = load_json_agent_target("test", config_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    results = run_remote_checks(target, transport=transport)

    assert results[0].status == "fail"
    assert "auth_failure" in results[0].message


def test_run_remote_checks_handles_missing_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remembra": {
                        "command": "remembra-mcp",
                        "env": {
                            "REMEMBRA_API_KEY": "rem_test",
                        },
                    }
                }
            }
        )
    )

    target = load_json_agent_target("test", config_path)
    results = run_remote_checks(target)

    assert results[0].status == "warn"
    assert "REMEMBRA_URL missing" in results[0].message


# ============================================================================
# Error Classification Tests
# ============================================================================


def test_classify_http_error_maps_dns_failures() -> None:
    assert classify_http_error(RuntimeError("nodename nor servname provided")) == "dns_failure"
    assert classify_http_error(RuntimeError("Name or service not known")) == "dns_failure"
    err = RuntimeError("Temporary failure in name resolution")
    assert classify_http_error(err) == "dns_failure"


def test_classify_http_error_maps_sandbox_blocked() -> None:
    assert classify_http_error(RuntimeError("Operation not permitted")) == "sandbox_blocked"


def test_classify_http_error_maps_timeout() -> None:
    assert classify_http_error(httpx.TimeoutException("timed out")) == "timeout"


def test_classify_http_error_maps_connect_error() -> None:
    assert classify_http_error(httpx.ConnectError("connection refused")) == "upstream_unreachable"


# ============================================================================
# Doctor All Tests
# ============================================================================


def test_doctor_all_returns_empty_when_no_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """doctor_all should return empty dict when no agent configs exist."""
    # Monkeypatch all default paths to nonexistent locations
    monkeypatch.setattr(
        "remembra.tools.doctor.DEFAULT_CODEX_CONFIG",
        tmp_path / "codex" / "config.toml",
    )
    monkeypatch.setattr(
        "remembra.tools.doctor.DEFAULT_CLAUDE_DESKTOP_CONFIG",
        tmp_path / "claude" / "config.json",
    )
    monkeypatch.setattr(
        "remembra.tools.doctor.DEFAULT_CLAUDE_CODE_CONFIG",
        tmp_path / "claude-code" / "config.json",
    )
    monkeypatch.setattr(
        "remembra.tools.doctor.DEFAULT_GEMINI_CONFIG",
        tmp_path / "gemini" / "config.json",
    )
    monkeypatch.setattr(
        "remembra.tools.doctor.DEFAULT_CURSOR_CONFIG",
        tmp_path / "cursor" / "config.json",
    )
    monkeypatch.setattr(
        "remembra.tools.doctor.DEFAULT_WINDSURF_CONFIG",
        tmp_path / "windsurf" / "config.json",
    )

    results = doctor_all()

    assert results == {}
