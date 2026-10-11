"""Staged transport tests; real JWT/API-key auth and synthetic durable Crew store."""

import inspect
import json
import time
import warnings
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from tests.crew.test_approvals import SESSION, operation
from tests.crew.test_approvals import h as h
from tests.security_harness import JWT_SECRET, secure_app

from remembra.api.v1.crew_approvals import make_approval_router
from remembra.crew.approvals import ApprovalError

SECRET = "ghp_" + "x" * 36


@pytest.fixture
async def http(h, tmp_path):
    calls = []

    def gate(action, user, token):
        calls.append((action, user.user_id, token))

    router = make_approval_router(h.service, rate_gate=gate)
    async with secure_app(tmp_path, [router]) as security:
        owner = await security.create_user("owner-http@example.com", verified=True)
        worker = await security.create_user("worker-http@example.com", verified=True)
        other = await security.create_user("other-http@example.com", verified=True)
        await h.db.conn.execute("UPDATE crews SET owner_user_id=? WHERE id=?", (owner, h.crew_id))
        await h.db.conn.execute("UPDATE crew_members SET user_id=? WHERE crew_id=? AND user_id=?", (worker, h.crew_id, "worker"))
        await h.db.conn.execute("UPDATE crew_sessions SET user_id=? WHERE id=?", (worker, SESSION))
        await h.db.conn.commit()
        key, key_id = await security.api_key(worker)
        admin, _ = await security.api_key(owner, role="admin")
        h.now = time.time_ns() // 1_000_000
        yield SimpleNamespace(
            h=h,
            security=security,
            client=security.client,
            calls=calls,
            owner=owner,
            worker=worker,
            key_id=key_id,
            owner_headers=security.jwt(owner),
            other_headers=security.jwt(other),
            worker_headers={"X-API-Key": key, "X-Remembra-Crew-Session": "synthetic-session-token"},
            admin_headers={"X-API-Key": admin},
            url=f"/api/v1/crews/{h.crew_id}/approvals",
            body={
                "operation": operation().model_dump(mode="json"),
                "expires_at_ms": h.now + 60_000,
                "idempotency_key": "http-request",
            },
        )


