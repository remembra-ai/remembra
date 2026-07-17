"""Tests for atomic stores (skip_extraction / skip_consolidation).

Atomic storage is required for chat messages, logs, and pre-structured data:
each item must be stored 1:1 — never split into multiple facts, and never
merged/deduped into another memory. Without this, LangChain chat history
loses messages (an AI reply gets consolidated into a similar user message).
"""

from __future__ import annotations

from typing import Any

import pytest

from remembra.config import Settings
from remembra.models.memory import StoreRequest
from remembra.services.memory import MemoryService


class FakeDB:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    async def save_memory_metadata(self, **kwargs: Any) -> None:
        self.saved.append(kwargs)

    async def index_memory_fts(self, **kwargs: Any) -> None:
        pass


class FakeQdrant:
    def __init__(self) -> None:
        self.upserted: list[Any] = []
        self.searches = 0

    async def upsert(self, memory: Any) -> None:
        self.upserted.append(memory)

    async def search(self, **kwargs: Any) -> list[Any]:
        self.searches += 1
        return []


class FakeEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class BoomConsolidator:
    """Fails if called — proves atomic stores never consolidate."""

    async def consolidate(self, fact: str, existing: list[Any]) -> Any:
        raise AssertionError("consolidation must be skipped for atomic stores")


def _service() -> tuple[MemoryService, FakeDB, FakeQdrant]:
    settings = Settings(openai_api_key="test", enable_entity_resolution=False)
    db, qd = FakeDB(), FakeQdrant()
    svc = MemoryService(settings=settings, qdrant=qd, db=db, embeddings=FakeEmbeddings())
    svc.consolidator = BoomConsolidator()
    return svc, db, qd


@pytest.mark.asyncio
async def test_skip_extraction_stores_one_memory_no_split() -> None:
    svc, db, _ = _service()
    # A multi-fact sentence that extraction would normally split
    content = "My name is Frank and I love hiking and I moved from Berlin in 2019."
    resp = await svc.store(StoreRequest(content=content, user_id="u1"), skip_extraction=True)

    fact_rows = [r for r in db.saved if r.get("memory_type") != "source"]
    assert len(fact_rows) == 1, "atomic store must produce exactly one memory"
    assert fact_rows[0]["content"] == content, "content stored verbatim, not split"
    assert resp.source_id is None, "no source record for atomic stores"


@pytest.mark.asyncio
async def test_skip_extraction_never_consolidates() -> None:
    # Three highly-similar messages must all persist as distinct memories
    # (a BoomConsolidator guarantees consolidation is never even attempted).
    svc, db, qd = _service()
    for msg in [
        "My name is Frank and I love hiking",
        "Nice to meet you Frank, hiking is wonderful",
        "Frank enjoys hiking a lot",
    ]:
        await svc.store(StoreRequest(content=msg, user_id="u1"), skip_extraction=True)

    fact_rows = [r for r in db.saved if r.get("memory_type") != "source"]
    assert len(fact_rows) == 3, "each atomic message is a distinct memory, never merged"
    assert qd.searches == 0, "atomic stores skip the similarity search entirely"


@pytest.mark.asyncio
async def test_normal_store_still_consolidates() -> None:
    # Sanity: without skip_extraction, the consolidator IS consulted.
    settings = Settings(openai_api_key="test", enable_entity_resolution=False)
    db, qd = FakeDB(), FakeQdrant()
    svc = MemoryService(settings=settings, qdrant=qd, db=db, embeddings=FakeEmbeddings())

    called = {"n": 0}

    class CountingConsolidator:
        async def consolidate(self, fact: str, existing: list[Any]) -> Any:
            from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult

            called["n"] += 1
            return ConsolidationResult(action=ConsolidationAction.ADD, content=fact, reason="t", target_id=None)

    svc.consolidator = CountingConsolidator()
    # Pass content through unchanged so it stays a single fact
    await svc.store(StoreRequest(content="ship it", user_id="u1"), skip_extraction=True)
    assert called["n"] == 0
    await svc.store(StoreRequest(content="ship it", user_id="u1"), skip_extraction=False)
    assert called["n"] == 1
