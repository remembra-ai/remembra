"""SEC-23 (audit P-022 / P-061 / P-275): status values, metadata and the brief never keep a credential.

Before this fix a key sent as a status value was redacted only in the memory's
text copy: ``metadata.status_value``, ``GET /session/status``, the session
brief and ``GET /timeline`` returned it in full, and free-form metadata on
``POST /memories`` was stored as sent. These tests drive the real routes over a
real SQLite ``Database`` and a real ``MemoryService`` (only the vector store and
the embedder are in-process fakes), with the production PII policy
(``mode=redact``) and without it.

Every key is synthetic and assembled at runtime (``tests.test_sec23_redaction.FAKE``).
"""

from __future__ import annotations

import os

os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

import json
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from remembra.api.router import api_router
from remembra.core.limiter import limiter
from remembra.core.time import utcnow
from remembra.extraction.extractor import ExtractionOutcome
from remembra.inbox.manager import InboxManager
from remembra.models.memory import Memory, MemorySummary, RecallResult, StoreRequest
from remembra.security.audit import AuditLogger
from remembra.security.pii_detector import PIIDetector
from remembra.security.sanitizer import ContentSanitizer
from remembra.security.secrets import scrub_memory_record
from remembra.services.memory import MemoryService
from remembra.storage.database import Database
from tests.test_sec23_redaction import FAKE

KINDS = sorted(FAKE)


class FakeQdrant:
    def __init__(self) -> None:
        self.upserted: list[Any] = []

    async def upsert(self, memory: Any) -> None:
        self.upserted.append(memory)

    async def search(self, **kwargs: Any) -> list[Any]:
        return []

    async def get_by_id(self, memory_id: str) -> None:
        return None


class FakeEmbeddings:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class VerbatimExtractor:
    """No model: every write keeps its text as one fact."""

    async def extract(self, content: str, **_: Any) -> list[str]:
        return [content]

    async def extract_detailed(self, content: str, **_: Any) -> ExtractionOutcome:
        return ExtractionOutcome(facts=[content], method="verbatim")


