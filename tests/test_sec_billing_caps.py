"""Security sweep 2026-09-26, billing: every write path pays the plan's limits.

* BILL-7: ``POST /memories/{id}/supersede`` goes through the plan gate (memory
  cap, per-store length, Free daily unenriched cap, embedding spend) and counts
  as a store.
* BILL-8: a ``/session/close`` that stores a new or changed handoff counts
  toward the Free daily unenriched-write cap and the embedding spend; an
  unchanged re-close does not.
* BILL-9: a conversation ingest that degrades to raw storage is gated for
  every message it stores (daily unenriched cap, memory cap).
* BILL-11: restoring an archived memory passes the memory cap (and counts as
  an unenriched, re-embedded write).
* BILL-10: the memory cap holds under concurrent writes: the slots a write
  may add are reserved atomically at the gate, until its rows land.

Every test drives the production routes over SQLite with the real gate and
UsageMeter (see ``tests/_cost_harness.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests._cost_harness import cost_app

NOTICE_PASSED = datetime(2026, 1, 1, tzinfo=UTC)  # Free memory cap = 10,000


async def _fill(c: Any, uid: str, n: int, *, prefix: str = "m") -> None:
    """``n`` plain memories for ``uid`` written straight to the table (counted toward the cap)."""
    now = datetime.now(UTC).isoformat()
    await c.h.db.conn.executemany(
        "INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at) VALUES (?, ?, 'default', ?, ?, ?)",
        [(f"{prefix}{i}", uid, f"note {i}", now, now) for i in range(n)],
    )
    await c.h.db.conn.commit()


async def _unenriched_today(c: Any, uid: str) -> int:
    cursor = await c.h.db.conn.execute(
        "SELECT COALESCE(unenriched_writes, 0) FROM cloud_usage_daily WHERE user_id = ? AND date = ?",
        (uid, datetime.now(UTC).strftime("%Y-%m-%d")),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _stores_today(c: Any, uid: str) -> int:
    cursor = await c.h.db.conn.execute(
        "SELECT COALESCE(stores, 0) FROM cloud_usage_daily WHERE user_id = ? AND date = ?",
        (uid, datetime.now(UTC).strftime("%Y-%m-%d")),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _count(c: Any, uid: str) -> int:
    cursor = await c.h.db.conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (uid,))
    return int((await cursor.fetchone())[0])


# ---------------------------------------------------------------------------
# BILL-7: supersede
# ---------------------------------------------------------------------------


async def test_supersede_at_the_memory_cap_is_429(tmp_path) -> None:
    async with cost_app(tmp_path, memory_cap_notice_effective_at=NOTICE_PASSED) as c:
        uid, hdr = await c.account("capped@example.com")
        await _fill(c, uid, 10_000)
        assert (await c.h.client.post("/api/v1/memories", json={"content": "one more"}, headers=hdr)).status_code == 429
        r = await c.h.client.post(
            "/api/v1/memories/m0/supersede", json={"new_content": "replacement", "reason": "newer"}, headers=hdr
        )
        assert r.status_code == 429, r.text
        assert "Memory limit reached" in r.json()["detail"]
        assert await _count(c, uid) == 10_000


async def test_supersede_over_the_free_store_length_is_413(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("long@example.com")
        await _fill(c, uid, 1)
        r = await c.h.client.post(
            "/api/v1/memories/m0/supersede", json={"new_content": "x" * 8_001, "reason": "newer"}, headers=hdr
        )
        assert r.status_code == 413, r.text
        assert await _count(c, uid) == 1
        # Past what any plan stores: rejected by the request model.
        r = await c.h.client.post(
            "/api/v1/memories/m0/supersede", json={"new_content": "x" * 50_001, "reason": "newer"}, headers=hdr
        )
        assert r.status_code == 422, r.text


async def test_supersede_counts_as_an_unenriched_store_with_embedding_spend(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("counted@example.com")
        await _fill(c, uid, 1)
        spend_before = await c.meter.ai_spend_month("free")
        r = await c.h.client.post(
            "/api/v1/memories/m0/supersede",
            json={"new_content": "Sam moved the office to Porto", "reason": "moved"},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        await c.settle_all()
        assert c.llm.calls == []  # stored atomically, as before
        assert await _count(c, uid) == 2
        assert await _unenriched_today(c, uid) == 1
        assert await _stores_today(c, uid) == 1
        assert await c.meter.ai_spend_month("free") > spend_before


async def test_supersede_past_the_free_daily_unenriched_cap_is_429(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("daily@example.com")
        await _fill(c, uid, 1)
        account = await c.meter.get_account(uid)
        assert await c.meter.take_unenriched_writes(account, 300)
        r = await c.h.client.post(
            "/api/v1/memories/m0/supersede", json={"new_content": "replacement", "reason": "newer"}, headers=hdr
        )
        assert r.status_code == 429, r.text
        assert "Daily limit reached" in r.json()["detail"]
        assert await _count(c, uid) == 1


# ---------------------------------------------------------------------------
# BILL-8: session close
# ---------------------------------------------------------------------------


def _close(session_id: str, todo: str = "credit-note rounding") -> dict[str, Any]:
    return {
        "agent_id": "claude-code",
        "session_id": session_id,
        "project_id": "invoices",
        "facts": {"branch": "feat/gct", "files_changed": ["src/tax.py"], "todos_open": [todo]},
    }


async def test_session_close_counts_toward_the_free_daily_unenriched_cap(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("closer-cap@example.com")
        spend_before = await c.meter.ai_spend_month("free")
        r = await c.h.client.post("/api/v1/session/close", json=_close("sess-1"), headers=hdr)
        assert r.status_code == 200 and r.json()["changed"] is True, r.text
        assert await _unenriched_today(c, uid) == 1
        assert await c.meter.ai_spend_month("free") > spend_before

        # Re-closing the same session with the same facts stores nothing and is not counted.
        r = await c.h.client.post("/api/v1/session/close", json=_close("sess-1"), headers=hdr)
        assert r.status_code == 200 and r.json()["changed"] is False, r.text
        assert await _unenriched_today(c, uid) == 1

        # Up to the cap: 300 unenriched writes today.
        account = await c.meter.get_account(uid)
        assert await c.meter.take_unenriched_writes(account, 299)
        r = await c.h.client.post("/api/v1/session/close", json=_close("sess-new"), headers=hdr)
        assert r.status_code == 429, r.text
        assert "Daily limit reached" in r.json()["detail"]
        assert await _unenriched_today(c, uid) == 300
        cursor = await c.h.db.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ? AND memory_type = 'handoff' AND superseded_by IS NULL", (uid,)
        )
        assert (await cursor.fetchone())[0] == 1  # nothing stored past the cap


async def test_a_paid_plan_close_is_not_capped(tmp_path) -> None:
    from remembra.cloud.plans import BillingInterval, PlanTier

    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("closer-solo@example.com", plan=PlanTier.SOLO, interval=BillingInterval.MONTH)
        for i in range(3):
            r = await c.h.client.post("/api/v1/session/close", json=_close(f"sess-{i}"), headers=hdr)
            assert r.status_code == 200, r.text
        assert await _unenriched_today(c, uid) == 0  # no daily cap on paid plans


# ---------------------------------------------------------------------------
# BILL-9: degraded conversation ingest
# ---------------------------------------------------------------------------


def _messages(n: int) -> list[dict[str, str]]:
    roles = ("user", "assistant")
    return [{"role": roles[i % 2], "content": f"message {i} about the Ocho Rios office"} for i in range(n)]


async def test_a_degraded_ingest_counts_every_stored_message(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("ingest-200@example.com")
        await c.set_credits_used(uid, 500)  # out of credits: the ingest degrades to raw messages
        r = await c.h.client.post("/api/v1/ingest/conversation", json={"messages": _messages(200)}, headers=hdr)
        assert r.status_code == 201, r.text
        assert r.headers["X-Remembra-Enrichment"] == "degraded"
        assert r.json()["stats"]["facts_stored"] == 200
        await c.settle_all()
        assert c.llm.calls == []
        assert await _count(c, uid) == 200
        assert await _unenriched_today(c, uid) == 200


async def test_a_degraded_ingest_past_the_memory_cap_is_429(tmp_path) -> None:
    async with cost_app(tmp_path, memory_cap_notice_effective_at=NOTICE_PASSED) as c:
        uid, hdr = await c.account("ingest-cap@example.com")
        await _fill(c, uid, 9_900)
        await c.set_credits_used(uid, 500)
        r = await c.h.client.post("/api/v1/ingest/conversation", json={"messages": _messages(200)}, headers=hdr)
        assert r.status_code == 429, r.text
        assert "Memory limit reached" in r.json()["detail"]
        assert await _count(c, uid) == 9_900
        assert await _unenriched_today(c, uid) == 0


async def test_a_degraded_ingest_past_the_daily_cap_is_429(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        uid, hdr = await c.account("ingest-daily@example.com")
        await c.set_credits_used(uid, 500)
        account = await c.meter.get_account(uid)
        assert await c.meter.take_unenriched_writes(account, 150)
        r = await c.h.client.post("/api/v1/ingest/conversation", json={"messages": _messages(200)}, headers=hdr)
        assert r.status_code == 429, r.text
        assert await _count(c, uid) == 0
        assert await _unenriched_today(c, uid) == 150


# ---------------------------------------------------------------------------
# BILL-11: restoring an archived memory
# ---------------------------------------------------------------------------


async def _archived(c: Any, uid: str, memory_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    await c.h.db.conn.execute(
        "INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at) VALUES (?, ?, 'default', ?, ?, ?)",
        (memory_id, uid, "Sam's old Lisbon office number", now, now),
    )
    await c.h.db.conn.commit()
    assert await c.h.db.archive_memory(memory_id, reason="decay_threshold")


def _mount_temporal(c: Any) -> None:
    from remembra.api.v1 import temporal

    c.h.app.include_router(temporal.router, prefix="/api/v1")


async def test_restoring_an_archived_memory_at_the_cap_is_429_and_it_stays_archived(tmp_path) -> None:
    async with cost_app(tmp_path, memory_cap_notice_effective_at=NOTICE_PASSED) as c:
        _mount_temporal(c)
        uid, hdr = await c.account("restore-cap@example.com")
        await _archived(c, uid, "arch-1")
        await _fill(c, uid, 10_000)
        r = await c.h.client.post("/api/v1/temporal/archive/arch-1/restore", headers=hdr)
        assert r.status_code == 429, r.text
        assert "Memory limit reached" in r.json()["detail"]
        assert await c.h.db.get_archived_memory("arch-1") is not None
        assert await c.h.db.get_memory("arch-1") is None
        assert await _count(c, uid) == 10_000


async def test_a_restore_under_the_cap_counts_as_an_unenriched_write(tmp_path) -> None:
    async with cost_app(tmp_path) as c:
        _mount_temporal(c)
        uid, hdr = await c.account("restore-ok@example.com")
        await _archived(c, uid, "arch-2")
        spend_before = await c.meter.ai_spend_month("free")
        r = await c.h.client.post("/api/v1/temporal/archive/arch-2/restore", headers=hdr)
        assert r.status_code == 200, r.text
        assert await c.h.db.get_memory("arch-2") is not None
        assert await _unenriched_today(c, uid) == 1
        assert await c.meter.ai_spend_month("free") > spend_before


# ---------------------------------------------------------------------------
# BILL-10: concurrent writes at the memory cap
# ---------------------------------------------------------------------------


async def test_concurrent_batches_at_the_cap_never_overshoot_it(tmp_path) -> None:
    import asyncio

    async with cost_app(tmp_path, memory_cap_notice_effective_at=NOTICE_PASSED) as c:
        uid, hdr = await c.account("racer@example.com")
        await _fill(c, uid, 9_990)

        async def batch(n: int) -> int:
            items = [{"content": f"racer {n} item {i}"} for i in range(10)]
            r = await c.h.client.post("/api/v1/memories/batch", json={"items": items, "skip_extraction": True}, headers=hdr)
            return r.status_code

        codes = await asyncio.gather(*(batch(n) for n in range(10)))
        assert sorted(codes) == [201] + [429] * 9, codes
        assert await _count(c, uid) == 10_000
        # Once those rows landed the cap is exact: nothing more fits, and a delete frees a slot again.
        r = await c.h.client.post("/api/v1/memories", json={"content": "over", "skip_extraction": True}, headers=hdr)
        assert r.status_code == 429
        await c.h.db.conn.execute("DELETE FROM memories WHERE id = 'm0'")
        await c.h.db.conn.commit()
        r = await c.h.client.post("/api/v1/memories", json={"content": "fits again", "skip_extraction": True}, headers=hdr)
        assert r.status_code == 201, r.text


async def test_reserved_slots_count_until_the_write_finishes_or_expires(tmp_path, monkeypatch) -> None:
    from remembra.cloud import metering

    async with cost_app(tmp_path) as c:
        uid, _ = await c.account("slots@example.com")
        account = await c.meter.get_account(uid)
        await _fill(c, uid, 90)
        # A write in flight holds 10 slots on top of the 90 stored: 5 more do not fit.
        fits, in_use, first = await c.meter.reserve_memory_slots(account, 10, cap=100)
        assert (fits, in_use) == (True, 90) and first is not None
        assert (await c.meter.reserve_memory_slots(account, 5, cap=100))[:2] == (False, 100)
        # A check without a hold (relay rows) reserves nothing.
        assert await c.meter.reserve_memory_slots(account, 0, cap=100) == (True, 100, None)
        # It stores 3 rows and finishes: its unused slots are free.
        await _fill(c, uid, 3, prefix="landed")
        await c.meter.release_memory_slots(first)
        fits, in_use, second = await c.meter.reserve_memory_slots(account, 7, cap=100)
        assert (fits, in_use) == (True, 93) and second is not None
        # A write that died without finishing is forgotten after the time-to-live.
        later = datetime.now(UTC) + metering.MEMORY_SLOT_HOLD_TTL + metering.timedelta(seconds=1)
        monkeypatch.setattr(metering, "now_utc", lambda: later)
        assert (await c.meter.reserve_memory_slots(account, 7, cap=100))[:2] == (True, 93)


async def test_slots_a_write_did_not_use_are_given_back(tmp_path) -> None:
    async with cost_app(tmp_path, memory_cap_notice_effective_at=NOTICE_PASSED) as c:
        uid, _ = await c.account("unused@example.com")
        await _fill(c, uid, 9_998)
        key, _ = await c.h.api_key(uid, "editor", project_ids=["default"])
        hdr = {"X-API-Key": key}
        # Two slots reserved; the item for a project the key may not write fails, one row lands.
        items = [{"content": "kept"}, {"content": "refused", "project_id": "not-mine"}]
        r = await c.h.client.post("/api/v1/memories/batch", json={"items": items, "skip_extraction": True}, headers=hdr)
        assert r.status_code == 201, r.text
        assert [item["success"] for item in r.json()["results"]] == [True, False]
        assert await _count(c, uid) == 9_999
        # The unused slot is free again at once (not only after the reservation expires).
        r = await c.h.client.post("/api/v1/memories", json={"content": "the last one", "skip_extraction": True}, headers=hdr)
        assert r.status_code == 201, r.text
        r = await c.h.client.post("/api/v1/memories", json={"content": "one too many", "skip_extraction": True}, headers=hdr)
        assert r.status_code == 429, r.text
