"""AGT-3 / AGT-5 / AGT-7: session brief, status upsert, memory-type policy, timeline.

Real FastAPI routes (the production ``api_router``) over a real SQLite
``Database`` and a real ``MemoryService``; only the vector store and the
embedding provider are in-process fakes (no network).
"""

from __future__ import annotations

import os

os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from remembra.api.router import api_router
from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.config import Settings
from remembra.core.limiter import limiter
from remembra.core.time import utcnow
from remembra.inbox.manager import InboxManager
from remembra.models.memory import Entity, StoreRequest
from remembra.security.audit import AuditLogger
from remembra.security.sanitizer import ContentSanitizer
from remembra.services.memory import MemoryService
from remembra.storage.database import Database


class FakeQdrant:
    def __init__(self) -> None:
        self.upserted: list[Any] = []

    async def upsert(self, memory: Any) -> None:
        self.upserted.append(memory)

    async def search(self, **kwargs: Any) -> list[Any]:
        return []


class FakeEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class NoLLMExtractor:
    """Fails loudly if extraction runs — hygiene types must be stored atomically."""

    async def extract(self, content: str) -> list[str]:
        raise AssertionError("fact extraction must be skipped for this memory type")


@pytest.fixture()
async def env(tmp_path):
    db = Database(str(tmp_path / "agent_session.db"))
    await db.connect()
    await db.init_schema()
    inbox = InboxManager(db)
    await inbox.init_schema()

    settings = Settings(openai_api_key="test", enable_entity_resolution=False)
    service = MemoryService(settings=settings, qdrant=FakeQdrant(), db=db, embeddings=FakeEmbeddings())  # type: ignore[arg-type]
    service.extractor = NoLLMExtractor()  # type: ignore[assignment]

    app = FastAPI()
    app.state.limiter = limiter
    app.state.db = db
    app.state.memory_service = service
    app.state.inbox_manager = inbox
    app.state.audit_logger = AuditLogger(db)
    app.state.sanitizer = ContentSanitizer()
    app.state.pii_detector = None
    app.include_router(api_router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield {"client": client, "db": db, "app": app, "inbox": inbox, "service": service}
    await db.close()


async def _seed(db: Database, memory_id: str, content: str, created_at: datetime, **kw: Any) -> None:
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id=kw.pop("user_id", "default_user"),
        project_id=kw.pop("project_id", "alpha"),
        content=content,
        extracted_facts=[content],
        metadata=kw.pop("metadata", {}),
        created_at=created_at,
        **kw,
    )


# ---------------------------------------------------------------------------
# Memory-type policy on POST /memories
# ---------------------------------------------------------------------------


async def test_checkpoint_gets_default_ttl_and_is_atomic(env):
    c = env["client"]
    content = "Checkpoint: finished SEC-19 test. Next I will wire the brief. Then docs."
    r = await c.post("/api/v1/memories", json={"content": content, "project_id": "alpha", "memory_type": "checkpoint"})
    assert r.status_code == 201, r.text
    body = r.json()
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=6, hours=23) < expires - utcnow() <= timedelta(days=7)

    row = await env["db"].get_memory(body["id"])
    assert row["memory_type"] == "checkpoint"
    assert row["content"] == content  # one unit, not split


async def test_checkpoint_explicit_ttl_is_kept(env):
    r = await env["client"].post(
        "/api/v1/memories",
        json={"content": "short checkpoint", "project_id": "alpha", "memory_type": "checkpoint", "ttl": "2d"},
    )
    assert r.status_code == 201, r.text
    expires = datetime.fromisoformat(r.json()["expires_at"])
    assert expires - utcnow() <= timedelta(days=2)


async def test_handoff_is_stored_verbatim_as_one_memory_without_ttl(env):
    snapshot = "[SESSION END] Was working on AGT. Completed: brief API. Next: MCP tools. Key files: server.py"
    r = await env["client"].post("/api/v1/memories", json={"content": snapshot, "project_id": "alpha", "memory_type": "handoff"})
    assert r.status_code == 201, r.text
    assert r.json()["expires_at"] is None
    rows = (await env["client"].get("/api/v1/timeline", params={"project_id": "alpha", "memory_type": "handoff"})).json()
    assert rows["total"] == 1
    assert rows["memories"][0]["content"] == snapshot


async def test_status_type_rejected_on_generic_store(env):
    r = await env["client"].post("/api/v1/memories", json={"content": "x", "memory_type": "status"})
    assert r.status_code == 400
    assert "/api/v1/session/status" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Status upsert
# ---------------------------------------------------------------------------


