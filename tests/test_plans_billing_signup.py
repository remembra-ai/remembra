"""New plans, legacy grandfathering, Paddle mapping, signup hardening, rate limits, queue, usage API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from limits.errors import ConfigurationError

import remembra.config as config_module
from remembra.cloud import signup_guard
from remembra.cloud.metering import UsageMeter
from remembra.cloud.plans import BillingInterval, PlanTier
from remembra.cloud.billing_paddle import checkout_binding
from remembra.cloud.ratelimit import CloudRateLimiter, network_key, set_cloud_rate_limiter, storage_uri_from_setting
from remembra.core import ai_spend
from remembra.models.memory import RecallResponse
from remembra.core.enrichment_queue import EnrichmentQueue
from tests._cost_harness import cost_app
from tests.security_harness import MASTER_KEY

SECRET = "pdl_ntfset_test_secret_value_for_unit_tests"
LEGACY_PRO_PRICE = "pri_01kmepby4nfy150jbfjkpkev5h"  # production $49/mo
PRICES = {
    "paddle_price_solo_monthly": "pri_solo_m",
    "paddle_price_solo_annual": "pri_solo_y",
    "paddle_price_pro_monthly": "pri_pro_m",
    "paddle_price_team_seat_monthly": "pri_team_m",
    "paddle_price_founding_annual": "pri_founding",
}


def _signed(event: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(event).encode()
    ts = int(time.time())
    h1 = hmac.new(SECRET.encode(), f"{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return body, {"paddle-signature": f"ts={ts};h1={h1}", "content-type": "application/json"}


def _paddle(c: Any, **prices: str) -> None:
    s = c.h.settings
    s.paddle_api_key = "pdl_test_api_key"
    s.paddle_webhook_secret = SECRET
    for name, value in prices.items():
        setattr(s, name, value)


# ---------------------------------------------------------------------------
# Legacy grandfathering
# ---------------------------------------------------------------------------


async def test_existing_49_and_199_subscribers_become_legacy_tiers_once(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        db = c.h.db
        now = datetime.now(UTC).isoformat()
        await db.conn.execute("DELETE FROM cloud_migrations")
        team_owner = await c.h.create_user("team-owner-199@example.com")
        await db.conn.execute(
            "INSERT INTO teams (id, name, slug, owner_id, plan, created_at, updated_at)"
            " VALUES ('t1', 'Acme', 'acme', ?, 'team', ?, ?)",
            (team_owner, now, now),
        )
        rows = [
            ("u_pro", "pro", "sub_1", None),
            (team_owner, "team", "sub_2", None),
            ("u_trial", "pro", None, "2099-01-01T00:00:00"),  # unpaid promo trial: stays on the new Pro
            ("u_free", "free", None, None),
        ]
        for uid, plan, sub, promo in rows:
            await db.conn.execute(
                "INSERT INTO cloud_tenants (user_id, plan, stripe_subscription_id, promo_expires_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (uid, plan, sub, promo, now, now),
            )
        await db.conn.commit()
        meter = UsageMeter(db)
        await meter.init_schema()
        assert await meter.get_tenant_plan("u_pro") == PlanTier.LEGACY_PRO
        assert await meter.get_tenant_plan(team_owner) == PlanTier.LEGACY_TEAM
        cursor = await db.conn.execute("SELECT plan FROM teams WHERE id = 't1'")
        assert (await cursor.fetchone())[0] == PlanTier.LEGACY_TEAM.value  # the owner's team follows
        assert await meter.get_tenant_plan("u_trial") == PlanTier.PRO
        assert await meter.get_tenant_plan("u_free") == PlanTier.FREE

        legacy = await meter.get_account("u_pro")
        assert legacy.credit_limit == 12_000  # $30 ceiling applies immediately
        assert legacy.memory_cap == 500_000  # memory cap only shrinks after the 30-day notice
        assert (await meter.get_account(team_owner)).credit_limit == 60_000

        # A customer who buys the NEW Pro later is never migrated again.
        await meter.apply_subscription("u_new", PlanTier.PRO, interval=BillingInterval.MONTH, subscription_id="sub_3")
        await meter.init_schema()
        assert await meter.get_tenant_plan("u_new") == PlanTier.PRO


async def test_memory_caps_shrink_only_after_notice_date(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("cap@example.com", plan=PlanTier.LEGACY_PRO)
        assert (await c.meter.get_account(uid)).memory_cap == 500_000
        free_uid, _ = await c.account("freecap@example.com")
        assert (await c.meter.get_account(free_uid)).memory_cap == 25_000
        c.h.settings.memory_cap_notice_effective_at = datetime(2026, 1, 1, tzinfo=UTC)
        assert (await c.meter.get_account(uid)).memory_cap == 250_000
        assert (await c.meter.get_account(free_uid)).memory_cap == 10_000


# ---------------------------------------------------------------------------
# Paddle: webhooks map price IDs; checkout needs configured prices
# ---------------------------------------------------------------------------


async def test_webhook_maps_prices_seats_founding_and_revenue(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("buyer@example.com")

        event = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_founding_1",
                "subscription_id": "sub_f",
                "customer_id": "ctm_1",
                "currency_code": "USD",
                "details": {"totals": {"earnings": "10210"}},
                "billing_period": {"starts_at": "2026-09-25T10:00:00Z"},
                "items": [{"price": {"id": "pri_founding"}, "quantity": 1}],
                "custom_data": {"remembra_user_id": uid, "remembra_binding": checkout_binding(uid), "plan": "solo"},
            },
        }
        for _ in range(2):  # Paddle retries: revenue must not double count
            body, headers = _signed(event)
            r = await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
            assert r.status_code == 200 and r.json()["applied"] == "applied", r.text
        account = await c.meter.get_account(uid)
        assert (account.tier, account.interval, account.founding) == (PlanTier.SOLO, BillingInterval.YEAR, True)
        assert account.period.key == "Y:2026-09-25" and account.credit_limit == 26_400
        assert await c.meter.founding_redemptions() == 1
        assert await c.meter.revenue_for_month("2026-09") == pytest.approx(102.10)

        team_uid = await c.h.create_user("team-owner@example.com")
        event = {
            "event_type": "subscription.activated",
            "data": {
                "id": "sub_t",
                "items": [{"price": {"id": "pri_team_m"}, "quantity": 5}],
                "custom_data": {"remembra_user_id": team_uid, "remembra_binding": checkout_binding(team_uid), "plan": "team"},
            },
        }
        body, headers = _signed(event)
        assert (await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)).status_code == 200
        team = await c.meter.get_account(team_uid)
        assert team.tier == PlanTier.TEAM and team.seats == 5
        assert team.credit_limit == 5 * 2_200 and team.limits.max_memories == 250_000


async def test_legacy_renewals_keep_the_grandfathered_tier(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c)
        uid = await c.h.create_user("legacy@example.com")
        # The account holds sub_old from before the relaunch (no checkout signature in its custom_data).
        await c.meter.register_tenant(uid, PlanTier.FREE, stripe_subscription_id="sub_old")
        # Renewal carrying the old $49 price ID.
        event = {
            "event_type": "subscription.updated",
            "data": {
                "id": "sub_old",
                "status": "active",
                "items": [{"price": {"id": LEGACY_PRO_PRICE}, "quantity": 1}],
                "custom_data": {"remembra_user_id": uid, "plan": "pro"},
            },
        }
        body, headers = _signed(event)
        assert (await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)).status_code == 200
        assert await c.meter.get_tenant_plan(uid) == PlanTier.LEGACY_PRO

        # An update without items for the same subscription must not reprice them to the new Pro.
        event["data"].pop("items")
        body, headers = _signed(event)
        assert (await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)).status_code == 200
        assert await c.meter.get_tenant_plan(uid) == PlanTier.LEGACY_PRO
        assert (await c.meter.get_account(uid)).credit_limit == 12_000


async def test_checkout_requires_configured_prices_and_enforces_offer_rules(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c)  # no catalog prices configured yet
        uid = await c.h.create_user("checkout@example.com")
        hdr = c.h.jwt(uid, "checkout@example.com")

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo"}, headers=hdr)
        assert r.status_code == 503 and "not available yet" in r.json()["detail"]

        _paddle(c, **PRICES)
        sent: list[dict] = []

        async def fake_request(self, method: str, endpoint: str, data: dict | None = None) -> dict:
            sent.append({"method": method, "endpoint": endpoint, "data": data})
            return {"data": {"id": "txn_1", "checkout": {"url": "https://pay.example/txn_1"}}}

        monkeypatch.setattr("remembra.cloud.billing_paddle.PaddleBillingManager._request", fake_request)

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "pro", "billing_cycle": "yearly"}, headers=hdr)
        assert r.status_code == 503 and "pro (yearly)" in r.json()["detail"]  # Pro annual price not created yet
        assert sent == []  # nothing reached Paddle

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "solo", "billing_cycle": "yearly"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert sent[-1]["data"]["items"] == [{"price_id": "pri_solo_y", "quantity": 1}]
        assert sent[-1]["data"]["custom_data"]["interval"] == "year"

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "team", "seats": 2}, headers=hdr)
        assert r.status_code == 400 and "3 seats" in r.text
        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "team", "seats": 4}, headers=hdr)
        assert r.status_code == 200
        assert sent[-1]["data"]["items"] == [{"price_id": "pri_team_m", "quantity": 4}]

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "founding"}, headers=hdr)
        assert r.status_code == 400 and "annually only" in r.text
        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "founding", "billing_cycle": "yearly"}, headers=hdr)
        assert r.status_code == 200
        assert sent[-1]["data"]["items"] == [{"price_id": "pri_founding", "quantity": 1}]
        assert sent[-1]["data"]["custom_data"]["founding"] is True

        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "legacy_pro_49"}, headers=hdr)
        assert r.status_code == 400

        now = datetime.now(UTC).isoformat()
        await c.h.db.conn.executemany(
            "INSERT INTO cloud_tenants (user_id, plan, founding, created_at, updated_at) VALUES (?, 'solo', 1, ?, ?)",
            [(f"f{i}", now, now) for i in range(100)],
        )
        await c.h.db.conn.commit()
        r = await c.h.client.post("/api/v1/billing/checkout", json={"plan": "founding", "billing_cycle": "yearly"}, headers=hdr)
        assert r.status_code == 409 and "sold out" in r.text

        plans = (await c.h.client.get("/api/v1/billing/plans")).json()
        by_id = {p["id"]: p for p in plans["plans"]}
        assert [p["id"] for p in plans["plans"]] == ["solo", "pro", "team"]
        assert by_id["solo"]["price_monthly"] == 1_200 and by_id["solo"]["price_yearly"] == 12_000
        assert by_id["pro"]["available_monthly"] is True and by_id["pro"]["available_yearly"] is False
        assert by_id["team"]["per_seat"] is True and by_id["team"]["min_seats"] == 3
        assert plans["founding"] == {
            "plan": "solo",
            "price_yearly": 10_800,
            "max_redemptions": 100,
            "remaining": 0,
            "available": False,
        }
        config = (await c.h.client.get("/api/v1/billing/client-config")).json()
        assert config["prices"]["solo"] == "pri_solo_m" and config["prices"]["solo_annual"] == "pri_solo_y"
        assert "pro_annual" not in config["prices"]


# ---------------------------------------------------------------------------
# Signup hardening: Turnstile + rate limits + Redis-configurable storage
# ---------------------------------------------------------------------------


def _mock_siteverify(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        seen.append({"url": str(request.url), **form})
        return handler(form)

    monkeypatch.setattr(signup_guard, "http_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return seen


async def test_turnstile_is_inert_without_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    async with cost_app(tmp_path, resend_api_key=None) as c:
        seen = _mock_siteverify(monkeypatch, lambda form: httpx.Response(200, json={"success": False}))
        r = await c.h.client.post("/api/v1/auth/signup", json={"email": "plain@example.com", "password": "Str0ng!Passw0rd"})
        assert r.status_code == 201, r.text
        assert seen == []  # no siteverify call at all


async def test_turnstile_active_verifies_server_side(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    async with cost_app(tmp_path, resend_api_key=None, turnstile_secret="ts-secret-for-tests") as c:
        seen = _mock_siteverify(monkeypatch, lambda form: httpx.Response(200, json={"success": form["response"] == "good-token"}))
        signup = {"email": "bot@example.com", "password": "Str0ng!Passw0rd"}
        r = await c.h.client.post("/api/v1/auth/signup", json=signup)
        assert r.status_code == 400 and "missing Turnstile token" in r.text
        r = await c.h.client.post("/api/v1/auth/signup", json={**signup, "turnstile_token": "forged"})
        assert r.status_code == 400 and r.json()["detail"] == "Human verification failed."
        r = await c.h.client.post("/api/v1/auth/signup", json={**signup, "turnstile_token": "good-token"})
        assert r.status_code == 201, r.text
        assert seen[-1]["secret"] == "ts-secret-for-tests" and seen[-1]["response"] == "good-token"
        assert seen[-1]["remoteip"] == "203.0.113.10"
        assert seen[-1]["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"

        # Header form works too; master-key /cloud/signup is protected the same way.
        r = await c.h.client.post(
            "/api/v1/cloud/signup",
            json={"email": "api@example.com", "client_ip": "198.51.100.7"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert r.status_code == 400
        r = await c.h.client.post(
            "/api/v1/cloud/signup",
            json={"email": "api@example.com", "client_ip": "198.51.100.7", "turnstile_token": "good-token"},
            headers={"X-API-Key": MASTER_KEY},
        )
        assert r.status_code == 201, r.text
        assert seen[-1]["remoteip"] == "198.51.100.7"


async def test_turnstile_fails_closed_when_siteverify_is_unreachable(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path, resend_api_key=None, turnstile_secret="ts-secret-for-tests") as c:

        def down(_form: dict) -> httpx.Response:
            raise httpx.ConnectError("siteverify down")

        _mock_siteverify(monkeypatch, down)
        r = await c.h.client.post(
            "/api/v1/auth/signup",
            json={"email": "x@example.com", "password": "Str0ng!Passw0rd", "turnstile_token": "t"},
        )
        assert r.status_code == 503


async def test_signup_rate_limits_per_network_and_domain(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    async with cost_app(tmp_path, resend_api_key=None, rate_limit_enabled=True, signup_domain_rate_limit="2/day") as c:

        async def cloud_signup(email: str, ip: str) -> int:
            r = await c.h.client.post(
                "/api/v1/cloud/signup", json={"email": email, "client_ip": ip}, headers={"X-API-Key": MASTER_KEY}
            )
            return r.status_code

        assert [await cloud_signup(f"a{i}@gmail.com", f"198.51.100.{i + 1}") for i in range(4)] == [201, 201, 201, 429]
        assert await cloud_signup("b@gmail.com", "198.51.101.1") == 201  # another /24 is fine
        # Per-domain: 2/day for throwaway domains; big mailbox providers are exempt.
        assert [await cloud_signup(f"c{i}@spam.example", f"192.0.2.{i * 64 + 1}") for i in range(3)] == [201, 201, 429]
        assert network_key("198.51.100.200") == "198.51.100.0/24"
        assert network_key("2001:db8::1") == "2001:db8::/56"


def test_rate_limit_storage_is_configurable() -> None:
    assert storage_uri_from_setting("memory") == "memory://"
    assert storage_uri_from_setting("redis://cache:6379/2") == "redis://cache:6379/2"
    limiter = CloudRateLimiter("memory://")
    assert [limiter.hit("t", "k", "2/minute") for _ in range(3)] == [True, True, False]
    try:
        import redis  # noqa: F401
    except ImportError:
        with pytest.raises(ConfigurationError):
            CloudRateLimiter("redis://localhost:6379/0")
    previous = config_module._settings
    try:
        config_module._settings = config_module.Settings(rate_limit_storage="memory")
        set_cloud_rate_limiter(None)
        from remembra.cloud.ratelimit import get_cloud_rate_limiter

        assert get_cloud_rate_limiter().storage_uri == "memory://"
    finally:
        set_cloud_rate_limiter(None)
        config_module._settings = previous


# ---------------------------------------------------------------------------
# Per-plan recall / relay limits and the project cap
# ---------------------------------------------------------------------------


async def test_recall_burst_monthly_cap_and_free_batch_recall_size(tmp_path) -> None:
    async with cost_app(tmp_path, rate_limit_enabled=True) as c:
        uid, hdr = await c.account("recall@example.com")
        recalled: list[str] = []

        async def recall(request: Any) -> RecallResponse:
            recalled.append(request.query)
            return RecallResponse(context="", memories=[], entities=[])

        c.service.recall = recall  # type: ignore[method-assign]
        batch = {"queries": [{"query": f"q{i}"} for i in range(6)]}
        r = await c.h.client.post("/api/v1/memories/batch/recall", json=batch, headers=hdr)
        assert r.status_code == 422 and "5 queries" in r.text
        assert recalled == []  # rejected before any recall work
        codes = [
            (await c.h.client.post("/api/v1/memories/recall", json={"query": "x"}, headers=hdr)).status_code for _ in range(21)
        ]
        assert codes[:20] == [200] * 20 and codes[20] == 429  # Free burst 20/min
        assert c.llm.calls == []  # recalls never touch the LLM or credits
        assert (await c.ledger(uid))["used"] == 0


async def test_relay_burst_and_soft_cap(tmp_path) -> None:
    async with cost_app(tmp_path, rate_limit_enabled=True) as c:
        uid, hdr = await c.account("relayburst@example.com")
        send = {"to_agent": "codex", "subject": "s", "body": "b"}
        codes = [(await c.h.client.post("/api/v1/inbox/send", json=send, headers=hdr)).status_code for _ in range(31)]
        assert codes[:30] == [201] * 30 and codes[30] == 429  # Free relay burst 30/min

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await c.h.db.conn.execute("UPDATE cloud_usage_daily SET relay_events = 5000 WHERE user_id = ? AND date = ?", (uid, today))
        await c.h.db.conn.commit()
        set_cloud_rate_limiter(CloudRateLimiter())
        r = await c.h.client.post("/api/v1/inbox/send", json=send, headers=hdr)
        assert r.status_code == 201  # soft cap: reported, never rejected
        assert r.headers["X-Remembra-Relay-Soft-Cap"] == "exceeded"
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert summary["relay_events"]["over_soft_cap"] is True and summary["relay_events"]["free"] is True


async def test_free_project_cap_blocks_only_new_projects(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _, hdr = await c.account("projects@example.com")
        for p in ("alpha", "beta", "gamma"):
            r = await c.h.client.post("/api/v1/memories", json={"content": f"note in {p}", "project_id": p}, headers=hdr)
            assert r.status_code == 201, r.text
        r = await c.h.client.post("/api/v1/memories", json={"content": "fourth", "project_id": "delta"}, headers=hdr)
        assert r.status_code == 403 and "Project limit reached (3" in r.text
        r = await c.h.client.post("/api/v1/memories", json={"content": "more alpha", "project_id": "alpha"}, headers=hdr)
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Bounded per-tenant enrichment queue
# ---------------------------------------------------------------------------


async def test_queue_bounds_per_tenant_and_global_concurrency() -> None:
    queue = EnrichmentQueue(global_concurrency=3, default_concurrency=4, max_pending_per_tenant=50)
    running: dict[str, int] = {"a": 0, "b": 0}
    peak: dict[str, int] = {"a": 0, "b": 0, "all": 0}
    release = asyncio.Event()

    async def job(tenant: str) -> None:
        running[tenant] += 1
        peak[tenant] = max(peak[tenant], running[tenant])
        peak["all"] = max(peak["all"], running["a"] + running["b"])
        await release.wait()
        running[tenant] -= 1

    tasks = [queue.submit("a", job("a"), name="t", concurrency=2) for _ in range(6)]
    tasks += [queue.submit("b", job("b"), name="t", concurrency=8) for _ in range(6)]
    await asyncio.sleep(0.05)
    assert peak["a"] == 2  # Free: 2 at a time
    assert peak["all"] == 3  # global cap
    release.set()
    await asyncio.gather(*[t for t in tasks if t is not None])
    assert queue.pending("a") == 0 and queue.running() == 0


async def test_queue_drops_droppable_work_beyond_backlog_and_releases_the_job() -> None:
    queue = EnrichmentQueue(global_concurrency=1, default_concurrency=1, max_pending_per_tenant=2)
    gate = asyncio.Event()
    settled: list[float] = []

    async def settle(usd: float, enriched: bool) -> int:
        settled.append(usd)
        return 0

    async def job() -> None:
        await gate.wait()

    spend = ai_spend.SpendJob(user_id="t", settle=settle)
    with ai_spend.activate(spend):
        kept = [queue.submit("t", job(), name="x") for _ in range(2)]
        dropped = queue.submit("t", job(), name="x")
        must_run = queue.submit("t", job(), name="x", droppable=False)
    assert all(kept) and dropped is None and must_run is not None and queue.dropped == 1
    await asyncio.sleep(0.01)
    assert settled == []  # the reservation waits for queued work
    gate.set()
    await spend.wait_settled()
    assert settled == [0.0]


# ---------------------------------------------------------------------------
# Usage API for the dashboard
# ---------------------------------------------------------------------------


async def test_usage_summary_reports_everything_the_dashboard_shows(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("dash@example.com", plan=PlanTier.TEAM, interval=BillingInterval.MONTH, seats=4)
        await c.h.client.post("/api/v1/memories", json={"content": "Team note about the Kingston office"}, headers=hdr)
        await c.h.client.post("/api/v1/memories", json={"content": "handoff text", "memory_type": "handoff"}, headers=hdr)
        await c.h.client.post("/api/v1/inbox/send", json={"to_agent": "a", "subject": "s", "body": "b"}, headers=hdr)
        await c.settle_all()
        await c.set_credits_used(uid, 8_800)  # the whole pooled allowance
        r = await c.h.client.post("/api/v1/memories", json={"content": "stored without enrichment"}, headers=hdr)
        assert r.headers["X-Remembra-Enrichment"] == "degraded"

        s = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert (s["plan"], s["plan_name"], s["interval"], s["seats"], s["founding"]) == ("team", "Team", "month", 4, False)
        assert s["period"]["type"] == "month" and s["period"]["key"].startswith("M:")
        assert s["credits"]["limit"] == 4 * 2_200 and s["credits"]["used"] == 8_800 and s["credits"]["remaining"] == 0
        assert s["credits"]["bank"] == "monthly" and s["credits"]["ceiling_usd"] == 22.0
        assert s["enrichment"] == {"status": "degraded", "reason": "credits_exhausted"}
        assert s["relay_events"]["this_month"] == 2 and s["relay_events"]["soft_cap"] == 100_000
        assert s["recalls"]["limit"] == 200_000 and s["recalls"]["burst_per_min"] == 120
        assert s["memories"] == {"stored": 3, "cap": 200_000, "handoffs": 0}
        assert s["stores"] == {"this_month": 2, "degraded_this_month": 1}

        daily = (await c.h.client.get("/api/v1/cloud/usage/daily", headers=hdr)).json()["days"]
        assert daily[0]["relay_events"] == 2 and daily[0]["credits_used"] >= 1
