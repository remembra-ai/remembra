"""Regressions for the 2026-09-25 review of the smart-credit / plans branch.

Each test drives the real runtime path (production routes, UsageMeter ledger,
MemoryService, enrichment queue, Paddle webhook) over SQLite; only the OpenAI,
TypeSafe, embedding and Qdrant edges are fakes (tests/_cost_harness.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import remembra.cloud.metering as metering
from remembra.cloud.plans import CREDIT_USD, RESERVE_CREDITS_PER_CHUNK, BillingInterval, PlanTier
from remembra.core import ai_spend
from remembra.core.tasks import TaskRegistry, get_task_registry, set_task_registry
from tests._cost_harness import USD_PER_CALL, cost_app

ENRICH = "X-Remembra-Enrichment"
SECRET = "pdl_ntfset_test_secret_value_for_review_fixes"
PRICES = {
    "paddle_price_solo_monthly": "pri_solo_m",
    "paddle_price_solo_annual": "pri_solo_y",
    "paddle_price_pro_monthly": "pri_pro_m",
    "paddle_price_team_seat_monthly": "pri_team_m",
    "paddle_price_founding_annual": "pri_founding",
}


def _move_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(metering, "now_utc", lambda: when)


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


async def _webhook(c: Any, event: dict) -> dict:
    body, headers = _signed(event)
    r = await c.h.client.post("/api/v1/billing/webhook/paddle", content=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. The reservation is a real AI budget; settle never passes it
# ---------------------------------------------------------------------------


async def test_write_stops_calling_the_llm_at_its_reservation_and_never_passes_the_ceiling(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("budget@example.com")  # verified Free: 500 credits
        await c.set_credits_used(uid, 484)  # 16 left: exactly one chunk hold
        sentences = [f"Mani met client number {i} in Kingston on Tuesday." for i in range(25)]
        c.llm.extraction_facts = [sentences]  # the first model call returns 25 facts

        r = await c.h.client.post("/api/v1/memories", json={"content": " ".join(sentences)}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "full"
        await c.settle_all()

        budget = RESERVE_CREDITS_PER_CHUNK * CREDIT_USD  # $0.04
        # Unbounded, 25 facts meant 26+ model calls ($0.07). The budget admits at
        # most budget / cost-per-call calls (plus the one whose estimate was short).
        assert len(c.llm.calls) <= int(budget / USD_PER_CALL) + 1
        ledger = await c.ledger(uid)
        assert ledger["reserved"] == 0
        assert ledger["used"] <= 500  # the plan ceiling holds
        cursor = await c.h.db.conn.execute("SELECT charged, actual_usd FROM cloud_credit_reservations WHERE user_id = ?", (uid,))
        charged, actual_usd = await cursor.fetchone()
        assert charged <= RESERVE_CREDITS_PER_CHUNK
        assert actual_usd <= budget + USD_PER_CALL  # at most one call past the budget
        # The write still stored every fact: past the budget it fell back to atomic (ADD, verbatim).
        cursor = await c.h.db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (uid,))
        assert (await cursor.fetchone())[0] >= 25


async def test_settle_caps_the_charge_at_the_reservation(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("cap@example.com")
        await c.set_credits_used(uid, 484)
        account = await c.meter.get_account(uid)
        rid = await c.meter.reserve_credits(account, 16, min_credits=1)
        assert rid is not None
        # $0.07 of spend (28 credits) against a 16-credit hold.
        assert await c.meter.settle_reservation(rid, 0.07) == 16
        ledger = await c.ledger(uid)
        assert ledger["used"] == 500 and ledger["reserved"] == 0
        assert ledger["usd"] == pytest.approx(0.07)  # real dollars still recorded
        assert await c.meter.ai_spend_month("free") == pytest.approx(0.07)  # and seen by the free breaker


async def test_spend_job_refuses_calls_past_its_budget_and_skips_queued_optional_work(tmp_path) -> None:
    from remembra.core.enrichment_queue import EnrichmentQueue

    job = ai_spend.SpendJob(user_id="u", budget_usd=0.01)
    ran: list[str] = []

    async def entity_pass() -> None:
        ran.append("entities")

    with ai_spend.activate(job):
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_: _async_value(SimpleNamespace(usage={"prompt_tokens": 40_000, "completion_tokens": 2_000}))
                )
            )
        )
        await ai_spend.metered_chat(client, model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}])
        assert job.usd == pytest.approx(40_000 * 0.15 / 1e6 + 2_000 * 0.6 / 1e6)  # $0.0072
        with pytest.raises(ai_spend.SpendBudgetExceeded):
            await ai_spend.metered_chat(client, model="gpt-4o-mini", messages=[{"role": "user", "content": "y" * 90_000}])
        assert job.exhausted and job.blocked_calls == 1
        queue = EnrichmentQueue()
        task = queue.submit("u", entity_pass(), name="entity_resolution", droppable=True)
        assert task is not None
        await task
    assert ran == []  # the optional entity pass of an exhausted write is skipped
    assert await job.wait_settled() is None


async def _async_value(value: Any) -> Any:
    return value


# ---------------------------------------------------------------------------
# 1 / 15. Stale expiry never releases a hold whose work is still alive
# ---------------------------------------------------------------------------


async def test_live_reservation_is_not_expired_and_cannot_be_stacked_on(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("slow@example.com")
        await c.set_credits_used(uid, 480)  # 20 left
        account = await c.meter.get_account(uid)
        rid = await c.meter.reserve_credits(account, 16, min_credits=1)
        assert rid is not None
        settled: list[float] = []

        async def settle(usd: float, enriched: bool) -> int:
            settled.append(usd)
            return await c.meter.settle_reservation(rid, usd, enriched=enriched)

        job = ai_spend.SpendJob(user_id=uid, settle=settle, budget_usd=0.04, reservation_id=rid)
        job.acquire()  # queued background work still holds the job

        _move_clock(monkeypatch, datetime.now(UTC) + timedelta(minutes=16))
        assert await c.meter.expire_stale_reservations() == 0
        # A second write does not fit next to the live hold, and does not expire it.
        assert await c.meter.reserve_credits(account, 16, min_credits=1) is None
        assert (await c.ledger(uid))["reserved"] == 16

        job.release()  # the request
        job.release()  # the background work
        await job.wait_settled()
        ledger = await c.ledger(uid)
        assert ledger["used"] <= 500 and ledger["reserved"] == 0
        assert not ai_spend.is_live_reservation(rid)


async def test_orphaned_reservations_expire_and_still_count_for_the_free_breaker(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("orphan@example.com")
        account = await c.meter.get_account(uid)
        old = await c.meter.reserve_credits(account, 32, min_credits=2)  # no live job: a previous process
        assert old is not None
        started = datetime.now(UTC) + timedelta(seconds=1)
        _move_clock(monkeypatch, started + timedelta(seconds=1))
        new = await c.meter.reserve_credits(account, 16, min_credits=1)
        job = ai_spend.SpendJob(user_id=uid, reservation_id=new)

        # Startup: only holds opened before this process started are released.
        assert await c.meter.expire_stale_reservations(created_before=started) == 1
        cursor = await c.h.db.conn.execute("SELECT id, status FROM cloud_credit_reservations ORDER BY created_at")
        assert [tuple(r) for r in await cursor.fetchall()] == [(old, "expired"), (new, "open")]
        # The expired hold's work may have spent up to 32 credits: the breaker keeps counting it.
        assert await c.meter.free_tier_spend() == pytest.approx((32 + 16) * CREDIT_USD)
        assert job.alive


# ---------------------------------------------------------------------------
# 4. A settle released after the task registry shut down is not lost
# ---------------------------------------------------------------------------


async def test_settle_after_registry_shutdown_still_runs_and_is_drained(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("shutdown@example.com")
        account = await c.meter.get_account(uid)
        rid = await c.meter.reserve_credits(account, 16, min_credits=1)
        assert rid is not None

        async def settle(usd: float, enriched: bool) -> int:
            return await c.meter.settle_reservation(rid, usd, enriched=enriched)

        previous = get_task_registry()
        registry = TaskRegistry()
        set_task_registry(registry)
        try:
            job = ai_spend.SpendJob(user_id=uid, settle=settle, reservation_id=rid)
            job.add(0.01)  # 4 credits of real spend
            await registry.shutdown()
            job.release()  # e.g. a cancelled enrichment task's finally block
            await ai_spend.drain_settles(timeout=5)
        finally:
            set_task_registry(previous)
        assert job.settled and job.settled_result == 4
        ledger = await c.ledger(uid)
        assert ledger == {"used": 4, "reserved": 0, "remaining": 496, "usd": pytest.approx(0.01)}


# ---------------------------------------------------------------------------
# 2 / 9. Team seats: exactly the quantity paid for
# ---------------------------------------------------------------------------


async def test_team_purchase_of_one_seat_grants_one_seat_and_flags_it(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("one-seat@example.com")
        event = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_team_1",
                "subscription_id": "sub_team_1",
                "items": [{"price": {"id": "pri_team_m"}, "quantity": 1}],
                "custom_data": {"remembra_user_id": uid, "plan": "team"},
            },
        }
        assert (await _webhook(c, event))["applied"] == "applied"
        account = await c.meter.get_account(uid)
        assert account.tier == PlanTier.TEAM
        assert account.seats == 1 and account.limits.max_users == 1
        assert account.credit_limit == 2_200 and account.limits.max_memories == 50_000  # $15 buys one seat
        assert (await c.meter.get_tenant(uid))["billing_flag"] == "team_seats_below_minimum"

        config = (await c.h.client.get("/api/v1/billing/client-config")).json()
        assert "team" not in config["prices"] and "founding" not in config["prices"]  # server checkout only
        assert config["prices"]["solo"] == "pri_solo_m"


# ---------------------------------------------------------------------------
# 3 / 8. Unknown prices never fall back to browser-controlled custom_data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "custom",
    [
        {"plan": "enterprise"},
        {"plan": "legacy_team_199"},
        {"plan": "solo", "founding": True},
        {"plan": "founding", "interval": "year"},
    ],
)
async def test_unknown_price_with_custom_data_plan_is_ignored(tmp_path, custom: dict) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("forger@example.com")
        event = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_cheap",
                "items": [{"price": {"id": "pri_some_cheap_one_time"}, "quantity": 1}],
                "custom_data": {"remembra_user_id": uid, **custom},
            },
        }
        result = await _webhook(c, event)
        assert result["action"] == "ignored" and result["applied"] == "no_change"
        account = await c.meter.get_account(uid)
        assert account.tier == PlanTier.FREE and not account.founding and account.credit_limit == 500
        assert await c.meter.founding_redemptions() == 0


# ---------------------------------------------------------------------------
# 12. Founding 100: the cap holds in the webhook too
# ---------------------------------------------------------------------------


async def test_founding_webhook_past_the_cap_grants_plain_solo_annual_and_flags_a_refund(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        now = datetime.now(UTC).isoformat()
        await c.h.db.conn.executemany(
            "INSERT INTO cloud_tenants (user_id, plan, founding, created_at, updated_at) VALUES (?, 'solo', 1, ?, ?)",
            [(f"f{i}", now, now) for i in range(100)],
        )
        await c.h.db.conn.commit()
        uid = await c.h.create_user("late@example.com")
        event = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_f_101",
                "subscription_id": "sub_f_101",
                "items": [{"price": {"id": "pri_founding"}, "quantity": 1}],
                "custom_data": {"remembra_user_id": uid},
            },
        }
        assert (await _webhook(c, event))["applied"] == "applied"
        assert await c.meter.founding_redemptions() == 100
        account = await c.meter.get_account(uid)
        assert (account.tier, account.interval, account.founding) == (PlanTier.SOLO, BillingInterval.YEAR, False)
        assert (await c.meter.get_tenant(uid))["billing_flag"] == "founding_over_cap_refund_due"

        # A holder's renewal keeps the founding price.
        holder = await c.h.create_user("holder@example.com")
        await c.h.db.conn.execute("DELETE FROM cloud_tenants WHERE user_id = 'f0'")
        await c.h.db.conn.commit()
        event["data"].update(id="txn_f_holder", subscription_id="sub_holder", custom_data={"remembra_user_id": holder})
        await _webhook(c, event)
        assert (await c.meter.get_account(holder)).founding is True
        await _webhook(c, {**event, "data": {**event["data"], "id": "txn_f_holder_renewal"}})
        assert (await c.meter.get_account(holder)).founding is True
        assert await c.meter.founding_redemptions() == 100


# ---------------------------------------------------------------------------
# 5. Creating a team inherits the owner's paid seats
# ---------------------------------------------------------------------------


async def test_team_owner_can_create_a_team_and_invite_up_to_the_paid_seats(tmp_path) -> None:
    from remembra.api.v1 import teams
    from remembra.teams.manager import TeamManager

    async with cost_app(tmp_path) as c:
        c.h.app.include_router(teams.router, prefix="/api/v1")
        manager = TeamManager(c.h.db)
        await manager.init_schema()
        c.h.app.state.team_manager = manager
        owner = await c.h.create_user("owner3@example.com")
        await c.meter.apply_subscription(owner, PlanTier.TEAM, interval=BillingInterval.MONTH, seats=3)
        hdr = c.h.jwt(owner, "owner3@example.com")

        r = await c.h.client.post("/api/v1/teams", json={"name": "Three Seats"}, headers=hdr)
        assert r.status_code == 201, r.text
        team = r.json()
        assert team["max_seats"] == 3 and team["plan"] == "team"

        for i in range(2):
            email = f"member{i}@example.com"
            member = await c.h.create_user(email)
            r = await c.h.client.post(f"/api/v1/teams/{team['id']}/invites", json={"email": email}, headers=hdr)
            assert r.status_code == 201, r.text
            token = r.json()["invite_url"].rsplit("/", 1)[-1]
            r = await c.h.client.post("/api/v1/teams/invites/accept", json={"token": token}, headers=c.h.jwt(member, email))
            assert r.status_code == 200, r.text
        r = await c.h.client.post(f"/api/v1/teams/{team['id']}/invites", json={"email": "fourth@example.com"}, headers=hdr)
        assert r.status_code == 403 and "maximum seats (3)" in r.text


# ---------------------------------------------------------------------------
# 11. Team members are metered against the owner's pooled account
# ---------------------------------------------------------------------------


async def _team(c: Any, owner: str, members: list[str]) -> None:
    now = datetime.now(UTC).isoformat()
    await c.h.db.conn.execute(
        "INSERT INTO teams (id, name, slug, owner_id, plan, max_seats, used_seats, created_at, updated_at)"
        " VALUES ('team1', 'Pool', 'pool', ?, 'team', 3, ?, ?, ?)",
        (owner, 1 + len(members), now, now),
    )
    rows = [("team1", owner, "owner", now, now)] + [("team1", m, "member", now, now) for m in members]
    await c.h.db.conn.executemany(
        "INSERT INTO team_members (team_id, user_id, role, joined_at, updated_at) VALUES (?, ?, ?, ?, ?)", rows
    )
    await c.h.db.conn.commit()


async def test_team_member_stores_against_the_owners_pooled_bank(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        owner, _ = await c.account("pool-owner@example.com", plan=PlanTier.TEAM, interval=BillingInterval.MONTH, seats=3)
        member, member_hdr = await c.account("pool-member@example.com")
        await _team(c, owner, [member])

        account = await c.meter.get_account(member)
        assert account.user_id == owner and account.member_user_id == member
        assert account.tier == PlanTier.TEAM and account.credit_limit == 6_600
        assert set(account.pool) == {owner, member}

        # Free limits would reject all three of these (413 / 403 x 3 projects / 10-item batch).
        r = await c.h.client.post("/api/v1/memories", json={"content": "Team note. " + "x" * 9_000}, headers=member_hdr)
        assert r.status_code == 201 and r.headers[ENRICH] == "full", r.text
        for i in range(4):
            r = await c.h.client.post(
                "/api/v1/memories",
                json={"content": f"project note {i}", "project_id": f"proj-{i}", "skip_extraction": True},
                headers=member_hdr,
            )
            assert r.status_code == 201, r.text
        await c.settle_all()

        cursor = await c.h.db.conn.execute("SELECT DISTINCT user_id FROM cloud_credit_reservations")
        assert [row[0] for row in await cursor.fetchall()] == [owner]  # the owner's ledger was charged
        assert (await c.ledger(owner))["used"] >= 2
        assert (await c.ledger(member))["used"] == (await c.ledger(owner))["used"]  # same pooled bank
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=member_hdr)).json()
        assert summary["plan"] == "team" and summary["credits"]["limit"] == 6_600
        assert summary["memories"]["stored"] == 5 and summary["memories"]["cap"] == 150_000

        # Members beyond the paid seats are not pooled.
        extra = [await c.h.create_user(f"extra{i}@example.com") for i in range(2)]
        now = datetime.now(UTC).isoformat()
        await c.h.db.conn.executemany(
            "INSERT INTO team_members (team_id, user_id, role, joined_at, updated_at) VALUES ('team1', ?, 'member', ?, ?)",
            [(u, now, now) for u in extra],
        )
        await c.h.db.conn.commit()
        pooled = [u for u in extra if (await c.meter.get_account(u)).user_id == owner]
        assert len(pooled) == 1  # 3 seats: owner + 2 members


# ---------------------------------------------------------------------------
# 10. The unverified-email hold is off until enabled, and grandfathers existing users
# ---------------------------------------------------------------------------


async def test_unverified_cap_is_off_by_default_and_grandfathers_existing_accounts(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        old_uid, _ = await c.account("existing@example.com", verified=False)
        assert (await c.meter.get_account(old_uid)).credit_limit == 500  # cap not enabled yet

        c.h.settings.unverified_credit_cap_effective_at = datetime.now(UTC) + timedelta(seconds=1)
        assert (await c.meter.get_account(old_uid)).credit_limit == 500  # created before: grandfathered
        await asyncio.sleep(1.1)
        new_uid, _ = await c.account("brand-new@example.com", verified=False)
        assert (await c.meter.get_account(new_uid)).credit_limit == 25
        await c.h.db.update_user_email_verified(new_uid, True)
        assert (await c.meter.get_account(new_uid)).credit_limit == 500

        # A master-key /cloud/signup tenant (no user record, no way to verify) is exempt.
        await c.meter.register_tenant("tenant_only_user", email="api-signup@example.com")
        assert (await c.meter.get_account("tenant_only_user")).credit_limit == 500


# ---------------------------------------------------------------------------
# 6. TypeSafe (Jev) calls are AI spend: billed on writes, skipped for free recalls
# ---------------------------------------------------------------------------


def _typesafe(calls: list[dict]) -> Any:
    from remembra.extraction.typesafe import TypeSafeClient

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        answers: dict[str, Any] = {}
        for qid, q in body["questions"].items():
            if q["type"] == "choice":
                options = list(q.get("criteria") or {}) or ["balanced"]
                answers[qid] = {"type": "choice", "choice": options[0], "probabilities": {options[0]: 0.9}}
            else:
                answers[qid] = {"type": "noul", "noul": 0.1}
        return httpx.Response(200, json={"answers": answers})

    return TypeSafeClient(api_key="ts-test", transport=httpx.MockTransport(respond), usd_per_request=0.004)


async def test_typesafe_calls_are_billed_to_the_write_and_skipped_on_free_recalls(tmp_path) -> None:
    async with cost_app(tmp_path, typesafe_api_key="ts-test", typesafe_mode="shadow", typesafe_usd_per_request=0.004) as c:
        ts_calls: list[dict] = []
        c.service.jev._client = _typesafe(ts_calls)
        assert c.service.jev.enabled and c.service.intent_router.jev is c.service.jev

        uid, hdr = await c.account("jev@example.com")
        for content in ("Mani moved the office to Montego Bay.", "Mani moved the office to Montego Bay in June."):
            c.llm.extraction_facts = [[content]]
            r = await c.h.client.post("/api/v1/memories", json={"content": content}, headers=hdr)
            assert r.status_code == 201 and r.headers[ENRICH] == "full"
            await c.settle_all()
        assert ts_calls, "shadow mode asks Jev about the fact that has a similar memory"
        cursor = await c.h.db.conn.execute(
            "SELECT COALESCE(SUM(actual_usd), 0) FROM cloud_credit_reservations WHERE user_id = ?", (uid,)
        )
        (actual_usd,) = await cursor.fetchone()
        # Every TypeSafe request was billed to the write that made it, next to the OpenAI calls.
        assert actual_usd == pytest.approx(len(c.llm.calls) * USD_PER_CALL + len(ts_calls) * 0.004)

        # Recalls resolve their ranking mode through the intent router
        # (MemoryService.recall -> intent_router.resolve); a query no rule
        # matches is the one that asks Jev in shadow mode.
        router = c.service.intent_router
        query = "office location details"
        before = len(ts_calls)
        # Free recalls: no Jev call at all (recalls never use credits).
        decision = await router.resolve("auto", query, uid, None)
        await c.settle_all()
        assert decision.mode == "balanced" and len(ts_calls) == before

        # Paid recalls may ask Jev; the dollars land in the paid group's monthly spend.
        solo, _ = await c.account("jev-solo@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)
        paid_before = await c.meter.ai_spend_month("paid")
        await router.resolve("auto", query, solo, None)
        await c.settle_all()
        assert len(ts_calls) == before + 1
        assert await c.meter.ai_spend_month("paid") == pytest.approx(paid_before + 0.004)


# ---------------------------------------------------------------------------
# 7. Free unenriched writes: daily cap and embedding spend on the free breaker
# ---------------------------------------------------------------------------


async def test_free_daily_unenriched_write_cap_and_embedding_spend(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("atomic@example.com")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await c.h.db.conn.execute(
            "INSERT INTO cloud_usage_daily (user_id, date, unenriched_writes) VALUES (?, ?, 299)", (uid, today)
        )
        await c.h.db.conn.commit()
        before = await c.meter.ai_spend_month("free")

        r = await c.h.client.post(
            "/api/v1/memories", json={"content": "handoff: " + "h" * 4_000, "memory_type": "handoff"}, headers=hdr
        )
        assert r.status_code == 201 and r.headers[ENRICH] == "atomic", r.text
        assert await c.meter.ai_spend_month("free") > before  # the embedding is counted for the breaker

        r = await c.h.client.post("/api/v1/memories", json={"content": "one too many", "skip_extraction": True}, headers=hdr)
        assert r.status_code == 429 and "300 stores without enrichment per day" in r.json()["detail"]
        r = await c.h.client.post("/api/v1/session/status", json={"key": "deploy:api", "value": "still blocked"}, headers=hdr)
        assert r.status_code == 429  # relay stores embed too
        # Degraded writes (out of credits) count as unenriched as well.
        await c.set_credits_used(uid, 500)
        r = await c.h.client.post("/api/v1/memories", json={"content": "degraded store"}, headers=hdr)
        assert r.status_code == 429
        # Enriched writes are metered by credits instead.
        await c.set_credits_used(uid, 0)
        r = await c.h.client.post("/api/v1/memories", json={"content": "Mani likes ackee"}, headers=hdr)
        assert r.status_code == 201 and r.headers[ENRICH] == "full"
        await c.settle_all()

        # Paid plans have no daily cap.
        solo, solo_hdr = await c.account("atomic-solo@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)
        await c.h.db.conn.execute(
            "INSERT INTO cloud_usage_daily (user_id, date, unenriched_writes) VALUES (?, ?, 5000)", (solo, today)
        )
        await c.h.db.conn.commit()
        r = await c.h.client.post("/api/v1/memories", json={"content": "paid atomic", "skip_extraction": True}, headers=solo_hdr)
        assert r.status_code == 201


async def test_unenriched_cap_is_atomic_under_concurrency(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("race-atomic@example.com")
        account = await c.meter.get_account(uid)
        results = await asyncio.gather(*(c.meter.take_unenriched_writes(account, 10) for _ in range(40)))
        assert sum(results) == 30  # 300 / 10, never more


# ---------------------------------------------------------------------------
# 13. Signup limits are charged only after Turnstile passes
# ---------------------------------------------------------------------------


async def test_tokenless_signups_cannot_lock_out_a_network(tmp_path, monkeypatch) -> None:
    from remembra.cloud import signup_guard
    from tests.security_harness import MASTER_KEY

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(
        signup_guard,
        "http_client_factory",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"success": b"response=good-token" in req.content})
            )
        ),
    )
    async with cost_app(tmp_path, resend_api_key=None, rate_limit_enabled=True, turnstile_secret="ts-secret") as c:

        async def signup(email: str, ip: str, token: str | None) -> int:
            body: dict[str, Any] = {"email": email, "client_ip": ip}
            if token:
                body["turnstile_token"] = token
            r = await c.h.client.post("/api/v1/cloud/signup", json=body, headers={"X-API-Key": MASTER_KEY})
            return r.status_code

        # An attacker without a token in the victim's /24, and at the victim's domain.
        assert [await signup(f"x{i}@victimcorp.com", "198.51.100.9", None) for i in range(25)] == [400] * 25
        assert await signup("alice@victimcorp.com", "198.51.100.20", "good-token") == 201
        # The strict limit still applies to verified signups (3/hour per /24).
        assert [await signup(f"v{i}@gmail.com", "198.51.100.30", "good-token") for i in range(3)] == [201, 201, 429]
        # The loose attempt cap bounds siteverify calls (30/hour per /24 by default).
        codes = [await signup(f"y{i}@example.org", "203.0.113.5", None) for i in range(31)]
        assert codes[:30] == [400] * 30 and codes[30] == 429


# ---------------------------------------------------------------------------
# 14. Behind Cloudflare the client IP is the visitor, not the edge
# ---------------------------------------------------------------------------


def _request(peer: str, headers: dict[str, str]) -> Any:
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 443),
    }
    return Request(scope)


def test_client_ip_behind_cloudflare(monkeypatch) -> None:
    import remembra.auth.middleware as middleware
    from tests.security_harness import make_settings

    settings = make_settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    edge = "162.158.10.20"  # a Cloudflare edge address
    proxy = "172.18.0.2"  # Coolify / Traefik on the docker network (trusted by default)

    # Traefik appended the edge to the chain Cloudflare sent.
    assert middleware.get_client_ip(_request(proxy, {"X-Forwarded-For": f"203.0.113.9, {edge}"})) == "203.0.113.9"
    # Traefik replaced the chain with the edge only: Cloudflare's header names the visitor.
    req = _request(proxy, {"X-Forwarded-For": edge, "CF-Connecting-IP": "198.51.100.7"})
    assert middleware.get_client_ip(req) == "198.51.100.7"
    # Cloudflare connecting directly.
    assert middleware.get_client_ip(_request(edge, {"CF-Connecting-IP": "198.51.100.8"})) == "198.51.100.8"
    # A direct, untrusted client cannot claim an address with either header.
    spoof = _request("203.0.113.66", {"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": f"1.2.3.4, {edge}"})
    assert middleware.get_client_ip(spoof) == "203.0.113.66"
    # A non-Cloudflare hop in the chain is the client, whatever CF-Connecting-IP says.
    req = _request(proxy, {"X-Forwarded-For": "192.0.2.44", "CF-Connecting-IP": "1.2.3.4"})
    assert middleware.get_client_ip(req) == "192.0.2.44"
    # Opt-out: the edge address is the client again.
    settings.trust_cloudflare_proxies = False
    assert middleware.get_client_ip(_request(proxy, {"X-Forwarded-For": edge, "CF-Connecting-IP": "198.51.100.7"})) == edge


# ---------------------------------------------------------------------------
# 16. Refunds and chargebacks; configurable release of the yearly bank
# ---------------------------------------------------------------------------


async def test_refund_and_chargeback_end_the_plan_and_reduce_revenue(tmp_path, monkeypatch) -> None:
    _move_clock(monkeypatch, datetime(2026, 9, 10, tzinfo=UTC))
    async with cost_app(tmp_path) as c:
        _paddle(c, **PRICES)
        uid = await c.h.create_user("refund@example.com")
        purchase = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_solo_y",
                "subscription_id": "sub_solo_y",
                "customer_id": "ctm_r",
                "currency_code": "USD",
                "details": {"totals": {"earnings": "10500"}},
                "items": [{"price": {"id": "pri_solo_y"}, "quantity": 1}],
                "custom_data": {"remembra_user_id": uid},
            },
        }
        await _webhook(c, purchase)
        assert (await c.meter.get_account(uid)).credit_limit == 26_400
        assert await c.meter.revenue_for_month("2026-09") == pytest.approx(105.0)

        def adjustment(adj_id: str, action: str, status: str, kind: str, earnings: str) -> dict:
            return {
                "event_type": "adjustment.updated",
                "data": {
                    "id": adj_id,
                    "action": action,
                    "type": kind,
                    "status": status,
                    "transaction_id": "txn_solo_y",
                    "subscription_id": "sub_solo_y",
                    "customer_id": "ctm_r",
                    "totals": {"currency_code": "USD", "earnings": earnings, "total": "12000"},
                },
            }

        # Pending refunds change nothing.
        assert (await _webhook(c, adjustment("adj_1", "refund", "pending_approval", "partial", "2000")))["action"] == "ignored"
        # An approved partial refund reduces revenue, keeps the plan.
        result = await _webhook(c, adjustment("adj_2", "refund", "approved", "partial", "2000"))
        assert result["applied"] == "no_change"
        assert await c.meter.revenue_for_month("2026-09") == pytest.approx(85.0)
        assert (await c.meter.get_account(uid)).tier == PlanTier.SOLO
        # An approved full refund ends the plan (and the banked credits) at once.
        result = await _webhook(c, adjustment("adj_3", "refund", "approved", "full", "8500"))
        assert result["applied"] == "applied"
        account = await c.meter.get_account(uid)
        assert account.tier == PlanTier.FREE and account.credit_limit == 500
        assert await c.meter.revenue_for_month("2026-09") == pytest.approx(0.0)
        assert (await c.meter.get_tenant(uid))["billing_flag"] == "refund_downgraded"

        # Chargebacks downgrade too (resubscribe first).
        await _webhook(c, {**purchase, "data": {**purchase["data"], "id": "txn_solo_y2"}})
        assert (await c.meter.get_account(uid)).tier == PlanTier.SOLO
        await _webhook(c, adjustment("adj_4", "chargeback", "approved", "full", "10500"))
        assert (await c.meter.get_account(uid)).tier == PlanTier.FREE


async def test_annual_bank_can_be_released_monthly(tmp_path, monkeypatch) -> None:
    anchor = datetime(2026, 3, 15, tzinfo=UTC)
    _move_clock(monkeypatch, datetime(2026, 3, 20, tzinfo=UTC))
    async with cost_app(tmp_path, annual_credit_upfront_months=3) as c:
        uid, _ = await c.account("drip@example.com", plan=PlanTier.SOLO, interval=BillingInterval.YEAR, period_anchor=anchor)
        assert (await c.meter.get_account(uid)).credit_limit == 3 * 2_200  # first month: 3 months' worth
        _move_clock(monkeypatch, datetime(2026, 7, 16, tzinfo=UTC))  # 4 whole months in
        assert (await c.meter.get_account(uid)).credit_limit == 7 * 2_200
        _move_clock(monkeypatch, datetime(2027, 2, 20, tzinfo=UTC))
        assert (await c.meter.get_account(uid)).credit_limit == 12 * 2_200  # never past the yearly bank


async def test_sleep_time_pass_is_budgeted_by_the_credits_left(tmp_path) -> None:
    from remembra.extraction.consolidator import ExistingMemory
    from remembra.services.sleep_time import SleepTimeWorker

    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("sleep-budget@example.com")
        await c.h.client.post("/api/v1/memories", json={"content": "atomic note", "skip_extraction": True}, headers=hdr)
        await c.set_credits_used(uid, 499)  # one credit ($0.0025) left
        worker = SleepTimeWorker(settings=c.h.settings, memory_service=c.service, usage_meter=c.meter)

        async def dedup(user_id: str, memories: list) -> int:
            for _ in range(5):  # would be 5 x $0.0027 unbudgeted
                await worker.consolidator.consolidate("atomic note", [ExistingMemory(id="x", content="atomic note!", score=0.99)])
            return 0

        worker._dedup_pass = dedup  # type: ignore[method-assign]
        await worker._consolidate_user(uid)
        await c.settle_all()
        assert len(c.llm.calls) == 1  # the budget refused the rest (the consolidator fell back to ADD)
        ledger = await c.ledger(uid)
        assert ledger["used"] == 500  # never past the limit
        assert ledger["usd"] == pytest.approx(USD_PER_CALL)


async def test_anthropic_entity_extraction_is_budgeted_and_metered() -> None:
    from remembra.cloud.model_prices import anthropic_usage, usd_for
    from remembra.extraction.entities import AnthropicEntityExtractor

    usage = SimpleNamespace(input_tokens=9_000, cache_read_input_tokens=1_000, output_tokens=500)
    mapped = anthropic_usage(usage)
    assert mapped == {"prompt_tokens": 10_000, "completion_tokens": 500, "prompt_tokens_details": {"cached_tokens": 1_000}}
    expected = usd_for(mapped, "claude-sonnet-4-5")
    assert expected == pytest.approx((9_000 * 3.0 + 1_000 * 0.30 + 500 * 15.0) / 1e6)

    calls: list[dict] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        block = SimpleNamespace(type="text", text='{"entities": [{"name": "Mani", "type": "PERSON"}], "relationships": []}')
        return SimpleNamespace(content=[block], usage=usage)

    extractor = AnthropicEntityExtractor.__new__(AnthropicEntityExtractor)
    extractor.model = "claude-sonnet-4-5"
    extractor._client = SimpleNamespace(messages=SimpleNamespace(create=create))

    job = ai_spend.SpendJob(user_id="u", budget_usd=0.05)
    with ai_spend.activate(job):
        result = await extractor.extract("Mani lives in Kingston and runs DolphyTech.")
        assert [e.name for e in result.entities] == ["Mani"]
        assert job.usd == pytest.approx(expected)
        job.budget_usd = job.usd  # used up: the next call must not be made
        result = await extractor.extract("Suzan works at the clinic in Ocho Rios.")
        assert result.entities == [] and len(calls) == 1
    await job.wait_settled()
