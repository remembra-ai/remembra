"""P-348: an open WebSocket must stop receiving events once its access is cut.

Before this fix the socket was checked only at the handshake. A socket opened
with an API key kept receiving memory events (including the first 100
characters of edited content) after the key was revoked, the account was
deactivated or the session was signed out, until the client disconnected.

Every check runs against the real routers, a real SQLite database and real
API keys / JWTs with authentication enabled. Revocations go through the same
REST routes the dashboard uses, inside the TestClient's event loop.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient

from remembra.api.v1 import auth, keys, websocket
from tests.security_harness import secure_app

PASSWORD = "Str0ng!Passw0rd"


def _next_message(ws: Any, timeout: float = 5.0) -> dict[str, Any]:
    """The next ASGI message the server sent this client; fails instead of hanging."""

    async def _receive() -> dict[str, Any]:
        with anyio.fail_after(timeout):
            message: dict[str, Any] = await ws._send_rx.receive()
            return message

    result: dict[str, Any] = ws.portal.call(_receive)
    return result


def _next_event(ws: Any, want_type: str, timeout: float = 5.0) -> dict[str, Any]:
    """Skip other messages until one of ``want_type`` arrives; a close is a failure."""
    for _ in range(10):
        message = _next_message(ws, timeout)
        assert message["type"] == "websocket.send", f"socket closed: {message}"
        body = json.loads(message["text"])
        if body["type"] == want_type:
            return body
    raise AssertionError(f"no {want_type} message")


def _expect_close(ws: Any, code: int, timeout: float = 5.0) -> dict[str, Any]:
    """The server must close the socket with ``code`` and send no event before it."""
    message = _next_message(ws, timeout)
    assert message["type"] == "websocket.close", f"expected a close, got {message}"
    assert message["code"] == code, message
    return message


def _broadcast(client: TestClient, user_id: str, content: str, project_id: str = "default") -> int:
    return client.portal.call(
        lambda: websocket.connection_manager.broadcast(
            "memory.updated",
            {"memory_id": f"m-{content}", "user_id": user_id, "new_content": content},
            user_id=user_id,
            project_id=project_id,
        )
    )


def _run(client: TestClient, sql: str, *args: Any) -> None:
    """Change the database from the app's own event loop (no revocation hook runs)."""

    async def _exec() -> None:
        db = client.app.state.db  # type: ignore[attr-defined]
        await db.conn.execute(sql, args)
        await db.conn.commit()

    client.portal.call(_exec)


def _mount_rest(h: Any) -> None:
    h.app.include_router(keys.router, prefix="/api/v1")
    h.app.include_router(auth.router, prefix="/api/v1")


@pytest.mark.parametrize("hard", [False, True])
async def test_revoking_a_key_closes_its_open_socket(tmp_path, hard):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        _mount_rest(h)
        uid = await h.create_user("owner@example.com", PASSWORD)
        revoked_key, revoked_id = await h.api_key(uid, "viewer")
        other_key, _ = await h.api_key(uid, "viewer")
        with (
            TestClient(h.app) as client,
            client.websocket_connect("/ws", headers={"X-API-Key": revoked_key}) as ws_revoked,
            client.websocket_connect("/ws", headers={"X-API-Key": other_key}) as ws_other,
        ):
            _next_event(ws_revoked, "connected")
            _next_event(ws_other, "connected")

            r = client.delete(f"/api/v1/keys/{revoked_id}", params={"hard": hard}, headers=h.jwt(uid))
            assert r.status_code == 200, r.text

            # The revoked key's socket is closed at once, before any further event.
            close = _expect_close(ws_revoked, websocket.CLOSE_UNAUTHORIZED)
            assert close["reason"] == "Access revoked or expired"

            # Later events reach only the socket whose key still works.
            assert _broadcast(client, uid, "after-revoke") == 1
            assert _next_event(ws_other, "memory.updated")["data"]["new_content"] == "after-revoke"
            stats = client.portal.call(lambda: websocket.connection_manager.get_stats(user_id=uid))
            assert stats == {"connections": 1}


