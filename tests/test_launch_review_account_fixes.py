"""Launch review: account deletion, erasure and Founding 100 edges.

Real routes over SQLite with Paddle faked at the HTTP layer
(tests/_paddle_mock.py):

* a deletion whose second cancel fails says the first one WAS cancelled (not
  "nothing changed") and the owner alert lists it;
* a payment that arrives for an already deleted account (a checkout opened
  before the deletion) is not applied: the subscription is cancelled at once,
  the account flagged and the owner alerted;
* deleting an account gives back a Founding seat it held;
* a Founding checkout needs a verified email, and one account gets one 2-hour
  hold per day, never extended;
* a lapsed founder sees the offer (and when the seat is released) even when
  the other seats are all taken;
* the superadmin hard delete never leaves an account active with its billing
  cancelled when the erasure fails;
* the erasure job skips an account made active again with ``deleted_at`` left
  set (an undo through an older API build);
* the owner of a team with members must confirm that the team ends;
* the deleted account is emailed the erase date and how to undo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from remembra.account import deletion
from remembra.account.erasure import AccountEraser
from remembra.cloud import metering
from tests import _paddle_mock
from tests._cost_harness import cost_app
from tests.test_account_deletion import PASSWORD
from tests.test_account_deletion_billing import _buy_solo, _events, _owner, _setup
from tests.test_founding_seats import NOW, _checkout_founding, _clock, _founders, _founding_purchase
from tests.test_paddle_subscription_ownership import _bound, _hook, _purchase


async def _delete(c: Any, uid: str, email: str, **extra: Any) -> httpx.Response:
    return await c.h.client.request(
        "DELETE", "/api/v1/auth/me", json={"password": PASSWORD, **extra}, headers=c.h.jwt(uid, email)
    )


# ---------------------------------------------------------------------------
# Deletion and billing
# ---------------------------------------------------------------------------


async def test_a_partial_cancel_says_what_was_cancelled(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        uid, _ = await c.account("two@example.com")
        await _buy_solo(c, paddle, uid, "sub_main", "ctm_two")
        paddle.add_subscription("sub_dup", "ctm_two", custom_data={"remembra_user_id": uid})
        paddle.failures["POST /subscriptions/sub_dup/cancel"] = 503

        r = await _delete(c, uid, "two@example.com")
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail.startswith("Your subscription was cancelled and will not charge again")
        assert "not deleted" in detail and "try again" in detail and "Nothing changed" not in detail
        assert paddle.subscriptions["sub_main"]["status"] == "canceled"
        failed = _events(alerts, f"account_deletion_billing_failed:{uid}")
        assert failed[0]["cancelled_subscription_ids"] == ["sub_main"] and failed[0]["subscription_id"] == "sub_dup"
        user = await c.h.db.get_user_by_id(uid)
        assert user["is_active"] and user["deleted_at"] is None


async def test_a_payment_for_a_deleted_account_is_cancelled_not_applied(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        alerts = await _setup(c)
        uid, _ = await c.account("gone@example.com")
        assert (await _delete(c, uid, "gone@example.com")).status_code == 200
        # A checkout opened before the deletion is paid afterwards.
        paddle.add_subscription("sub_late", "ctm_gone", custom_data=_bound(uid))
        result = await _hook(
            c, "transaction.completed", _purchase("txn_late", "sub_late", "pri_solo_m", _bound(uid), customer="ctm_gone")
        )
        assert result.get("result", result.get("status")) != "applied"
        tenant = await c.meter.get_tenant(uid) or {}
        assert str(tenant.get("plan") or "free") == "free"
        assert tenant.get("billing_flag") == "paid_after_deletion"
        assert paddle.subscriptions["sub_late"]["status"] == "canceled"
        sent = _events(alerts, f"paddle_payment_after_deletion:{uid}")
        assert sent and sent[0]["transaction_id"] == "txn_late" and sent[0]["subscription_cancelled"] is True


async def test_deleting_an_account_gives_back_its_founding_seat(tmp_path, monkeypatch) -> None:
    _clock(monkeypatch, NOW)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        await _founders(c, 99)
        uid = await c.h.create_user("holder@example.com", verified=True)
        assert (await _checkout_founding(c, uid, "holder@example.com")).status_code == 200
        assert await c.meter.founding_seats_taken() == 100
        assert (await _delete(c, uid, "holder@example.com")).status_code == 200
        assert await c.meter.founding_seats_taken() == 99
        cursor = await c.h.db.conn.execute("SELECT COUNT(*) FROM founding_holds WHERE user_id = ?", (uid,))
        assert (await cursor.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# Founding 100 holds
# ---------------------------------------------------------------------------


async def test_founding_checkout_needs_a_verified_email(tmp_path, monkeypatch) -> None:
    _clock(monkeypatch, NOW)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        uid = await c.h.create_user("fresh@example.com", verified=False)
        r = await _checkout_founding(c, uid, "fresh@example.com")
        assert r.status_code == 403 and "Verify your email" in r.json()["detail"]
        assert await c.meter.founding_seats_taken() == 0  # no seat held
        # Plain Solo needs no verification.
        r = await c.h.client.post(
            "/api/v1/billing/checkout",
            json={"plan": "solo", "billing_cycle": "monthly"},
            headers=c.h.jwt(uid, "fresh@example.com"),
        )
        assert r.status_code == 200, r.text


async def test_a_founding_hold_is_never_extended_and_one_per_day(tmp_path, monkeypatch) -> None:
    _clock(monkeypatch, NOW)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        uid = await c.h.create_user("squat@example.com", verified=True)

        async def until() -> str:
            cursor = await c.h.db.conn.execute("SELECT until FROM founding_holds WHERE user_id = ?", (uid,))
            return str((await cursor.fetchone())[0])

        assert (await _checkout_founding(c, uid, "squat@example.com")).status_code == 200
        first = await until()
        assert first == (NOW + timedelta(hours=2)).isoformat()
        # Re-opening checkout an hour later keeps the same expiry.
        _clock(monkeypatch, NOW + timedelta(hours=1, minutes=50))
        assert (await _checkout_founding(c, uid, "squat@example.com")).status_code == 200
        assert await until() == first
        # After it expires unpaid, no new hold until a day after the first.
        _clock(monkeypatch, NOW + timedelta(hours=3))
        r = await _checkout_founding(c, uid, "squat@example.com")
        assert r.status_code == 429 and "2026-10-02 12:00" in r.json()["detail"]
        assert await c.meter.founding_seats_taken() == 0
        _clock(monkeypatch, NOW + timedelta(hours=24, minutes=1))
        assert (await _checkout_founding(c, uid, "squat@example.com")).status_code == 200
        assert await until() == (NOW + timedelta(hours=26, minutes=1)).isoformat()


async def test_a_lapsed_founder_sees_the_offer_when_the_other_seats_are_full(tmp_path, monkeypatch) -> None:
    _clock(monkeypatch, NOW)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        await _founders(c, 99)
        founder = await c.h.create_user("founder@example.com", verified=True)
        assert (await _checkout_founding(c, founder, "founder@example.com")).status_code == 200
        await _hook(c, "transaction.completed", _founding_purchase("txn_f", "sub_f", founder, "ctm_f"))
        await _hook(c, "subscription.canceled", {"id": "sub_f", "customer_id": "ctm_f", "status": "canceled"})
        newcomer = await c.h.create_user("new@example.com", verified=True)

        public = (await c.h.client.get("/api/v1/billing/plans")).json()["founding"]
        assert (public["remaining"], public["available"], public["held_until"]) == (0, False, None)
        other = (await c.h.client.get("/api/v1/billing/plans", headers=c.h.jwt(newcomer, "new@example.com"))).json()
        assert other["founding"]["available"] is False and other["founding"]["held_until"] is None
        mine = (await c.h.client.get("/api/v1/billing/plans", headers=c.h.jwt(founder, "founder@example.com"))).json()
        assert mine["founding"]["available"] is True and mine["founding"]["held_kind"] == "lapsed"
        assert datetime.fromisoformat(mine["founding"]["held_until"]) == NOW + timedelta(days=14)
        # A bad token on the public endpoint is never an error.
        bad = await c.h.client.get("/api/v1/billing/plans", headers={"Authorization": "Bearer not-a-token"})
        assert bad.status_code == 200 and bad.json()["founding"]["available"] is False


# ---------------------------------------------------------------------------
# Superadmin hard delete and the erasure job
# ---------------------------------------------------------------------------


class _BrokenEraser(AccountEraser):
    async def erase(self, user_id: str) -> Any:  # type: ignore[override]
        raise ConnectionError("qdrant down")


async def test_a_failed_hard_delete_leaves_the_account_deactivated_and_due(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        owner = await _owner(c)
        uid, key = await c.account("hard@example.com")
        await _buy_solo(c, paddle, uid, "sub_hard", "ctm_hard")
        c.h.app.state.account_eraser = _BrokenEraser(c.h.db, None)

        r = await c.h.client.delete(f"/api/v1/admin/users/{uid}", params={"confirm": "true"}, headers=owner)
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert "1 subscription(s) cancelled" in detail and "ConnectionError" in detail and "marked for erasure" in detail
        assert paddle.subscriptions["sub_hard"]["status"] == "canceled"
        user = await c.h.db.get_user_by_id(uid)
        assert not user["is_active"] and user["deleted_at"]
        assert (await c.h.client.get("/api/v1/memories", headers=key)).status_code == 401
        assert uid in await AccountEraser(c.h.db, None).due_accounts(timedelta(days=7))

        # Retrying once the vector store is back finishes the job (billing is already cancelled).
        c.h.app.state.account_eraser = AccountEraser(c.h.db, None)
        r = await c.h.client.delete(f"/api/v1/admin/users/{uid}", params={"confirm": "true"}, headers=owner)
        assert r.status_code == 200, r.text
        assert await c.h.db.get_user_by_id(uid) is None


async def test_the_erasure_job_skips_an_account_made_active_again(tmp_path, monkeypatch) -> None:
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        undone, _ = await c.account("undone@example.com")
        gone, _ = await c.account("gone@example.com")
        long_ago = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        # 0741a96/b034314's activate endpoint sets is_active and leaves deleted_at.
        await c.h.db.conn.execute("UPDATE users SET deleted_at = ?, is_active = 1 WHERE id = ?", (long_ago, undone))
        await c.h.db.conn.execute("UPDATE users SET deleted_at = ?, is_active = 0 WHERE id = ?", (long_ago, gone))
        await c.h.db.conn.commit()
        due = await AccountEraser(c.h.db, None).due_accounts(timedelta(days=7))
        assert gone in due and undone not in due


# ---------------------------------------------------------------------------
# Team owners and the deletion email
# ---------------------------------------------------------------------------


async def test_a_team_owner_confirms_the_team_ends_and_the_account_is_emailed(tmp_path, monkeypatch) -> None:
    from remembra.teams.manager import TeamManager

    sent: list[tuple[str, str, str, int]] = []

    async def record(to: str, deleted_on: str, erase_after: str, cancelled: int) -> None:
        sent.append((to, deleted_on, erase_after, cancelled))

    monkeypatch.setattr(deletion, "account_deleted_notifier", record)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        owner_id, _ = await c.account("lead@example.com")
        member, _ = await c.account("member@example.com")
        solo, _ = await c.account("solo-owner@example.com")
        teams = TeamManager(c.h.db)
        team = await teams.create_team("Crew A", owner_id=owner_id)
        await teams.add_member(team["id"], member, invited_by=owner_id)
        await teams.create_team("Just me", owner_id=solo)  # no other members: no confirmation needed

        r = await _delete(c, owner_id, "lead@example.com")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "TEAM_OWNER" and "Crew A (1 other member)" in r.json()["detail"]["message"]
        assert (await c.h.db.get_user_by_id(owner_id))["deleted_at"] is None
        assert sent == []

        r = await _delete(c, owner_id, "lead@example.com", end_teams=True)
        assert r.status_code == 200, r.text
        body = r.json()
        assert sent == [("lead@example.com", body["deleted_at"][:10], body["erasure_after"][:10], 0)]

        assert (await _delete(c, solo, "solo-owner@example.com")).status_code == 200


def test_the_account_deleted_email_says_when_and_how_to_undo() -> None:
    from remembra.cloud import email_templates as tpl

    email = tpl.account_deleted(
        dashboard="https://app.remembra.dev", deleted_on="2026-09-26", erase_after="2026-10-03", subscriptions_cancelled=1
    )
    assert "2026-10-03" in email.text and "support@remembra.dev" in email.text
    assert "cancelled subscription and revoked API keys do not" in email.text
    assert "will not charge again" in email.text
    none = tpl.account_deleted(dashboard=None, deleted_on="d", erase_after="e", subscriptions_cancelled=0)
    assert "will not charge again" not in none.text


@pytest.mark.parametrize("kind", ["pending", "lapsed"])
async def test_founding_hold_of_reports_only_live_holds(tmp_path, monkeypatch, kind: str) -> None:
    _clock(monkeypatch, NOW)
    async with cost_app(tmp_path) as c:
        await c.h.db.conn.execute(
            "INSERT INTO founding_holds (user_id, kind, until, created_at) VALUES ('u1', ?, ?, ?)",
            (kind, (NOW + timedelta(hours=1)).isoformat(), NOW.isoformat()),
        )
        await c.h.db.conn.commit()
        assert await c.meter.founding_hold_of("u1") == (kind, NOW + timedelta(hours=1))
        monkeypatch.setattr(metering, "now_utc", lambda: NOW + timedelta(hours=2))
        assert await c.meter.founding_hold_of("u1") is None


async def test_signing_in_to_a_deleted_account_says_when_it_is_erased(tmp_path, monkeypatch) -> None:
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _setup(c)
        uid, _ = await c.account("leaving@example.com")
        body = (await _delete(c, uid, "leaving@example.com")).json()
        r = await c.h.client.post("/api/v1/auth/login", json={"email": "leaving@example.com", "password": PASSWORD})
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert f"deleted on {body['deleted_at'][:10]}" in detail and f"after {body['erasure_after'][:10]}" in detail
        assert "support@remembra.dev" in detail and "cannot sign up again" in detail
        # The wrong password learns nothing.
        r = await c.h.client.post("/api/v1/auth/login", json={"email": "leaving@example.com", "password": "nope-nope-1"})
        assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password"
