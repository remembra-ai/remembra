"""API-level store tests: real MemoryService + real SQLite behind POST /memories.

Covers the store response contract (ING-1/ING-24), durable idempotency
(ING-18), webhook payloads (ING-24) and batch store (ING-21).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from remembra.api.v1 import memories
from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.config import get_settings
from remembra.security.sanitizer import ContentSanitizer
from tests._ingest_fakes import all_rows, close_ingest_dbs, make_service, row, seed  # noqa: F401

EXISTING = "X repo is at /a; next: check fixes-master.md"
NEW_INPUT = "Next: compile fix list"


class _Webhooks:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def dispatch(self, event: Any) -> None:
        self.events.append(event)


async def _api(tmp_path, **kw):  # type: ignore[no-untyped-def]
    service, db, qdrant, cons_llm, ext_llm = await make_service(tmp_path, **kw)
    app = FastAPI()
    app.state.memory_service = service
    app.state.audit_logger = SimpleNamespace(log_memory_store=AsyncMock())
    app.state.sanitizer = ContentSanitizer()
    app.state.pii_detector = None
    app.state.webhook_manager = _Webhooks()
    identity = AuthenticatedUser("u1", "key-1", "standard")
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_settings] = lambda: service.settings
    app.include_router(memories.router, prefix="/api/v1")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
    return SimpleNamespace(client=client, app=app, service=service, db=db, qdrant=qdrant, cons=cons_llm, ext=ext_llm)


@pytest.mark.asyncio
async def test_api_store_regression_returns_only_the_stored_fact(tmp_path) -> None:
    t = await _api(tmp_path, extractor_replies=[{"facts": [NEW_INPUT]}])
    existing = await seed(t.service, EXISTING)
    t.cons.replies = [
        {
            "action": "UPDATE",
            "target_id": existing,
            "content": "X repo is at /a; next: compile fix list",
            "reason": "next step changed",
            "confidence": 0.9,
        }
    ]
    async with t.client:
        r = await t.client.post("/api/v1/memories", json={"content": NEW_INPUT})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["extracted_facts"] == [NEW_INPUT]
    assert body["status"] == "stored"
    assert body["consolidation"] == [
        {
            "fact": NEW_INPUT,
            "action": "supersede",
            "target_id": existing,
            "memory_id": body["id"],
            "confidence": 0.9,
            "decided_by": "llm",
            "reason": "next step changed",
        }
    ]
    assert body["entities_status"] == "disabled"
    assert "repo is at /a" not in r.text.replace(existing, "")
    assert (await row(t.db, existing))["superseded_by"] == body["id"]
    # ING-24: webhook carries the real facts (was always None).
    event = t.app.state.webhook_manager.events[0]
    assert event.payload["extracted_facts"] == [NEW_INPUT]


@pytest.mark.asyncio
async def test_api_duplicate_store_is_not_announced_as_created(tmp_path) -> None:
    t = await _api(tmp_path)
    existing = await seed(t.service, "Mani prefers dark mode")
    async with t.client:
        r = await t.client.post("/api/v1/memories", json={"content": "Mani prefers dark mode"})

    body = r.json()
    assert body["status"] == "duplicate" and body["duplicate_of"] == existing
    assert body["extracted_facts"] == []
    assert t.app.state.webhook_manager.events == []


@pytest.mark.asyncio
async def test_api_idempotency_replays_response_and_stores_once(tmp_path) -> None:
    t = await _api(tmp_path, extractor_replies=[{"facts": ["Mani ships Remembra fixes today"]}])
    headers = {"Idempotency-Key": "retry-1"}
    payload = {"content": "Mani ships Remembra fixes today"}
    async with t.client:
        first = await t.client.post("/api/v1/memories", json=payload, headers=headers)
        second = await t.client.post("/api/v1/memories", json=payload, headers=headers)
        mismatch = await t.client.post("/api/v1/memories", json={"content": "something else"}, headers=headers)

    assert first.status_code == 201 and second.status_code == 201
    assert second.json() == first.json()
    assert len(await all_rows(t.db)) == 1
    assert mismatch.status_code == 422


@pytest.mark.asyncio
async def test_api_idempotency_in_flight_conflict_and_survives_restart(tmp_path) -> None:
    t = await _api(tmp_path)
    # Another worker/process holds the key (state lives in SQLite, not memory).
    body_hash = memories._idempotency_request_hash(
        memories.StoreRequest(content="Deploy is running", user_id="u1", project_id="default")
    )
    state, _ = await t.db.idempotency_claim("u1", "k-busy", body_hash, 3600, 300)
    assert state == "claimed"
    async with t.client:
        r = await t.client.post("/api/v1/memories", json={"content": "Deploy is running"}, headers={"Idempotency-Key": "k-busy"})
    assert r.status_code == 409
    assert r.headers["Retry-After"] == "2"


@pytest.mark.asyncio
async def test_api_failed_store_releases_idempotency_key(tmp_path) -> None:
    t = await _api(tmp_path)

    async def boom(text: str) -> list[float]:
        raise RuntimeError("provider down")

    t.service.embeddings.embed = boom  # type: ignore[method-assign]
    async with t.client:
        r = await t.client.post("/api/v1/memories", json={"content": "short note"}, headers={"Idempotency-Key": "k-fail"})
    assert r.status_code == 500
    cursor = await t.db.conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE idem_key = 'k-fail'")
    assert (await cursor.fetchone())[0] == 0, "a failed store must free the key for a retry"


@pytest.mark.asyncio
async def test_batch_store_keeps_trust_and_checksum_and_hides_internal_errors(tmp_path) -> None:
    """ING-21."""
    t = await _api(tmp_path)
    real_store = t.service.store

    async def flaky_store(item: Any, **kwargs: Any) -> Any:
        if "explode" in item.content:
            raise RuntimeError("sqlite3.OperationalError: /var/lib/remembra/secret.db locked")
        return await real_store(item, **kwargs)

    t.service.store = flaky_store  # type: ignore[method-assign]
    items = [{"content": f"batch note number {i}"} for i in range(5)] + [{"content": "please explode"}]
    async with t.client:
        r = await t.client.post("/api/v1/memories/batch", json={"items": items, "skip_extraction": True})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["succeeded"] == 5 and body["failed"] == 1
    failed = [x for x in body["results"] if not x["success"]][0]
    assert failed["index"] == 5 and failed["error"] == "Failed to store item"
    assert "secret.db" not in r.text
    rows = await all_rows(t.db)
    assert len(rows) == 5
    assert all(r_["checksum"] and len(r_["checksum"]) == 64 for r_ in rows)
    assert [x["index"] for x in body["results"]] == list(range(6))


@pytest.mark.asyncio
async def test_idempotency_claim_reclaims_abandoned_in_flight_key(tmp_path) -> None:
    t = await _api(tmp_path)
    state, _ = await t.db.idempotency_claim("u1", "k", "h", 3600, 300)
    assert state == "claimed"
    assert (await t.db.idempotency_claim("u1", "k", "h", 3600, 300))[0] == "in_flight"
    await t.db.conn.execute("UPDATE idempotency_keys SET updated_at = '2000-01-01T00:00:00'")
    await t.db.conn.commit()
    assert (await t.db.idempotency_claim("u1", "k", "h", 3600, 300))[0] == "claimed"
    await t.db.idempotency_complete("u1", "k", '{"ok": true}')
    assert await t.db.idempotency_claim("u1", "k", "h", 3600, 300) == ("done", '{"ok": true}')
    # Another user's identical key is independent.
    assert (await t.db.idempotency_claim("u2", "k", "h", 3600, 300))[0] == "claimed"
    await t.client.aclose()
    await asyncio.sleep(0)
