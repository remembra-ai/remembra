"""Tests for the local bridge proxy."""

from __future__ import annotations

from email.message import Message
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from remembra.tools.bridge import (
    BridgePortInUseError,
    FORWARDED_AGENT_HEADER,
    FORWARDED_BRIDGE_HEADER,
    BridgeConfig,
    BridgeRequestHandler,
    build_forward_headers,
    build_bridge_user_agent,
    check_port_available,
    forward_upstream_request,
    is_process_running,
    read_pid_file,
    remove_pid_file,
    stop_bridge,
    write_pid_file,
)


def test_forward_upstream_request_injects_api_key_and_forwards_json() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["api_key"] = request.headers["X-API-Key"]
        seen["user_agent"] = request.headers["User-Agent"]
        seen["agent_name"] = request.headers[FORWARDED_AGENT_HEADER]
        seen["bridge"] = request.headers[FORWARDED_BRIDGE_HEADER]
        body = json.loads(request.content.decode())
        return httpx.Response(200, json={"echo": body, "status": "ok"})

    headers = Message()
    headers["Content-Type"] = "application/json"
    body = json.dumps({"query": "hello"}).encode()

    with httpx.Client(
        base_url="https://api.remembra.dev",
        transport=httpx.MockTransport(handler),
    ) as client:
        response = forward_upstream_request(
            client=client,
            method="POST",
            path="/api/v1/memories/recall?source=test",
            headers=headers,
            body=body,
            api_key="rem_test",
            agent_name="codex",
        )

    assert response.status_code == 200
    assert response.json() == {"echo": {"query": "hello"}, "status": "ok"}
    assert seen == {
        "path": "/api/v1/memories/recall",
        "query": "source=test",
        "api_key": "rem_test",
        "user_agent": build_bridge_user_agent("codex"),
        "agent_name": "codex",
        "bridge": "true",
    }


def test_build_forward_headers_preserves_client_api_key_when_not_configured() -> None:
    headers = Message()
    headers["X-API-Key"] = "from-client"
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "Python-urllib/3.13"

    forwarded = build_forward_headers(headers, api_key=None, agent_name="codex")

    assert forwarded["X-API-Key"] == "from-client"
    assert forwarded["Content-Type"] == "application/json"
    assert forwarded["User-Agent"] == build_bridge_user_agent("codex")
    assert forwarded[FORWARDED_AGENT_HEADER] == "codex"
    assert forwarded[FORWARDED_BRIDGE_HEADER] == "true"


def test_check_port_available_raises_on_port_in_use() -> None:
    """Test that check_port_available raises BridgePortInUseError when port is busy."""
    port = 9819
    mock_socket = MagicMock()
    mock_socket.bind.side_effect = OSError("Address already in use")
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_socket

    with patch("remembra.tools.bridge.socket.socket", return_value=mock_context):
        with pytest.raises(BridgePortInUseError) as exc_info:
            check_port_available("127.0.0.1", port)
        assert str(port) in str(exc_info.value)
        assert "already in use" in str(exc_info.value)


def test_pid_file_operations(tmp_path: Path) -> None:
    """Test PID file write/read/remove operations."""
    pid_file = tmp_path / "test.pid"

    # Initially no PID file
    assert read_pid_file(pid_file) is None

    # Write and read back
    write_pid_file(12345, pid_file)
    assert read_pid_file(pid_file) == 12345

    # Remove
    remove_pid_file(pid_file)
    assert not pid_file.exists()
    assert read_pid_file(pid_file) is None


def test_stop_bridge_returns_false_when_no_pid_file(tmp_path: Path) -> None:
    """Test stop_bridge returns False when no PID file exists."""
    pid_file = tmp_path / "nonexistent.pid"
    assert stop_bridge(pid_file) is False


def test_stop_bridge_cleans_up_stale_pid_file(tmp_path: Path) -> None:
    """Test stop_bridge cleans up stale PID file for non-running process."""
    pid_file = tmp_path / "stale.pid"
    # Write a PID that doesn't exist (very high number)
    write_pid_file(999999999, pid_file)

    with patch("remembra.tools.bridge.is_process_running", return_value=False):
        result = stop_bridge(pid_file)

    assert result is False
    assert not pid_file.exists()


def test_is_process_running_returns_false_for_invalid_pid() -> None:
    """Test is_process_running returns False for non-existent process."""
    assert is_process_running(999999999) is False


def test_bridge_strips_content_encoding_when_relaying_decompressed_payload() -> None:
    handler = BridgeRequestHandler.__new__(BridgeRequestHandler)
    handler.server = SimpleNamespace(
        client=MagicMock(),
        config=BridgeConfig(upstream="https://api.remembra.dev"),
    )
    handler.command = "GET"
    handler.path = "/health"
    handler.headers = Message()
    handler.rfile = MagicMock()
    handler.wfile = MagicMock()
    sent_headers: list[tuple[str, str]] = []

    handler.send_response = MagicMock()
    handler.send_header = MagicMock(side_effect=lambda k, v: sent_headers.append((k, v)))
    handler.end_headers = MagicMock()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.content = b'{"status":"ok"}'
    fake_response.headers.items.return_value = [
        ("Content-Type", "application/json"),
        ("Content-Encoding", "gzip"),
        ("X-Test", "1"),
    ]

    with patch("remembra.tools.bridge.forward_upstream_request", return_value=fake_response):
        handler._forward()

    assert ("Content-Encoding", "gzip") not in sent_headers
    assert ("Content-Type", "application/json") in sent_headers
    assert ("X-Test", "1") in sent_headers
