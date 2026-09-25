"""SEC-1: WebSocket must authenticate and never leak another tenant's events."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from remembra.api.v1 import memories, websocket
from tests.security_harness import secure_app


def _event(user_id: str, content: str) -> dict:
    return {"memory_id": f"m-{content}", "user_id": user_id, "facts": [content]}


def _drain_until(ws, want_type: str, limit: int = 5) -> dict:
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg["type"] == want_type:
            return msg
    raise AssertionError(f"no {want_type} message")


async def test_unauthenticated_and_forged_credentials_rejected(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        with TestClient(h.app) as client:
            for url in ("/ws", "/ws?api_key=rem_forged_value_000000000000", "/ws?token=not-a-jwt"):
                with client.websocket_connect(url) as ws:
                    if url == "/ws":
                        ws.send_text(json.dumps({"type": "auth", "api_key": "rem_forged_value_000000000000"}))
                    with pytest.raises(WebSocketDisconnect) as exc:
                        ws.receive_text()
                    assert exc.value.code == websocket.CLOSE_UNAUTHORIZED


async def test_events_are_isolated_per_tenant(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        key_a, _ = await h.api_key("tenant-a", "viewer")
        key_b, _ = await h.api_key("tenant-b", "viewer")
        # Both tenants ask for the same client-chosen namespace/project — irrelevant now.
        url = "/ws?namespace=alpha&project_id=alpha"
        with TestClient(h.app) as client:
            ws_a_ctx = client.websocket_connect(url, headers={"X-API-Key": key_a})
            ws_b_ctx = client.websocket_connect(url, headers={"X-API-Key": key_b})
            with ws_a_ctx as ws_a, ws_b_ctx as ws_b:
                assert _drain_until(ws_a, "connected")["data"]["namespace"] == "tenant-a:alpha"
                _drain_until(ws_b, "connected")

                # Real call chain used by the REST handlers.
                client.portal.call(memories._broadcast_websocket, "memory.created", _event("tenant-b", "VICTIM-SECRET"), "alpha")
                client.portal.call(memories._broadcast_websocket, "memory.created", _event("tenant-a", "own"), "alpha")

                got_a = _drain_until(ws_a, "memory.created")
                assert got_a["data"]["user_id"] == "tenant-a"
                assert "VICTIM-SECRET" not in json.dumps(got_a)
                got_b = _drain_until(ws_b, "memory.created")
                assert got_b["data"]["facts"] == ["VICTIM-SECRET"]


async def test_first_message_auth_and_project_restriction(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        scoped, _ = await h.api_key("tenant-a", "viewer", project_ids=["alpha"])
        with TestClient(h.app) as client:
            # Subscribing to a project outside the key's allow-list is refused.
            with client.websocket_connect("/ws?project_id=beta", headers={"X-API-Key": scoped}) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
                assert exc.value.code == websocket.CLOSE_FORBIDDEN

            with client.websocket_connect("/ws") as ws:
                ws.send_text(json.dumps({"type": "auth", "api_key": scoped}))
                _drain_until(ws, "connected")
                ws.send_text(json.dumps({"type": "subscribe", "project_id": "beta"}))
                assert _drain_until(ws, "error")["data"]["project_id"] == "beta"
                # Events for a project the key cannot see are not delivered.
                sent = client.portal.call(
                    lambda: websocket.connection_manager.broadcast(
                        "memory.created", _event("tenant-a", "beta-only"), user_id="tenant-a", project_id="beta"
                    )
                )
                assert sent == 0
                sent = client.portal.call(
                    lambda: websocket.connection_manager.broadcast(
                        "memory.created", _event("tenant-a", "alpha-ok"), user_id="tenant-a", project_id="alpha"
                    )
                )
                assert sent == 1
                assert _drain_until(ws, "memory.created")["data"]["facts"] == ["alpha-ok"]


async def test_broadcast_without_owner_is_dropped(tmp_path):
    manager = websocket.ConnectionManager()
    assert await manager.broadcast("memory.created", {"memory_id": "x"}, user_id=None, project_id="alpha") == 0


async def test_ws_stats_requires_auth_and_is_tenant_scoped(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        assert (await h.client.get("/ws/stats")).status_code == 401
        key, _ = await h.api_key("tenant-a", "viewer")
        r = await h.client.get("/ws/stats", headers={"X-API-Key": key})
        assert r.status_code == 200
        assert set(r.json()) == {"connections"}