@pytest.fixture(params=[None, "redact"], ids=["no-pii", "pii-redact"])
async def env(request, tmp_path):
    db = Database(str(tmp_path / "sec23_status.db"))
    await db.connect()
    await db.init_schema()
    inbox = InboxManager(db)
    await inbox.init_schema()

    from remembra.config import Settings

    settings = Settings(openai_api_key="test", enable_entity_resolution=False)
    qdrant = FakeQdrant()
    service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=FakeEmbeddings())  # type: ignore[arg-type]
    service.extractor = VerbatimExtractor()  # type: ignore[assignment]

    app = FastAPI()
    app.state.limiter = limiter
    app.state.db = db
    app.state.memory_service = service
    app.state.inbox_manager = inbox
    app.state.audit_logger = AuditLogger(db)
    app.state.sanitizer = ContentSanitizer()
    # The production policy (docker-compose.prod.yml: REMEMBRA_PII_MODE=redact), or none.
    app.state.pii_detector = PIIDetector(enabled=True, mode=request.param) if request.param else None
    app.include_router(api_router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield {"client": client, "db": db, "inbox": inbox, "qdrant": qdrant}
    await db.close()


async def _raw_rows(db: Database, where: str = "1") -> str:
    cursor = await db.conn.execute(f"SELECT content, extracted_facts, metadata FROM memories WHERE {where}")  # noqa: S608
    return json.dumps([list(r) for r in await cursor.fetchall()])


async def _everything_read(c: httpx.AsyncClient, project: str = "alpha") -> str:
    """Every response a reader gets for the project: status list, brief (JSON + rendered), timeline, trail."""
    reads = [
        await c.get("/api/v1/session/status", params={"project_id": project}),
        await c.get("/api/v1/session/brief", params={"project_id": project, "agent_id": "claude-code"}),
        await c.get("/api/v1/timeline", params={"project_id": project, "include_superseded": "true", "limit": 100}),
        await c.get("/api/v1/trail", params={"project_id": project}),
    ]
    for r in reads:
        assert r.status_code == 200, r.text
    return "\n".join(r.text for r in reads)


# ---------------------------------------------------------------------------
# Status values (P-022): written and read back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_status_value_secret_is_never_stored_or_shown(env, kind):
    secret = FAKE[kind]
    c, db = env["client"], env["db"]
    r = await c.post(
        "/api/v1/session/status",
        json={"key": "deploy:api", "value": f"deployed with {secret} at noon", "project_id": "alpha"},
    )
    assert r.status_code == 200, r.text
    assert secret not in r.text
    assert "[REDACTED:" in r.json()["value"]

    assert secret not in await _raw_rows(db)
    assert all(secret not in json.dumps(m.metadata) + m.content for m in env["qdrant"].upserted)

    shown = await _everything_read(c)
    assert secret not in shown
    listing = (await c.get("/api/v1/session/status", params={"project_id": "alpha"})).json()
    assert listing["items"][0]["value"].startswith("deployed with [REDACTED:")


async def test_resending_a_redacted_status_value_is_unchanged(env):
    c = env["client"]
    body = {"key": "deploy:api", "value": f"token {FAKE['github_token']}", "project_id": "alpha"}
    first = (await c.post("/api/v1/session/status", json=body)).json()
    again = (await c.post("/api/v1/session/status", json=body)).json()
    assert first["changed"] is True
    assert again["changed"] is False and again["memory_id"] == first["memory_id"]
    listing = (await c.get("/api/v1/session/status", params={"project_id": "alpha"})).json()
    assert listing["count"] == 1


async def test_status_key_and_metadata_are_redacted(env):
    c, db = env["client"], env["db"]
    token = FAKE["openai_key"]
    r = await c.post(
        "/api/v1/session/status",
        json={
            "key": f"cred:{FAKE['anthropic_key']}",
            "value": "rotated",
            "project_id": "alpha",
            "metadata": {"note": f"old key {token}", "history": [{"was": token}, 7, True, None]},
        },
    )
    assert r.status_code == 200, r.text
    raw = await _raw_rows(db)
    assert token not in raw and FAKE["anthropic_key"].lower() not in raw.lower()
    shown = await _everything_read(c)
    assert token not in shown and FAKE["anthropic_key"].lower() not in shown.lower()
    # Non-string values keep their type.
    row = await (await db.conn.execute("SELECT metadata FROM memories WHERE memory_type = 'status'")).fetchone()
    history = json.loads(row[0])["history"]
    assert history[1:] == [7, True, None] and history[0] == {"was": "[REDACTED:openai_key]"}


# ---------------------------------------------------------------------------
# Memory metadata (P-022 / P-275): every write path
# ---------------------------------------------------------------------------


async def test_memory_metadata_is_redacted_on_store_and_update(env):
    c, db = env["client"], env["db"]
    gh, stripe = FAKE["github_token"], FAKE["stripe_key"]
    r = await c.post(
        "/api/v1/memories",
        json={
            "content": "Deploy notes for the billing service.",
            "project_id": "alpha",
            "memory_type": "handoff",
            "metadata": {"note": f"use {gh}", "nested": {"keys": [stripe, "plain"]}, gh: "key named by a token"},
        },
    )
    assert r.status_code == 201, r.text
    memory_id = r.json()["id"]
    raw = await _raw_rows(db)
    assert gh not in raw and stripe not in raw
    assert "plain" in raw  # ordinary values are kept

    patched = await c.patch(f"/api/v1/memories/{memory_id}", json={"content": "Deploy notes, v2.", "metadata": {"later": stripe}})
    assert patched.status_code == 200, patched.text
    raw = await _raw_rows(db)
    assert stripe not in raw
    assert all(stripe not in json.dumps(m.metadata) for m in env["qdrant"].upserted)

    got = await c.get(f"/api/v1/memories/{memory_id}")
    assert got.status_code == 200 and gh not in got.text and stripe not in got.text
    assert gh not in await _everything_read(c)


async def test_import_metadata_is_redacted(env):
    c, db = env["client"], env["db"]
    token = FAKE["slack_token"]
    payload = json.dumps([{"content": "Imported note about the release.", "metadata": {"src": f"slack {token}"}}])
    r = await c.post("/api/v1/transfer/import", json={"format": "json", "data": payload, "project_id": "alpha"})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
    assert token not in await _raw_rows(db)


# ---------------------------------------------------------------------------
# Rows stored before this fix: scrubbed when they are read (render time)
# ---------------------------------------------------------------------------


async def _seed(db: Database, memory_id: str, content: str, metadata: dict[str, Any], memory_type: str, at: datetime) -> None:
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id="default_user",
        project_id="alpha",
        content=content,
        extracted_facts=[content],
        metadata=metadata,
        created_at=at,
        memory_type=memory_type,
    )


