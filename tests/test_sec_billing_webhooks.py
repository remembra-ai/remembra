"""Security sweep 2026-09-26, billing: what a Paddle webhook may change.

* BILL-5: a NEW subscription on a retired price ($49 Pro, $199 Team) never
  grants the legacy tier; renewals of a legacy subscription the account holds
  keep it.
* BILL-2: a repeat delivery of the same Paddle event is a duplicate (one state
  change), and an event older than a cancel, a refund or a newer applied event
  of the same subscription changes nothing.
* BILL-4: an annual renewal that has not been paid (past its paid-through date,
  or past due) releases at most one month of the new year's credit bank; the
  payment releases the rest.

Every test drives the real webhook route (signed body in, tenant row out).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from remembra.cloud.plans import BillingInterval, PlanTier
from tests import _paddle_mock
from tests._cost_harness import cost_app
from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase, _state
from tests.test_plans_billing_signup import LEGACY_PRO_PRICE, PRICES, _paddle, _signed

LEGACY_TEAM_PRICE = "pri_01kmepewmfpqdz413hc4f4fr3r"  # production $199/mo


@pytest.fixture()
def alerts() -> RecordingAlerts:
    return RecordingAlerts()


async def _app(c: Any, alerts: RecordingAlerts) -> None:
    _paddle(c, **PRICES)
    c.h.app.state.alerts = alerts
    c.h.app.state.tasks = None


# ---------------------------------------------------------------------------
# BILL-5: retired prices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price", [LEGACY_PRO_PRICE, LEGACY_TEAM_PRICE])
async def test_a_new_purchase_of_a_retired_price_does_not_grant_the_legacy_tier(tmp_path, alerts, price) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("new-legacy@example.com")
        # Paddle.js opened with the old price id, bound to the buyer's own account.
        result = await _hook(c, "transaction.completed", _purchase("txn_l", "sub_l", price, _bound(uid), customer="ctm_l"))
        assert result["applied"] == "flagged"
        plan, sub, flag = await _state(c, uid)
        assert (plan, sub, flag) == ("free", None, "legacy_price_new_purchase")
        assert (await c.meter.get_account(uid)).tier == PlanTier.FREE
        event, _message, details = alerts.sent[-1]
        assert event == f"paddle_legacy_price_new_purchase:{uid}:sub_l"
        assert details["subscription_id"] == "sub_l" and details["plan"] in ("legacy_pro_49", "legacy_team_199")

        # Its renewals are refused the same way (the account never held it).
        renew = _purchase("txn_l2", "sub_l", price, _bound(uid), customer="ctm_l")
        assert (await _hook(c, "transaction.completed", renew))["applied"] == "flagged"
        assert (await _state(c, uid))[0] == "free"


async def test_a_held_legacy_subscription_keeps_renewing(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("held-legacy@example.com")
        await c.meter.apply_subscription(uid, PlanTier.LEGACY_PRO, interval=BillingInterval.MONTH, subscription_id="sub_old")
        renew = _purchase("txn_r", "sub_old", LEGACY_PRO_PRICE, {"remembra_user_id": uid, "plan": "pro"})
        assert (await _hook(c, "transaction.completed", renew))["applied"] == "applied"
        update = {**renew, "id": "sub_old", "status": "active"}
        assert (await _hook(c, "subscription.updated", update))["applied"] == "applied"
        assert await _state(c, uid) == ("legacy_pro_49", "sub_old", None)
        assert alerts.sent == []


async def test_a_legacy_row_without_a_recorded_subscription_keeps_its_own_tier(tmp_path, alerts) -> None:
    """Rows migrated before subscription ids were stored: a renewal on the same legacy tier still applies."""
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("migrated@example.com")
        await c.meter.register_tenant(uid, PlanTier.LEGACY_TEAM, stripe_customer_id="ctm_m")
        renew = _purchase("txn_m", "sub_m", LEGACY_TEAM_PRICE, {"remembra_user_id": uid}, customer="ctm_m")
        assert (await _hook(c, "transaction.completed", renew))["applied"] == "applied"
        assert await _state(c, uid) == ("legacy_team_199", "sub_m", None)
        # ...but not a move to the OTHER legacy tier.
        other = await c.h.create_user("migrated-pro@example.com")
        await c.meter.register_tenant(other, PlanTier.LEGACY_PRO, stripe_customer_id="ctm_o")
        upgrade = _purchase("txn_o", "sub_o", LEGACY_TEAM_PRICE, {"remembra_user_id": other}, customer="ctm_o")
        assert (await _hook(c, "transaction.completed", upgrade))["applied"] == "flagged"
        assert (await _state(c, other))[0] == "legacy_pro_49"


# ---------------------------------------------------------------------------
# BILL-2: duplicate and out-of-order deliveries
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _at(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _envelope(event_type: str, data: dict[str, Any], event_id: str, minutes: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "notification_id": f"ntf_{event_id}",
        "event_type": event_type,
        "occurred_at": _at(minutes),
        "data": data,
    }


async def _post(c: Any, event: dict[str, Any]) -> dict[str, Any]:
    body, headers = _signed(event)
    r = await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _update(sub: str, price: str, custom: dict[str, Any], *, customer: str = "ctm_x") -> dict[str, Any]:
    return {"id": sub, "status": "active", "customer_id": customer, "items": [{"price": {"id": price}}], "custom_data": custom}


async def test_the_same_event_twice_is_applied_once(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("twice@example.com")
        event = _envelope("transaction.completed", _purchase("txn_1", "sub_1", "pri_pro_m", _bound(uid)), "evt_1", 0)
        first = await _post(c, event)
        assert first["applied"] == "applied"
        stamp = (await c.meter.get_tenant(uid))["updated_at"]
        again = await _post(c, event)  # the same signed body: a Paddle retry
        assert again == {"status": "duplicate", "action": "ignored", "applied": "duplicate"}
        assert (await c.meter.get_tenant(uid))["updated_at"] == stamp
        # A replay carries the same event id under a new notification id: still a duplicate.
        replay = {**event, "notification_id": "ntf_replayed"}
        assert (await _post(c, replay))["applied"] == "duplicate"


async def test_a_late_update_after_a_cancel_leaves_the_account_on_free(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("late-update@example.com")
        buy = _purchase("txn_p", "sub_p", "pri_pro_m", _bound(uid))
        assert (await _post(c, _envelope("transaction.completed", buy, "evt_buy", 0)))["applied"] == "applied"
        cancel = {"id": "sub_p", "status": "canceled", "custom_data": _bound(uid)}
        assert (await _post(c, _envelope("subscription.canceled", cancel, "evt_cancel", 30)))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "free"
        # An update that occurred before the cancel, delivered after it (a retried delivery).
        late = _envelope("subscription.updated", _update("sub_p", "pri_pro_m", _bound(uid)), "evt_late", 10)
        assert (await _post(c, late))["applied"] == "stale"
        # Same for a late activation with no custom_data at all.
        activated = _envelope("subscription.activated", _update("sub_p", "pri_pro_m", {}), "evt_act", 5)
        assert (await _post(c, activated))["applied"] == "stale"
        assert (await _state(c, uid))[0] == "free"
        assert (await c.meter.get_account(uid)).tier == PlanTier.FREE


async def test_a_late_payment_after_a_full_refund_leaves_the_account_on_free(tmp_path, monkeypatch, alerts) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.add_subscription("sub_r", "ctm_r")
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("refunded@example.com")
        buy = _purchase("txn_r1", "sub_r", "pri_pro_m", _bound(uid), customer="ctm_r")
        assert (await _post(c, _envelope("transaction.completed", buy, "evt_r1", 0)))["applied"] == "applied"
        refund = {
            "id": "adj_1",
            "action": "refund",
            "type": "full",
            "status": "approved",
            "transaction_id": "txn_r1",
            "subscription_id": "sub_r",
            "customer_id": "ctm_r",
            "totals": {"currency_code": "USD", "total": "2900", "earnings": "2500"},
        }
        assert (await _post(c, _envelope("adjustment.updated", refund, "evt_adj", 60)))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "free"
        assert paddle.subscriptions["sub_r"]["status"] == "canceled"
        # A payment of the same subscription that occurred before the refund, delivered late.
        late = _purchase("txn_r2", "sub_r", "pri_pro_m", _bound(uid), customer="ctm_r")
        assert (await _post(c, _envelope("transaction.completed", late, "evt_r2", 30)))["applied"] == "stale"
        assert (await _state(c, uid))[0] == "free"


@pytest.mark.parametrize("interval", [BillingInterval.MONTH, BillingInterval.YEAR])
async def test_a_partial_refund_keeps_the_plan_bank_and_renewal_and_records_revenue_once(
    tmp_path, monkeypatch, alerts, interval
) -> None:
    paddle = _paddle_mock.install(monkeypatch)
    paddle.add_subscription("sub_partial", "ctm_partial")
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("partial@example.com")
        await c.meter.apply_subscription(
            uid,
            PlanTier.PRO,
            interval=interval,
            customer_id="ctm_partial",
            subscription_id="sub_partial",
        )
        await c.set_credits_used(uid, 10)
        before = await c.meter.get_account(uid)
        balance = await c.meter.get_credit_balance(before)
        refund = {
            "id": "adj_partial",
            "action": "refund",
            "type": "partial",
            "status": "approved",
            "transaction_id": "txn_partial",
            "subscription_id": "sub_partial",
            "customer_id": "ctm_partial",
            "totals": {"currency_code": "USD", "total": "1200", "earnings": "1000"},
        }
        event = _envelope("adjustment.updated", refund, "evt_partial", 60)
        result = await _post(c, event)
        assert (result["action"], result["applied"]) == ("refund_partial", "no_change")
        assert await _state(c, uid) == ("pro", "sub_partial", None)
        after = await c.meter.get_account(uid)
        assert (after.interval, after.period, after.credit_limit) == (before.interval, before.period, before.credit_limit)
        assert await c.meter.get_credit_balance(after) == balance
        assert paddle.subscriptions["sub_partial"]["status"] == "active"
        assert paddle.requests("POST", "/subscriptions/sub_partial/cancel") == []

        # Duplicate notification and replay with another event id must not
        # deduct the same adjustment twice from the revenue ledger.
        assert (await _post(c, event))["applied"] == "duplicate"
        replay = _envelope("adjustment.updated", refund, "evt_partial_replay", 61)
        assert (await _post(c, replay))["applied"] == "no_change"
        cursor = await c.h.app.state.db.conn.execute(
            "SELECT net_usd FROM cloud_revenue_events WHERE transaction_id = ?", ("adj:adj_partial",)
        )
        assert [tuple(row) for row in await cursor.fetchall()] == [(-10.0,)]
        assert alerts.sent == []


async def test_an_older_update_never_overwrites_a_newer_one(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("order@example.com")
        buy = _purchase("txn_o", "sub_o", "pri_solo_m", _bound(uid))
        assert (await _post(c, _envelope("transaction.completed", buy, "evt_o1", 0)))["applied"] == "applied"
        newer = _envelope("subscription.updated", _update("sub_o", "pri_pro_m", _bound(uid)), "evt_o3", 20)
        assert (await _post(c, newer))["applied"] == "applied"
        older = _envelope("subscription.updated", _update("sub_o", "pri_solo_m", _bound(uid)), "evt_o2", 10)
        assert (await _post(c, older))["applied"] == "stale"
        assert (await _state(c, uid))[0] == "pro"
        # A newer change still applies.
        newest = _envelope("subscription.updated", _update("sub_o", "pri_solo_m", _bound(uid)), "evt_o4", 40)
        assert (await _post(c, newest))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "solo"


async def test_a_failed_delivery_is_processed_again_on_retry(tmp_path, alerts, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("retry@example.com")
        event = _envelope("transaction.completed", _purchase("txn_f", "sub_f", "pri_solo_m", _bound(uid)), "evt_f", 0)
        real = c.meter.apply_subscription

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(c.meter, "apply_subscription", broken)
        body, headers = _signed(event)
        with pytest.raises(RuntimeError):
            await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        monkeypatch.setattr(c.meter, "apply_subscription", real)
        assert (await _post(c, event))["applied"] == "applied"  # the claim was released
        assert (await _state(c, uid))[0] == "solo"


async def test_an_unmatched_event_can_be_replayed_later(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        event = _envelope("transaction.completed", _purchase("txn_u", "sub_u", "pri_solo_m", {}, customer="ctm_u"), "evt_u", 0)
        assert (await _post(c, event))["applied"] == "unmatched"
        # The owner links the customer to its account, then replays the event from Paddle.
        uid = await c.h.create_user("found-later@example.com")
        await c.meter.register_tenant(uid, PlanTier.FREE, stripe_customer_id="ctm_u")
        assert (await _post(c, event))["applied"] == "applied"


async def test_webhook_claims_are_exclusive_until_finished_or_stale(tmp_path, monkeypatch) -> None:
    from remembra.cloud import metering

    async with cost_app(tmp_path) as c:
        meter = c.meter
        assert await meter.claim_webhook_event("evt_c", "transaction.completed", T0) == "claimed"
        assert await meter.claim_webhook_event("evt_c", "transaction.completed", T0) == "busy"
        later = datetime.now(UTC) + metering.WEBHOOK_CLAIM_STALE + timedelta(seconds=1)
        monkeypatch.setattr(metering, "now_utc", lambda: later)
        assert await meter.claim_webhook_event("evt_c", "transaction.completed", T0) == "claimed"  # crashed claim
        await meter.finish_webhook_event("evt_c", "applied")
        assert await meter.claim_webhook_event("evt_c", "transaction.completed", T0) == "duplicate"
        # Finished ids are pruned after the retention window.
        pruned = later + metering.WEBHOOK_EVENT_RETENTION + timedelta(days=1)
        monkeypatch.setattr(metering, "now_utc", lambda: pruned)
        assert await meter.claim_webhook_event("evt_other", None, None) == "claimed"
        assert await meter.claim_webhook_event("evt_c", "transaction.completed", T0) == "claimed"


# ---------------------------------------------------------------------------
# BILL-4: the next year's bank waits for the renewal payment
# ---------------------------------------------------------------------------

YEAR_START = datetime(2025, 9, 26, 9, tzinfo=UTC)
ANNIVERSARY = datetime(2026, 9, 26, 9, tzinfo=UTC)
ANNUAL_PRICES = {**PRICES, "paddle_price_pro_annual": "pri_pro_y"}


def _clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    from remembra.cloud import metering

    monkeypatch.setattr(metering, "now_utc", lambda: when)


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _annual_payment(txn: str, uid: str, starts: datetime, ends: datetime) -> dict[str, Any]:
    data = _purchase(txn, "sub_year", "pri_pro_y", _bound(uid), customer="ctm_year")
    data["billing_period"] = {"starts_at": _iso(starts), "ends_at": _iso(ends)}
    return data


def _event(event_type: str, data: dict[str, Any], event_id: str, when: datetime) -> dict[str, Any]:
    return {"event_id": event_id, "event_type": event_type, "occurred_at": _iso(when), "data": data}


async def test_an_unpaid_annual_renewal_releases_one_month_until_it_is_paid(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, YEAR_START)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        _paddle(c, **ANNUAL_PRICES)
        uid = await c.h.create_user("annual-renewal@example.com")
        first = _annual_payment("txn_y1", uid, YEAR_START, ANNIVERSARY)
        assert (await _post(c, _event("transaction.completed", first, "evt_y1", YEAR_START)))["applied"] == "applied"
        assert (await c.meter.get_tenant(uid))["paid_through"] == ANNIVERSARY.isoformat()

        # Mid-year the whole paid bank is there.
        _clock(monkeypatch, YEAR_START + timedelta(days=200))
        assert (await c.meter.get_account(uid)).credit_limit == 60_000

        # The renewal charge fails at the anniversary; Paddle's dunning starts.
        past_due = {"id": "sub_year", "status": "past_due", "customer_id": "ctm_year", "items": first["items"]}
        _clock(monkeypatch, ANNIVERSARY + timedelta(minutes=5))
        result = await _post(c, _event("subscription.past_due", past_due, "evt_pd", ANNIVERSARY + timedelta(minutes=5)))
        assert result["action"] == "payment_failed"
        assert (await c.meter.get_tenant(uid))["past_due_at"] is not None

        # Two hours in: still Pro, but only one month of the new year's credits.
        _clock(monkeypatch, ANNIVERSARY + timedelta(hours=2))
        account = await c.meter.get_account(uid)
        assert (account.tier, account.period.key) == (PlanTier.PRO, "Y:2026-09-26")
        assert account.credit_limit == 5_000
        await c.set_credits_used(uid, 5_000)
        assert await c.meter.reserve_credits(account, 1, min_credits=1) is None

        # The dunning retry succeeds: the whole new bank is released.
        _clock(monkeypatch, ANNIVERSARY + timedelta(days=3))
        paid = _annual_payment("txn_y2", uid, ANNIVERSARY, datetime(2027, 9, 26, 9, tzinfo=UTC))
        assert (await _post(c, _event("transaction.completed", paid, "evt_y2", ANNIVERSARY + timedelta(days=3))))[
            "applied"
        ] == "applied"
        tenant = await c.meter.get_tenant(uid)
        assert tenant["past_due_at"] is None and tenant["paid_through"] == "2027-09-26T09:00:00+00:00"
        account = await c.meter.get_account(uid)
        assert account.credit_limit == 60_000


async def test_past_due_alone_holds_the_bank_of_an_account_without_a_paid_through_date(tmp_path, monkeypatch, alerts) -> None:
    """Annual subscribers from before paid_through was recorded: the past_due event is the signal."""
    _clock(monkeypatch, ANNIVERSARY + timedelta(hours=2))
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        _paddle(c, **ANNUAL_PRICES)
        uid = await c.h.create_user("older-annual@example.com")
        await c.meter.apply_subscription(
            uid, PlanTier.PRO, interval=BillingInterval.YEAR, period_anchor=YEAR_START, subscription_id="sub_old_y"
        )
        assert (await c.meter.get_account(uid)).credit_limit == 60_000
        issue = {"id": "sub_old_y", "status": "past_due", "items": [{"price": {"id": "pri_pro_y"}}]}
        result = await _post(c, _event("subscription.updated", issue, "evt_issue", ANNIVERSARY))
        assert result["action"] == "payment_issue"
        assert (await c.meter.get_account(uid)).credit_limit == 5_000
        # A late past_due that occurred before the recovery changes nothing afterwards.
        active = {"id": "sub_old_y", "status": "active", "items": [{"price": {"id": "pri_pro_y"}}]}
        assert (await _post(c, _event("subscription.updated", active, "evt_active", ANNIVERSARY + timedelta(hours=1))))[
            "applied"
        ] == "applied"
        assert (await c.meter.get_account(uid)).credit_limit == 60_000
        late = await _post(c, _event("subscription.past_due", {**issue}, "evt_late_pd", ANNIVERSARY + timedelta(minutes=30)))
        assert late["action"] == "payment_failed"
        assert (await c.meter.get_account(uid)).credit_limit == 60_000
        # A past_due for a subscription the account does not hold changes nothing either.
        other = {"id": "sub_not_held", "status": "past_due", "items": [{"price": {"id": "pri_pro_y"}}]}
        await _post(c, _event("subscription.past_due", other, "evt_other_pd", ANNIVERSARY + timedelta(hours=1, minutes=5)))
        assert (await c.meter.get_account(uid)).credit_limit == 60_000


# ---------------------------------------------------------------------------
# BILL-2 x BILL-4 (review regression): a paid renewal delivered after a newer
# event of its subscription still records the period it paid for.
# ---------------------------------------------------------------------------

NEXT_ANNIVERSARY = datetime(2027, 9, 26, 9, tzinfo=UTC)


def _annual_update(uid: str, starts: datetime, ends: datetime) -> dict[str, Any]:
    return {
        "id": "sub_year",
        "status": "active",
        "customer_id": "ctm_year",
        "items": [{"price": {"id": "pri_pro_y"}, "quantity": 1}],
        "custom_data": _bound(uid),
        "current_billing_period": {"starts_at": _iso(starts), "ends_at": _iso(ends)},
    }


@pytest.mark.parametrize("order", ["payment_first", "update_first"])
async def test_a_paid_annual_renewal_releases_the_year_whatever_the_delivery_order(tmp_path, monkeypatch, alerts, order) -> None:
    _clock(monkeypatch, YEAR_START)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        _paddle(c, **ANNUAL_PRICES)
        uid = await c.h.create_user(f"annual-{order}@example.com")
        first = _annual_payment("txn_y1", uid, YEAR_START, ANNIVERSARY)
        assert (await _post(c, _event("transaction.completed", first, "evt_y1", YEAR_START)))["applied"] == "applied"

        # The renewal is paid at +10s; the subscription moves to its new period at +20s.
        _clock(monkeypatch, ANNIVERSARY + timedelta(minutes=2))
        paid = _event(
            "transaction.completed",
            _annual_payment("txn_y2", uid, ANNIVERSARY, NEXT_ANNIVERSARY),
            "evt_y2",
            ANNIVERSARY + timedelta(seconds=10),
        )
        update = _event(
            "subscription.updated",
            _annual_update(uid, ANNIVERSARY, NEXT_ANNIVERSARY),
            "evt_upd",
            ANNIVERSARY + timedelta(seconds=20),
        )
        deliveries = [paid, update] if order == "payment_first" else [update, paid]
        outcomes = [(await _post(c, event))["applied"] for event in deliveries]
        expected = ["applied", "applied"] if order == "payment_first" else ["applied", "stale"]
        assert outcomes == expected

        tenant = await c.meter.get_tenant(uid)
        assert tenant["paid_through"] == NEXT_ANNIVERSARY.isoformat()
        assert tenant["past_due_at"] is None
        _clock(monkeypatch, ANNIVERSARY + timedelta(hours=2))
        account = await c.meter.get_account(uid)
        assert (account.tier, account.credit_limit) == (PlanTier.PRO, 60_000)


async def test_a_late_renewal_payment_clears_an_older_past_due_and_can_be_replayed(tmp_path, monkeypatch, alerts) -> None:
    _clock(monkeypatch, YEAR_START)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        _paddle(c, **ANNUAL_PRICES)
        uid = await c.h.create_user("annual-late-payment@example.com")
        first = _annual_payment("txn_y1", uid, YEAR_START, ANNIVERSARY)
        assert (await _post(c, _event("transaction.completed", first, "evt_y1", YEAR_START)))["applied"] == "applied"

        # The first charge fails (past_due), the retry succeeds at +1 day, but
        # a later subscription.updated (+1 day 1 min) is delivered first.
        _clock(monkeypatch, ANNIVERSARY + timedelta(days=1, minutes=5))
        past_due = {"id": "sub_year", "status": "past_due", "customer_id": "ctm_year", "items": first["items"]}
        await _post(c, _event("subscription.past_due", past_due, "evt_pd", ANNIVERSARY + timedelta(minutes=1)))
        update = _annual_update(uid, ANNIVERSARY, NEXT_ANNIVERSARY)
        assert (await _post(c, _event("subscription.updated", update, "evt_upd", ANNIVERSARY + timedelta(days=1, minutes=1))))[
            "applied"
        ] == "applied"
        paid = _event(
            "transaction.completed",
            _annual_payment("txn_y2", uid, ANNIVERSARY, NEXT_ANNIVERSARY),
            "evt_y2",
            ANNIVERSARY + timedelta(days=1),
        )

        # The payment's first delivery fails (a deploy restart); Paddle retries it.
        real = c.meter.record_late_payment

        async def broken(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("restarting")

        monkeypatch.setattr(c.meter, "record_late_payment", broken)
        body, headers = _signed(paid)
        with pytest.raises(RuntimeError):
            await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        monkeypatch.setattr(c.meter, "record_late_payment", real)
        assert (await _post(c, paid))["applied"] == "stale"  # the plan change is not re-applied...
        tenant = await c.meter.get_tenant(uid)
        assert tenant["paid_through"] == NEXT_ANNIVERSARY.isoformat()  # ...but the paid period is recorded
        assert tenant["past_due_at"] is None
        assert (await c.meter.get_account(uid)).credit_limit == 60_000

        # A stale outcome is not kept as "done": the owner's replay from Paddle is processed again.
        assert (await _post(c, {**paid, "notification_id": "ntf_replay"}))["applied"] == "stale"
        assert (await c.meter.get_tenant(uid))["paid_through"] == NEXT_ANNIVERSARY.isoformat()


async def test_a_late_payment_never_moves_the_paid_period_back_or_revives_an_ended_subscription(
    tmp_path, monkeypatch, alerts
) -> None:
    _clock(monkeypatch, YEAR_START)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        _paddle(c, **ANNUAL_PRICES)
        uid = await c.h.create_user("annual-older-payment@example.com")
        renewal = _annual_payment("txn_y2", uid, ANNIVERSARY, NEXT_ANNIVERSARY)
        assert (await _post(c, _event("transaction.completed", renewal, "evt_y2", YEAR_START + timedelta(days=1))))[
            "applied"
        ] == "applied"
        # An older payment (a shorter period) delivered afterwards: stale, and paid_through stays.
        older = _annual_payment("txn_y1", uid, YEAR_START, ANNIVERSARY)
        assert (await _post(c, _event("transaction.completed", older, "evt_y1", YEAR_START)))["applied"] == "stale"
        assert (await c.meter.get_tenant(uid))["paid_through"] == NEXT_ANNIVERSARY.isoformat()

        # After a cancel, a late payment of the ended subscription records nothing.
        cancel = {"id": "sub_year", "status": "canceled", "custom_data": _bound(uid)}
        assert (await _post(c, _event("subscription.canceled", cancel, "evt_cancel", YEAR_START + timedelta(days=3))))[
            "applied"
        ] == "applied"
        late = _annual_payment("txn_y3", uid, NEXT_ANNIVERSARY, datetime(2028, 9, 26, 9, tzinfo=UTC))
        assert (await _post(c, _event("transaction.completed", late, "evt_y3", YEAR_START + timedelta(days=2))))[
            "applied"
        ] == "stale"
        tenant = await c.meter.get_tenant(uid)
        assert (tenant["plan"], tenant["paid_through"]) == ("free", None)
