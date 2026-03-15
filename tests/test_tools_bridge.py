"""Tests for the local bridge proxy."""

from __future__ import annotations

import json
import threading

import httpx

from remembra.tools.bridge import BridgeConfig, RemembraBridgeServer


def test_bridge_forwards_request_and_injects_api_key() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["api_key"] = request.headers["X-API-Key"]
        body = json.loads(request.content.decode())
        return httpx.Response(200, json={"echo": body, "status": "ok"})

    server = RemembraBridgeServer(
        BridgeConfig(
            upstream="https://api.remembra.dev",
            host="127.0.0.1",
            port=0,
            api_key="rem_test",
        ),
        transport=httpx.MockTransport(handler),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    port = server.server_address[1]
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/v1/memories/recall?source=test",
            json={"query": "hello"},
        )
        assert response.status_code == 200
        assert response.json() == {"echo": {"query": "hello"}, "status": "ok"}
        assert seen == {
            "path": "/api/v1/memories/recall",
            "query": "source=test",
            "api_key": "rem_test",
        }
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_bridge_preserves_client_api_key_when_not_configured() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api_key"] = request.headers["X-API-Key"]
        return httpx.Response(200, json={"status": "ok"})

    server = RemembraBridgeServer(
        BridgeConfig(upstream="https://api.remembra.dev", host="127.0.0.1", port=0),
        transport=httpx.MockTransport(handler),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    port = server.server_address[1]
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            headers={"X-API-Key": "from-client"},
        )
        assert response.status_code == 200
        assert seen["api_key"] == "from-client"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
