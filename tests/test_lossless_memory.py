"""Tests for lossless memory: verbatim source records, receipts, and fact verification.

The provenance guarantee under test:
- when extraction derives facts, the exact original text is preserved as an
  immutable source record (SQLite + FTS only, no vector),
- every derived fact carries a metadata.source_id receipt,
- facts that don't lexically overlap their source are flagged verified=false,
- async_enrichment stores the verbatim source and returns before extraction.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from remembra.config import Settings
from remembra.models.memory import StoreRequest
from remembra.services.memory import MemoryService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDB:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []
        self.fts: list[dict[str, Any]] = []

    async def save_memory_metadata(self, **kwargs: Any) -> None:
        self.saved.append(kwargs)

    async def index_memory_fts(self, **kwargs: Any) -> None:
        self.fts.append(kwargs)


class FakeQdrant:
    def __init__(self) -> None:
        self.upserted: list[Any] = []

    async def upsert(self, memory: Any) -> None:
        self.upserted.append(memory)

    async def search(self, **kwargs: Any) -> list[Any]:
        return []  # no similar memories -> consolidator sees a clean slate


class FakeEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class FakeExtractor:
    def __init__(self, facts: list[str]) -> None:
        self.facts = facts
        self.calls: list[str] = []

    async def extract(self, content: str) -> list[str]:
        self.calls.append(content)
        return self.facts


class FakeConsolidator:
    async def consolidate(self, fact: str, existing: list[Any]) -> Any:
        from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult

        return ConsolidationResult(action=ConsolidationAction.ADD, content=fact, reason="test", target_id=None)


def make_service(extracted_facts: list[str], **settings_overrides: Any) -> tuple[MemoryService, FakeDB, FakeQdrant]:
    settings = Settings(
        openai_api_key="test",
        enable_entity_resolution=False,  # keep background entity work out of tests
        enable_hybrid_search=True,
        **settings_overrides,
    )
    db = FakeDB()
    qdrant = FakeQdrant()
    service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=FakeEmbeddings())
    service.extractor = FakeExtractor(extracted_facts)
    service.consolidator = FakeConsolidator()
    return service, db, qdrant


SOURCE_TEXT = (
    "Mani prefers TradingView Pine v6 strategies with margin_long=100 "
    "because the default 0 caused empty backtest reports on futures."
)


# ---------------------------------------------------------------------------
# Overlap heuristic
# ---------------------------------------------------------------------------


def test_overlap_full_for_faithful_fact() -> None:
    fact = "Mani prefers Pine v6 strategies with margin_long set to 100"
    assert MemoryService._fact_source_overlap(fact, SOURCE_TEXT) >= 0.8


def test_overlap_low_for_hallucinated_fact() -> None:
    fact = "The backend POST /api/listings/:id/promote is live and works, verified with curl"
    assert MemoryService._fact_source_overlap(fact, SOURCE_TEXT) < 0.5


def test_overlap_counts_numbers() -> None:
    # An invented figure must lower the score even among common words
    faithful = MemoryService._fact_source_overlap("margin_long 100 futures", SOURCE_TEXT)
    invented = MemoryService._fact_source_overlap("margin_long 500 futures", SOURCE_TEXT)
    assert faithful > invented


# ---------------------------------------------------------------------------
# Sync store: source record + receipts + verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_preserves_verbatim_source_with_receipts() -> None:
    facts = [
        "Mani prefers TradingView Pine v6 strategies with margin_long set to 100",
        "The default margin_long value of 0 caused empty backtest reports on futures",
    ]
    service, db, qdrant = make_service(facts)

    resp = await service.store(StoreRequest(content=SOURCE_TEXT, user_id="u1"))

    # A verbatim source record exists in SQLite with the EXACT original text
    source_rows = [r for r in db.saved if r.get("memory_type") == "source"]
    assert len(source_rows) == 1
    assert source_rows[0]["content"] == SOURCE_TEXT
    assert resp.source_id == source_rows[0]["memory_id"]

    # Source records carry no vector (must never pollute semantic recall)
    assert all(getattr(m, "memory_type", None) != "source" for m in qdrant.upserted)

    # ...but ARE keyword-searchable (FTS indexed)
    assert any(f["memory_id"] == resp.source_id for f in db.fts)

    # Every derived fact carries the receipt and a verification verdict
    fact_rows = [r for r in db.saved if r.get("memory_type") != "source"]
    assert len(fact_rows) == 2
    for row in fact_rows:
        assert row["metadata"]["source_id"] == resp.source_id
        assert row["metadata"]["verified"] is True


@pytest.mark.asyncio
async def test_hallucinated_fact_is_flagged_not_trusted() -> None:
    facts = [
        "Mani prefers TradingView Pine v6 strategies with margin_long set to 100",
        "The backend POST /api/listings/:id/promote is live and works, verified with curl",
    ]
    service, db, _ = make_service(facts)

    await service.store(StoreRequest(content=SOURCE_TEXT, user_id="u1"))

    fact_rows = [r for r in db.saved if r.get("memory_type") != "source"]
    verdicts = {row["content"]: row["metadata"]["verified"] for row in fact_rows}
    assert verdicts[facts[0]] is True
    assert verdicts[facts[1]] is False  # the hallucination is flagged


@pytest.mark.asyncio
async def test_no_source_record_when_content_passes_through() -> None:
    # Extraction returned the raw content unchanged -> nothing was derived,
    # so a separate source record would be a pure duplicate.
    service, db, _ = make_service([SOURCE_TEXT.strip()])

    resp = await service.store(StoreRequest(content=SOURCE_TEXT, user_id="u1"))

    assert resp.source_id is None
    assert all(r.get("memory_type") != "source" for r in db.saved)


@pytest.mark.asyncio
async def test_no_source_record_when_extraction_skipped() -> None:
    service, db, _ = make_service(["ignored"])

    resp = await service.store(StoreRequest(content=SOURCE_TEXT, user_id="u1"), skip_extraction=True)

    assert resp.source_id is None
    assert all(r.get("memory_type") != "source" for r in db.saved)


@pytest.mark.asyncio
async def test_source_records_disabled_by_flag() -> None:
    service, db, _ = make_service(["a derived fact about margin_long 100"], enable_source_records=False)

    resp = await service.store(StoreRequest(content=SOURCE_TEXT, user_id="u1"))

    assert resp.source_id is None
    assert all(r.get("memory_type") != "source" for r in db.saved)


# ---------------------------------------------------------------------------
# Async fast path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_enrichment_returns_source_immediately_then_enriches() -> None:
    facts = ["Mani prefers Pine v6 with margin_long 100"]
    service, db, _ = make_service(facts, async_enrichment=True)

    resp = await service.store(StoreRequest(content=SOURCE_TEXT, user_id="u1"))

    # Immediate response: verbatim stored, enrichment pending
    assert resp.enrichment == "pending"
    assert resp.source_id == resp.id
    source_rows = [r for r in db.saved if r.get("memory_type") == "source"]
    assert len(source_rows) == 1
    assert source_rows[0]["content"] == SOURCE_TEXT

    # Background enrichment lands the derived facts with receipts
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any(r.get("memory_type") != "source" for r in db.saved):
            break
    fact_rows = [r for r in db.saved if r.get("memory_type") != "source"]
    assert len(fact_rows) == 1
    assert fact_rows[0]["metadata"]["source_id"] == resp.source_id
    # No duplicate source record from the background pass
    assert len([r for r in db.saved if r.get("memory_type") == "source"]) == 1
