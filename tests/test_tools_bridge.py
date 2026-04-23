"""Tests for the local bridge proxy."""

from __future__ import annotations

from email.message import Message
import json
import os
import socket
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
    DoctorReport,
    build_forward_headers,
    build_bridge_user_agent,
    check_port_available,
    find_pid_on_port,
    forward_upstream_request,
    format_doctor_report,
    is_port_listening,
    is_process_running,
    read_pid_file,
    remove_pid_file,
    run_doctor,
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


# ---------------------------------------------------------------------------
# Issue #10 regression tests — bridge zombie state recovery
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_is_port_listening_true_when_bound() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        _, port = sock.getsockname()
        assert is_port_listening("127.0.0.1", port) is True


def test_is_port_listening_false_when_unused() -> None:
    port = _pick_free_port()
    assert is_port_listening("127.0.0.1", port) is False


def test_find_pid_on_port_returns_none_when_unused() -> None:
    port = _pick_free_port()
    assert find_pid_on_port(port) is None


def test_find_pid_on_port_finds_current_process() -> None:
    """lsof should be able to see a socket this process is holding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        _, port = sock.getsockname()
        pid = find_pid_on_port(port)
        # Best-effort: lsof might not be installed in every CI env;
        # treat None as "can't tell" rather than a failure.
        if pid is not None:
            assert pid == os.getpid()


def test_check_port_available_mentions_holder_when_resolvable(tmp_path: Path) -> None:
    """BridgePortInUseError should name the PID when find_pid_on_port returns one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        _, port = sock.getsockname()

        with patch("remembra.tools.bridge.find_pid_on_port", return_value=12345):
            with pytest.raises(BridgePortInUseError) as exc_info:
                check_port_available("127.0.0.1", port)

    msg = str(exc_info.value)
    assert "12345" in msg
    assert "--stop --force" in msg


def test_check_port_available_fallback_when_holder_unknown(tmp_path: Path) -> None:
    """When find_pid_on_port returns None, the error still suggests --stop --force."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        _, port = sock.getsockname()

        with patch("remembra.tools.bridge.find_pid_on_port", return_value=None):
            with pytest.raises(BridgePortInUseError) as exc_info:
                check_port_available("127.0.0.1", port)

    msg = str(exc_info.value)
    assert "already in use" in msg
    assert "--stop --force" in msg


def test_stop_bridge_force_kills_orphan_when_no_pidfile(tmp_path: Path) -> None:
    """force=True + orphan + no pidfile → kill the orphan."""
    pid_file = tmp_path / "no.pid"
    terminated: list[int] = []

    def fake_terminate(pid: int) -> bool:
        terminated.append(pid)
        return True

    with patch("remembra.tools.bridge.find_pid_on_port", return_value=4321), \
         patch("remembra.tools.bridge._terminate_pid", side_effect=fake_terminate):
        result = stop_bridge(pid_file, port=9819, force=True)

    assert result is True
    assert terminated == [4321]


def test_stop_bridge_force_handles_matching_pid_without_double_kill(tmp_path: Path) -> None:
    """If pidfile PID equals port holder, don't terminate twice."""
    pid_file = tmp_path / "match.pid"
    write_pid_file(7777, pid_file)
    terminated: list[int] = []

    def fake_terminate(pid: int) -> bool:
        terminated.append(pid)
        return True

    with patch("remembra.tools.bridge.is_process_running", return_value=True), \
         patch("remembra.tools.bridge.find_pid_on_port", return_value=7777), \
         patch("remembra.tools.bridge._terminate_pid", side_effect=fake_terminate):
        result = stop_bridge(pid_file, port=9819, force=True)

    assert result is True
    assert terminated == [7777]  # not called twice


