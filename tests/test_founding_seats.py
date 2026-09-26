"""R-26: the Founding 100 closes at seat 100, server side, and billing anomalies reach the owner.

* Checkout takes a seat BEFORE the buyer pays (a 2-hour hold), so the 101st
  buyer is refused at checkout, even when two buyers race for the last seat.
* The 101st founding webhook (a client-side purchase with a leaked price id)
  leaves 100 holders, flags the account and alerts the owner.
* Taking seat 100 archives the Founding price in Paddle; a seat freed later
  (a founder lapsed past the 14-day grace) re-activates it at the next checkout.
* A founder whose subscription ends keeps the seat for 14 days and can buy
  the price back; after that the seat is released.
* ``GET /billing/founding`` is public; ``GET /admin/billing-flags`` lists every
  flagged account; unmatched and unknown-price payments alert the owner.
* A Paddle API failure in checkout or portal is a clean 502 JSON error.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from remembra.api.v1 import admin
from remembra.cloud import metering
from remembra.cloud.plans import BillingInterval, PlanTier
from tests import _paddle_mock
from tests._cost_harness import cost_app
from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase
from tests.test_plans_billing_signup import PRICES, _paddle

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(metering, "now_utc", lambda: when)


async def _app(c: Any, alerts: RecordingAlerts) -> None:
    _paddle(c, **PRICES)
    c.h.app.state.alerts = alerts
    c.h.app.state.tasks = None
    c.h.app.include_router(admin.router, prefix="/api/v1")


async def _founders(c: Any, n: int, prefix: str = "f") -> None:
    now = NOW.isoformat()
    await c.h.db.conn.executemany(
        "INSERT INTO cloud_tenants (user_id, plan, founding, billing_interval, created_at, updated_at)"
        " VALUES (?, 'solo', 1, 'year', ?, ?)",
        [(f"{prefix}{i}", now, now) for i in range(n)],
    )
    await c.h.db.conn.commit()


async def _seats(c: Any) -> dict[str, Any]:
    r = await c.h.client.get("/api/v1/billing/founding")  # no credentials: public
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _checkout_founding(c: Any, uid: str, email: str) -> httpx.Response:
    return await c.h.client.post(
        "/api/v1/billing/checkout", json={"plan": "founding", "billing_cycle": "yearly"}, headers=c.h.jwt(uid, email)
    )


def _founding_purchase(txn: str, sub: str, uid: str, customer: str) -> dict[str, Any]:
    event = _purchase(txn, sub, "pri_founding", _bound(uid), customer=customer)
    event["current_billing_period"] = {"starts_at": NOW.isoformat()}
    return event


@pytest.fixture()
def alerts() -> RecordingAlerts:
    return RecordingAlerts()


async def test_the_101st_founding_webhook_leaves_100_holders_flags_and_alerts(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, NOW)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        await _founders(c, 100)
        late = await c.h.create_user("late@example.com")
        result = await _hook(c, "transaction.completed", _founding_purchase("txn_101", "sub_101", late, "ctm_101"))
        assert result["applied"] == "applied"
        assert await c.meter.founding_redemptions() == 100
        account = await c.meter.get_account(late)
        assert (account.tier, account.interval, account.founding) == (PlanTier.SOLO, BillingInterval.YEAR, False)
        assert (await c.meter.get_tenant(late))["billing_flag"] == "founding_over_cap"
        event, message, details = alerts.sent[-1]
        assert event == f"billing_flag:founding_over_cap:{late}"
        assert "honor the $108 price" in message and details["subscription_id"] == "sub_101"
        assert "refund the difference" not in message

        # The flag is listed for the owner, and can be cleared after review.
        owner = await c.h.create_user("owner@example.com", verified=True)
        owner_hdr = c.h.jwt(owner, "owner@example.com")
        flags = (await c.h.client.get("/api/v1/admin/billing-flags", headers=owner_hdr)).json()
        assert flags["total"] == 1
        row = flags["flags"][0]
        assert (row["user_id"], row["email"], row["billing_flag"], row["paddle_subscription_id"]) == (
            late,
            "late@example.com",
            "founding_over_cap",
            "sub_101",
        )
        member = c.h.jwt(late, "late@example.com")
        assert (await c.h.client.get("/api/v1/admin/billing-flags", headers=member)).status_code == 403
        r = await c.h.client.delete(f"/api/v1/admin/billing-flags/{late}", headers=owner_hdr)
        assert r.status_code == 200 and r.json()["flag"] == "founding_over_cap"
        assert (await c.h.client.get("/api/v1/admin/billing-flags", headers=owner_hdr)).json()["total"] == 0
        assert (await c.h.client.delete(f"/api/v1/admin/billing-flags/{late}", headers=owner_hdr)).status_code == 404


async def test_checkout_holds_the_seat_so_two_buyers_cannot_both_take_the_last_one(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, NOW)
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        await _founders(c, 99)
        assert await _seats(c) == {"max_redemptions": 100, "taken": 99, "remaining": 1, "available": True}
        a = await c.h.create_user("a@example.com")
        b = await c.h.create_user("b@example.com")
        ra, rb = await asyncio.gather(_checkout_founding(c, a, "a@example.com"), _checkout_founding(c, b, "b@example.com"))
        assert sorted([ra.status_code, rb.status_code]) == [200, 409]
        loser = rb if rb.status_code == 409 else ra
        assert "sold out" in loser.json()["detail"]
        assert len(paddle.requests("POST", "/transactions")) == 1  # the loser never reached Paddle
        assert await _seats(c) == {"max_redemptions": 100, "taken": 100, "remaining": 0, "available": False}
        plans = (await c.h.client.get("/api/v1/billing/plans")).json()
        assert plans["founding"]["remaining"] == 0 and plans["founding"]["available"] is False
        winner = a if ra.status_code == 200 else b
        cursor = await c.h.db.conn.execute("SELECT kind, transaction_id FROM founding_holds WHERE user_id = ?", (winner,))
        assert tuple(await cursor.fetchone()) == ("pending", "txn_mock_1")

        # The winner pays: the hold becomes the seat.
        result = await _hook(c, "transaction.completed", _founding_purchase("txn_w", "sub_w", winner, "ctm_w"))
        assert result["applied"] == "applied"
        assert (await c.meter.get_account(winner)).founding is True
        assert await _seats(c) == {"max_redemptions": 100, "taken": 100, "remaining": 0, "available": False}

        # Seat 100 archived the Founding price in Paddle and told the owner.
        assert paddle.prices == {"pri_founding": "archived"}
        assert any(e == "founding_100_full" and d["archived"] for e, _m, d in alerts.sent)


async def test_an_unpaid_hold_expires_and_a_failed_checkout_releases_it(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, NOW)
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        await _founders(c, 99)
        a = await c.h.create_user("a@example.com")
        b = await c.h.create_user("b@example.com")

        # Paddle is down: 502, and the seat is not kept for a checkout that never opened.
        paddle.failures["POST /transactions"] = httpx.ConnectError("paddle down")
        r = await _checkout_founding(c, a, "a@example.com")
        assert r.status_code == 502 and "try again" in r.json()["detail"]
        assert (await _seats(c))["remaining"] == 1
        paddle.failures.clear()

        assert (await _checkout_founding(c, a, "a@example.com")).status_code == 200
        assert (await _checkout_founding(c, b, "b@example.com")).status_code == 409
        # A walks away. Two hours later the seat is free again.
        _clock(monkeypatch, NOW + timedelta(hours=2, minutes=1))
        assert (await _seats(c))["remaining"] == 1
        assert (await _checkout_founding(c, b, "b@example.com")).status_code == 200
        # A comes back and pays for the old checkout after the seat went to B: over the cap.
        result = await _hook(c, "transaction.completed", _founding_purchase("txn_a", "sub_a", a, "ctm_a"))
        assert result["applied"] == "applied"
        assert (await c.meter.get_tenant(a))["billing_flag"] == "founding_over_cap"


async def test_a_lapsed_founder_keeps_the_seat_14_days_then_it_reopens(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, NOW)
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        await _founders(c, 99)
        founder = await c.h.create_user("founder@example.com")
        assert (await _checkout_founding(c, founder, "founder@example.com")).status_code == 200
        await _hook(c, "transaction.completed", _founding_purchase("txn_f", "sub_f", founder, "ctm_f"))
        assert paddle.prices == {"pri_founding": "archived"}

        # The founder cancels: back to Free, but the seat and the price are kept for 14 days.
        await _hook(c, "subscription.canceled", {"id": "sub_f", "customer_id": "ctm_f", "status": "canceled"})
        account = await c.meter.get_account(founder)
        assert (account.tier, account.founding) == (PlanTier.FREE, False)
        assert await _seats(c) == {"max_redemptions": 100, "taken": 100, "remaining": 0, "available": False}
        newcomer = await c.h.create_user("new@example.com")
        assert (await _checkout_founding(c, newcomer, "new@example.com")).status_code == 409

        # Within the grace the founder buys the price back.
        _clock(monkeypatch, NOW + timedelta(days=10))
        assert (await _checkout_founding(c, founder, "founder@example.com")).status_code == 200
        await _hook(c, "transaction.completed", _founding_purchase("txn_f2", "sub_f2", founder, "ctm_f"))
        assert (await c.meter.get_account(founder)).founding is True
        assert (await c.meter.get_tenant(founder))["billing_flag"] is None

        # Lapses again and stays away past 14 days: the seat is released and the price reopens.
        await _hook(c, "subscription.canceled", {"id": "sub_f2", "customer_id": "ctm_f", "status": "canceled"})
        _clock(monkeypatch, NOW + timedelta(days=25))
        assert await _seats(c) == {"max_redemptions": 100, "taken": 99, "remaining": 1, "available": True}
        assert (await _checkout_founding(c, newcomer, "new@example.com")).status_code == 200
        assert paddle.prices == {"pri_founding": "active"}  # re-activated before the transaction
        patch_index = next(i for i, call in enumerate(paddle.calls) if call[0] == "PATCH" and call[3] == {"status": "active"})
        txn_index = max(i for i, call in enumerate(paddle.calls) if call[:2] == ("POST", "/transactions"))
        assert patch_index < txn_index


async def test_founders_already_on_free_become_lapsed_once(tmp_path, monkeypatch) -> None:
    _clock(monkeypatch, NOW)
    async with cost_app(tmp_path) as c:
        db = c.h.db
        await db.conn.execute("DELETE FROM cloud_migrations WHERE name = '2026_09_founding_lapse_holds'")
        old = (NOW - timedelta(days=30)).isoformat()
        recent = (NOW - timedelta(days=3)).isoformat()
        await db.conn.executemany(
            "INSERT INTO cloud_tenants (user_id, plan, founding, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            [("paying", "solo", old, old), ("gone_long", "free", old, old), ("gone_recent", "free", recent, recent)],
        )
        await db.conn.commit()
        await c.meter.init_schema()
        assert await c.meter.founding_redemptions() == 1
        assert await c.meter.founding_seats_taken() == 2  # paying + gone_recent (inside its 14 days)
        cursor = await db.conn.execute("SELECT user_id, kind FROM founding_holds ORDER BY user_id")
        assert [tuple(r) for r in await cursor.fetchall()] == [("gone_long", "lapsed"), ("gone_recent", "lapsed")]
        await c.meter.init_schema()  # runs once
        cursor = await db.conn.execute("SELECT COUNT(*) FROM founding_holds")
        assert (await cursor.fetchone())[0] == 2


async def test_unmatched_and_unknown_price_payments_alert_the_owner(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, NOW)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        # Nobody can be identified: no custom_data, unknown customer and subscription.
        orphan = _purchase("txn_o", "sub_o", "pri_solo_m", {}, customer="ctm_nobody")
        assert (await _hook(c, "transaction.completed", orphan))["applied"] == "unmatched"
        event, _message, details = alerts.sent[-1]
        assert event == "paddle_unmatched_purchase:sub_o" and details["customer_id"] == "ctm_nobody"
        # A price the catalog does not know.
        uid = await c.h.create_user("buyer@example.com")
        odd = _purchase("txn_x", "sub_x", "pri_not_in_catalog", _bound(uid), customer="ctm_b")
        assert (await _hook(c, "transaction.completed", odd))["applied"] == "no_change"
        event, _message, details = alerts.sent[-1]
        assert event == "paddle_unknown_price:txn_x" and details["prices"] == ["pri_not_in_catalog"]
        # Team below the seat minimum: flagged and alerted.
        await _hook(
            c, "transaction.completed", _purchase("txn_t", "sub_t", "pri_team_m", _bound(uid), customer="ctm_b", quantity=2)
        )
        assert (await c.meter.get_tenant(uid))["billing_flag"] == "team_seats_below_minimum"
        assert alerts.sent[-1][0] == f"billing_flag:team_seats_below_minimum:{uid}"


# ---------------------------------------------------------------------------
# Paddle API errors in checkout and portal: 502 JSON, never a bare 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", [500, 429, httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_checkout_and_portal_turn_paddle_failures_into_502(tmp_path, monkeypatch, failure) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("err@example.com")
        hdr = c.h.jwt(uid, "err@example.com")
        paddle.failures["POST /transactions"] = failure
        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=hdr)
        assert r.status_code == 502, r.text
        assert r.headers["content-type"].startswith("application/json")
        assert "Nothing was charged" in r.json()["detail"]

        paddle.failures["GET /customers"] = failure
        r = await c.h.client.post("/api/v1/billing/portal", headers=hdr)
        assert r.status_code == 502 and r.json()["detail"].startswith("Our billing provider")

        # The recorded customer id is used directly (no lookup by email).
        await c.meter.register_tenant(uid, PlanTier.FREE, stripe_customer_id="ctm_err")
        paddle.failures["POST /customers/ctm_err/portal-sessions"] = failure
        assert (await c.h.client.post("/api/v1/billing/portal", headers=hdr)).status_code == 502
        paddle.failures.clear()
        r = await c.h.client.post("/api/v1/billing/portal", headers=hdr)
        assert r.status_code == 200 and r.json()["portal_url"] == "https://portal.example/ctm_err"


async def test_portal_without_a_billing_account_is_404_not_502(tmp_path, monkeypatch) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("new+tag@example.com")
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(uid, "new+tag@example.com"))
        assert r.status_code == 404
        # The email is sent as a query parameter, encoded ("+" survives).
        assert paddle.requests("GET", "/customers")[-1][2] == {"email": "new+tag@example.com"}
        paddle.customers_by_email["new+tag@example.com"] = "ctm_found"
        r = await c.h.client.post("/api/v1/billing/portal", headers=c.h.jwt(uid, "new+tag@example.com"))
        assert r.status_code == 200 and r.json()["portal_url"] == "https://portal.example/ctm_found"


async def test_moving_off_the_founding_price_ends_the_lock_with_the_same_grace(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, NOW)
    _paddle_mock.install(monkeypatch)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        founder = await c.h.create_user("mover@example.com")
        await _hook(c, "transaction.completed", _founding_purchase("txn_m", "sub_m", founder, "ctm_m"))
        assert (await c.meter.get_account(founder)).founding is True
        # Renewals keep it.
        await _hook(c, "transaction.completed", _founding_purchase("txn_m2", "sub_m", founder, "ctm_m"))
        assert (await c.meter.get_account(founder)).founding is True

        # A plan change in the portal: the same subscription now bills Pro monthly.
        changed = {
            "id": "sub_m",
            "status": "active",
            "customer_id": "ctm_m",
            "items": [{"price": {"id": "pri_pro_m"}, "quantity": 1}],
            "custom_data": _bound(founder),
        }
        assert (await _hook(c, "subscription.updated", changed))["applied"] == "applied"
        account = await c.meter.get_account(founder)
        assert (account.tier, account.founding) == (PlanTier.PRO, False)
        assert (await _seats(c))["taken"] == 1  # the seat is held through the 14-day grace
        _clock(monkeypatch, NOW + timedelta(days=15))
        assert (await _seats(c))["taken"] == 0
