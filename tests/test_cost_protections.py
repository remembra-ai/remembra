"""Cost protections: relay gate, smart credits, degrade, reservation settle, annual bank, breaker.

Every test drives the production routes (real gate, real UsageMeter ledger,
real MemoryService, real enrichment queue) over SQLite; only the OpenAI,
embedding and Qdrant edges are fakes (see tests/_cost_harness.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import remembra.cloud.metering as metering
from remembra.cloud.plans import (
    CREDIT_USD,
    RESERVE_CREDITS_PER_CHUNK,
    BillingInterval,
    PlanTier,
    charge_for,
    credits_for_usd,
    estimate_chunks,
)
from tests._cost_harness import USD_PER_CALL, cost_app

# The unverified-email credit hold is off until the owner sets its start date.
CAP_ON = datetime(2026, 1, 1, tzinfo=UTC)
ENRICH = "X-Remembra-Enrichment"
REMAINING = "X-Remembra-Credits-Remaining"


def _move_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(metering, "now_utc", lambda: when)


# ---------------------------------------------------------------------------
# Credit math
# ---------------------------------------------------------------------------


def test_credit_math_is_chunk_and_dollar_based() -> None:
    assert estimate_chunks(["x" * 8000]) == 1
    assert estimate_chunks(["x" * 8001]) == 2
    assert estimate_chunks(["a", "b" * 20_000, "c" * 8000]) == 1 + 3 + 1
    assert credits_for_usd(0.0075) == 3  # no float drift to 4
    assert credits_for_usd(0.0) == 0
    assert charge_for(2, 0.0010) == 2  # chunk minimum wins
    assert charge_for(1, 0.0101) == 5  # actual spend wins
    assert CREDIT_USD == 0.0025 and RESERVE_CREDITS_PER_CHUNK == 16


# ---------------------------------------------------------------------------
# P0 relay cost gate
# ---------------------------------------------------------------------------


async def test_handoff_checkpoint_status_and_inbox_make_zero_llm_calls(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("relay@example.com")

        r = await c.h.client.post(
            "/api/v1/memories",
            json={
                "content": "Handoff: finished the billing gate, next wire the dashboard. Mani, Kingston.",
                "memory_type": "handoff",
            },
            headers=hdr,
        )
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "atomic"
        assert r.json()["entities_status"] == "disabled"
        r = await c.h.client.post(
            "/api/v1/memories",
            json={"content": "Checkpoint: tests green on the credit ledger for Remembra.", "memory_type": "checkpoint"},
            headers=hdr,
        )
        assert r.status_code == 201, r.text
        r = await c.h.client.post(
            "/api/v1/session/status", json={"key": "deploy:api", "value": "blocked on Paddle price IDs"}, headers=hdr
        )
        assert r.status_code == 200, r.text
        r = await c.h.client.post(
            "/api/v1/inbox/send", json={"to_agent": "codex", "subject": "pickup", "body": "review the gate"}, headers=hdr
        )
        assert r.status_code == 201, r.text
        await c.settle_all()

        assert c.llm.calls == []  # no extraction, no entity pass, nothing
        month = await c.meter.get_monthly_usage(uid)
        assert month["relay_events"] == 4
        assert month["credits_used"] == 0
        assert (await c.ledger(uid))["used"] == 0


async def test_normal_store_still_runs_entity_resolution(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _, hdr = await c.account("normal@example.com")
        r = await c.h.client.post("/api/v1/memories", json={"content": "Suzan lives in Kingston with Mani."}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "full"
        await c.settle_all()
        prompts = " ".join(c.llm.systems()).lower()
        assert len(c.llm.calls) >= 2  # fact extraction + the background entity pass
        assert "entit" in prompts


# ---------------------------------------------------------------------------
# Credit exhaustion degrades, never rejects
# ---------------------------------------------------------------------------


async def test_out_of_credits_degrades_to_atomic_with_headers_not_429(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("broke@example.com")  # verified Free: 500 credits
        await c.set_credits_used(uid, 500)

        r = await c.h.client.post("/api/v1/memories", json={"content": "Mani prefers dark mode in the dashboard."}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "degraded"
        assert r.headers[REMAINING] == "0"
        assert "stores still save" in r.headers["X-Remembra-Upgrade-Hint"]
        body = r.json()
        assert body["enrichment"] == "degraded" and body["extraction"] == "skipped"
        assert body["entities_status"] == "disabled"
        await c.settle_all()
        assert c.llm.calls == []
        rows = await c.h.db.conn.execute("SELECT content FROM memories WHERE user_id = ?", (uid,))
        assert [row[0] for row in await rows.fetchall()] == ["Mani prefers dark mode in the dashboard."]
        assert (await c.meter.get_monthly_usage(uid))["degraded_stores"] == 1
        assert (await c.ledger(uid))["used"] == 500  # nothing charged for a degraded store


async def test_memory_cap_is_the_only_store_429(tmp_path) -> None:
    async with cost_app(tmp_path, memory_cap_notice_effective_at=datetime(2026, 1, 1, tzinfo=UTC)) as c:
        uid, hdr = await c.account("full@example.com")
        now = datetime.now(UTC).isoformat()
        await c.h.db.conn.executemany(
            "INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at) VALUES (?, ?, 'default', 'x', ?, ?)",
            [(f"m{i}", uid, now, now) for i in range(10_000)],
        )
        await c.h.db.conn.commit()
        r = await c.h.client.post("/api/v1/memories", json={"content": "one more"}, headers=hdr)
        assert r.status_code == 429
        assert "Memory limit reached (10,000 memories)" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Chunk metering BEFORE any LLM call (batch, bulk, ingest)
# ---------------------------------------------------------------------------


async def test_batch_reserves_per_8k_chunk_before_the_first_llm_call(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("batch@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)
        items = [{"content": "a" * 20_000}, {"content": "short fact about Mani"}, {"content": "b" * 8_000}]
        chunks = 3 + 1 + 1

        seen: list[dict] = []
        first: list[bool] = []

        async def observe(_kwargs: dict) -> None:
            if first:
                return
            first.append(True)  # items run concurrently; look at the ledger once, at the first call
            cursor = await c.h.db.conn.execute(
                "SELECT credits, min_credits, status FROM cloud_credit_reservations WHERE user_id = ?", (uid,)
            )
            seen.extend(dict(zip(("credits", "min", "status"), row, strict=True)) for row in await cursor.fetchall())

        c.llm.on_call = observe
        r = await c.h.client.post("/api/v1/memories/batch", json={"items": items}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "full"
        await c.settle_all()

        # The single reservation for the whole batch existed before the first model call.
        assert seen == [{"credits": chunks * RESERVE_CREDITS_PER_CHUNK, "min": chunks, "status": "open"}]
        ledger = await c.ledger(uid)
        assert ledger["reserved"] == 0
        assert ledger["used"] == charge_for(chunks, len(c.llm.calls) * USD_PER_CALL)


async def test_batch_that_does_not_fit_degrades_whole_batch_before_llm(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("tight@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)
        items = [{"content": "a" * 20_000}, {"content": "short"}]  # 4 chunks -> 64 credits needed
        await c.set_credits_used(uid, 2_200 - 63)
        r = await c.h.client.post("/api/v1/memories/batch", json={"items": items}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "degraded" and r.headers[REMAINING] == "63"
        assert r.json()["succeeded"] == 2
        assert all(res["response"]["enrichment"] == "degraded" for res in r.json()["results"])
        await c.settle_all()
        assert c.llm.calls == []
        assert (await c.meter.get_monthly_usage(uid))["degraded_stores"] == 2


async def test_free_batch_and_content_limits_apply_before_any_work(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _, hdr = await c.account("limits@example.com")
        r = await c.h.client.post("/api/v1/memories", json={"content": "x" * 8_001}, headers=hdr)
        assert r.status_code == 413 and "8,000 characters" in r.text
        r = await c.h.client.post(
            "/api/v1/memories/batch", json={"items": [{"content": f"f{i}"} for i in range(11)]}, headers=hdr
        )
        assert r.status_code == 422 and "10 items" in r.text
        r = await c.h.client.post("/api/v1/memories/bulk", json={"items": [{"content": "y" * 9_000}]}, headers=hdr)
        assert r.status_code == 413
        assert c.llm.calls == [] and c.service.embeddings.calls == []  # type: ignore[attr-defined]


async def test_bulk_import_is_atomic_and_never_uses_credits(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("bulk@example.com")
        await c.set_credits_used(uid, 500)  # out of credits: bulk still works
        r = await c.h.client.post(
            "/api/v1/memories/bulk", json={"items": [{"content": "row one"}, {"content": "row two"}]}, headers=hdr
        )
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "atomic" and r.json()["stored"] == 2
        await c.settle_all()
        assert c.llm.calls == []
        assert (await c.ledger(uid))["used"] == 500


async def test_conversation_ingest_is_metered_from_transcript_length(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("ingest@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)
        messages = [{"role": "user", "content": "m" * 9_000}, {"role": "assistant", "content": "ok, noted"}]
        transcript_chunks = estimate_chunks(["\n".join(m["content"] for m in messages)])
        assert transcript_chunks == 2

        seen: list[int] = []
        first: list[bool] = []

        async def observe(_kwargs: dict) -> None:
            if first:
                return
            first.append(True)
            cursor = await c.h.db.conn.execute("SELECT credits FROM cloud_credit_reservations WHERE user_id = ?", (uid,))
            seen.extend(row[0] for row in await cursor.fetchall())

        c.llm.on_call = observe
        r = await c.h.client.post("/api/v1/ingest/conversation", json={"messages": messages}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "full"
        await c.settle_all()
        assert seen == [transcript_chunks * RESERVE_CREDITS_PER_CHUNK]
        assert (await c.ledger(uid))["used"] == charge_for(transcript_chunks, len(c.llm.calls) * USD_PER_CALL)


async def test_conversation_ingest_degrades_to_raw_messages_when_out_of_credits(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("ingest-broke@example.com")
        await c.set_credits_used(uid, 500)
        messages = [{"role": "user", "content": "I moved to Ocho Rios"}, {"role": "assistant", "content": "Noted!"}]
        r = await c.h.client.post("/api/v1/ingest/conversation", json={"messages": messages}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers[ENRICH] == "degraded"
        assert r.json()["stats"]["facts_stored"] == 2  # both messages kept verbatim
        await c.settle_all()
        assert c.llm.calls == []


async def test_transfer_import_degrades_when_out_of_credits(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("import@example.com")
        await c.set_credits_used(uid, 495)  # 5 left, import needs 2 x 16
        data = "first imported paragraph\n\nsecond imported paragraph"
        r = await c.h.client.post("/api/v1/transfer/import", json={"format": "plaintext", "data": data}, headers=hdr)
        assert r.status_code == 200, r.text
        assert r.headers[ENRICH] == "degraded" and r.json()["imported"] == 2
        await c.settle_all()
        assert c.llm.calls == []


# ---------------------------------------------------------------------------
# Reservation + reconcile + refund
# ---------------------------------------------------------------------------


async def test_reservation_settles_from_actual_usage_after_background_work_and_refunds(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("settle@example.com")
        r = await c.h.client.post("/api/v1/memories", json={"content": "Mani founded DolphyTech in Kingston."}, headers=hdr)
        assert r.status_code == 201, r.text
        # The response went out with 16 held; the entity pass may still be running.
        assert int(r.headers[REMAINING]) == 500 - RESERVE_CREDITS_PER_CHUNK
        await c.settle_all()

        calls = len(c.llm.calls)
        assert calls >= 2  # extraction in the request + entity pass in the background
        actual = calls * USD_PER_CALL
        cursor = await c.h.db.conn.execute(
            "SELECT credits, charged, actual_usd, status FROM cloud_credit_reservations WHERE user_id = ?", (uid,)
        )
        (reserved, charged, actual_usd, state) = await cursor.fetchone()
        assert state == "settled"
        assert actual_usd == pytest.approx(actual)  # includes the background entity call
        assert charged == charge_for(1, actual)
        ledger = await c.ledger(uid)
        assert ledger == {"used": charged, "reserved": 0, "remaining": 500 - charged, "usd": pytest.approx(actual)}
        assert reserved - charged > 0  # the unused part of the hold was refunded


async def test_failed_store_charges_only_real_spend(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("fails@example.com")

        async def boom(*_a, **_k):
            raise RuntimeError("disk full")

        c.service.store = boom  # type: ignore[method-assign]
        r = await c.h.client.post("/api/v1/memories", json={"content": "will fail"}, headers=hdr)
        assert r.status_code == 500
        await c.settle_all()
        assert await c.ledger(uid) == {"used": 0, "reserved": 0, "remaining": 500, "usd": 0}


async def test_stale_reservation_expires_and_late_settle_charges_difference(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("stale@example.com")
        account = await c.meter.get_account(uid)
        rid = await c.meter.reserve_credits(account, 32, min_credits=2)
        assert rid is not None
        assert (await c.ledger(uid))["reserved"] == 32

        _move_clock(monkeypatch, datetime.now(UTC) + timedelta(minutes=30))
        assert await c.meter.expire_stale_reservations() == 1
        assert (await c.ledger(uid))["used"] == 2 and (await c.ledger(uid))["reserved"] == 0

        # The work finished after all: $0.02 = 8 credits; 2 already charged.
        assert await c.meter.settle_reservation(rid, 0.02) == 8
        ledger = await c.ledger(uid)
        assert ledger["used"] == 8 and ledger["reserved"] == 0
        assert await c.meter.settle_reservation(rid, 0.02) == 0  # idempotent


async def test_reservation_is_a_hard_ceiling_under_concurrency(tmp_path) -> None:
    import asyncio

    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        uid, _ = await c.account("race@example.com", verified=False)  # 25 credits until verified
        account = await c.meter.get_account(uid)
        assert account.credit_limit == 25
        results = await asyncio.gather(*(c.meter.reserve_credits(account, 16, min_credits=1) for _ in range(5)))
        assert sum(1 for r in results if r) == 1  # only one 16-credit hold fits in 25
        assert (await c.ledger(uid))["reserved"] == 16


# ---------------------------------------------------------------------------
# Email verification gate
# ---------------------------------------------------------------------------


async def test_unverified_free_account_is_held_at_25_credits(tmp_path) -> None:
    async with cost_app(tmp_path, unverified_credit_cap_effective_at=CAP_ON) as c:
        uid, hdr = await c.account("new@example.com", verified=False)
        assert (await c.meter.get_account(uid)).credit_limit == 25
        r = await c.h.client.post("/api/v1/memories", json={"content": "Mani lives in Kingston"}, headers=hdr)
        assert r.status_code == 201 and r.headers[ENRICH] == "full"
        await c.settle_all()

        await c.set_credits_used(uid, 10)  # 15 left: a 16-credit hold no longer fits
        r = await c.h.client.post("/api/v1/memories", json={"content": "Suzan works at the clinic"}, headers=hdr)
        assert r.status_code == 201 and r.headers[ENRICH] == "degraded" and r.headers[REMAINING] == "15"
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert summary["credits"]["limit"] == 25 and summary["credits"]["unverified_cap_applied"] is True
        assert summary["email_verified"] is False

        await c.h.db.update_user_email_verified(uid, True)
        assert (await c.meter.get_account(uid)).credit_limit == 500
        r = await c.h.client.post("/api/v1/memories", json={"content": "Verified users get the full allowance"}, headers=hdr)
        assert r.headers[ENRICH] == "full" and r.headers[REMAINING] == str(500 - 10 - RESERVE_CREDITS_PER_CHUNK)
        await c.settle_all()
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert summary["credits"]["limit"] == 500 and summary["credits"]["unverified_cap_applied"] is False


# ---------------------------------------------------------------------------
# Annual plans: yearly credit bank
# ---------------------------------------------------------------------------


async def test_annual_plan_banks_the_whole_year_up_front(tmp_path, monkeypatch) -> None:
    anchor = datetime(2026, 3, 15, tzinfo=UTC)
    _move_clock(monkeypatch, datetime(2026, 4, 2, tzinfo=UTC))
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("annual@example.com", plan=PlanTier.SOLO, interval=BillingInterval.YEAR, period_anchor=anchor)
        account = await c.meter.get_account(uid)
        assert account.credit_limit == 26_400
        assert account.period.key == "Y:2026-03-15"
        assert account.period.end == datetime(2027, 3, 15, tzinfo=UTC)

        # Spend 25,000 credits in April: far past a month's 2,200, still enriched.
        await c.set_credits_used(uid, 25_000)
        r = await c.h.client.post("/api/v1/memories", json={"content": "annual still enriches"}, headers=hdr)
        assert r.headers[ENRICH] == "full"
        await c.settle_all()

        # Months later, same subscription year: the bank does not reset monthly...
        _move_clock(monkeypatch, datetime(2026, 11, 20, tzinfo=UTC))
        used = (await c.ledger(uid))["used"]
        assert used > 25_000
        # ...and is a hard ceiling once spent.
        await c.set_credits_used(uid, 26_400)
        r = await c.h.client.post("/api/v1/memories", json={"content": "annual bank empty"}, headers=hdr)
        assert r.headers[ENRICH] == "degraded"
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=hdr)).json()
        assert summary["credits"]["bank"] == "yearly" and summary["period"]["type"] == "year"
        assert summary["enrichment"] == {"status": "degraded", "reason": "credits_exhausted"}

        # The next subscription year starts a fresh bank.
        _move_clock(monkeypatch, datetime(2027, 3, 16, tzinfo=UTC))
        account = await c.meter.get_account(uid)
        assert account.period.key == "Y:2027-03-15"
        assert (await c.ledger(uid))["used"] == 0


async def test_monthly_plan_resets_each_calendar_month(tmp_path, monkeypatch) -> None:
    _move_clock(monkeypatch, datetime(2026, 9, 25, tzinfo=UTC))
    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("monthly@example.com", plan=PlanTier.PRO, interval=BillingInterval.MONTH)
        assert (await c.meter.get_account(uid)).credit_limit == 5_000
        await c.set_credits_used(uid, 5_000)
        assert (await c.ledger(uid))["remaining"] == 0
        _move_clock(monkeypatch, datetime(2026, 10, 1, tzinfo=UTC))
        assert (await c.ledger(uid))["remaining"] == 5_000


# ---------------------------------------------------------------------------
# Global free-tier circuit breaker
# ---------------------------------------------------------------------------


async def test_free_breaker_trips_degrades_only_free_and_recovers_next_month(tmp_path, monkeypatch) -> None:
    _move_clock(monkeypatch, datetime(2026, 9, 10, tzinfo=UTC))
    async with cost_app(tmp_path) as c:
        free_uid, free_hdr = await c.account("free@example.com")
        _, solo_hdr = await c.account("solo@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)

        assert await c.meter.free_breaker_budget() == 50.0  # no revenue known -> $50 floor
        await c.meter._add_ai_spend(metering.now_utc(), "free", 50.0)
        await c.h.db.conn.commit()
        assert await c.meter.free_breaker_open()

        r = await c.h.client.post("/api/v1/memories", json={"content": "free user store"}, headers=free_hdr)
        assert r.status_code == 201 and r.headers[ENRICH] == "degraded"
        r = await c.h.client.post("/api/v1/memories", json={"content": "paid user store"}, headers=solo_hdr)
        assert r.headers[ENRICH] == "full"  # paid accounts are never paused by the free breaker
        # Relay and recall keep working for free users while the breaker is open.
        r = await c.h.client.post("/api/v1/memories", json={"content": "handoff", "memory_type": "handoff"}, headers=free_hdr)
        assert r.status_code == 201 and r.headers[ENRICH] == "atomic"
        summary = (await c.h.client.get("/api/v1/cloud/usage/summary", headers=free_hdr)).json()
        assert summary["enrichment"] == {"status": "degraded", "reason": "free_breaker_open"}
        await c.settle_all()

        _move_clock(monkeypatch, datetime(2026, 10, 1, 0, 5, tzinfo=UTC))
        assert not await c.meter.free_breaker_open()
        r = await c.h.client.post("/api/v1/memories", json={"content": "free user next month"}, headers=free_hdr)
        assert r.headers[ENRICH] == "full"
        await c.settle_all()
        # Free-group spend is tracked for the new month.
        assert await c.meter.ai_spend_month("free") > 0
        assert free_uid


async def test_free_breaker_budget_follows_last_months_revenue(tmp_path, monkeypatch) -> None:
    _move_clock(monkeypatch, datetime(2026, 9, 10, tzinfo=UTC))
    async with cost_app(tmp_path) as c:
        await c.meter.record_revenue("txn_1", 900.0, at=datetime(2026, 8, 3, tzinfo=UTC))
        await c.meter.record_revenue("txn_1", 900.0, at=datetime(2026, 8, 3, tzinfo=UTC))  # webhook retry
        await c.meter.record_revenue("txn_2", 100.0, at=datetime(2026, 8, 20, tzinfo=UTC))
        assert await c.meter.free_breaker_budget() == pytest.approx(200.0)  # 20% of $1,000
        await c.meter._add_ai_spend(metering.now_utc(), "free", 120.0)
        assert not await c.meter.free_breaker_open()
        await c.meter._add_ai_spend(metering.now_utc(), "free", 80.0)
        assert await c.meter.free_breaker_open()


async def test_free_breaker_counts_open_free_reservations(tmp_path, monkeypatch) -> None:
    async with cost_app(tmp_path, free_breaker_min_usd=0.05) as c:
        uid, _ = await c.account("hold@example.com")
        account = await c.meter.get_account(uid)
        assert await c.meter.reserve_credits(account, 32, min_credits=2)  # $0.08 held
        assert await c.meter.free_breaker_open()


async def test_free_breaker_can_be_disabled(tmp_path) -> None:
    async with cost_app(tmp_path, free_breaker_enabled=False) as c:
        await c.meter._add_ai_spend(metering.now_utc(), "free", 10_000.0)
        assert not await c.meter.free_breaker_open()


# ---------------------------------------------------------------------------
# Dollar metering from response.usage
# ---------------------------------------------------------------------------


def test_usage_is_priced_per_model_with_cached_tokens_and_a_safe_fallback() -> None:
    from types import SimpleNamespace

    from remembra.cloud.model_prices import usd_for

    usage = SimpleNamespace(
        prompt_tokens=1_000_000, completion_tokens=100_000, prompt_tokens_details=SimpleNamespace(cached_tokens=400_000)
    )
    # 600K fresh x $0.15 + 400K cached x $0.075 + 100K out x $0.60 (per 1M)
    assert usd_for(usage, "gpt-4o-mini-2024-07-18") == pytest.approx(0.09 + 0.03 + 0.06)
    assert usd_for({"prompt_tokens": 1_000_000, "completion_tokens": 0}, "gpt-4.1-nano") == pytest.approx(0.10)
    # Unknown models are metered at the most expensive known price, never for free.
    assert usd_for({"prompt_tokens": 1_000_000, "completion_tokens": 0}, "some-new-model") == pytest.approx(2.50)
    assert usd_for(None, "gpt-4o-mini") == 0.0


async def test_update_is_gated_like_a_store(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("update@example.com")
        r = await c.h.client.post(
            "/api/v1/memories", json={"content": "Mani drives a Tacoma", "skip_extraction": True}, headers=hdr
        )
        memory_id = r.json()["id"]
        await c.settle_all()
        assert c.llm.calls == []  # skip_extraction: atomic, no entity pass

        await c.set_credits_used(uid, 500)
        r = await c.h.client.patch(f"/api/v1/memories/{memory_id}", json={"content": "Mani drives a Hilux now"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert r.headers[ENRICH] == "degraded"
        await c.settle_all()
        assert c.llm.calls == []  # no re-extraction, no entity re-resolution

        await c.set_credits_used(uid, 0)
        r = await c.h.client.patch(
            f"/api/v1/memories/{memory_id}", json={"content": "Mani drives a Hilux in Kingston"}, headers=hdr
        )
        assert r.headers[ENRICH] == "full"
        await c.settle_all()
        assert c.llm.calls  # enriched again once credits are back
        assert (await c.ledger(uid))["used"] >= 1


async def test_sleep_time_llm_work_is_billed_and_skipped_when_out_of_credits(tmp_path) -> None:
    from remembra.extraction.consolidator import ExistingMemory
    from remembra.services.sleep_time import SleepTimeWorker

    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("sleep@example.com")
        await c.h.client.post("/api/v1/memories", json={"content": "atomic note", "skip_extraction": True}, headers=hdr)
        worker = SleepTimeWorker(settings=c.h.settings, memory_service=c.service, usage_meter=c.meter)

        async def dedup(user_id: str, memories: list) -> int:
            # One real consolidator decision (LLM call through the metered client).
            await worker.consolidator.consolidate("atomic note", [ExistingMemory(id="x", content="atomic note!", score=0.99)])
            return 0

        worker._dedup_pass = dedup  # type: ignore[method-assign]
        await worker._consolidate_user(uid)
        await c.settle_all()
        assert len(c.llm.calls) == 1
        assert (await c.ledger(uid))["used"] == credits_for_usd(USD_PER_CALL)

        await c.set_credits_used(uid, 500)
        await worker._consolidate_user(uid)
        assert len(c.llm.calls) == 1  # skipped: no credits left


async def test_relay_session_close_is_a_free_relay_event(tmp_path) -> None:
    """POST /session/close stores a handoff with zero LLM calls and zero
    credits, and counts as a relay event (never a store)."""
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("closer@example.com")
        r = await c.h.client.post(
            "/api/v1/session/close",
            json={
                "agent_id": "claude-code",
                "session_id": "sess-cost-1",
                "project_id": "invoices",
                "facts": {
                    "branch": "feat/gct",
                    "head_commit": "a41f2c9",
                    "commits": [{"sha": "a41f2c9", "subject": "feat: gct rate table"}],
                    "files_changed": ["src/tax.py"],
                    "tests": [{"cmd": "pytest -q", "passed": False, "summary": "1 failed"}],
                    "todos_open": ["credit-note rounding"],
                },
            },
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        await c.settle_all()

        assert c.llm.calls == []
        month = await c.meter.get_monthly_usage(uid)
        assert month["relay_events"] >= 1
        assert month["credits_used"] == 0
        assert (await c.ledger(uid))["used"] == 0