async def test_legacy_rows_are_scrubbed_on_every_read(env):
    c, db = env["client"], env["db"]
    gh, jwt, aws = FAKE["github_token"], FAKE["jwt"], FAKE["aws_access_key"]
    now = utcnow() - timedelta(minutes=5)
    # Written straight to SQLite, the way rows were stored before redaction reached metadata.
    await _seed(db, str(uuid.uuid4()), f"deploy:api: {gh}", {"status_key": "deploy:api", "status_value": gh}, "status", now)
    handoff_id = str(uuid.uuid4())
    await _seed(
        db,
        handoff_id,
        f"[HANDOFF] codex: shipped billing. Next: rotate {jwt}",
        {"agent_id": "codex", "note": f"aws {aws}", "files": ["a.py", {"why": gh}]},
        "handoff",
        now + timedelta(minutes=1),
    )
    await _seed(db, str(uuid.uuid4()), f"checkpoint with {aws}", {"agent_id": "codex", "detail": jwt}, "checkpoint", now)
    await db.conn.execute(
        "INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata, status, created_at)"
        " VALUES ('inbox_legacy', 'default_user', 'codex', 'claude-code', ?, ?, '{}', 'unread', ?)",
        (f"key {gh}", "x" * 190 + f" {jwt}", datetime.now(UTC).isoformat()),
    )
    await db.conn.commit()

    shown = await _everything_read(c)
    for secret in (gh, jwt, aws):
        assert secret not in shown
    # A key cut by the 200-character inbox preview must not leak its first half either.
    assert jwt[:12] not in shown
    brief = (await c.get("/api/v1/session/brief", params={"project_id": "alpha", "agent_id": "claude-code"})).json()
    assert brief["status_items"][0]["value"] == "[REDACTED:github_token]"
    assert brief["handoff"]["id"] == handoff_id
    assert "Next: rotate [REDACTED:jwt]" in brief["rendered"]
    assert "- deploy:api: [REDACTED:github_token]" in brief["rendered"]
    assert brief["handoff"]["metadata"]["note"] == "aws [REDACTED:aws_access_key]"
    assert brief["inbox"]["items"][0]["subject"] == "key [REDACTED:github_token]"

    got = await c.get(f"/api/v1/memories/{handoff_id}")
    assert got.status_code == 200 and all(s not in got.text for s in (gh, jwt, aws))


# ---------------------------------------------------------------------------
# The shared metadata scrub
# ---------------------------------------------------------------------------


def test_scrub_value_walks_nested_values_and_keeps_types():
    from remembra.security.secrets import scrub_value

    gh, oa = FAKE["github_token"], FAKE["openai_key"]
    value = {"a": [f"x {gh}", {"b": (oa, 3)}], "n": 1.5, "t": True, "z": None, gh: "v"}
    out = scrub_value(value)
    dumped = json.dumps(out)
    assert gh not in dumped and oa not in dumped
    assert out["n"] == 1.5 and out["t"] is True and out["z"] is None
    assert out["a"][1]["b"] == ["[REDACTED:openai_key]", 3]
    assert scrub_value(out) == out  # idempotent


def test_identifier_values_keep_random_ids_but_lose_provider_keys():
    """A long random session id reads like a token; it keys the relay row, so the
    high-entropy fallback leaves identifier fields alone. A provider key is never an id."""
    from remembra.security.secrets import scrub_value

    session = "S" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(38)) + "aZ7"
    relay_key = f"claude-code\x1f{session}"
    meta = {
        "session_id": session,
        "relay_key": relay_key,
        "relay": {"session_id": session, "commits": [{"sha": "a" * 40}], "location": {"fingerprints": ["root:" + "b" * 40]}},
        "agent_id": FAKE["github_token"],
        "note": session,
    }
    out = scrub_value(meta)
    assert out["session_id"] == session and out["relay_key"] == relay_key
    assert out["relay"]["session_id"] == session
    assert out["relay"]["commits"][0]["sha"] == "a" * 40
    assert out["relay"]["location"]["fingerprints"] == ["root:" + "b" * 40]
    assert out["agent_id"] == "[REDACTED:github_token]"
    assert out["note"] == "[REDACTED:high_entropy_token]"


def test_models_redact_metadata_on_write_and_read():
    gh = FAKE["github_token"]
    meta = {"note": f"token {gh}", "deep": [{"k": gh}]}
    written = [
        Memory(user_id="u", content="x", metadata=meta).metadata,
        StoreRequest(content="x", metadata=meta).metadata,
    ]
    read = [
        RecallResult(id="m", relevance=0.5, content="x", created_at=datetime.now(UTC), metadata=meta).model_dump_json(),
        MemorySummary(id="m", user_id="u", content="x", created_at="now", metadata=meta).model_dump_json(),
        json.dumps(scrub_memory_record({"content": "x", "metadata": meta})),
    ]
    assert all(gh not in json.dumps(m) for m in written)
    assert all(gh not in r for r in read)
    assert meta["note"] == f"token {gh}"  # the caller's dict is not mutated