async def test_key_switched_off_without_the_revoke_route_gets_no_further_events(tmp_path):
    """The per-event check: no hook ran, yet the next event is not delivered and the socket closes."""
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, key_id = await h.api_key(uid, "viewer")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")
            assert _broadcast(client, uid, "before") == 1
            assert _next_event(ws, "memory.updated")["data"]["new_content"] == "before"

            _run(client, "UPDATE api_keys SET active = 0 WHERE id = ?", key_id)

            assert _broadcast(client, uid, "SECRET-after") == 0
            _expect_close(ws, websocket.CLOSE_UNAUTHORIZED)


async def test_deactivated_account_gets_no_further_events(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, _ = await h.api_key(uid, "viewer")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")
            _run(client, "UPDATE users SET is_active = 0 WHERE id = ?", uid)
            assert _broadcast(client, uid, "SECRET-after") == 0
            _expect_close(ws, websocket.CLOSE_UNAUTHORIZED)


async def test_idle_socket_is_closed_by_the_periodic_check(tmp_path, monkeypatch):
    """No event and no hook: the socket is still closed within the re-check interval."""
    monkeypatch.setattr(websocket, "REVALIDATE_INTERVAL_SECONDS", 0.2)
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, key_id = await h.api_key(uid, "viewer")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")
            _run(client, "UPDATE api_keys SET active = 0 WHERE id = ?", key_id)
            _expect_close(ws, websocket.CLOSE_UNAUTHORIZED, timeout=3.0)


async def test_periodic_check_keeps_a_valid_socket_open(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket, "REVALIDATE_INTERVAL_SECONDS", 0.05)
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, key_id = await h.api_key(uid, "viewer")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")
            before = await h.keys.get_key_info(key_id)
            time.sleep(0.4)  # several re-checks run on the server
            ws.send_text("ping")
            assert _next_message(ws)["text"] == "pong"
            assert _broadcast(client, uid, "still-here") == 1
            assert _next_event(ws, "memory.updated")["data"]["new_content"] == "still-here"
            # Re-checks do not count as uses of the key.
            after = await h.keys.get_key_info(key_id)
            assert before is not None and after is not None
            assert after.last_used_at == before.last_used_at


async def test_logout_closes_the_dashboard_socket_but_not_key_sockets(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        _mount_rest(h)
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, _ = await h.api_key(uid, "viewer")
        session = h.jwt(uid, "owner@example.com")
        token = session["Authorization"][len("Bearer ") :]
        with (
            TestClient(h.app) as client,
            client.websocket_connect("/ws") as ws_session,
            client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws_key,
        ):
            # The dashboard authenticates with a first message, not the URL.
            ws_session.send_text(json.dumps({"type": "auth", "token": token}))
            _next_event(ws_session, "connected")
            _next_event(ws_key, "connected")

            assert client.post("/api/v1/auth/logout", headers=session).status_code == 200

            _expect_close(ws_session, websocket.CLOSE_UNAUTHORIZED)
            assert _broadcast(client, uid, "after-logout") == 1
            assert _next_event(ws_key, "memory.updated")["data"]["new_content"] == "after-logout"


async def test_password_change_closes_session_sockets(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        _mount_rest(h)
        uid = await h.create_user("owner@example.com", PASSWORD)
        old = h.jwt(uid, "owner@example.com")
        time.sleep(0.01)  # the cut-off is in milliseconds; the old session must predate it
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers=old) as ws:
            _next_event(ws, "connected")
            r = client.post(
                "/api/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "N3w!Passw0rd-long"},
                headers=old,
            )
            assert r.status_code == 200, r.text
            _expect_close(ws, websocket.CLOSE_UNAUTHORIZED)
            assert _broadcast(client, uid, "SECRET-after") == 0


async def test_account_deletion_closes_every_socket_of_the_account(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        _mount_rest(h)
        uid = await h.create_user("owner@example.com", PASSWORD)
        other = await h.create_user("other@example.com", PASSWORD)
        key, _ = await h.api_key(uid, "viewer")
        other_key, _ = await h.api_key(other, "viewer")
        session = h.jwt(uid, "owner@example.com")
        with (
            TestClient(h.app) as client,
            client.websocket_connect("/ws", headers=session) as ws_session,
            client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws_key,
            client.websocket_connect("/ws", headers={"X-API-Key": other_key}) as ws_other,
        ):
            for ws in (ws_session, ws_key, ws_other):
                _next_event(ws, "connected")

            r = client.request("DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=session)
            assert r.status_code == 200, r.text

            _expect_close(ws_session, websocket.CLOSE_UNAUTHORIZED)
            _expect_close(ws_key, websocket.CLOSE_UNAUTHORIZED)
            assert _broadcast(client, uid, "SECRET-after") == 0
            # Another account's socket is untouched.
            assert _broadcast(client, other, "theirs") == 1
            assert _next_event(ws_other, "memory.updated")["data"]["new_content"] == "theirs"


async def test_recheck_never_puts_credentials_in_the_subscriber_repr(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, _ = await h.api_key(uid, "viewer")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")
            subs = client.portal.call(lambda: _subscribers(uid))
            assert len(subs) == 1
            assert key not in repr(subs[0])


async def _subscribers(user_id: str) -> list[Any]:
    async with websocket.connection_manager._lock:
        return list(websocket.connection_manager._by_user.get(user_id, ()))


def _drop_key_cache(client: TestClient) -> None:
    """Forget cached key records, as a restart would, so the next check reads the new role row."""
    from remembra.auth import keys as keys_module

    client.portal.call(lambda: _clear(keys_module))


async def _clear(keys_module: Any) -> None:
    keys_module._key_cache.clear()


async def test_socket_that_loses_memory_recall_is_closed_with_4003(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, key_id = await h.api_key(uid, "viewer", scopes=["memory:recall"])
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")
            _run(client, "UPDATE api_key_roles SET scopes = 'memory:store' WHERE api_key_id = ?", key_id)
            _drop_key_cache(client)
            assert _broadcast(client, uid, "SECRET-after") == 0
            close = _expect_close(ws, websocket.CLOSE_FORBIDDEN)
            assert close["reason"] == "memory:recall permission required"


async def test_socket_following_a_project_the_key_lost_is_closed_with_4003(tmp_path):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, key_id = await h.api_key(uid, "viewer")
        with (
            TestClient(h.app) as client,
            client.websocket_connect("/ws?project_id=alpha", headers={"X-API-Key": key}) as ws,
        ):
            _next_event(ws, "connected")
            _run(client, "UPDATE api_key_roles SET project_ids = 'beta' WHERE api_key_id = ?", key_id)
            _drop_key_cache(client)
            assert _broadcast(client, uid, "SECRET-alpha", project_id="alpha") == 0
            close = _expect_close(ws, websocket.CLOSE_FORBIDDEN)
            assert close["reason"] == "No access to project"


async def test_recheck_that_cannot_run_closes_with_1011_and_sends_nothing(tmp_path, monkeypatch):
    async with secure_app(tmp_path, [websocket.router], prefix="") as h:
        uid = await h.create_user("owner@example.com", PASSWORD)
        key, _ = await h.api_key(uid, "viewer")
        with TestClient(h.app) as client, client.websocket_connect("/ws", headers={"X-API-Key": key}) as ws:
            _next_event(ws, "connected")

            async def _broken(*_: Any, **__: Any) -> None:
                raise RuntimeError("database is locked")

            monkeypatch.setattr(websocket, "_authenticate", _broken)
            assert _broadcast(client, uid, "SECRET-after") == 0
            close = _expect_close(ws, websocket.CLOSE_INTERNAL_ERROR)
            assert close["reason"] == "Could not re-check access"
