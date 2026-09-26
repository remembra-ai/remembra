"""SEC-15 (Paddle webhook) and SEC-16 (promo redemption) regressions."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from remembra.api.v1 import billing, cloud
from remembra.cloud.billing_paddle import checkout_binding
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import PlanTier
from tests.security_harness import make_settings, secure_app

SECRET = "pdl_ntfset_test_secret_value_for_unit_tests"


def _signed(event: dict, secret: str = SECRET, ts: int | None = None) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(event).encode()
    ts = int(time.time()) if ts is None else ts
    h1 = hmac.new(secret.encode(), f"{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return body, {"paddle-signature": f"ts={ts};h1={h1}", "content-type": "application/json"}


TEAM_SEAT_PRICE = "pri_team_seat_sec"


def _event(event_type: str, user_id: str, plan: str = "pro", price: str | None = None, quantity: int = 1) -> dict:
    custom = {"remembra_user_id": user_id, "remembra_binding": checkout_binding(user_id), "plan": plan}
    data: dict = {"id": "sub_1", "status": "active", "custom_data": custom}
    if price:
        data["items"] = [{"price": {"id": price}, "quantity": quantity}]
    return {"event_type": event_type, "data": data}


async def _app(tmp_path, secret: str | None):
    settings = make_settings(cloud_enabled=True)
    # These fields use env aliases, so set them on the instance.
    settings.paddle_api_key = "pdl_test_api_key"
    settings.paddle_webhook_secret = secret
    settings.paddle_price_team_seat_monthly = TEAM_SEAT_PRICE
    ctx = secure_app(tmp_path, [billing.router, cloud.router], settings=settings)
    h = await ctx.__aenter__()
    h.app.state.usage_meter = UsageMeter(h.db)
    return ctx, h


async def test_webhook_fails_closed_without_secret(tmp_path):
    ctx, h = await _app(tmp_path, secret=None)
    try:
        uid = await h.create_user("buyer@example.com")
        # Anyone could compute this "signature" with an empty secret.
        body, headers = _signed(_event("subscription.activated", uid, "enterprise"), secret="")
        r = await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        assert r.status_code == 503
        assert r.json()["detail"] == "Paddle webhook verification is not configured."
        assert await h.app.state.usage_meter.get_tenant_plan(uid) == PlanTier.FREE
    finally:
        await ctx.__aexit__(None, None, None)


async def test_webhook_rejects_forged_and_stale_events(tmp_path):
    ctx, h = await _app(tmp_path, secret=SECRET)
    try:
        uid = await h.create_user("buyer@example.com")
        body, headers = _signed(_event("subscription.activated", uid), secret="wrong-secret")
        assert (await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)).status_code == 400
        body, headers = _signed(_event("subscription.activated", uid), ts=int(time.time()) - 600)
        r = await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        assert r.status_code == 400  # replayed / stale beyond 5 min
        assert await h.app.state.usage_meter.get_tenant_plan(uid) == PlanTier.FREE
    finally:
        await ctx.__aexit__(None, None, None)


async def test_verified_webhook_is_applied_to_the_plan(tmp_path):
    ctx, h = await _app(tmp_path, secret=SECRET)
    try:
        uid = await h.create_user("buyer@example.com")
        body, headers = _signed(_event("subscription.activated", uid, "team", price=TEAM_SEAT_PRICE, quantity=3))
        r = await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["applied"] == "applied"
        assert await h.app.state.usage_meter.get_tenant_plan(uid) == PlanTier.TEAM

        # A cancel that names the account but not the subscription it holds changes nothing.
        body, headers = _signed({"event_type": "subscription.canceled", "data": {"custom_data": {"remembra_user_id": uid}}})
        r = await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        assert r.status_code == 200 and r.json()["applied"] == "unmatched"
        assert await h.app.state.usage_meter.get_tenant_plan(uid) == PlanTier.TEAM

        body, headers = _signed(
            {"event_type": "subscription.canceled", "data": {"id": "sub_1", "custom_data": {"remembra_user_id": uid}}}
        )
        assert (await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)).status_code == 200
        assert await h.app.state.usage_meter.get_tenant_plan(uid) == PlanTier.FREE

        unknown = _event("subscription.activated", "user_does_not_exist", price=TEAM_SEAT_PRICE, quantity=3)
        unknown["data"]["id"] = "sub_unknown"
        body, headers = _signed(unknown)
        r = await h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
        assert r.status_code == 200 and r.json()["applied"] == "unmatched"
    finally:
        await ctx.__aexit__(None, None, None)


def test_paddle_manager_rejects_everything_without_secret():
    import pytest

    from remembra.cloud.billing_paddle import PaddleBillingManager

    manager = PaddleBillingManager(api_key="k", webhook_secret="", sandbox=True)
    body, headers = _signed({"event_type": "x"}, secret="")
    with pytest.raises(ValueError, match="not configured"):
        manager.verify_webhook(body, headers["paddle-signature"])


# ---------------------------------------------------------------------------
# SEC-16: promo redemptions are persisted, capped and one-per-user
# ---------------------------------------------------------------------------


def _trial(code: str, cap: int | None, days: int = 14):
    from datetime import datetime, timedelta

    from remembra.cloud.promocodes import PromoCode, PromoType

    return PromoCode(
        code=code,
        promo_type=PromoType.TRIAL,
        plan_tier=PlanTier.PRO,
        duration_days=days,
        max_redemptions=cap,
        expires_at=datetime.now() + timedelta(days=30),
    )


async def test_redemptions_persist_across_instances_and_are_capped(tmp_path, in_memory_db):
    import asyncio

    from remembra.cloud.promocodes import PromoCodeManager

    first = PromoCodeManager(in_memory_db)
    first.add_code(_trial("TESTCAP", cap=3))
    assert (await first.redeem("TESTCAP", "user-1")).success
    # "Restart": a fresh manager sees the persisted redemption.
    second = PromoCodeManager(in_memory_db)
    second.add_code(_trial("TESTCAP", cap=3))
    again = await second.redeem("testcap", "user-1")
    assert not again.success and "already redeemed" in again.error

    results = await asyncio.gather(*(second.redeem("TESTCAP", f"racer-{i}") for i in range(10)))
    assert sum(r.success for r in results) == 2  # cap 3, one already used
    assert await second.redemption_count("TESTCAP") == 3


async def test_redeem_endpoint_applies_trial_once_and_trial_expires(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from remembra.cloud import promocodes

    monkeypatch.setitem(promocodes.PROMO_CODES, "TESTTRIAL", _trial("TESTTRIAL", cap=None))
    ctx, h = await _app(tmp_path, secret=SECRET)
    try:
        meter = h.app.state.usage_meter
        await meter.init_schema()
        uid = await h.create_user("trial@example.com")
        hdr = h.jwt(uid, "trial@example.com")
        r = await h.client.post("/api/v1/cloud/promo/redeem", json={"code": "TESTTRIAL"}, headers=hdr)
        assert r.status_code == 200 and r.json()["success"], r.text
        assert await meter.get_tenant_plan(uid) == PlanTier.PRO
        r = await h.client.post("/api/v1/cloud/promo/redeem", json={"code": "TESTTRIAL"}, headers=hdr)
        assert r.json()["success"] is False

        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        await h.db.conn.execute("UPDATE cloud_tenants SET promo_expires_at = ? WHERE user_id = ?", (past, uid))
        await h.db.conn.commit()
        assert await meter.get_tenant_plan(uid) == PlanTier.FREE  # trials end
    finally:
        await ctx.__aexit__(None, None, None)
