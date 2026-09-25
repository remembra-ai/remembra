"""Shared harness: production API routes over real SQLite, reachable by the sync SDK.

Starlette's ``TestClient`` is an ``httpx.Client`` over ASGI, so a
``remembra.Memory`` whose ``_client`` is swapped for it exercises the real
routes, the real ``MemoryService`` and a real SQLite ``Database``. Only the
vector store and the embedder are in-process fakes (no network).
"""

from __future__ import annotations

import os

os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from starlette.testclient import TestClient

from remembra import __version__
from remembra.api.router import api_router
from remembra.client.memory import Memory
from remembra.config import Settings
from remembra.core.limiter import limiter
from remembra.inbox.manager import InboxManager
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.spaces.manager import SpaceManager
from remembra.storage.database import Database


class FakeQdrant:
    async def upsert(self, memory: Any) -> None:
        return None

    async def search(self, **kwargs: Any) -> list[Any]:
        return []

    async def delete_by_project(self, user_id: str, project_id: str) -> int:
        return 0

    async def delete(self, memory_id: str, user_id: str | None = None) -> None:
        return None


class FakeEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


from remembra.extraction.extractor import ExtractionOutcome  # noqa: E402


class OneFactExtractor:
    async def extract(self, content: str) -> list[str]:
        return [content]

    async def extract_detailed(self, content: str, reference_date: Any = None) -> ExtractionOutcome:
        return ExtractionOutcome(facts=[content], method="llm")


def build_api(tmp_path: Any) -> Iterator[dict[str, Any]]:
    """Generator for a pytest fixture: ``yield from build_api(tmp_path)``."""
    app = FastAPI()
    app.state.limiter = limiter
    app.state.reported_version = __version__
    app.include_router(api_router)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # Minimal stand-in for main.py's /health (Qdrant probe not needed here).
        return {"status": "ok", "version": app.state.reported_version}

    with TestClient(app, base_url="http://testserver") as http:

        async def _setup() -> None:
            db = Database(str(tmp_path / "sdk.db"))
            await db.connect()
            await db.init_schema()
            inbox = InboxManager(db)
            await inbox.init_schema()
            spaces = SpaceManager(db)
            await spaces.init_schema()
            settings = Settings(openai_api_key="test", enable_entity_resolution=False)
            service = MemoryService(settings=settings, qdrant=FakeQdrant(), db=db, embeddings=FakeEmbeddings())  # type: ignore[arg-type]
            service.extractor = OneFactExtractor()  # type: ignore[assignment]
            app.state.db = db
            app.state.memory_service = service
            app.state.inbox_manager = inbox
            app.state.space_manager = spaces
            app.state.audit_logger = AuditLogger(db)
            app.state.sanitizer = ContentSanitizer()
            app.state.pii_detector = None

        assert http.portal is not None
        http.portal.call(_setup)

        def make_client(**kwargs: Any) -> Memory:
            client = Memory(base_url="http://testserver", **kwargs)
            client._client.close()
            client._client = http  # route the SDK through the ASGI app
            return client

        yield {"http": http, "app": app, "make_client": make_client}

        async def _teardown() -> None:
            await app.state.db.close()

        http.portal.call(_teardown)


def seed(api: dict[str, Any], memory_id: str, content: str, created_at: Any, **kw: Any) -> None:
    """Insert a memory row directly (controlled created_at) for time-based tests."""

    async def _save() -> None:
        await api["app"].state.db.save_memory_metadata(
            memory_id=memory_id,
            user_id=kw.pop("user_id", "default_user"),
            project_id=kw.pop("project_id", "alpha"),
            content=content,
            extracted_facts=[content],
            metadata=kw.pop("metadata", {}),
            created_at=created_at,
            **kw,
        )

    api["http"].portal.call(_save)


def row(api: dict[str, Any], memory_id: str) -> dict[str, Any]:
    async def _get() -> dict[str, Any] | None:
        return await api["app"].state.db.get_memory(memory_id)

    row = api["http"].portal.call(_get)
    assert row is not None
    return row