async def test_lifecycle_identity_replay_and_explicit_no_executor(http):
    a = await http.client.post(http.url, headers=http.worker_headers, json=http.body)
    assert a.status_code == 200, a.text
    record = a.json()["approval"]
    assert record["request"]["requester"]["user_id"] == http.worker
    assert record["request"]["requester"]["credential_id"] == http.key_id
    assert record["request"]["executor"] == record["request"]["requester"]
    assert a.json()["execution"] == {"supported": False, "reason": "unsupported_adapter"}
    replay = await http.client.post(http.url, headers=http.worker_headers, json=http.body)
    assert replay.json()["approval"]["id"] == record["id"]
    url = http.url + "/" + record["id"]
    other = await http.client.get(url, headers=http.other_headers)
    missing = await http.client.get(http.url + "/missing", headers=http.other_headers)
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()
    digest = a.json()["request_digest"]
    decision = {"request_digest": digest, "expected_version": record["version"], "approve": True}
    decided = await http.client.post(url + "/decide", headers=http.owner_headers, json=decision)
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["decision"]["user_id"] == http.owner
    duplicate = await http.client.post(url + "/decide", headers=http.owner_headers, json=decision)
    assert duplicate.status_code == 409
    revoked = await http.client.post(
        url + "/revoke", headers=http.owner_headers, json={"request_digest": digest, "expected_version": 2}
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["approval"]["invalidation"]["reason"] == "revoked"
    assert revoked.json()["approval"]["decision"]["state"] == "approved"
    assert (
        await http.client.post(url + "/consume", headers=http.worker_headers, json={"request_digest": digest})
    ).status_code == 404
    assert len(await http.h.store.history(record["id"])) == 3


@pytest.mark.parametrize("kind", ["missing", "forged", "admin_key", "stale"])
async def test_auth_and_human_stepup_precede_any_body_read(http, kind):
    headers = {}
    if kind == "forged":
        headers = {
            "Authorization": "Bearer " + jwt.encode({"sub": http.owner}, "wrong-key-long-enough-for-signature", algorithm="HS256")
        }
    if kind == "admin_key":
        headers = http.admin_headers
    if kind == "stale":
        token = http.owner_headers["Authorization"][7:]
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        claims.update(iat=int(time.time()) - 1200, iat_ms=http.h.now - 1_200_000)
        headers = {"Authorization": "Bearer " + jwt.encode(claims, JWT_SECRET, algorithm="HS256")}
    reads = 0

    async def body():
        nonlocal reads
        reads += 1
        yield b"{" + SECRET.encode()

    response = await http.client.post(
        http.url + "/missing/decide", headers={**headers, "Content-Type": "application/json"}, content=body()
    )
    assert response.status_code == (403 if kind == "admin_key" else 401), response.text
    assert reads == 0


async def test_streamed_body_cap_stops_before_third_chunk(http):
    reads = 0

    async def body():
        nonlocal reads
        for _ in range(4):
            reads += 1
            yield b"x" * 20000

    result = await http.client.post(http.url, headers={**http.worker_headers, "Content-Type": "application/json"}, content=body())
    assert result.status_code == 413, result.text
    assert reads == 2
    assert await http.h.db.fetchall("SELECT * FROM test_approvals") == []


@pytest.mark.parametrize("kind", ["key", "session", "membership"])
async def test_authority_revalidated_after_body_stream(http, kind):
    async def body():
        if kind == "key":
            assert await http.security.keys.revoke_key(http.key_id, http.worker)
        elif kind == "session":
            await http.h.db.conn.execute("UPDATE crew_sessions SET state=? WHERE id=?", ("ended", SESSION))
            await http.h.db.conn.commit()
        else:
            await http.h.db.conn.execute("DELETE FROM crew_members WHERE crew_id=? AND user_id=?", (http.h.crew_id, http.worker))
            await http.h.db.conn.commit()
        yield json.dumps(http.body).encode()

    response = await http.client.post(
        http.url, headers={**http.worker_headers, "Content-Type": "application/json"}, content=body()
    )
    assert response.status_code in (401, 404), response.text
    assert await http.h.db.fetchall("SELECT * FROM test_approvals") == []


@pytest.mark.parametrize("kind", ["extra_authority", "secret_key", "policy_error"])
async def test_rejections_never_echo_inputs_or_policy_exception(http, kind, caplog):
    body = dict(http.body)
    if kind == "extra_authority":
        body["executor"] = SECRET
    elif kind == "secret_key":
        body[SECRET] = "untrusted"
    else:

        def reject(op):
            raise ApprovalError(SECRET)

        http.h.policy.validate = reject
    response = await http.client.post(http.url, headers=http.worker_headers, json=body)
    assert response.status_code == (409 if kind == "policy_error" else 422), response.text
    assert SECRET not in response.text + caplog.text
    assert await http.h.db.fetchall("SELECT * FROM test_approvals") == []


async def test_mandatory_rate_gate_runs_after_auth_before_body(http):
    seen = []

    def reject(action, user, token):
        seen.append((action, user.user_id, token))
        raise HTTPException(429, "Rate limit reached.")

    router = make_approval_router(http.h.service, rate_gate=reject)
    http.security.app.include_router(router, prefix="/rate")
    reads = 0

    async def body():
        nonlocal reads
        reads += 1
        yield b"{}"

    url = f"/rate/crews/{http.h.crew_id}/approvals"
    response = await http.client.post(url, headers={**http.worker_headers, "Content-Type": "application/json"}, content=body())
    assert response.status_code == 429
    assert reads == 0
    assert seen == [("request", http.worker, "synthetic-session-token")]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Length": "40000", "Content-Type": "application/json"}, 413),
        ({"Content-Length": "bad", "Content-Type": "application/json"}, 400),
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, 415),
    ],
)
async def test_bad_transport_metadata_is_rejected_without_reading(http, headers, status):
    reads = 0

    async def body():
        nonlocal reads
        reads += 1
        yield b"{}"

    result = await http.client.post(http.url, headers={**http.worker_headers, **headers}, content=body())
    assert result.status_code == status, result.text
    assert reads == 0


