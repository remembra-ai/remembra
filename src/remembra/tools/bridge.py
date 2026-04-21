"""Local bridge for sandboxed agents that cannot reach the cloud directly."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import sys
import time
from dataclasses import dataclass
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx

try:
    from remembra import __version__ as REMEMBRA_VERSION
except Exception:  # pragma: no cover - fallback for partial installs
    REMEMBRA_VERSION = "dev"

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 9819
DEFAULT_BRIDGE_UPSTREAM = "https://api.remembra.dev"
DEFAULT_PID_FILE = Path.home() / ".remembra" / "bridge.pid"
DEFAULT_BRIDGE_AGENT_NAME = "codex"
FORWARDED_AGENT_HEADER = "X-Remembra-Agent"
FORWARDED_BRIDGE_HEADER = "X-Remembra-Bridge"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
RESPONSE_HEADERS_TO_STRIP = {
    "content-encoding",
}


class BridgePortInUseError(Exception):
    """Raised when the bridge port is already in use."""


class BridgeStartupError(Exception):
    """Raised when the bridge fails to start or become healthy."""


def check_port_available(host: str, port: int) -> None:
    """Check if a port is available for binding.

    Raises:
        BridgePortInUseError: If the port is already in use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise BridgePortInUseError(
                f"Port {port} is already in use on {host}. "
                f"Stop the existing bridge with 'remembra-bridge --stop' "
                f"or use a different port with '--port'."
            ) from exc


def write_pid_file(pid: int, pid_file: Path = DEFAULT_PID_FILE) -> None:
    """Write the bridge PID to a file for later cleanup."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid))


def read_pid_file(pid_file: Path = DEFAULT_PID_FILE) -> int | None:
    """Read the bridge PID from the file, or None if not found."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid_file(pid_file: Path = DEFAULT_PID_FILE) -> None:
    """Remove the PID file."""
    import contextlib

    with contextlib.suppress(OSError):
        pid_file.unlink(missing_ok=True)


def is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def stop_bridge(pid_file: Path = DEFAULT_PID_FILE) -> bool:
    """Stop a running bridge using the PID file.

    Returns:
        True if a bridge was stopped, False if none was running.
    """
    pid = read_pid_file(pid_file)
    if pid is None:
        return False

    if not is_process_running(pid):
        remove_pid_file(pid_file)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 3 seconds for graceful shutdown
        for _ in range(30):
            if not is_process_running(pid):
                break
            time.sleep(0.1)
        else:
            # Force kill if still running
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    remove_pid_file(pid_file)
    return True


def wait_for_healthy(
    host: str,
    port: int,
    timeout: float = 5.0,
    interval: float = 0.2,
) -> bool:
    """Wait for the bridge to become healthy.

    Returns:
        True if healthy, False if timeout reached.
    """
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=1.0) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    return True
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(interval)

    return False


@dataclass(slots=True)
class BridgeConfig:
    """Runtime configuration for the local bridge."""

    upstream: str
    host: str = DEFAULT_BRIDGE_HOST
    port: int = DEFAULT_BRIDGE_PORT
    api_key: str | None = None
    timeout: float = 30.0
    agent_name: str = DEFAULT_BRIDGE_AGENT_NAME

    @property
    def base_url(self) -> str:
        """Return the bridge URL advertised to sandboxed agents."""
        return f"http://{self.host}:{self.port}"


