"""Local bridge for sandboxed agents that cannot reach the cloud directly."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from dataclasses import dataclass
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import httpx

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 8765
DEFAULT_BRIDGE_UPSTREAM = "https://api.remembra.dev"
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


@dataclass(slots=True)
class BridgeConfig:
    """Runtime configuration for the local bridge."""

    upstream: str
    host: str = DEFAULT_BRIDGE_HOST
    port: int = DEFAULT_BRIDGE_PORT
    api_key: str | None = None
    timeout: float = 30.0

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
        headers = build_forward_headers(self.headers, api_key=server.config.api_key)

        try:
            response = server.client.request(
                method=self.command,
                url=path,
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            self._write_json_error(502, str(exc))
            return

        payload = response.content
        self.send_response(response.status_code)
        for header, value in response.headers.items():
            if header.lower() in HOP_BY_HOP_HEADERS:
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


def build_forward_headers(headers: HTTPMessage, *, api_key: str | None) -> dict[str, str]:
    """Prepare upstream headers while stripping hop-by-hop ones."""
    forwarded: dict[str, str] = {}
    for header, value in headers.items():
        if header.lower() in HOP_BY_HOP_HEADERS:
            continue
        forwarded[header] = value

    if api_key:
        forwarded["X-API-Key"] = api_key
    return forwarded


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

    config = BridgeConfig(
        upstream=args.upstream,
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        timeout=args.timeout,
    )
    server = RemembraBridgeServer(config)

    print(f"Remembra bridge listening on {config.base_url}")
    print(f"Forwarding to {config.upstream}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