async def test_revoked_jwt_during_body_cannot_decide(http):
    async def body():
        token = http.owner_headers["Authorization"][7:]
        assert await http.security.users.invalidate_token(http.owner, token)
        yield json.dumps({"request_digest": "a" * 64, "expected_version": 1, "approve": True}).encode()

    result = await http.client.post(
        http.url + "/missing/decide", headers={**http.owner_headers, "Content-Type": "application/json"}, content=body()
    )
    assert result.status_code == 401, result.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []


async def test_body_timeout_never_writes(http, monkeypatch):
    import asyncio

    import remembra.api.v1.crew_approvals as boundary

    monkeypatch.setattr(boundary, "BODY_TIMEOUT_SECONDS", 0.01)

    async def body():
        await asyncio.sleep(0.1)
        yield json.dumps(http.body).encode()

    result = await http.client.post(http.url, headers={**http.worker_headers, "Content-Type": "application/json"}, content=body())
    assert result.status_code == 408, result.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []


async def test_body_disconnect_never_writes(http):
    from starlette.requests import ClientDisconnect

    async def body():
        yield b"{"
        raise ClientDisconnect()

    result = await http.client.post(http.url, headers={**http.worker_headers, "Content-Type": "application/json"}, content=body())
    assert result.status_code == 400, result.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []


@pytest.mark.parametrize("lengths", [[b"\xb2"], [b"2", b"2"], [b"2", b"3"]])
async def test_ambiguous_or_non_ascii_lengths_never_read_or_echo(http, lengths, caplog):
    reads = 0

    async def body():
        nonlocal reads
        reads += 1
        yield b"{}"

    headers = [(k.encode(), v.encode()) for k, v in http.worker_headers.items()]
    headers += [(b"content-type", b"application/json")] + [(b"content-length", value) for value in lengths]
    result = await http.client.post(http.url, headers=headers, content=body())
    assert result.status_code == 400, result.text
    assert reads == 0
    assert "\u00b2" not in result.text + caplog.text


async def test_declared_length_must_match_received_bytes(http):
    async def body():
        yield json.dumps(http.body).encode()

    result = await http.client.post(
        http.url, headers={**http.worker_headers, "Content-Type": "application/json", "Content-Length": "1"}, content=body()
    )
    assert result.status_code == 400, result.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []


@pytest.mark.parametrize("change", ["step_up", "owner"])
async def test_human_authority_change_during_stream_is_rechecked(http, change):
    async def body():
        assert not http.h.db.in_transaction
        if change == "step_up":
            http.h.now += 16 * 60_000
        else:
            await http.h.db.conn.execute("UPDATE crews SET owner_user_id=? WHERE id=?", ("different-owner", http.h.crew_id))
            await http.h.db.conn.commit()
        yield json.dumps({"request_digest": "a" * 64, "expected_version": 1, "approve": True}).encode()

    result = await http.client.post(
        http.url + "/missing/decide", headers={**http.owner_headers, "Content-Type": "application/json"}, content=body()
    )
    assert result.status_code == (401 if change == "step_up" else 404), result.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []


@pytest.mark.parametrize(
    ("method", "suffix"), [("POST", ""), ("GET", "/missing"), ("POST", "/missing/decide"), ("POST", "/missing/revoke")]
)
async def test_each_handler_authenticates_before_rate_or_body(http, method, suffix):
    reads = 0

    async def body():
        nonlocal reads
        reads += 1
        yield b"{}"

    result = await http.client.request(method, http.url + suffix, headers={"Content-Type": "application/json"}, content=body())
    assert result.status_code == 401, result.text
    assert reads == 0
    assert http.calls == []


@pytest.mark.parametrize(
    ("action", "method", "suffix"),
    [
        ("request", "POST", ""),
        ("get", "GET", "/missing"),
        ("decide", "POST", "/missing/decide"),
        ("revoke", "POST", "/missing/revoke"),
    ],
)
async def test_each_handler_applies_required_gate_before_body(http, action, method, suffix):
    seen = []

    def gate(operation, user, token):
        seen.append(operation)
        raise HTTPException(429, "Rate limit reached.")

    http.security.app.include_router(make_approval_router(http.h.service, rate_gate=gate), prefix="/all-rate")
    reads = 0

    async def body():
        nonlocal reads
        reads += 1
        yield b"{}"

    headers = http.worker_headers if action == "request" else http.owner_headers
    result = await http.client.request(
        method,
        f"/all-rate/crews/{http.h.crew_id}/approvals" + suffix,
        headers={**headers, "Content-Type": "application/json"},
        content=body(),
    )
    assert result.status_code == 429, result.text
    assert reads == 0
    assert seen == [action]