class RemembraBridgeServer(ThreadingHTTPServer):
    """HTTP bridge that forwards requests to the upstream Remembra API."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        config: BridgeConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.client = httpx.Client(
            base_url=config.upstream.rstrip("/"),
            timeout=config.timeout,
            transport=transport,
        )
        super().__init__((config.host, config.port), BridgeRequestHandler)

    def server_close(self) -> None:
        """Close the upstream client before closing the socket."""
        self.client.close()
        super().server_close()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    """Proxy requests to the configured upstream Remembra API."""

    protocol_version = "HTTP/1.1"

    def _server(self) -> RemembraBridgeServer:
        return cast(RemembraBridgeServer, self.server)

    def _forward(self) -> None:
        server = self._server()
        body = self._read_body()
        path = self.path if self.path.startswith("/") else f"/{self.path}"

        try:
            response = forward_upstream_request(
                client=server.client,
                method=self.command,
                path=path,
                headers=self.headers,
                body=body,
                api_key=server.config.api_key,
                agent_name=server.config.agent_name,
            )
        except httpx.HTTPError as exc:
            self._write_json_error(502, str(exc))
            return

        payload = response.content
        self.send_response(response.status_code)
        for header, value in response.headers.items():
            if header.lower() in HOP_BY_HOP_HEADERS | RESPONSE_HEADERS_TO_STRIP:
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _write_json_error(self, status_code: int, message: str) -> None:
        payload = json.dumps({"status": "error", "error": message}).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        """Keep the bridge quiet by default."""


def build_bridge_user_agent(agent_name: str) -> str:
    """Build a deterministic user agent for Cloudflare allow rules."""
    return f"Remembra-Bridge/{REMEMBRA_VERSION} ({agent_name}; +https://remembra.dev)"


def build_forward_headers(
    headers: HTTPMessage,
    *,
    api_key: str | None,
    agent_name: str,
) -> dict[str, str]:
    """Prepare upstream headers while stripping hop-by-hop ones."""
    forwarded: dict[str, str] = {}
    for header, value in headers.items():
        if header.lower() in HOP_BY_HOP_HEADERS:
            continue
        forwarded[header] = value

    if api_key:
        forwarded["X-API-Key"] = api_key
    forwarded["User-Agent"] = build_bridge_user_agent(agent_name)
    forwarded[FORWARDED_AGENT_HEADER] = agent_name
    forwarded[FORWARDED_BRIDGE_HEADER] = "true"
    return forwarded


def forward_upstream_request(
    *,
    client: httpx.Client,
    method: str,
    path: str,
    headers: HTTPMessage,
    body: bytes | None,
    api_key: str | None,
    agent_name: str = DEFAULT_BRIDGE_AGENT_NAME,
) -> httpx.Response:
    """Forward a request to the upstream Remembra API."""
    forwarded_headers = build_forward_headers(
        headers,
        api_key=api_key,
        agent_name=agent_name,
    )
    return client.request(
        method=method,
        url=path,
        content=body,
        headers=forwarded_headers,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the bridge."""
    parser = argparse.ArgumentParser(description="Start the local Remembra bridge.")
    parser.add_argument(
        "--upstream",
        default=DEFAULT_BRIDGE_UPSTREAM,
        help="Upstream Remembra API URL",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_BRIDGE_HOST,
        help="Local host interface to bind",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_BRIDGE_PORT,
        help="Local port to bind",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("REMEMBRA_API_KEY"),
        help="API key to inject into upstream requests",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Upstream request timeout in seconds",
    )
    parser.add_argument(
        "--agent-name",
        default=os.environ.get("REMEMBRA_AGENT_NAME", DEFAULT_BRIDGE_AGENT_NAME),
        help="Agent name to stamp on forwarded requests",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop a running bridge (using PID file)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check if a bridge is running",
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_PID_FILE,
        help="Path to the PID file",
    )
    return parser


def parse_bridge_command(command: str | None) -> list[str]:
    """Resolve a bridge command string into argv form."""
    if not command:
        return ["remembra-bridge"]
    return shlex.split(command)


def main() -> None:
    """CLI entry point for the local bridge."""
    parser = build_parser()
    args = parser.parse_args()

    # Handle --stop
    if args.stop:
        if stop_bridge(args.pid_file):
            print("Bridge stopped.")
        else:
            print("No running bridge found.")
        return

    # Handle --status
    if args.status:
        pid = read_pid_file(args.pid_file)
        if pid and is_process_running(pid):
            print(f"Bridge is running (PID {pid})")
            # Try to get health
            if wait_for_healthy(args.host, args.port, timeout=1.0):
                print(f"  Listening on http://{args.host}:{args.port}")
                print("  Status: healthy")
            else:
                print("  Status: not responding")
        else:
            print("Bridge is not running.")
            if pid:
                remove_pid_file(args.pid_file)
        return

    # Check if port is available before starting
    try:
        check_port_available(args.host, args.port)
    except BridgePortInUseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    config = BridgeConfig(
        upstream=args.upstream,
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        timeout=args.timeout,
        agent_name=args.agent_name,
    )

    # Write PID file
    write_pid_file(os.getpid(), args.pid_file)

    server = RemembraBridgeServer(config)

    print(f"Remembra bridge listening on {config.base_url}")
    print(f"Forwarding to {config.upstream}")
    print(f"PID file: {args.pid_file}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        remove_pid_file(args.pid_file)


if __name__ == "__main__":
    main()