def test_stop_bridge_default_mode_ignores_orphan(tmp_path: Path) -> None:
    """Without --force, orphans must NOT be killed (safety)."""
    pid_file = tmp_path / "no.pid"
    terminated: list[int] = []

    with patch("remembra.tools.bridge.find_pid_on_port", return_value=9999), \
         patch("remembra.tools.bridge._terminate_pid",
               side_effect=lambda p: terminated.append(p) or True):
        result = stop_bridge(pid_file, port=9819, force=False)

    assert result is False
    assert terminated == []


def test_run_doctor_healthy_state(tmp_path: Path) -> None:
    """Healthy: pidfile alive, port listening, port pid matches, /health ok."""
    pid_file = tmp_path / "good.pid"
    write_pid_file(12345, pid_file)

    fake_resp = MagicMock()
    fake_resp.status_code = 200

    with patch("remembra.tools.bridge.is_process_running", return_value=True), \
         patch("remembra.tools.bridge.is_port_listening", return_value=True), \
         patch("remembra.tools.bridge.find_pid_on_port", return_value=12345), \
         patch("httpx.Client") as httpx_client:
        httpx_client.return_value.__enter__.return_value.get.return_value = fake_resp
        report = run_doctor("127.0.0.1", 9819, pid_file)

    assert report.healthy is True
    assert report.has_orphan is False
    assert report.pidfile_pid == 12345
    assert report.port_pid == 12345


def test_run_doctor_detects_orphan(tmp_path: Path) -> None:
    """Orphan: port is held but pidfile is missing."""
    pid_file = tmp_path / "ghost.pid"  # does not exist

    with patch("remembra.tools.bridge.is_port_listening", return_value=True), \
         patch("remembra.tools.bridge.find_pid_on_port", return_value=1329), \
         patch("httpx.Client"):
        report = run_doctor("127.0.0.1", 9819, pid_file)

    assert report.healthy is False
    assert report.has_orphan is True
    assert report.port_pid == 1329
    assert report.pidfile_pid is None


def test_run_doctor_detects_pidfile_mismatch(tmp_path: Path) -> None:
    """Mismatch: pidfile says X, but port is held by Y."""
    pid_file = tmp_path / "mismatch.pid"
    write_pid_file(111, pid_file)

    fake_resp = MagicMock()
    fake_resp.status_code = 200

    with patch("remembra.tools.bridge.is_process_running", return_value=True), \
         patch("remembra.tools.bridge.is_port_listening", return_value=True), \
         patch("remembra.tools.bridge.find_pid_on_port", return_value=222), \
         patch("httpx.Client") as httpx_client:
        httpx_client.return_value.__enter__.return_value.get.return_value = fake_resp
        report = run_doctor("127.0.0.1", 9819, pid_file)

    assert report.has_orphan is True
    assert report.healthy is False


def test_run_doctor_not_running(tmp_path: Path) -> None:
    """Not running: no pidfile, no listener."""
    pid_file = tmp_path / "gone.pid"

    with patch("remembra.tools.bridge.is_port_listening", return_value=False):
        report = run_doctor("127.0.0.1", 9819, pid_file)

    assert report.healthy is False
    assert report.has_orphan is False
    assert report.pidfile_pid is None
    assert report.port_listening is False


def test_format_doctor_report_healthy(tmp_path: Path) -> None:
    report = DoctorReport(
        pidfile_pid=12345,
        pidfile_process_alive=True,
        port_pid=12345,
        port_listening=True,
        health_ok=True,
        health_error=None,
    )
    text = format_doctor_report(report, "127.0.0.1", 9819, tmp_path / "p.pid")
    assert "HEALTHY" in text
    assert "12345" in text


def test_format_doctor_report_orphan_suggests_force(tmp_path: Path) -> None:
    report = DoctorReport(
        pidfile_pid=None,
        pidfile_process_alive=False,
        port_pid=1329,
        port_listening=True,
        health_ok=True,
        health_error=None,
    )
    text = format_doctor_report(report, "127.0.0.1", 9819, tmp_path / "p.pid")
    assert "ORPHAN" in text
    assert "1329" in text
    assert "--stop --force" in text