@pytest.mark.parametrize("kind", ["function", "callable_object"])
async def test_factory_rejects_async_gate_before_invocation(h, kind):
    called = False

    async def gate(action, user, token):
        nonlocal called
        called = True
        raise HTTPException(429, SECRET)

    class AsyncGate:
        async def __call__(self, action, user, token):
            await gate(action, user, token)

    with pytest.raises(TypeError, match="^Approval rate gate must be synchronous\\.$"):
        make_approval_router(h.service, rate_gate=gate if kind == "function" else AsyncGate())
    assert not called


@pytest.mark.parametrize(
    ("action", "method", "suffix"),
    [
        ("request", "POST", ""),
        ("get", "GET", "/missing"),
        ("decide", "POST", "/missing/decide"),
        ("revoke", "POST", "/missing/revoke"),
    ],
)
async def test_coroutine_returning_gate_fails_closed_on_every_route(http, action, method, suffix, caplog):
    coroutines = []
    ran = False
    reads = 0

    async def deny():
        nonlocal ran
        ran = True
        raise HTTPException(429, SECRET)

    def gate(operation, user, token):
        pending = deny()
        coroutines.append(pending)
        return pending

    async def body():
        nonlocal reads
        reads += 1
        yield json.dumps(http.body).encode()

    http.security.app.include_router(make_approval_router(http.h.service, rate_gate=gate), prefix="/async-return")
    headers = http.worker_headers if action == "request" else http.owner_headers
    with warnings.catch_warnings(record=True) as caught:
        result = await http.client.request(
            method,
            f"/async-return/crews/{http.h.crew_id}/approvals" + suffix,
            headers={**headers, "Content-Type": "application/json"},
            content=body(),
        )
        assert len(coroutines) == 1
        assert inspect.getcoroutinestate(coroutines.pop()) == inspect.CORO_CLOSED
    assert result.status_code == 503, result.text
    assert not ran and reads == 0
    assert caught == []
    assert SECRET not in result.text + caplog.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []


@pytest.mark.parametrize("kind", ["awaitable", "future", "false", "true", "mapping"])
async def test_unexpected_gate_results_never_allow_work(http, kind):
    import asyncio

    class Awaitable:
        def __await__(self):
            pytest.fail("The synchronous port must not await adapter work.")

    result = {
        "awaitable": Awaitable,
        "future": lambda: asyncio.get_running_loop().create_future(),
        "false": lambda: False,
        "true": lambda: True,
        "mapping": lambda: {"allowed": True},
    }[kind]()

    def gate(action, user, token):
        return result

    http.security.app.include_router(make_approval_router(http.h.service, rate_gate=gate), prefix="/invalid-return")
    response = await http.client.post(
        f"/invalid-return/crews/{http.h.crew_id}/approvals", headers=http.worker_headers, json=http.body
    )
    assert response.status_code == 503
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []
    if kind == "future":
        assert result.cancelled()


@pytest.mark.parametrize("kind", ["rate_limit", "http_error", "adapter_error"])
async def test_rate_adapter_errors_never_echo_details_or_headers(http, kind, caplog):
    def gate(action, user, token):
        if kind == "adapter_error":
            raise ValueError(SECRET)
        raise HTTPException(429 if kind == "rate_limit" else 500, SECRET, headers={"X-Adapter-Error": SECRET})

    http.security.app.include_router(make_approval_router(http.h.service, rate_gate=gate), prefix="/gate-error")
    response = await http.client.post(
        f"/gate-error/crews/{http.h.crew_id}/approvals", headers=http.worker_headers, json=http.body
    )
    assert response.status_code == (429 if kind == "rate_limit" else 503)
    assert SECRET not in response.text + str(response.headers) + caplog.text
    assert await http.h.db.fetchall("SELECT * FROM test_approval_audit") == []