async def test_status_upsert_supersedes_prior_value(env):
    c, db = env["client"], env["db"]

    r1 = (await c.post("/api/v1/session/status", json={"key": "Deploy:API", "value": "v1 live", "project_id": "alpha"})).json()
    assert r1["changed"] is True and r1["key"] == "deploy:api" and r1["superseded"] == []

    same = (await c.post("/api/v1/session/status", json={"key": "deploy:api", "value": "v1 live", "project_id": "alpha"})).json()
    assert same["changed"] is False and same["memory_id"] == r1["memory_id"]

    r2 = (await c.post("/api/v1/session/status", json={"key": "deploy:api", "value": "v2 live", "project_id": "alpha"})).json()
    assert r2["changed"] is True
    assert r2["superseded"] == [r1["memory_id"]]

    listing = (await c.get("/api/v1/session/status", params={"project_id": "alpha"})).json()
    assert listing["count"] == 1
    assert listing["items"][0]["value"] == "v2 live"
    assert listing["items"][0]["memory_id"] == r2["memory_id"]

    # Old value kept as history but excluded from recall's active set.
    assert await db.filter_active_memory_ids([r1["memory_id"], r2["memory_id"]]) == {r2["memory_id"]}
    history = (
        await c.get(
            "/api/v1/timeline",
            params={"project_id": "alpha", "memory_type": "status", "include_superseded": "true"},
        )
    ).json()
    assert [m["content"] for m in history["memories"]] == ["deploy:api: v1 live", "deploy:api: v2 live"]


async def test_status_is_project_scoped(env):
    c = env["client"]
    await c.post("/api/v1/session/status", json={"key": "k", "value": "alpha-value", "project_id": "alpha"})
    await c.post("/api/v1/session/status", json={"key": "k", "value": "beta-value", "project_id": "beta"})
    alpha = (await c.get("/api/v1/session/status", params={"project_id": "alpha"})).json()["items"]
    beta = (await c.get("/api/v1/session/status", params={"project_id": "beta"})).json()["items"]
    assert [i["value"] for i in alpha] == ["alpha-value"]
    assert [i["value"] for i in beta] == ["beta-value"]


