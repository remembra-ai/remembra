"""REL-4 / REL-5 / REL-10 / REL-16 / REL-17 / ING-19 wiring + fallback-extraction
reprocessing, through the real store/recall/worker paths."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from remembra.core.metrics import RECALL_DEGRADED
from remembra.core.tasks import get_task_registry
from remembra.extraction import background
from remembra.extraction.extractor import ExtractionOutcome
from remembra.models.memory import RecallRequest, StoreRequest
from remembra.services.reprocess import find_fallback_facts, reprocess_fallback
from remembra.storage.pending_embeddings import PendingEmbeddingWorker
from tests._ret_harness import USER, make_stack, quota_error, unit, with_cosine

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def stack(tmp_path):
    s = await make_stack(tmp_path)
    yield s
    await background.drain(timeout=5.0)
    await s.close()


class ScriptedExtractor:
    """Stands in for FactExtractor.extract_detailed (the LLM edge)."""

    def __init__(self, outcome: ExtractionOutcome | None = None, delay: float = 0.0) -> None:
        self.outcome = outcome
        self.delay = delay
        self.calls = 0

    async def extract_detailed(self, content: str, reference_date: Any = None) -> ExtractionOutcome:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.outcome is None:
            raise AssertionError("extractor should not have been called")
        return self.outcome


async def _all_rows(db) -> list[dict[str, Any]]:
    cursor = await db.conn.execute("SELECT * FROM memories ORDER BY created_at")
    return [dict(r) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Required regression (5): quota error on store -> pending, then the worker embeds
# ---------------------------------------------------------------------------


async def test_regression_quota_on_store_is_pending_then_worker_embeds(stack) -> None:
    stack.emb.fail = quota_error()

    resp = await stack.service.store(
        StoreRequest(content="Mani moved the POS backend to Supabase", user_id=USER, project_id="p"),
        skip_extraction=True,
    )

    assert resp.status == "pending"
    assert resp.enrichment == "pending"
    mid = resp.id
    row = await stack.row(mid)
    assert row["content"] == "Mani moved the POS backend to Supabase"
    assert (await stack.db.search_fts("Supabase", USER, "p"))[0][0] == mid  # keyword-searchable now
    queued = await stack.service.pending_queue.get(mid)
    assert queued is not None and queued.reason == "embedding_quota_exhausted"
    assert await stack.qdrant.existing_ids([mid]) == set()

    # Provider still down: the worker defers without burning attempts.
    worker = PendingEmbeddingWorker(stack.service.pending_queue, stack.db, stack.qdrant, stack.emb)
    out = await worker.run_once()
    assert out["deferred"] == 1 and await stack.qdrant.existing_ids([mid]) == set()

    # Provider recovers: the worker embeds, upserts the full payload, clears the queue.
    stack.emb.fail = None
    await stack.db.conn.execute("UPDATE pending_embeddings SET next_attempt_at = '2000-01-01T00:00:00'")
    await stack.db.conn.commit()
    out = await worker.run_once()
    assert out["done"] == 1
    assert await stack.qdrant.existing_ids([mid]) == {mid}
    assert await stack.service.pending_queue.get(mid) is None

    stack.emb.vectors["where is the POS backend"] = await stack.emb.embed("Mani moved the POS backend to Supabase")
    recalled = await stack.service.recall(
        RecallRequest(query="where is the POS backend", user_id=USER, project_id="p", enable_hybrid=False)
    )
    assert [m.id for m in recalled.memories] == [mid]
    assert "semantic" in recalled.memories[0].match_sources


async def test_store_with_pending_disabled_still_fails_loudly_and_leaves_nothing(tmp_path) -> None:
    s = await make_stack(tmp_path, store_pending_on_embedding_failure=False)
    try:
        s.emb.fail = quota_error()
        with pytest.raises(Exception) as exc:
            await s.service.store(StoreRequest(content="x fact", user_id=USER, project_id="p"), skip_extraction=True)
        assert "quota" in str(exc.value)
        assert await _all_rows(s.db) == []
    finally:
        await s.close()


async def test_vector_store_failure_after_row_write_is_pending_not_lost(stack) -> None:
    async def broken_upsert(memory: Any) -> None:
        raise ConnectionError("qdrant down")

    stack.qdrant.upsert = broken_upsert  # type: ignore[method-assign]
    resp = await stack.service.store(
        StoreRequest(content="Litestream replica is on R2", user_id=USER, project_id="p"), skip_extraction=True
    )
    assert resp.status == "pending"
    assert (await stack.service.pending_queue.get(resp.id)).reason == "vector_store_failed"  # type: ignore[union-attr]
    assert (await stack.row(resp.id))["content"] == "Litestream replica is on R2"


async def test_sqlite_is_written_before_qdrant(stack) -> None:
    """ING-19 / REL-10: a failed row write leaves no vector behind."""
    order: list[str] = []
    real_upsert = stack.qdrant.upsert

    async def track_upsert(memory: Any) -> None:
        order.append("qdrant")
        await real_upsert(memory)

    real_save = stack.db.save_memory_metadata

    async def track_save(**kw: Any) -> None:
        order.append("sqlite")
        await real_save(**kw)

    stack.qdrant.upsert = track_upsert  # type: ignore[method-assign]
    stack.db.save_memory_metadata = track_save  # type: ignore[method-assign]
    await stack.seed("ordering probe")
    assert order == ["sqlite", "qdrant"]

    async def failing_save(**kw: Any) -> None:
        raise RuntimeError("disk full")

    stack.db.save_memory_metadata = failing_save  # type: ignore[method-assign]
    before = len(order)
    with pytest.raises(RuntimeError):
        await stack.seed("never stored")
    assert order[before:] == []  # Qdrant never touched


async def test_hard_failure_rolls_back_source_row_facts_vectors_and_supersessions(tmp_path) -> None:
    s = await make_stack(tmp_path, smart_extraction_enabled=True, consolidation_threshold=0.1)
    try:
        s.emb.vectors["Mani uses Stripe for billing"] = unit(1)
        target = await s.seed("Mani uses Stripe for billing", vector=unit(1))
        s.emb.vectors["Mani switched billing to Paddle"] = with_cosine(0.9)
        s.emb.vectors["Paddle payouts go to NCB"] = unit(0, 0, 1)

        s.service.extractor = ScriptedExtractor(  # type: ignore[assignment]
            ExtractionOutcome(facts=["Mani switched billing to Paddle", "Paddle payouts go to NCB"], method="llm")
        )

        class Supersede:
            async def consolidate(self, fact: str, existing: list[Any]) -> Any:
                from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult

                return ConsolidationResult(
                    ConsolidationAction.SUPERSEDE, existing[0].id, reason="switched", confidence=0.95, decided_by="llm"
                )

        s.service.consolidator = Supersede()  # type: ignore[assignment]

        real_fts = s.db.index_memory_fts
        calls = {"n": 0}

        async def fts_fails_on_second_fact(*a: Any, **k: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # fact 1 indexes fine, fact 2 fails
                raise RuntimeError("fts corrupted")
            await real_fts(*a, **k)

        s.db.index_memory_fts = fts_fails_on_second_fact  # type: ignore[method-assign]
        before = {r["id"] for r in await _all_rows(s.db)}

        with pytest.raises(RuntimeError, match="fts corrupted"):
            await s.service.store(
                StoreRequest(content="Mani switched billing to Paddle. Paddle payouts go to NCB.", user_id=USER, project_id="p")
            )

        rows = await _all_rows(s.db)
        assert {r["id"] for r in rows} == before  # no orphan source row, no half-stored facts
        target_row = await s.row(target)
        assert target_row["superseded_by"] is None and target_row["valid_to"] is None
        points = await s.qdrant.search(unit(1), USER, "p", limit=50, score_threshold=0.0)
        assert {mid for mid, _, _ in points} == before
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# REL-17: store time budget degrades instead of failing
# ---------------------------------------------------------------------------


async def test_store_budget_exhausted_by_extraction_stores_verbatim_marked_for_reprocess(tmp_path) -> None:
    s = await make_stack(tmp_path, smart_extraction_enabled=True, store_time_budget_seconds=0.05)
    try:
        slow = ScriptedExtractor(ExtractionOutcome(facts=["never used"], method="llm"), delay=1.0)
        s.service.extractor = slow  # type: ignore[assignment]

        resp = await s.service.store(StoreRequest(content="Deploy window is Sunday 2am EST", user_id=USER, project_id="p"))

        assert resp.extraction == "fallback"
        assert resp.status == "pending"  # the budget was gone before embedding too
        row = await s.row(resp.id)
        meta = json.loads(row["metadata"])
        assert row["content"] == "Deploy window is Sunday 2am EST"
        assert meta["extraction"] == "fallback"
        assert meta["extraction_error_kind"] == "store_budget_exceeded"
        assert await s.service.pending_queue.get(resp.id) is not None
    finally:
        await s.close()


async def test_store_budget_exhausted_during_consolidation_adds_without_decision(tmp_path) -> None:
    s = await make_stack(tmp_path, store_time_budget_seconds=0.3, consolidation_threshold=0.1)
    try:
        s.emb.vectors["Office wifi password rotated"] = unit(1)
        s.emb.vectors["Office wifi password rotated again"] = with_cosine(0.95)
        await s.seed("Office wifi password rotated", vector=unit(1))

        class SlowConsolidator:
            async def consolidate(self, fact: str, existing: list[Any]) -> Any:
                await asyncio.sleep(2.0)
                raise AssertionError("should have been cut off")

        s.service.consolidator = SlowConsolidator()  # type: ignore[assignment]
        resp = await s.service.store(
            StoreRequest(content="Office wifi password rotated again", user_id=USER, project_id="p"),
            skip_extraction=False,
        )
        entry = next(c for c in resp.consolidation if c.fact == "Office wifi password rotated again")
        assert entry.action == "add" and entry.decided_by == "fallback"
        assert "budget" in (entry.reason or "")
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Required regression (6): recall during an outage -> keyword_only with content
# ---------------------------------------------------------------------------


async def test_regression_recall_during_outage_is_keyword_only_with_content(stack) -> None:
    mid = await stack.seed("Coolify redeploy needs the BUILD_SHA arg", metadata={"source": "ops"})
    await stack.seed("Unrelated grocery list")
    before = RECALL_DEGRADED.get(mode="keyword_only")
    stack.emb.fail = quota_error()

    resp = await stack.service.recall(RecallRequest(query="how do I redeploy on Coolify", user_id=USER, project_id="p"))

    assert resp.degraded == "keyword_only"
    assert [m.id for m in resp.memories] == [mid]
    hit = resp.memories[0]
    assert hit.content == "Coolify redeploy needs the BUILD_SHA arg"
    assert hit.metadata == {"source": "ops"}
    assert hit.match_sources == ["keyword"]
    assert "BUILD_SHA" in resp.context
    assert RECALL_DEGRADED.get(mode="keyword_only") == before + 1


async def test_keyword_arm_runs_even_when_semantic_finds_nothing(stack) -> None:
    stack.emb.vectors["BUILD_SHA"] = unit(1)
    mid = await stack.seed("Coolify redeploy needs the BUILD_SHA arg", vector=unit(0, 1))
    resp = await stack.service.recall(RecallRequest(query="BUILD_SHA", user_id=USER, project_id="p", threshold=0.99))
    assert resp.degraded is None
    assert [m.id for m in resp.memories] == [mid]


async def test_keyword_fallback_disabled_raises(tmp_path) -> None:
    s = await make_stack(tmp_path, recall_keyword_fallback=False)
    try:
        s.emb.fail = quota_error()
        with pytest.raises(Exception, match="quota"):
            await s.service.recall(RecallRequest(query="anything", user_id=USER, project_id="p"))
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# REL-7 marker persisted + reprocess job
# ---------------------------------------------------------------------------


async def test_extraction_fallback_is_persisted_and_reprocessed(tmp_path) -> None:
    s = await make_stack(tmp_path, smart_extraction_enabled=True)
    try:
        from remembra.core.llm_guard import mark_llm_fallback

        class QuotaExtractor:
            async def extract_detailed(self, content: str, reference_date: Any = None) -> ExtractionOutcome:
                mark_llm_fallback("extraction", "quota_exhausted")  # what the real extractor does
                return ExtractionOutcome(facts=["Mani prefers dark roast.", "He buys it in Kingston."], method="fallback")

        s.service.extractor = QuotaExtractor()  # type: ignore[assignment]
        resp = await s.service.store(
            StoreRequest(content="Mani prefers dark roast. He buys it in Kingston.", user_id=USER, project_id="p")
        )
        assert resp.extraction == "fallback" and resp.source_id
        facts = await find_fallback_facts(s.db)
        assert len(facts) == 2
        meta = json.loads(facts[0]["metadata"])
        assert meta["extraction_error_kind"] == "quota_exhausted" and meta["source_id"] == resp.source_id

        # Dry run: no model call, no writes.
        s.service.extractor = ScriptedExtractor(None)  # type: ignore[assignment]
        report = await reprocess_fallback(s.service)
        assert report.to_dict()["mode"] == "dry_run"
        assert (report.fallback_facts, report.groups, report.by_error_kind) == (2, 1, {"quota_exhausted": 2})
        assert len(await find_fallback_facts(s.db)) == 2

        # Apply with a recovered extractor: new facts stored, old ones superseded (kept).
        s.service.extractor = ScriptedExtractor(  # type: ignore[assignment]
            ExtractionOutcome(facts=["Mani prefers dark roast coffee", "Mani buys coffee in Kingston"], method="llm")
        )
        report = await reprocess_fallback(s.service, apply=True)
        assert (report.replaced_groups, report.new_memories, report.superseded) == (1, 2, 2)
        assert await find_fallback_facts(s.db) == []
        old_ids = [f["id"] for f in facts]
        for oid in old_ids:
            old = await s.row(oid)
            assert old["superseded_by"] and old["valid_to"]
        live = [r for r in await _all_rows(s.db) if r["superseded_by"] is None and r["memory_type"] != "source"]
        assert sorted(r["content"] for r in live) == ["Mani buys coffee in Kingston", "Mani prefers dark roast coffee"]
        assert all(json.loads(r["metadata"])["extraction"] == "reprocessed" for r in live)
    finally:
        await s.close()


async def test_reprocess_unchanged_facts_only_clears_marker_and_skips_while_still_failing(stack) -> None:
    mid = await stack.seed("Single sentence fact", metadata={"extraction": "fallback", "extraction_error_kind": "unavailable"})
    stack.service.extractor = ScriptedExtractor(ExtractionOutcome(facts=["x"], method="fallback"))  # type: ignore[assignment]
    report = await reprocess_fallback(stack.service, apply=True)
    assert report.still_failing == 1 and len(await find_fallback_facts(stack.db)) == 1

    stack.service.extractor = ScriptedExtractor(  # type: ignore[assignment]
        ExtractionOutcome(facts=["single sentence fact"], method="llm")
    )
    report = await reprocess_fallback(stack.service, apply=True)
    assert report.unchanged_cleared == 1
    row = await stack.row(mid)
    meta = json.loads(row["metadata"])
    assert meta["extraction"] == "reprocessed" and "extraction_error_kind" not in meta
    assert row["superseded_by"] is None


# ---------------------------------------------------------------------------
# REL-16: background work goes through the task registry
# ---------------------------------------------------------------------------


async def test_background_spawn_uses_task_registry() -> None:
    registry = get_task_registry()
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()

    task = background.spawn(work(), "ret_probe")
    await started.wait()
    assert "ret_probe" in registry.names()
    release.set()
    await task
    assert "ret_probe" not in registry.names()


# ---------------------------------------------------------------------------
# ING-19: bulk import writes SQLite + FTS first; provider/vector failures -> pending
# ---------------------------------------------------------------------------


async def test_bulk_import_is_sqlite_first_keyword_searchable_and_vectorised(stack) -> None:
    items = [StoreRequest(content=f"bulk fact number {i} about Negril", project_id="p") for i in range(3)]
    result = await stack.service.bulk_import(items, user_id=USER, project_id="p")
    assert (result["stored"], result["pending"], result["qdrant_count"]) == (3, 0, 3)
    ids = [r["id"] for r in await _all_rows(stack.db)]
    assert len(await stack.db.search_fts("Negril", USER, "p")) == 3
    assert await stack.qdrant.existing_ids(ids) == set(ids)


async def test_bulk_import_during_quota_outage_queues_everything(stack) -> None:
    stack.emb.fail = quota_error()
    items = [StoreRequest(content=f"outage fact {i}", project_id="p") for i in range(2)]
    result = await stack.service.bulk_import(items, user_id=USER, project_id="p")
    assert (result["stored"], result["pending"], result["qdrant_count"]) == (2, 2, 0)
    for row in await _all_rows(stack.db):
        assert (await stack.service.pending_queue.get(row["id"])).reason == "embedding_quota_exhausted"  # type: ignore[union-attr]


async def test_bulk_import_vector_store_failure_keeps_rows_and_queues(stack) -> None:
    async def broken(memories: Any) -> int:
        raise ConnectionError("qdrant down")

    stack.qdrant.upsert_batch = broken  # type: ignore[method-assign]
    result = await stack.service.bulk_import([StoreRequest(content="kept anyway")], user_id=USER, project_id="p")
    assert (result["stored"], result["pending"]) == (1, 1)
    [row] = await _all_rows(stack.db)
    assert (await stack.service.pending_queue.get(row["id"])).reason == "vector_store_failed"  # type: ignore[union-attr]
