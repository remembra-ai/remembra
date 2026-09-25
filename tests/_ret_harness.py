"""Shared stack for the RET-stream tests.

Everything is real except the embedding provider:

* ``MemoryService`` (store + recall pipelines as shipped),
* ``Database`` on a temp SQLite file (FTS5, migrations, transactions),
* ``QdrantStore`` over ``qdrant_client``'s in-process engine
  (``location=":memory:"``) - the real filter/offset/score semantics,
* the embedder is a controllable fake: explicit vectors per text, a hashed
  bag-of-words fallback, and a switch that makes it fail like a provider
  that ran out of quota.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

from remembra.config import Settings
from remembra.core.provider_errors import ProviderErrorKind
from remembra.core.time import utcnow
from remembra.models.memory import StoreRequest
from remembra.retrieval import graph as graph_module
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingProviderError
from remembra.storage.qdrant import QdrantStore

DIM = 32
USER = "u1"


def unit(*components: float) -> list[float]:
    """A DIM-length L2-normalised vector from its leading components."""
    vec = list(components) + [0.0] * (DIM - len(components))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def with_cosine(cos: float) -> list[float]:
    """A unit vector whose cosine similarity to ``unit(1)`` is exactly ``cos``."""
    return unit(cos, math.sqrt(max(0.0, 1.0 - cos * cos)))


def quota_error() -> EmbeddingProviderError:
    return EmbeddingProviderError(
        "OpenAI embeddings: quota exhausted", 429, kind=ProviderErrorKind.QUOTA_EXHAUSTED, provider="openai"
    )


class VecEmbeddings:
    """Embedding provider stand-in with per-text vectors and a failure switch."""

    breaker = None

    def __init__(self) -> None:
        self.vectors: dict[str, list[float]] = {}
        self.fail: Exception | None = None
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail is not None:
            raise self.fail
        if text in self.vectors:
            return self.vectors[text]
        vec = [0.0] * DIM
        for tok in re.findall(r"\w+", text.lower()):
            vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % (DIM - 2) + 2] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


@dataclass
class Stack:
    service: MemoryService
    db: Database
    qdrant: QdrantStore
    emb: VecEmbeddings
    settings: Settings

    async def seed(
        self,
        content: str,
        *,
        vector: list[float] | None = None,
        days_ago: float = 0.0,
        project_id: str = "p",
        user_id: str = USER,
        **fields: Any,
    ) -> str:
        """Store one atomic memory; optionally pin its vector and backdate it."""
        if vector is not None:
            self.emb.vectors[content] = vector
        resp = await self.service.store(
            StoreRequest(content=content, user_id=user_id, project_id=project_id, **fields), skip_extraction=True
        )
        assert resp.id, resp
        if days_ago:
            await self.backdate(resp.id, days_ago)
        return resp.id

    async def backdate(self, memory_id: str, days_ago: float) -> None:
        stamp = (utcnow() - timedelta(days=days_ago)).isoformat()
        await self.db.conn.execute("UPDATE memories SET created_at = ?, valid_from = ? WHERE id = ?", (stamp, stamp, memory_id))
        await self.db.conn.commit()

    async def row(self, memory_id: str) -> dict[str, Any]:
        row = await self.db.get_memory(memory_id)
        assert row is not None, memory_id
        return row

    async def close(self) -> None:
        await self.qdrant.close()
        await self.db.close()


async def make_stack(tmp_path: Path, **overrides: Any) -> Stack:
    graph_module._query_cache.clear()
    kwargs: dict[str, Any] = {
        "openai_api_key": "t",
        "embedding_dimensions": DIM,
        "qdrant_collection": "ret_test",
        "smart_extraction_enabled": False,
        "enable_entity_resolution": False,
        "enable_reranking": False,
        "async_enrichment": False,
        "typesafe_mode": "off",
        "conflict_detection_enabled": False,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    db = Database(str(tmp_path / "ret.db"))
    await db.connect()
    await db.init_schema()
    qdrant = QdrantStore(settings)
    qdrant._client = AsyncQdrantClient(location=":memory:")
    await qdrant.init_collection()
    emb = VecEmbeddings()
    service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=emb)  # type: ignore[arg-type]
    return Stack(service=service, db=db, qdrant=qdrant, emb=emb, settings=settings)
