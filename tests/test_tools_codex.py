"""Tests for Codex installation and diagnostics helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx

from remembra.tools.codex import (
    build_codex_mcp_block,
    install_codex_config,
    start_bridge_background,
    upsert_codex_mcp_block,
)
from remembra.tools.doctor import (
    classify_http_error,
    doctor_codex,
    load_codex_target,
    run_remote_checks,
)


def test_upsert_codex_mcp_block_replaces_existing_block() -> None:
    original = """
model = "gpt-5"

[mcp_servers.remembra]
command = "old-command"

[mcp_servers.remembra.env]
REMEMBRA_URL = "https://old.example"

[notice]
hide = true
""".strip()
    replacement = build_codex_mcp_block(
        command="remembra-mcp",
        url="https://api.remembra.dev",
        api_key="rem_test",
        project="clawdbot",
        user_id="user_123",
    )

    updated = upsert_codex_mcp_block(original, replacement)

    assert updated.count("[mcp_servers.remembra]") == 1
    assert 'command = "remembra-mcp"' in updated
    assert 'REMEMBRA_PROJECT = "clawdbot"' in updated
    assert "[notice]" in updated


def test_install_codex_config_creates_expected_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = install_codex_config(
        config_path,
        api_key="rem_test",
        project="clawdbot",
        user_id="user_123",
        command="/tmp/remembra-mcp",
        url="https://api.remembra.dev",
    )

    content = config_path.read_text()
    assert result.config_path == config_path
    assert 'command = "/tmp/remembra-mcp"' in content
    assert 'REMEMBRA_URL = "http://127.0.0.1:8765"' in content
    assert 'REMEMBRA_API_KEY = "rem_test"' not in content
    assert result.bridge_enabled is True


def test_install_codex_config_can_disable_bridge(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    install_codex_config(
        config_path,
        api_key="rem_test",
        project="clawdbot",
        user_id="user_123",
        url="https://api.remembra.dev",
        use_bridge=False,
    )

    content = config_path.read_text()
    assert 'REMEMBRA_URL = "https://api.remembra.dev"' in content
    assert 'REMEMBRA_API_KEY = "rem_test"' in content


def test_load_codex_target_parses_nested_toml(tmp_path: Path) -> None:
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

    target = load_codex_target(config_path)

    assert target.command == "remembra-mcp"
    assert target.url == "https://api.remembra.dev"
    assert target.api_key == "rem_test"
    assert target.project == "clawdbot"
    assert target.user_id == "user_123"


def test_run_remote_checks_passes_with_mock_transport(tmp_path: Path) -> None:
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


def test_doctor_codex_reports_missing_command(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        build_codex_mcp_block(
            command="/does/not/exist",
            url="https://api.remembra.dev",
            api_key="rem_test",
            project="clawdbot",
            user_id="user_123",
        )
    )

    results = doctor_codex(config_path)

    assert results[0].status == "pass"
    assert results[1].status == "fail"
    assert "command_missing" in results[1].message


def test_classify_http_error_maps_dns_and_sandbox() -> None:
    assert classify_http_error(RuntimeError("nodename nor servname provided")) == "dns_failure"
    assert classify_http_error(RuntimeError("Operation not permitted")) == "sandbox_blocked"


def test_start_bridge_background_uses_bridge_command_and_env(tmp_path: Path) -> None:
    stdout_path = tmp_path / "bridge.stdout.log"
    stderr_path = tmp_path / "bridge.stderr.log"

    with patch("remembra.tools.codex.subprocess.Popen") as popen_mock:
        popen_mock.return_value.pid = 321
        pid = start_bridge_background(
            upstream="https://api.remembra.dev",
            port=8765,
            api_key="rem_test",
            command="remembra-bridge",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    assert pid == 321
    argv = popen_mock.call_args.args[0]
    env = popen_mock.call_args.kwargs["env"]
    assert argv[:5] == [
        "remembra-bridge",
        "--upstream",
        "https://api.remembra.dev",
        "--host",
        "127.0.0.1",
    ]
    assert env["REMEMBRA_API_KEY"] == "rem_test"
