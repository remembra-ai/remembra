"""Paddle events only change the subscription an account actually holds.

Regression tests for two pre-deploy findings:

* A subscription the account does not hold (bought by someone else with a
  browser-written ``custom_data.remembra_user_id``) could re-plan the account
  and, once cancelled, drop it to Free while it still paid for its own.
* A paying customer with two subscriptions (a legacy $49/$199 one plus a new
  catalog plan) was dropped to Free by cancelling either one, and renewals of
  either one flipped the plan back and forth.

Every test drives the real webhook route (signed body in, tenant row out) and
the real checkout / client-config / usage-summary routes.
"""

from __future__ import annotations

from typing import Any

import pytest

from remembra.cloud.billing_paddle import CHECKOUT_BINDING_KEY, checkout_binding, checkout_binding_valid
from remembra.cloud.plans import BillingInterval, PlanTier
from tests._cost_harness import cost_app
from tests.test_plans_billing_signup import LEGACY_PRO_PRICE, PRICES, _paddle, _signed


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def notify(self, event: str, message: str, details: dict[str, Any] | None = None) -> bool:
        self.sent.append((event, message, details or {}))
        return True


async def _hook(c: Any, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    body, headers = _signed({"event_type": event_type, "data": data})
    r = await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _bound(uid: str, **extra: Any) -> dict[str, Any]:
    """custom_data as the server checkout (or the account's own client config) writes it."""
    return {"remembra_user_id": uid, CHECKOUT_BINDING_KEY: checkout_binding(uid), **extra}


def _purchase(txn: str, sub: str, price: str, custom: dict[str, Any], *, customer: str = "ctm_x", quantity: int = 1) -> dict:
    return {
        "id": txn,
        "subscription_id": sub,
        "customer_id": customer,
        "items": [{"price": {"id": price}, "quantity": quantity}],
        "custom_data": custom,
    }


async def _state(c: Any, uid: str) -> tuple[str, str | None, str | None]:
    tenant = await c.meter.get_tenant(uid) or {}
    return str(tenant.get("plan")), tenant.get("stripe_subscription_id"), tenant.get("billing_flag")


@pytest.fixture()
def alerts() -> RecordingAlerts:
    return RecordingAlerts()


async def _app(c: Any, alerts: RecordingAlerts) -> None:
    _paddle(c, **PRICES)
    c.h.app.state.alerts = alerts
    c.h.app.state.tasks = None  # deliver inline so the test can read them


# ---------------------------------------------------------------------------
# Finding: a subscription the account does not hold (client-controlled custom_data)
# ---------------------------------------------------------------------------


async def test_foreign_subscription_can_neither_replan_nor_cancel_the_account(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        victim = await c.h.create_user("victim@example.com")
        await _hook(
            c,
            "transaction.completed",
            _purchase("txn_v", "sub_victim", "pri_team_m", _bound(victim), customer="ctm_victim", quantity=5),
        )
        assert await _state(c, victim) == ("team", "sub_victim", None)

        # Someone buys Solo in the overlay naming the victim (the team exposes the owner id).
        bought = await _hook(
            c,
            "transaction.completed",
            _purchase("txn_a", "sub_attacker", "pri_solo_m", {"remembra_user_id": victim}, customer="ctm_attacker"),
        )
        assert bought["applied"] == "unmatched"
        assert await _state(c, victim) == ("team", "sub_victim", None)
        assert [event for event, _m, _d in alerts.sent] == ["paddle_unverified_account:sub_attacker"]

        # ... then cancels it: the victim keeps the Team plan they pay for.
        canceled = await _hook(
            c,
            "subscription.canceled",
            {
                "id": "sub_attacker",
                "customer_id": "ctm_attacker",
                "status": "canceled",
                "custom_data": {"remembra_user_id": victim},
            },
        )
        assert canceled["applied"] == "unmatched"
        updated = await _hook(
            c,
            "subscription.updated",
            {
                "id": "sub_attacker",
                "status": "active",
                "customer_id": "ctm_attacker",
                "items": [{"price": {"id": "pri_solo_m"}, "quantity": 1}],
                "custom_data": {"remembra_user_id": victim},
            },
        )
        assert updated["applied"] == "unmatched"
        account = await c.meter.get_account(victim)
        assert (account.tier, account.seats) == (PlanTier.TEAM, 5)


async def test_a_binding_for_another_account_does_not_verify(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        victim = await c.h.create_user("victim@example.com")
        attacker = await c.h.create_user("attacker@example.com")
        forged = {"remembra_user_id": victim, CHECKOUT_BINDING_KEY: checkout_binding(attacker)}
        result = await _hook(c, "transaction.completed", _purchase("txn_f", "sub_f", "pri_solo_m", forged, customer="ctm_a"))
        assert result["applied"] == "unmatched"
        assert await c.meter.get_tenant(victim) is None
        assert checkout_binding_valid(victim, checkout_binding(victim)) is True
        for bad in (None, "", 42, "x" * 200, checkout_binding(attacker)):
            assert checkout_binding_valid(victim, bad) is False
        assert checkout_binding_valid(None, checkout_binding(victim)) is False


async def test_unsigned_event_is_credited_to_the_account_of_its_paddle_customer(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("customer@example.com")
        await c.meter.register_tenant(uid, PlanTier.FREE, stripe_customer_id="ctm_known")
        result = await _hook(
            c, "transaction.completed", _purchase("txn_c", "sub_c", "pri_solo_m", {"remembra_user_id": uid}, customer="ctm_known")
        )
        assert result["applied"] == "applied"
        assert await _state(c, uid) == ("solo", "sub_c", None)
        # No custom_data at all (e.g. a subscription created in the Paddle dashboard): the customer still finds it.
        other = await c.h.create_user("other@example.com")
        await c.meter.register_tenant(other, PlanTier.FREE, stripe_customer_id="ctm_other")
        result = await _hook(
            c, "subscription.activated", {**_purchase("", "", "pri_pro_m", {}, customer="ctm_other"), "id": "sub_o"}
        )
        assert result["applied"] == "applied"
        assert await _state(c, other) == ("pro", "sub_o", None)
        assert alerts.sent == []


async def test_renewals_of_a_held_subscription_need_no_signature(tmp_path, alerts) -> None:
    """Legacy $49 subscriptions predate the signature; their renewals still apply."""
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("legacy@example.com")
        await c.meter.apply_subscription(uid, PlanTier.LEGACY_PRO, interval=BillingInterval.MONTH, subscription_id="sub_old")
        someone = await c.h.create_user("someone@example.com")
        # Even when custom_data names another account, the holder of the subscription gets the event.
        renewal = _purchase("txn_r", "sub_old", LEGACY_PRO_PRICE, {"remembra_user_id": someone, "plan": "pro"})
        assert (await _hook(c, "transaction.completed", renewal))["applied"] == "applied"
        assert await _state(c, uid) == ("legacy_pro_49", "sub_old", None)
        assert await c.meter.get_tenant(someone) is None


# ---------------------------------------------------------------------------
# Finding: two subscriptions on one account (legacy subscriber buys a new plan)
# ---------------------------------------------------------------------------


async def test_second_subscription_is_flagged_not_applied_and_old_cancel_is_reported(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("legacy2@example.com")
        await c.meter.apply_subscription(uid, PlanTier.LEGACY_PRO, interval=BillingInterval.MONTH, subscription_id="sub_old")

        second = {"id": "sub_new", "items": [{"price": {"id": "pri_solo_m"}, "quantity": 1}], "custom_data": _bound(uid)}
        assert (await _hook(c, "subscription.activated", second))["applied"] == "flagged"
        assert await _state(c, uid) == ("legacy_pro_49", "sub_old", "second_subscription_review")
        assert alerts.sent[-1][0] == f"paddle_second_subscription:{uid}"
        assert alerts.sent[-1][2]["plan"] == "solo"

        # Renewals of the held subscription keep the grandfathered plan; the second one never flips it.
        renew_old = _purchase("txn_old_2", "sub_old", LEGACY_PRO_PRICE, {"remembra_user_id": uid})
        assert (await _hook(c, "transaction.completed", renew_old))["applied"] == "applied"
        renew_new = _purchase("txn_new_2", "sub_new", "pri_solo_m", _bound(uid))
        assert (await _hook(c, "transaction.completed", renew_new))["applied"] == "flagged"
        update_new = {**second, "status": "active"}
        assert (await _hook(c, "subscription.updated", update_new))["applied"] == "no_change"
        assert (await _state(c, uid))[:2] == ("legacy_pro_49", "sub_old")

        # Cancelling the second subscription does nothing to the held one.
        cancel_new = {"id": "sub_new", "status": "canceled", "custom_data": _bound(uid)}
        assert (await _hook(c, "subscription.canceled", cancel_new))["applied"] == "no_change"
        assert (await _state(c, uid))[:2] == ("legacy_pro_49", "sub_old")

        # Cancelling the held one ends it, and the operator is told a second one may still bill.
        cancel_old = {"id": "sub_old", "status": "canceled", "custom_data": {"remembra_user_id": uid, "plan": "pro"}}
        assert (await _hook(c, "subscription.canceled", cancel_old))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "free"
        assert alerts.sent[-1][0] == f"paddle_second_subscription:{uid}:cancel"
        # The next payment of the remaining subscription is applied (the account holds nothing active now).
        renew_new_3 = _purchase("txn_new_3", "sub_new", "pri_solo_m", _bound(uid))
        assert (await _hook(c, "transaction.completed", renew_new_3))["applied"] == "applied"
        assert (await _state(c, uid))[:2] == ("solo", "sub_new")


async def test_cancel_needs_the_held_subscription_id(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("pro@example.com")
        await _hook(c, "transaction.completed", _purchase("txn_p", "sub_p", "pri_pro_m", _bound(uid), customer="ctm_p"))
        assert await _state(c, uid) == ("pro", "sub_p", None)
        # No subscription id, or another one: nothing changes.
        assert (await _hook(c, "subscription.canceled", {"custom_data": _bound(uid)}))["applied"] == "no_change"
        assert (await _hook(c, "subscription.canceled", {"id": "sub_zzz", "custom_data": _bound(uid)}))["applied"] == "no_change"
        assert (await _state(c, uid))[0] == "pro"
        # The held one, even without custom_data (the holder is found by id).
        assert (await _hook(c, "subscription.canceled", {"id": "sub_p", "status": "canceled"}))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "free"
        # A repeat of the same cancel is harmless.
        assert (await _hook(c, "subscription.canceled", {"id": "sub_p", "status": "canceled"}))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "free"


async def test_cancel_for_an_account_without_a_recorded_subscription_uses_its_customer(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("oldrow@example.com")
        # A paid row upgraded before subscription ids were stored.
        await c.meter.register_tenant(uid, PlanTier.LEGACY_TEAM, stripe_customer_id="ctm_old")
        foreign = {"id": "sub_x", "customer_id": "ctm_other", "status": "canceled", "custom_data": {"remembra_user_id": uid}}
        assert (await _hook(c, "subscription.canceled", foreign))["applied"] == "unmatched"
        assert (await _state(c, uid))[0] == "legacy_team_199"
        own = {"id": "sub_y", "customer_id": "ctm_old", "status": "canceled", "custom_data": {"remembra_user_id": uid}}
        assert (await _hook(c, "subscription.canceled", own))["applied"] == "applied"
        assert (await _state(c, uid))[0] == "free"


async def test_a_free_account_with_an_old_subscription_can_buy_again(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("again@example.com")
        await _hook(c, "transaction.completed", _purchase("txn_1", "sub_1", "pri_solo_m", _bound(uid)))
        await _hook(c, "subscription.canceled", {"id": "sub_1", "status": "canceled"})
        assert await _state(c, uid) == ("free", "sub_1", None)
        assert (await _hook(c, "transaction.completed", _purchase("txn_2", "sub_2", "pri_pro_m", _bound(uid))))[
            "applied"
        ] == "applied"
        assert await _state(c, uid) == ("pro", "sub_2", None)
        assert alerts.sent == []


# ---------------------------------------------------------------------------
# Checkout, client config and the dashboard's view
# ---------------------------------------------------------------------------


async def test_checkout_refuses_a_second_subscription(tmp_path, alerts, monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def fake_request(self: Any, method: str, endpoint: str, data: dict | None = None) -> dict:
        sent.append({"method": method, "endpoint": endpoint, "data": data})
        return {"data": {"id": f"txn_{len(sent)}", "checkout": {"url": "https://pay.example/t"}}}

    monkeypatch.setattr("remembra.cloud.billing_paddle.PaddleBillingManager._request", fake_request)
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid = await c.h.create_user("buyer@example.com")
        hdr = c.h.jwt(uid, "buyer@example.com")
        # Free: allowed, and the server transaction carries the signature.
        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=hdr)
        assert r.status_code == 200, r.text
        custom = sent[-1]["data"]["custom_data"]
        assert custom["remembra_user_id"] == uid and checkout_binding_valid(uid, custom[CHECKOUT_BINDING_KEY])

        # A promo trial (paid plan, no subscription) may still buy.
        await c.meter.register_tenant(uid, PlanTier.PRO)
        assert (await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=hdr)).status_code == 200

        # Holding a paid subscription (legacy or new): 409, nothing reaches Paddle.
        for plan in (PlanTier.LEGACY_PRO, PlanTier.SOLO):
            await c.meter.apply_subscription(uid, plan, subscription_id="sub_held")
            before = len(sent)
            for body in ({"plan": "pro"}, {"plan": "team", "seats": 3}, {"plan": "founding", "billing_cycle": "yearly"}):
                r = await c.h.client.post("/api/v1/billing/checkout", json=body, headers=hdr)
                assert r.status_code == 409 and "Manage subscription" in r.json()["detail"], r.text
            assert len(sent) == before

        # After the subscription ends, buying works again.
        await c.meter.apply_subscription(uid, PlanTier.FREE)
        assert (await c.h.client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=hdr)).status_code == 200


async def test_client_config_signs_for_the_account_and_withholds_prices_from_subscribers(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        anonymous = (await c.h.client.get("/api/v1/billing/client-config")).json()
        assert anonymous["prices"]["solo"] == "pri_solo_m"
        assert anonymous["checkout_binding"] is None and anonymous["has_subscription"] is False

        uid = await c.h.create_user("cfg@example.com")
        hdr = c.h.jwt(uid, "cfg@example.com")
        mine = (await c.h.client.get("/api/v1/billing/client-config", headers=hdr)).json()
        assert mine["prices"]["solo"] == "pri_solo_m"
        assert mine["checkout_binding"] == checkout_binding(uid) and mine["has_subscription"] is False
        # The signature the dashboard puts in customData is what the webhook accepts.
        data = _purchase(
            "txn_cfg",
            "sub_cfg",
            mine["prices"]["solo"],
            {"remembra_user_id": uid, CHECKOUT_BINDING_KEY: mine["checkout_binding"]},
        )
        assert (await _hook(c, "transaction.completed", data))["applied"] == "applied"

        subscribed = (await c.h.client.get("/api/v1/billing/client-config", headers=hdr)).json()
        assert subscribed == {
            "provider": "paddle",
            "client_token": None,
            "prices": {},
            "success_url": "https://app.remembra.dev/?checkout=success",
            "checkout_binding": None,
            "has_subscription": True,
        }
        # A bad token is treated as anonymous, never as someone else.
        bad = await c.h.client.get("/api/v1/billing/client-config", headers={"Authorization": "Bearer not-a-jwt"})
        assert bad.status_code == 200 and bad.json()["checkout_binding"] is None


async def test_usage_summary_reports_an_active_subscription(tmp_path, alerts) -> None:
    async with cost_app(tmp_path) as c:
        await _app(c, alerts)
        uid, hdr = await c.account("summary@example.com")
        assert (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()["subscription_active"] is False
        await c.meter.apply_subscription(uid, PlanTier.LEGACY_PRO, subscription_id="sub_l")
        body = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert body["plan"] == "legacy_pro_49" and body["subscription_active"] is True
        await c.meter.apply_subscription(uid, PlanTier.FREE)
        assert (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()["subscription_active"] is False


def test_active_subscription_id_edge_cases() -> None:
    from remembra.cloud.metering import UsageMeter

    held = UsageMeter.active_subscription_id
    assert held(None) is None
    assert held({}) is None
    assert held({"plan": "pro"}) is None  # promo trial: no subscription
    assert held({"plan": "free", "stripe_subscription_id": "sub_1"}) is None  # ended
    assert held({"plan": None, "stripe_subscription_id": "sub_1"}) is None
    assert held({"plan": "legacy_team_199", "stripe_subscription_id": "sub_1"}) == "sub_1"


async def test_operator_alert_failures_never_fail_the_webhook(tmp_path) -> None:
    class BrokenTasks:
        def spawn(self, coro: Any, *, name: str) -> None:
            raise RuntimeError("registry closed")

    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        c.h.app.state.alerts = RecordingAlerts()
        c.h.app.state.tasks = BrokenTasks()
        uid = await c.h.create_user("alert@example.com")
        await c.meter.apply_subscription(uid, PlanTier.SOLO, subscription_id="sub_a")
        second = _purchase("txn_b", "sub_b", "pri_pro_m", _bound(uid))
        assert (await _hook(c, "transaction.completed", second))["applied"] == "flagged"
        assert (await _state(c, uid))[2] == "second_subscription_review"