async def test_status_rejects_blank_key(env):
    r = await env["client"].post("/api/v1/session/status", json={"key": "   ", "value": "x"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Session brief
# ---------------------------------------------------------------------------


async def test_session_brief_contents_and_ordering(env):
    c, db, inbox = env["client"], env["db"], env["inbox"]
    base = utcnow() - timedelta(hours=5)
    await _seed(db, "h-old", "old handoff", base, memory_type="handoff")
    await _seed(db, "h-new", "new handoff", base + timedelta(hours=1), memory_type="handoff")
    await _seed(db, "src", "verbatim source", base + timedelta(hours=2), memory_type="source")
    await _seed(db, "m1", "first fact", base + timedelta(hours=2))
    await _seed(db, "m2", "second fact", base + timedelta(hours=3), metadata={"agent_id": "codex", "source_id": "src"})
    await _seed(db, "other", "other project fact", base + timedelta(hours=4), project_id="beta")
    await _seed(db, "expired", "expired fact", base, expires_at=utcnow() - timedelta(minutes=1))
    await c.post("/api/v1/session/status", json={"key": "sprint", "value": "AGT remediation", "project_id": "alpha"})
    await inbox.send(owner_user_id="default_user", from_agent="codex", to_agent="claude-code", subject="review", body="x" * 500)
    await inbox.send(owner_user_id="default_user", from_agent="codex", to_agent="gemini", subject="not mine", body="y")

    r = await c.get("/api/v1/session/brief", params={"project_id": "alpha", "agent_id": "claude-code", "recent_n": 5})
    assert r.status_code == 200, r.text
    brief = r.json()

    assert brief["handoff"]["id"] == "h-new"
    assert brief["inbox"]["unread_count"] == 1
    item = brief["inbox"]["items"][0]
    assert item["subject"] == "review" and item["from_agent"] == "codex"
    assert len(item["body_preview"]) == 203  # 200 chars + "..."
    assert [s["key"] for s in brief["status"]] == ["sprint"]
    # recent: by time, newest first; no handoff/status/source/expired/other-project rows
    assert [m["id"] for m in brief["recent"]] == ["m2", "m1"]
    assert brief["recent"][0]["agent_id"] == "codex"
    assert brief["recent"][0]["source_id"] == "src"
    assert set(brief["known_agents"]) == {"codex", "claude-code", "gemini"}
    assert brief["warnings"] == []


async def test_session_brief_without_agent_warns_and_skips_inbox(env):
    brief = (await env["client"].get("/api/v1/session/brief", params={"project_id": "alpha"})).json()
    assert brief["inbox"] is None
    assert any("REMEMBRA_AGENT_ID" in w for w in brief["warnings"])


async def test_session_brief_warns_on_unknown_agent_id(env):
    await env["inbox"].send(owner_user_id="default_user", from_agent="codex", to_agent="claude-code", subject="s", body="b")
    brief = (await env["client"].get("/api/v1/session/brief", params={"project_id": "alpha", "agent_id": "claude_code"})).json()
    assert brief["inbox"]["unread_count"] == 0
    assert any("claude_code" in w for w in brief["warnings"])


async def test_session_brief_respects_project_restricted_key(env):
    env["app"].dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="default_user", api_key_id="k", rate_limit_tier="standard", project_ids=["alpha"]
    )
    try:
        denied = await env["client"].get("/api/v1/session/brief", params={"project_id": "beta"})
        assert denied.status_code == 403
        pinned = await env["client"].get("/api/v1/session/brief")
        assert pinned.status_code == 200 and pinned.json()["project_id"] == "alpha"
    finally:
        env["app"].dependency_overrides.clear()


async def test_session_brief_is_tenant_scoped(env):
    db = env["db"]
    await _seed(db, "mine", "mine", utcnow())
    await _seed(db, "theirs", "theirs", utcnow(), user_id="someone-else")
    await env["inbox"].send(owner_user_id="someone-else", from_agent="x", to_agent="claude-code", subject="s", body="b")
    brief = (await env["client"].get("/api/v1/session/brief", params={"project_id": "alpha", "agent_id": "claude-code"})).json()
    assert [m["id"] for m in brief["recent"]] == ["mine"]
    assert brief["inbox"]["unread_count"] == 0


# ---------------------------------------------------------------------------
# Timeline (AGT-7)
# ---------------------------------------------------------------------------


async def test_timeline_filters_by_created_at_range_server_side(env):
    db = env["db"]
    for day in (1, 5, 10, 20):
        await _seed(db, f"d{day}", f"event on day {day}", datetime(2026, 1, day, 12, 0))
    r = await env["client"].get(
        "/api/v1/timeline",
        params={"project_id": "alpha", "start": "2026-01-05", "end": "2026-01-20", "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["id"] for m in body["memories"]] == ["d5", "d10"]
    assert body["total"] == 2

    desc = (
        await env["client"].get("/api/v1/timeline", params={"project_id": "alpha", "order": "desc", "limit": 1, "offset": 1})
    ).json()
    assert [m["id"] for m in desc["memories"]] == ["d10"]
    assert desc["total"] == 4

    tz = (
        await env["client"].get("/api/v1/timeline", params={"project_id": "alpha", "start": "2026-01-10T07:00:00-05:00"})
    ).json()
    assert [m["id"] for m in tz["memories"]] == ["d10", "d20"]  # 07:00 EST == 12:00 UTC, inclusive

    bad = await env["client"].get("/api/v1/timeline", params={"start": "2026-02-01", "end": "2026-01-01"})
    assert bad.status_code == 400


async def test_timeline_entity_filter_is_exact_not_substring(env):
    db = env["db"]
    await _seed(db, "m-claw", "clawbot release planned", datetime(2026, 1, 2))
    await _seed(db, "m-bot", "some bot thing", datetime(2026, 1, 3))
    claw = Entity(canonical_name="Clawbot", type="product", aliases=["POS app"])
    bot = Entity(canonical_name="bot", type="concept")
    await db.save_entity(claw, user_id="default_user", project_id="alpha")
    await db.save_entity(bot, user_id="default_user", project_id="alpha")
    await db.link_memory_to_entity("m-claw", claw.id)
    await db.link_memory_to_entity("m-bot", bot.id)

    by_name = (await env["client"].get("/api/v1/timeline", params={"entity": "clawbot"})).json()
    assert [m["id"] for m in by_name["memories"]] == ["m-claw"]
    by_alias = (await env["client"].get("/api/v1/timeline", params={"entity": "pos APP"})).json()
    assert [m["id"] for m in by_alias["memories"]] == ["m-claw"]
    by_bot = (await env["client"].get("/api/v1/timeline", params={"entity": "bot"})).json()
    assert [m["id"] for m in by_bot["memories"]] == ["m-bot"]


# ---------------------------------------------------------------------------
# ING-24: all-duplicate store is reported as a duplicate
# ---------------------------------------------------------------------------


async def test_all_duplicate_store_sets_duplicate_of(env):
    from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult

    service = env["service"]

    class MatchQdrant(FakeQdrant):
        async def search(self, **kwargs: Any) -> list[Any]:
            return [("existing-id", 0.97, {"content": "Alice works at Acme"})]

    class NoopConsolidator:
        async def consolidate(self, fact: str, existing: list[Any]) -> ConsolidationResult:
            return ConsolidationResult(action=ConsolidationAction.NOOP, content=None, reason="dup", target_id="existing-id")

    class OneFact:
        async def extract(self, content: str) -> list[str]:
            return [content]

    service.qdrant = MatchQdrant()
    service.consolidator = NoopConsolidator()
    service.extractor = OneFact()
    resp = await service.store(StoreRequest(content="Alice works at Acme", user_id="default_user"))
    assert resp.id == "existing-id"
    assert resp.duplicate_of == "existing-id"
