"""Where a buyer goes around a Paddle checkout (R-4).

* After paying, Paddle.js sends the buyer to the dashboard home with
  ?checkout=success, which confirms the plan. remembra.dev/dashboard no
  longer exists (nginx redirects it, query string kept, for old checkouts).
* A server-created transaction's checkout.url is the dashboard's /pay page:
  Paddle appends ?_ptxn=<id> and /pay opens the overlay with Paddle.js. It is
  never the success page, which would not open a checkout.
* Both follow Settings.public_dashboard_url when it is set.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from remembra.api.v1.billing import checkout_success_url, dashboard_origin, payment_link_url
from remembra.cloud.billing_paddle import DEFAULT_PAYMENT_LINK, PaddleBillingManager
from remembra.cloud.plans import PlanTier
from tests._cost_harness import cost_app

PRICES = {"paddle_price_solo_monthly": "pri_solo_m", "paddle_price_solo_annual": "pri_solo_y"}


def _paddle(c: Any) -> None:
    s = c.h.settings
    s.paddle_api_key = "pdl_test_api_key"
    s.paddle_webhook_secret = "pdl_ntfset_test_secret"
    for name, value in PRICES.items():
        setattr(s, name, value)


@pytest.mark.parametrize(
    ("configured", "origin"),
    [
        (None, "https://app.remembra.dev"),
        ("", "https://app.remembra.dev"),
        ("   ", "https://app.remembra.dev"),
        ("https://dash.example.com", "https://dash.example.com"),
        ("https://dash.example.com/", "https://dash.example.com"),
        (" https://dash.example.com// ", "https://dash.example.com"),
    ],
)
def test_dashboard_origin_defaults_to_app_remembra_dev(configured: str | None, origin: str) -> None:
    settings = SimpleNamespace(public_dashboard_url=configured)
    assert dashboard_origin(settings) == origin
    assert checkout_success_url(settings) == f"{origin}/?checkout=success"
    assert payment_link_url(settings) == f"{origin}/pay"


def test_settings_without_the_field_still_get_the_default() -> None:
    assert dashboard_origin(object()) == "https://app.remembra.dev"


async def test_client_config_sends_buyers_to_the_dashboard_after_paying(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c)
        config = (await c.h.client.get("/api/v1/billing/client-config")).json()
        assert config["provider"] == "paddle"
        assert config["success_url"] == "https://app.remembra.dev/?checkout=success"
        assert "remembra.dev/dashboard" not in config["success_url"]

        c.h.settings.public_dashboard_url = "https://dash.example.com/"
        config = (await c.h.client.get("/api/v1/billing/client-config")).json()
        assert config["success_url"] == "https://dash.example.com/?checkout=success"


async def test_server_checkout_uses_the_pay_page_as_paddles_checkout_url(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c)
        uid = await c.h.create_user("pay@example.com")
        hdr = c.h.jwt(uid, "pay@example.com")
        sent: list[dict] = []

        async def fake_request(self: PaddleBillingManager, method: str, endpoint: str, data: dict | None = None) -> dict:
            sent.append({"method": method, "endpoint": endpoint, "data": data})
            url = f"{(data or {})['checkout']['url']}?_ptxn=txn_1"
            return {"data": {"id": "txn_1", "checkout": {"url": url}}}

        monkeypatch.setattr(PaddleBillingManager, "_request", fake_request)

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo", "billing_cycle": "yearly"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert sent[-1]["endpoint"] == "/transactions"
        assert sent[-1]["data"]["checkout"] == {"url": "https://app.remembra.dev/pay"}
        # The dashboard's fallback navigates here when Paddle.js did not load; /pay opens it.
        assert r.json()["checkout_url"] == "https://app.remembra.dev/pay?_ptxn=txn_1"

        c.h.settings.public_dashboard_url = "https://dash.example.com"
        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert sent[-1]["data"]["checkout"] == {"url": "https://dash.example.com/pay"}


async def test_the_manager_defaults_to_the_pay_page(monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_request(self: PaddleBillingManager, method: str, endpoint: str, data: dict | None = None) -> dict:
        sent.append(data or {})
        return {"data": {"id": "txn_2", "checkout": {"url": None}}}

    monkeypatch.setattr(PaddleBillingManager, "_request", fake_request)
    monkeypatch.setattr(PaddleBillingManager, "_price_for", lambda self, plan, interval, founding: "pri_x")
    manager = PaddleBillingManager(api_key="k", webhook_secret="s", sandbox=True)
    await manager.create_checkout_transaction(
        paddle_customer_id=None, plan=PlanTier.SOLO, user_id="u1", customer_email="u@example.com"
    )
    assert DEFAULT_PAYMENT_LINK == "https://app.remembra.dev/pay"
    assert sent[-1]["checkout"] == {"url": DEFAULT_PAYMENT_LINK}
