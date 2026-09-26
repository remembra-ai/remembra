"""R-11 billing and account-kind edges of account deletion.

Real routes over SQLite with the Paddle API faked at the HTTP layer
(tests/_paddle_mock.py). Covered:

* two accounts paid by one Paddle customer: deleting one cancels only its own
  subscriptions (the one it holds and a duplicate its checkout names) and
  leaves the other account's, plus any it cannot attribute, with an owner alert;
* a recorded subscription Paddle does not know (404) no longer blocks deletion,
  for the user or the superadmin, and alerts the owner;
* a transient Paddle failure says "try again", a refusal says support will
  sort it out; both alert the owner, and the superadmin can force past it;
* API-signup tenants (no ``users`` row) can be erased by the superadmin;
* accounts deactivated by the pre-R-11 "Delete account" are listed and can be
  scheduled for erasure.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from remembra.account.erasure import AccountEraser
from remembra.api.v1 import admin
from tests import _paddle_mock
from tests._cost_harness import cost_app
from tests.security_harness import MASTER_KEY
from tests.test_account_deletion import PASSWORD, _cells
from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase
from tests.test_plans_billing_signup import PRICES, _paddle
from tests.test_tenant_email_verification import FakeEmail


async def _setup(c: Any) -> RecordingAlerts:
    _paddle(c, **PRICES)
    alerts = RecordingAlerts()
    c.h.app.state.alerts = alerts
    c.h.app.state.tasks = None  # alerts delivered inline so the test reads them
    c.h.app.include_router(admin.router, prefix="/api/v1")
    return alerts


async def _owner(c: Any) -> dict[str, str]:
    owner = await c.h.create_user("owner@example.com", verified=True)
    return dict(c.h.jwt(owner, "owner@example.com"))


async def _buy_solo(c: Any, paddle: _paddle_mock.PaddleMock, uid: str, sub: str, customer: str) -> None:
    await _hook(c, "transaction.completed", _purchase(f"txn_{sub}", sub, "pri_solo_m", _bound(uid), customer=customer))
    paddle.add_subscription(sub, customer, custom_data=_bound(uid))
    assert (await c.meter.get_tenant(uid))["plan"] == "solo"


def _events(alerts: RecordingAlerts, prefix: str) -> list[dict[str, Any]]:
    return [details for event, _message, details in alerts.sent if event.startswith(prefix)]


# ---------------------------------------------------------------------------
# One Paddle customer paying for two accounts
# ---------------------------------------------------------------------------


async def test_deleting_one_account_leaves_another_accounts_subscription_on_the_same_customer(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        a, _akey = await c.account("a@example.com")
        b, bkey = await c.account("b@example.com")
        await _buy_solo(c, paddle, a, "sub_a", "ctm_same")
        await _buy_solo(c, paddle, b, "sub_b", "ctm_same")
        # A second purchase A's own checkout made, one naming an account that is not here, one with no marks.
        paddle.add_subscription("sub_a_dup", "ctm_same", custom_data={"remembra_user_id": a})
        paddle.add_subscription("sub_stranger", "ctm_same", custom_data={"remembra_user_id": "u_stranger"})
        paddle.add_subscription("sub_unmarked", "ctm_same")

        r = await c.h.client.request(
            "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(a, "a@example.com")
        )
        assert r.status_code == 200, r.text
        assert r.json()["subscriptions_cancelled"] == 2
        status_of = {sid: s["status"] for sid, s in paddle.subscriptions.items()}
        assert status_of == {
            "sub_a": "canceled",
            "sub_b": "active",
            "sub_a_dup": "canceled",
            "sub_stranger": "active",
            "sub_unmarked": "active",
        }
        cancels = sorted(call[1] for call in paddle.requests("POST", "/subscriptions/"))
        assert cancels == ["/subscriptions/sub_a/cancel", "/subscriptions/sub_a_dup/cancel"]
        left = _events(alerts, f"account_deletion_shared_customer:{a}")
        assert len(left) == 1 and sorted(left[0]["subscription_ids"]) == ["sub_b", "sub_stranger", "sub_unmarked"]

        # B still holds its paid plan and keeps working.
        assert (await c.meter.get_tenant(b))["plan"] == "solo"
        assert (await c.h.client.get("/api/v1/memories", headers=bkey)).status_code == 200


async def test_an_unmarked_duplicate_is_cancelled_when_no_other_account_shares_the_customer(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        a, _ = await c.account("solo@example.com")
        await _buy_solo(c, paddle, a, "sub_solo", "ctm_solo")
        paddle.add_subscription("sub_extra", "ctm_solo")  # no custom_data, no account records it
        r = await c.h.client.request(
            "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(a, "solo@example.com")
        )
        assert r.status_code == 200 and r.json()["subscriptions_cancelled"] == 2
        assert paddle.subscriptions["sub_extra"]["status"] == "canceled"
        assert _events(alerts, "account_deletion_shared_customer") == []


# ---------------------------------------------------------------------------
# Subscriptions Paddle does not know, and Paddle refusing
# ---------------------------------------------------------------------------


async def test_a_subscription_paddle_does_not_know_no_longer_blocks_deletion(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        owner = await _owner(c)
        for email in ("gone@example.com", "admin-gone@example.com"):
            uid, _ = await c.account(email)
            # Recorded id from before the live catalog (Stripe-era, sandbox or hand-edited): Paddle answers 404.
            await _hook(c, "transaction.completed", _purchase(f"txn_{uid}", f"sub_gone_{uid}", "pri_solo_m", _bound(uid)))
            assert (await c.meter.get_tenant(uid))["plan"] == "solo"
            if email.startswith("admin"):
                r = await c.h.client.delete(f"/api/v1/admin/users/{uid}", params={"confirm": "true"}, headers=owner)
                assert r.status_code == 200, r.text
                assert await c.h.db.get_user_by_id(uid) is None
            else:
                r = await c.h.client.request(
                    "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(uid, email)
                )
                assert r.status_code == 200, r.text
                user = await c.h.db.get_user_by_id(uid)
                assert user["deleted_at"] and not user["is_active"]
                assert (await c.meter.get_tenant(uid))["plan"] == "free"
            assert r.json()["subscriptions_cancelled"] == 0
            unknown = _events(alerts, f"account_deletion_subscription_unknown:{uid}")
            assert unknown == [{"user_id": uid, "subscription_ids": [f"sub_gone_{uid}"]}]
        assert paddle.requests("POST", "/subscriptions/") == []


@pytest.mark.parametrize(
    ("failure", "transient", "wording"),
    [
        (500, True, "try again in a few minutes"),
        (429, True, "try again in a few minutes"),
        (httpx.ConnectTimeout("slow"), True, "try again in a few minutes"),
        (403, False, "support@remembra.dev"),
        (422, False, "support@remembra.dev"),
    ],
)
async def test_a_failed_cancel_tells_the_user_what_to_do_and_alerts_the_owner(
    tmp_path, monkeypatch, failure: Any, transient: bool, wording: str
) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        uid, key = await c.account("stuck@example.com")
        await _buy_solo(c, paddle, uid, "sub_stuck", "ctm_stuck")
        paddle.failures["POST /subscriptions/sub_stuck/cancel"] = failure
        r = await c.h.client.request(
            "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(uid, "stuck@example.com")
        )
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert "not deleted" in detail and wording in detail
        if not transient:
            assert "try again" not in detail
        failed = _events(alerts, f"account_deletion_billing_failed:{uid}")
        assert len(failed) == 1
        assert failed[0]["subscription_id"] == "sub_stuck" and failed[0]["transient"] is transient
        user = await c.h.db.get_user_by_id(uid)
        assert user["is_active"] and user["deleted_at"] is None
        assert (await c.h.client.get("/api/v1/memories", headers=key)).status_code == 200


async def test_the_superadmin_can_force_a_deletion_past_a_paddle_refusal(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        owner = await _owner(c)
        uid, _ = await c.account("refused@example.com")
        await _buy_solo(c, paddle, uid, "sub_refused", "ctm_refused")
        paddle.failures["POST /subscriptions/sub_refused/cancel"] = 403

        r = await c.h.client.delete(f"/api/v1/admin/users/{uid}", params={"confirm": "true"}, headers=owner)
        assert r.status_code == 502 and "force_billing=skip" in r.json()["detail"]
        r = await c.h.client.delete(
            f"/api/v1/admin/users/{uid}", params={"confirm": "true", "force_billing": "cancel"}, headers=owner
        )
        assert r.status_code == 422  # only "skip" is accepted
        assert await c.h.db.get_user_by_id(uid) is not None

        calls = len(paddle.calls)
        r = await c.h.client.delete(
            f"/api/v1/admin/users/{uid}", params={"confirm": "true", "force_billing": "skip"}, headers=owner
        )
        assert r.status_code == 200, r.text
        assert r.json()["billing_skipped"] is True and r.json()["subscriptions_cancelled"] == 0
        assert len(paddle.calls) == calls  # Paddle not asked
        skipped = _events(alerts, f"account_deletion_billing_skipped:{uid}")
        assert skipped == [{"user_id": uid, "subscription_id": "sub_refused", "customer_id": "ctm_refused"}]
        assert await _cells(c.h.db.conn, [uid, "refused@example.com"]) == {}


# ---------------------------------------------------------------------------
# API-signup tenants and pre-R-11 deactivations
# ---------------------------------------------------------------------------


async def test_the_superadmin_erases_an_api_signup_tenant(tmp_path, monkeypatch) -> None:
    from remembra.cloud import email as email_module

    monkeypatch.setattr(email_module.EmailService, "create", classmethod(lambda cls, **_: FakeEmail()))
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        owner = await _owner(c)
        r = await c.h.client.post(
            "/api/v1/cloud/signup",
            json={"email": "api-only@example.com", "client_ip": "198.51.100.9"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert r.status_code == 201, r.text
        tenant_id, key = r.json()["user_id"], {"X-API-Key": r.json()["api_key"]}
        assert await c.h.db.get_user_by_id(tenant_id) is None  # no dashboard user row
        r = await c.h.client.post("/api/v1/memories", json={"content": "api tenant note"}, headers=key)
        assert r.status_code == 201, r.text
        await _buy_solo(c, paddle, tenant_id, "sub_api", "ctm_api")
        assert await _cells(c.h.db.conn, [tenant_id])

        r = await c.h.client.delete(f"/api/v1/admin/users/{tenant_id}", params={"confirm": "true"}, headers=owner)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_kind"] == "api_tenant" and body["email"] == "api-only@example.com"
        assert body["subscriptions_cancelled"] == 1 and paddle.subscriptions["sub_api"]["status"] == "canceled"
        assert await _cells(c.h.db.conn, [tenant_id, "api-only@example.com"]) == {}
        assert (await c.h.client.get("/api/v1/memories", headers=key)).status_code == 401

        r = await c.h.client.delete("/api/v1/admin/users/u_nobody", params={"confirm": "true"}, headers=owner)
        assert r.status_code == 404


async def test_accounts_deactivated_by_the_old_delete_are_listed_and_can_be_scheduled(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        owner = await _owner(c)
        old, old_key = await c.account("old-delete@example.com")
        await _buy_solo(c, paddle, old, "sub_old", "ctm_old")  # the old delete never cancelled billing
        active, _ = await c.account("still-here@example.com")
        new, _ = await c.account("new-delete@example.com")
        # What the pre-R-11 DELETE /auth/me did: deactivate, revoke access, nothing else.
        from remembra.auth.users import revoke_user_access

        await c.h.db.deactivate_user(old)
        await revoke_user_access(c.h.db, old)
        r = await c.h.client.request(
            "DELETE", "/api/v1/auth/me", json={"password": PASSWORD}, headers=c.h.jwt(new, "new-delete@example.com")
        )
        assert r.status_code == 200

        r = await c.h.client.get("/api/v1/admin/deactivated-accounts", headers=owner)
        assert r.status_code == 200, r.text
        rows = r.json()["accounts"]
        assert [row["user_id"] for row in rows] == [old]  # not the active one, not the new-style deletion
        assert rows[0]["paddle_subscription_id"] == "sub_old" and rows[0]["plan"] == "solo"

        for uid, expected in ((active, 409), (new, 409), ("u_nobody", 404)):
            r = await c.h.client.post(
                f"/api/v1/admin/deactivated-accounts/{uid}/schedule-erasure", params={"confirm": "true"}, headers=owner
            )
            assert r.status_code == expected, (uid, r.text)
        r = await c.h.client.post(f"/api/v1/admin/deactivated-accounts/{old}/schedule-erasure", headers=owner)
        assert r.status_code == 400  # confirm required

        r = await c.h.client.post(
            f"/api/v1/admin/deactivated-accounts/{old}/schedule-erasure", params={"confirm": "true"}, headers=owner
        )
        assert r.status_code == 200, r.text
        assert r.json()["subscriptions_cancelled"] == 1 and paddle.subscriptions["sub_old"]["status"] == "canceled"
        assert (await c.h.client.get("/api/v1/admin/deactivated-accounts", headers=owner)).json()["total"] == 0

        from datetime import datetime

        deleted_at = datetime.fromisoformat(r.json()["deleted_at"])
        eraser = AccountEraser(c.h.db, None)
        due = await eraser.due_accounts(timedelta(days=7), now=deleted_at + timedelta(days=7, minutes=1))
        assert old in due
        await eraser.erase(old)
        assert await _cells(c.h.db.conn, [old, "old-delete@example.com"]) == {}
        assert (await c.h.client.get("/api/v1/memories", headers=old_key)).status_code == 401
