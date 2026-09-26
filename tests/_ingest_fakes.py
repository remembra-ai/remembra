"""Shared fakes for ingest-pipeline tests.

Only the network edges are faked: the embedding provider (deterministic
hashed bag-of-words), Qdrant (in-memory cosine search with the real filter
semantics: user + optional project), and the OpenAI chat client (scripted
JSON replies). SQLite is the real ``Database`` on a temp file, so candidate
filtering, supersession marks, FTS rows, idempotency and decision_log are all
exercised for real.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from remembra.config import Settings
from remembra.extraction import background
from remembra.services.memory import MemoryService
from remembra.storage.database import Database

DIM = 256


class HashEmbeddings:
    """Deterministic embedding: hashed bag of lowercase word tokens, L2-normalised."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        vec = [0.0] * DIM
        for tok in re.findall(r"\w+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


class MemQdrant:
    """In-memory stand-in for QdrantStore (same method contracts)."""

    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.search_calls: list[dict[str, Any]] = []

    async def upsert(self, memory: Any) -> None:
        self.points[memory.id] = {
            "vector": list(memory.embedding),
            "payload": {
                "user_id": memory.user_id,
                "project_id": memory.project_id,
                "content": memory.content,
                "metadata": dict(memory.metadata),
                "created_at": memory.created_at.isoformat(),
                "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
            },
        }

    async def search(
        self,
        query_vector: list[float],
        user_id: str,
        project_id: str | None = None,
        limit: int = 5,
        score_threshold: float = 0.7,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        self.search_calls.append({"user_id": user_id, "project_id": project_id, "limit": limit})
        hits = []
        for pid, point in self.points.items():
            payload = point["payload"]
            if payload["user_id"] != user_id or (project_id is not None and payload["project_id"] != project_id):
                continue
            score = sum(a * b for a, b in zip(query_vector, point["vector"], strict=False))
            if score >= score_threshold:
                hits.append((pid, score, dict(payload)))
        hits.sort(key=lambda h: h[1], reverse=True)
        return hits[:limit]

    async def delete(self, memory_id: str, user_id: str | None = None) -> bool:
        point = self.points.get(memory_id)
        if point is None or (user_id is not None and point["payload"]["user_id"] != user_id):
            return False
        del self.points[memory_id]
        return True

    async def delete_by_user(self, user_id: str) -> int:
        doomed = [pid for pid, point in self.points.items() if point["payload"]["user_id"] == user_id]
        for pid in doomed:
            del self.points[pid]
        return len(doomed)

    async def delete_by_user_everywhere(self, user_id: str, also: Any = ()) -> int:
        return await self.delete_by_user(user_id)  # one collection, no rollback copies

    async def get_by_id(self, memory_id: str) -> dict[str, Any] | None:
        point = self.points.get(memory_id)
        return None if point is None else {"id": memory_id, **point["payload"]}


class ScriptedOpenAI:
    """Mimics ``AsyncOpenAI().chat.completions.create`` with queued JSON replies.

    Each reply is a dict (serialised to JSON), a str (returned verbatim), or an
    Exception (raised). When the queue is empty, ``default`` is used.
    """

    def __init__(self, replies: list[Any] | None = None, default: Any = None) -> None:
        self.replies = list(replies or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            raise RuntimeError("ScriptedOpenAI: no reply scripted")
        content = reply if isinstance(reply, str) else json.dumps(reply)
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def user_prompts(self) -> list[str]:
        return [m["content"] for call in self.calls for m in call["messages"] if m["role"] == "user"]


_OPEN_DBS: list[Database] = []


@pytest.fixture(autouse=True)
async def close_ingest_dbs():  # type: ignore[no-untyped-def]
    """Drain background tasks and close every DB a test opened (import into test modules)."""
    yield
    await background.drain(timeout=5.0)
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


async def make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "ingest.db"))
    await db.connect()
    await db.init_schema()
    _OPEN_DBS.append(db)
    return db


async def make_service(
    tmp_path: Path,
    *,
    consolidator_replies: list[Any] | None = None,
    extractor_replies: list[Any] | None = None,
    **overrides: Any,
) -> tuple[MemoryService, Database, MemQdrant, ScriptedOpenAI, ScriptedOpenAI]:
    """Real MemoryService over real SQLite; only network edges are faked.

    Returns (service, db, qdrant, consolidator_llm, extractor_llm).
    """
    settings_kwargs: dict[str, Any] = {
        "openai_api_key": "test",
        "enable_entity_resolution": False,
        "enable_hybrid_search": True,
        # The hashed embedder gives lower absolute similarities than a real
        # model; a low threshold keeps "related" memories as candidates.
        "consolidation_threshold": 0.1,
        "typesafe_mode": "off",
    }
    settings_kwargs.update(overrides)
    settings = Settings(**settings_kwargs)
    db = await make_db(tmp_path)
    qdrant = MemQdrant()
    service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=HashEmbeddings())  # type: ignore[arg-type]
    cons_llm = ScriptedOpenAI(consolidator_replies)
    ext_llm = ScriptedOpenAI(extractor_replies)
    service.consolidator._client = cons_llm  # type: ignore[assignment]
    service.extractor._client = ext_llm  # type: ignore[assignment]
    return service, db, qdrant, cons_llm, ext_llm


async def seed(service: MemoryService, content: str, user_id: str = "u1", **fields: Any) -> str:
    """Store an existing memory atomically (no extraction, no consolidation)."""
    from remembra.models.memory import StoreRequest

    resp = await service.store(StoreRequest(content=content, user_id=user_id, **fields), skip_extraction=True)
    return resp.id


async def row(db: Database, memory_id: str) -> dict[str, Any]:
    r = await db.get_memory(memory_id)
    assert r is not None, f"memory {memory_id} missing"
    return r


async def all_rows(db: Database, user_id: str = "u1") -> list[dict[str, Any]]:
    cursor = await db.conn.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY created_at", (user_id,))
    return [dict(r) for r in await cursor.fetchall()]


async def fts_content(db: Database, memory_id: str) -> str | None:
    cursor = await db.conn.execute("SELECT content FROM memories_fts WHERE id = ?", (memory_id,))
    r = await cursor.fetchone()
    return None if r is None else str(r[0])
