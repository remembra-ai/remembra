"""A retried inbox send does not leave the recipient two copies.

An agent whose send timed out (or whose response was lost) cannot tell whether the message
arrived. Real routes, real keys and a real SQLite inbox (``security_harness``):

* ``Idempotency-Key``: the same key returns the first message (200, ``Idempotent-Replayed``),
  reused for a different message it is a 409, and keys are per account and sender;
* without a key, the same sender's identical message, still unread from the last few minutes,
  is returned instead of stored again; once read, or after the window, sending again is new.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from remembra.api.v1 import inbox as inbox_api
from remembra.auth.rbac import Role
from remembra.client.memory import Memory
from remembra.inbox.manager import RESEND_WINDOW_SECONDS, InboxManager
from tests.security_harness import Harness, secure_app


@pytest.fixture()
async def h(tmp_path):
    async with secure_app(tmp_path, [inbox_api.router]) as harness:
        manager = InboxManager(harness.db)
        await manager.init_schema()
        harness.app.state.inbox_manager = manager
        yield harness


async def _key(h: Harness, user_id: str, agent: str = "codex") -> str:
    created = await h.keys.create_key(user_id=user_id, name="k", agent_id=agent)
    await h.roles.assign_role(created.id, Role("editor"))
    return created.key


async def _post(h: Harness, key: str, body: dict[str, Any], idem: str | None = None):
    headers = {"X-API-Key": key, **({"Idempotency-Key": idem} if idem else {})}
    return await h.client.post("/api/v1/inbox/send", headers=headers, json=body)


async def _rows(h: Harness, to: str = "claude-code") -> list[dict[str, Any]]:
    cur = await h.db.conn.execute("SELECT inbox_id, status FROM agent_inbox WHERE to_agent = ? ORDER BY created_at", (to,))
    return [dict(zip(("inbox_id", "status"), r, strict=True)) for r in await cur.fetchall()]


MSG = {"to_agent": "claude-code", "subject": "Deploy check", "body": "Is v0.17.3 live?", "project_id": "remembra"}


async def test_the_same_idempotency_key_returns_the_first_message(h):
    key = await _key(h, "u1")
    first = await _post(h, key, MSG, idem="send-1")
    assert first.status_code == 201, first.text
    again = await _post(h, key, MSG, idem="send-1")
    assert again.status_code == 200 and again.headers.get("Idempotent-Replayed") == "true", again.text
    assert again.json()["inbox_id"] == first.json()["inbox_id"]
    assert again.json()["sender_verified"] is True
    assert len(await _rows(h)) == 1
    # still the same after it was read: a replayed key never stores a second copy
    await h.client.post(
        f"/api/v1/inbox/{first.json()['inbox_id']}/ack", headers={"X-API-Key": await _key(h, "u1", "claude-code")}, json={}
    )
    assert (await _post(h, key, MSG, idem="send-1")).json()["inbox_id"] == first.json()["inbox_id"]
    assert len(await _rows(h)) == 1


async def test_a_key_reused_for_another_message_is_refused(h):
    key = await _key(h, "u1")
    assert (await _post(h, key, MSG, idem="send-2")).status_code == 201
    other = await _post(h, key, {**MSG, "body": "Something else"}, idem="send-2")
    assert other.status_code == 409, other.text
    assert len(await _rows(h)) == 1


async def test_keys_belong_to_one_account_and_sender(h):
    a = await _key(h, "u1")
    b = await _key(h, "u2")
    gem = await _key(h, "u1", "gemini")
    ids = {(await _post(h, k, MSG, idem="same")).json()["inbox_id"] for k in (a, b, gem)}
    assert len(ids) == 3


@pytest.mark.parametrize("bad", ["", " ", "x" * 201, "a\x00b"])
async def test_a_bad_key_is_refused(h, bad):
    key = await _key(h, "u1")
    resp = await h.client.post("/api/v1/inbox/send", headers={"X-API-Key": key, "Idempotency-Key": bad}, json=MSG)
    assert resp.status_code == 422, resp.text


async def test_an_identical_unread_resend_without_a_key_returns_the_first(h):
    key = await _key(h, "u1")
    first = await _post(h, key, MSG)
    assert first.status_code == 201
    again = await _post(h, key, MSG)
    assert again.status_code == 200 and again.json()["inbox_id"] == first.json()["inbox_id"], again.text
    assert len(await _rows(h)) == 1
    # anything different is a new message: body, recipient, project, the message it answers
    for change in (
        {"body": "Is v0.17.4 live?"},
        {"to_agent": "gemini"},
        {"project_id": "yaadbooks"},
        {"metadata": {"in_reply_to": "inbox_0123456789abcdef"}},
    ):
        assert (await _post(h, key, {**MSG, **change})).status_code == 201, change


async def test_a_resend_after_it_was_read_or_after_the_window_is_new(h):
    key = await _key(h, "u1")
    first = (await _post(h, key, MSG)).json()["inbox_id"]
    await h.client.post(f"/api/v1/inbox/{first}/ack", headers={"X-API-Key": await _key(h, "u1", "claude-code")}, json={})
    second = await _post(h, key, MSG)
    assert second.status_code == 201 and second.json()["inbox_id"] != first
    old = (datetime.now(UTC) - timedelta(seconds=RESEND_WINDOW_SECONDS + 5)).isoformat()
    await h.db.conn.execute("UPDATE agent_inbox SET created_at = ? WHERE inbox_id = ?", (old, second.json()["inbox_id"]))
    await h.db.conn.commit()
    third = await _post(h, key, MSG)
    assert third.status_code == 201 and third.json()["inbox_id"] not in (first, second.json()["inbox_id"])


def test_the_sdk_sends_the_key_as_a_header():
    seen: dict[str, Any] = {}

    class Probe(Memory):
        def _request(self, method, path, json=None, params=None, headers=None):  # type: ignore[override]
            seen.update(method=method, path=path, headers=headers)
            return {"inbox_id": "inbox_0000000000000000"}

    m = Probe.__new__(Probe)
    m.agent_id = "codex"
    m.send_to_inbox("claude-code", "s", "b", idempotency_key="k-1")
    assert seen["headers"] == {"Idempotency-Key": "k-1"} and seen["path"] == "/api/v1/inbox/send"


async def test_different_metadata_kind_or_expiry_is_a_new_message(h):
    """Codex review P1/P2: only an exact re-send is matched; an expired row is never replayed."""
    key = await _key(h, "u1")
    base = {**MSG, "metadata": {"action": "deploy"}}
    first = (await _post(h, key, base)).json()["inbox_id"]
    assert (await _post(h, key, {**MSG, "metadata": {"action": "rollback"}})).status_code == 201
    assert (await _post(h, key, {**base, "kind": "question"})).status_code == 201
    assert (await _post(h, key, base)).json()["inbox_id"] == first  # the exact message is still matched
    reused = await _post(h, key, {**MSG, "metadata": {"action": "deploy"}}, idem="k-meta")
    assert reused.status_code == 201
    other = await _post(h, key, {**MSG, "metadata": {"action": "rollback"}}, idem="k-meta")
    assert other.status_code == 409, other.text
    # an expired unread row does not swallow a fresh send of the same text
    soon = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    e1 = (await _post(h, key, {**MSG, "subject": "Short-lived", "expires_at": soon})).json()["inbox_id"]
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await h.db.conn.execute("UPDATE agent_inbox SET expires_at = ? WHERE inbox_id = ?", (past, e1))
    await h.db.conn.commit()
    fresh = await _post(h, key, {**MSG, "subject": "Short-lived", "expires_at": past})
    assert fresh.status_code == 201 and fresh.json()["inbox_id"] != e1, fresh.text


async def test_a_concurrent_send_with_the_same_key_and_another_message_is_refused(h, monkeypatch):
    """Codex review P2: both sends pass the lookup before either stores; the one that loses the insert
    must not report success for a message that was never stored."""
    key = await _key(h, "u1")
    manager = h.app.state.inbox_manager
    real = manager.get_one

    calls = {"n": 0}

    async def not_yet(*a, **kw):  # the second request looked before the first one stored
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(*a, **kw)

    assert (await _post(h, key, MSG, idem="race")).status_code == 201
    monkeypatch.setattr(manager, "get_one", not_yet)
    lost = await _post(h, key, {**MSG, "body": "A different body"}, idem="race")
    monkeypatch.setattr(manager, "get_one", real)
    assert lost.status_code == 409, lost.text
    assert len(await _rows(h)) == 1
