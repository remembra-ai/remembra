"""R-27: a new yearly credit bank unlocks in full only after 14 days.

Paddle refunds within 14 days (and the EU withdrawal right does the same), but
the AI spend of credits used before a refund cannot be taken back. So a NEW
yearly bank releases one month's credits for its first 14 days and the rest on
day 15. Renewals of a subscription the account already holds are not held back.
A refund of an account that used more than 25% of its bank alerts the owner.
Driven through the real webhook route and the real credit ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from remembra.cloud import metering
from remembra.cloud.plans import BillingInterval, PlanTier
from tests._cost_harness import cost_app
from tests.test_paddle_subscription_ownership import RecordingAlerts, _bound, _hook, _purchase
from tests.test_plans_billing_signup import PRICES, _paddle

START = datetime(2026, 10, 1, 9, tzinfo=UTC)
ANNUAL = {**PRICES, "paddle_price_pro_annual": "pri_pro_y"}


def _at(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(metering, "now_utc", lambda: when)


def _annual(txn: str, sub: str, uid: str, price: str = "pri_pro_y", starts: datetime = START) -> dict[str, Any]:
    event = _purchase(txn, sub, price, _bound(uid), customer=f"ctm_{uid[-6:]}")
    event["current_billing_period"] = {"starts_at": starts.isoformat()}
    return event


async def _spendable(c: Any, uid: str) -> int:
    """Credits the account can still reserve right now, probed through the real ledger."""
    account = await c.meter.get_account(uid)
    balance = await c.meter.get_credit_balance(account)
    return balance.remaining


async def test_pro_annual_holds_the_bank_for_14_days_then_unlocks_it(tmp_path, monkeypatch) -> None:
    _at(monkeypatch, START)
    async with cost_app(tmp_path) as c:
        _paddle(c, **ANNUAL)
        uid, key = await c.account("annual@example.com")
        await _hook(c, "transaction.completed", _annual("txn_1", "sub_1", uid))

        _at(monkeypatch, START + timedelta(days=3))
        account = await c.meter.get_account(uid)
        assert (account.tier, account.interval) == (PlanTier.PRO, BillingInterval.YEAR)
        assert (account.credit_limit, account.full_credit_limit) == (5_000, 60_000)
        assert account.bank_unlock_at == START + timedelta(days=14)
        # Spend it all: nothing past the 14-day allowance can be reserved.
        await c.set_credits_used(uid, 4_990)
        assert await c.meter.reserve_credits(account, 10, min_credits=1) is not None
        assert await c.meter.reserve_credits(account, 1, min_credits=1) is None
        assert await _spendable(c, uid) == 0

        # The dashboard is told what is held and when it unlocks.
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=key)).json()
        assert summary["credits"]["limit"] == 5_000 and summary["credits"]["full_limit"] == 60_000
        assert summary["credits"]["bank_unlocks_at"] == (START + timedelta(days=14)).isoformat()

        # Day 15: the whole year's bank.
        _at(monkeypatch, START + timedelta(days=14, minutes=1))
        account = await c.meter.get_account(uid)
        assert account.credit_limit == 60_000 and account.bank_unlock_at is None
        assert await _spendable(c, uid) == 60_000 - 5_000
        assert await c.meter.reserve_credits(account, 1_000, min_credits=1) is not None
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=key)).json()
        assert summary["credits"]["bank_unlocks_at"] is None


async def test_renewals_and_repeats_of_a_held_subscription_are_not_held_back(tmp_path, monkeypatch) -> None:
    _at(monkeypatch, START)
    async with cost_app(tmp_path) as c:
        _paddle(c, **ANNUAL)
        uid, _key = await c.account("renew@example.com")
        await _hook(c, "transaction.completed", _annual("txn_1", "sub_1", uid))
        # Paddle's subscription.activated for the same purchase: still one hold, same date.
        activated = {**_annual("", "", uid), "id": "sub_1"}
        await _hook(c, "subscription.activated", activated)
        assert (await c.meter.get_account(uid)).bank_unlock_at == START + timedelta(days=14)

        next_year = START.replace(year=2027)
        _at(monkeypatch, next_year + timedelta(days=1))
        await _hook(c, "transaction.completed", _annual("txn_2", "sub_1", uid, starts=next_year))
        account = await c.meter.get_account(uid)
        assert account.period.key == "Y:2027-10-01"
        assert account.credit_limit == 60_000 and account.bank_unlock_at is None


async def test_switching_a_monthly_subscription_to_yearly_holds_the_new_bank(tmp_path, monkeypatch) -> None:
    _at(monkeypatch, START)
    async with cost_app(tmp_path) as c:
        _paddle(c, **ANNUAL)
        uid, _key = await c.account("switch@example.com")
        await _hook(c, "transaction.completed", _purchase("txn_m", "sub_s", "pri_pro_m", _bound(uid), customer="ctm_s"))
        assert (await c.meter.get_account(uid)).credit_limit == 5_000

        switched_at = START + timedelta(days=40)
        _at(monkeypatch, switched_at)
        updated = {**_annual("", "", uid, starts=switched_at), "id": "sub_s", "status": "active", "customer_id": "ctm_s"}
        assert (await _hook(c, "subscription.updated", updated))["applied"] == "applied"
        account = await c.meter.get_account(uid)
        assert (account.interval, account.credit_limit, account.full_credit_limit) == (BillingInterval.YEAR, 5_000, 60_000)
        _at(monkeypatch, switched_at + timedelta(days=15))
        assert (await c.meter.get_account(uid)).credit_limit == 60_000


async def test_existing_annual_subscribers_keep_their_whole_bank(tmp_path, monkeypatch) -> None:
    _at(monkeypatch, START + timedelta(days=2))
    async with cost_app(tmp_path) as c:
        uid, _key = await c.account("legacy@example.com", plan=PlanTier.PRO, interval=BillingInterval.YEAR, period_anchor=START)
        account = await c.meter.get_account(uid)
        assert account.credit_limit == 60_000 and account.bank_unlock_at is None


async def test_the_unlock_is_configurable_and_can_be_turned_off(tmp_path, monkeypatch) -> None:
    _at(monkeypatch, START)
    async with cost_app(tmp_path, annual_credit_unlock_days=0) as c:
        _paddle(c, **ANNUAL)
        uid, _key = await c.account("off@example.com")
        await _hook(c, "transaction.completed", _annual("txn_1", "sub_1", uid))
        assert (await c.meter.get_account(uid)).credit_limit == 60_000
    (tmp_path / "two").mkdir()
    async with cost_app(tmp_path / "two", annual_credit_initial_months=2) as c:
        _paddle(c, **ANNUAL)
        uid, _key = await c.account("two@example.com")
        await _hook(c, "transaction.completed", _annual("txn_1", "sub_1", uid))
        assert (await c.meter.get_account(uid)).credit_limit == 10_000


@pytest.mark.parametrize(
    ("day", "used", "bank", "alerted"),
    [
        (16, 15_001, 60_000, True),  # over 25% of the unlocked bank
        (16, 15_000, 60_000, False),
        (5, 1_251, 5_000, True),  # inside the refund window: 25% of the month released so far
    ],
)
async def test_a_refund_after_heavy_use_alerts_the_owner(tmp_path, monkeypatch, day, used, bank, alerted) -> None:
    _at(monkeypatch, START)
    async with cost_app(tmp_path) as c:
        _paddle(c, **ANNUAL)
        alerts = RecordingAlerts()
        c.h.app.state.alerts = alerts
        c.h.app.state.tasks = None
        uid, _key = await c.account("refunder@example.com")
        await _hook(c, "transaction.completed", _annual("txn_1", "sub_1", uid))
        _at(monkeypatch, START + timedelta(days=day))
        await c.set_credits_used(uid, used)
        refund = {
            "id": "adj_1",
            "action": "refund",
            "type": "full",
            "status": "approved",
            "transaction_id": "txn_1",
            "subscription_id": "sub_1",
            "customer_id": f"ctm_{uid[-6:]}",
            "totals": {"currency_code": "USD", "earnings": "26000", "total": "29000"},
        }
        assert (await _hook(c, "adjustment.updated", refund))["applied"] == "applied"
        heavy = [a for a in alerts.sent if a[0] == f"paddle_refund_heavy_use:{uid}"]
        assert bool(heavy) is alerted
        if alerted:
            details = heavy[0][2]
            assert (details["credits_used"], details["credit_bank"]) == (used, bank)
            assert f"{used:,} of its {bank:,} credits" in heavy[0][1] and "Review for refund abuse" in heavy[0][1]
        # The refund itself is flagged and ends the plan either way.
        assert (await c.meter.get_tenant(uid))["billing_flag"] == "refund_downgraded"
        assert (await c.meter.get_account(uid)).tier == PlanTier.FREE
