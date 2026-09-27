"""P-074 / P-096: the sleep-time pass deletes nothing unless an operator turns decay cleanup on.

At f70abac the sleep-time worker (on by default, every 6 hours) ran a decay
cleanup that deleted any memory over 90 days old that no search had returned
and that had no expiry: handoffs, structured checkpoints, status values and
pinned notes included. Its delete also skipped ``Database.delete_memory``, so a
handoff's pickup events and a note's full-text row stayed behind. The public
retention text says handoffs and checkpoints stay until you delete them, and
notes are kept while the account is open.

Now:

1. With the default settings the pass deletes nothing.
2. With ``sleep_time_decay_cleanup_enabled`` on, it deletes only old ordinary
   notes that were never recalled and have no expiry. It never deletes a
   handoff, checkpoint, status value, pinned memory or source record. Each
   delete is complete (entity links, full-text row, pickup events, vector) and
   leaves a security-log entry.
3. The dedup pass never embeds, rewrites or deletes one of those protected rows.

Everything runs on the real Database (SQLite on disk) and the real worker. Only
the vector store, the embedder and the consolidator are in-process fakes that
record their calls.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from remembra.config import Settings
from remembra.core.time import utcnow
from remembra.extraction.consolidator import ConsolidationAction, ConsolidationResult
from remembra.models.memory import Entity, Relationship
from remembra.services.sleep_time import SleepTimeWorker
from remembra.storage.database import Database

OLD = timedelta(days=120)
RELAY = {"relay": {"agent_id": "claude-code", "session_id": "s1"}}


class FakeQdrant:
    """Records deletes; every search returns ``hits``."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str | None]] = []
        self.hits: list[dict[str, Any]] = []

    async def search(self, **_: Any) -> list[dict[str, Any]]:
        return list(self.hits)

    async def delete(self, memory_id: str, user_id: str | None = None) -> bool:
        self.deleted.append((memory_id, user_id))
        return True


class FakeEmbeddings:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [0.1, 0.2, 0.3, 0.4]


class FakeConsolidator:
    """Says every candidate is the same memory, to be merged (UPDATE)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def consolidate(self, new_fact: str, existing: list[Any]) -> ConsolidationResult:
        self.calls.append((new_fact, [e.id for e in existing]))
        return ConsolidationResult(action=ConsolidationAction.UPDATE, target_id=existing[0].id, content="merged text")


@pytest.fixture()
async def db(tmp_path):
    database = Database(str(tmp_path / "sleep.db"))
    await database.connect()
    await database.init_schema()
    yield database
    await database.close()


def _worker(db: Database, settings: Settings) -> tuple[SleepTimeWorker, FakeQdrant, FakeEmbeddings, FakeConsolidator]:
    qdrant, embeddings, consolidator = FakeQdrant(), FakeEmbeddings(), FakeConsolidator()
    service = SimpleNamespace(db=db, qdrant=qdrant, embeddings=embeddings, consolidator=consolidator, entity_matcher=None)
    return SleepTimeWorker(settings=settings, memory_service=service), qdrant, embeddings, consolidator


async def _row(
    db: Database,
    memory_id: str,
    *,
    user_id: str = "u1",
    age: timedelta = OLD,
    memory_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    pinned: bool = False,
    expires_in: timedelta | None = None,
    access_count: int = 0,
    source: str = "user_input",
) -> None:
    now = utcnow()
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id=user_id,
        project_id="p1",
        content=f"text of {memory_id}",
        extracted_facts=[],
        metadata=metadata or {},
        created_at=now - age,
        expires_at=now + expires_in if expires_in else None,
        memory_type=memory_type,
        pinned=pinned,
        source=source,
    )
    await db.index_memory_fts(memory_id, user_id, "p1", f"text of {memory_id}")
    if access_count:
        await db.conn.execute("UPDATE memories SET access_count = ? WHERE id = ?", (access_count, memory_id))
        await db.conn.commit()


async def _ids(db: Database, sql: str, *params: Any) -> set[str]:
    cursor = await db.conn.execute(sql, params)
    return {row[0] for row in await cursor.fetchall()}


async def _count(db: Database, sql: str, *params: Any) -> int:
    cursor = await db.conn.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def _old_account(db: Database) -> None:
    """Every kind of row the old pass deleted, plus the ones it rightly kept."""
    await _row(db, "note-old")  # an ordinary note: 120 days, never recalled, no expiry
    await _row(db, "fact-old", memory_type="fact")
    await _row(db, "handoff-relay", memory_type="handoff", metadata=RELAY)
    await _row(db, "handoff-plain", memory_type="handoff")
    await _row(db, "checkpoint-relay", memory_type="checkpoint", metadata=RELAY)
    await _row(db, "status-relay", memory_type="status", metadata={"source": "relay"}, source="agent_generated")
    await _row(db, "pinned-old", pinned=True)
    await _row(db, "source-old", memory_type="source", metadata={"record_kind": "source"})
    await _row(db, "note-89d", age=timedelta(days=89))
    await _row(db, "note-recalled", access_count=1)
    await _row(db, "note-expiring", expires_in=timedelta(days=30))
    await _row(db, "note-today", age=timedelta(0))  # today's store makes the account "active"
    await _row(db, "other-user-old", user_id="u2")

    # A handoff another agent picked up (ids and times only).
    await db.conn.execute(
        "INSERT INTO relay_pickups (user_id, project_id, handoff_id, reader_agent, picked_up_at)"
        " VALUES ('u1', 'p1', 'handoff-relay', 'codex', ?)",
        (utcnow().isoformat(),),
    )
    # Graph rows pulled from the old note.
    person, company = Entity(canonical_name="Dana", type="person"), Entity(canonical_name="Acme", type="company")
    for entity in (person, company):
        await db.save_entity(entity, user_id="u1", project_id="p1")
    await db.link_memory_to_entity("note-old", person.id)
    await db.save_relationship(
        Relationship(from_entity_id=person.id, to_entity_id=company.id, type="works_at", source_memory_id="note-old")
    )
    await db.conn.commit()


ALL_U1 = {
    "note-old",
    "fact-old",
    "handoff-relay",
    "handoff-plain",
    "checkpoint-relay",
    "status-relay",
    "pinned-old",
    "source-old",
    "note-89d",
    "note-recalled",
    "note-expiring",
    "note-today",
}


async def test_default_settings_delete_nothing(db: Database) -> None:
    await _old_account(db)
    worker, qdrant, _, _ = _worker(db, Settings(openai_api_key="test"))

    report = await worker.run_consolidation(user_id="u1")

    assert report.memories_decayed == 0
    assert qdrant.deleted == []
    assert await _ids(db, "SELECT id FROM memories WHERE user_id = 'u1'") == ALL_U1
    assert await _count(db, "SELECT COUNT(*) FROM relay_pickups WHERE handoff_id = 'handoff-relay'") == 1
    assert await _count(db, "SELECT COUNT(*) FROM audit_log") == 0
    # Deleting old notes is something an operator has to switch on.
    assert Settings.model_fields["sleep_time_decay_cleanup_enabled"].default is False
    assert Settings(openai_api_key="test").sleep_time_decay_cleanup_enabled is False


async def test_enabled_decay_deletes_only_old_unrecalled_ordinary_notes_and_says_so(db: Database) -> None:
    await _old_account(db)
    settings = Settings(openai_api_key="test", sleep_time_decay_cleanup_enabled=True)
    assert settings.sleep_time_decay_cleanup_days == 90
    worker, qdrant, _, _ = _worker(db, settings)

    report = await worker.run_consolidation(user_id="u1")

    gone = {"note-old", "fact-old"}
    assert report.memories_decayed == 2
    assert await _ids(db, "SELECT id FROM memories WHERE user_id = 'u1'") == ALL_U1 - gone
    # Relay records, pinned, source, recent, recalled and expiring rows stay; so does the other account.
    assert await _count(db, "SELECT COUNT(*) FROM memories WHERE id = 'other-user-old'") == 1
    assert await _count(db, "SELECT COUNT(*) FROM relay_pickups WHERE handoff_id = 'handoff-relay'") == 1
    # Each delete is the full one a user's delete does: vector (owner-scoped), full-text row,
    # entity links and relationships pulled from the note.
    assert sorted(qdrant.deleted) == [("fact-old", "u1"), ("note-old", "u1")]
    assert await _ids(db, "SELECT id FROM memories_fts WHERE user_id = 'u1'") == ALL_U1 - gone
    assert await _count(db, "SELECT COUNT(*) FROM memory_entities WHERE memory_id = 'note-old'") == 0
    assert await _count(db, "SELECT COUNT(*) FROM relationships WHERE source_memory_id = 'note-old'") == 0
    # And each one is in the account's security log.
    assert await _ids(db, "SELECT resource_id FROM audit_log WHERE user_id = 'u1' AND action = 'memory_decayed'") == gone


async def test_enabled_decay_honours_the_age_setting(db: Database) -> None:
    await _row(db, "note-40d", age=timedelta(days=40))
    await _row(db, "note-20d", age=timedelta(days=20))
    settings = Settings(openai_api_key="test", sleep_time_decay_cleanup_enabled=True, sleep_time_decay_cleanup_days=30)
    worker, _, _, _ = _worker(db, settings)

    assert (await worker.run_consolidation(user_id="u1")).memories_decayed == 1
    assert await _ids(db, "SELECT id FROM memories") == {"note-20d"}


async def test_sleep_time_delete_refuses_protected_rows_and_keeps_pickups(db: Database) -> None:
    """P-096: the worker's own delete can never remove a handoff, so its pickup events never dangle."""
    await _old_account(db)
    worker, qdrant, _, _ = _worker(db, Settings(openai_api_key="test", sleep_time_decay_cleanup_enabled=True))

    for memory_id in ("handoff-relay", "handoff-plain", "checkpoint-relay", "status-relay", "pinned-old", "source-old"):
        assert await worker._delete_memory(memory_id, "u1") is False
    assert await worker._delete_memory("note-old", "u2") is False  # not the owner
    assert qdrant.deleted == []
    assert await _ids(db, "SELECT id FROM memories WHERE user_id = 'u1'") == ALL_U1
    assert await _count(db, "SELECT COUNT(*) FROM relay_pickups WHERE handoff_id = 'handoff-relay'") == 1


async def test_dedup_never_embeds_rewrites_or_deletes_protected_rows(db: Database) -> None:
    await _row(db, "handoff-relay", memory_type="handoff", metadata=RELAY)
    await _row(db, "handoff-new", age=timedelta(0), memory_type="handoff", metadata=RELAY)
    await _row(db, "note-new", age=timedelta(0))
    worker, qdrant, embeddings, consolidator = _worker(db, Settings(openai_api_key="test"))
    # The vector search offers an old handoff as the near-duplicate of everything.
    qdrant.hits = [{"id": "handoff-relay", "content": "text of handoff-relay", "score": 0.99}]

    report = await worker.run_consolidation(user_id="u1")

    assert report.duplicates_merged == 0
    assert embeddings.texts == ["text of note-new"]  # the new handoff's text is never re-embedded
    assert consolidator.calls == []  # nor offered to the consolidator as a merge candidate
    assert await _ids(db, "SELECT id FROM memories") == {"handoff-relay", "handoff-new", "note-new"}
    assert await _ids(db, "SELECT content FROM memories WHERE id = 'handoff-relay'") == {"text of handoff-relay"}
    assert qdrant.deleted == []


async def test_dedup_still_merges_two_ordinary_notes_with_a_full_delete(db: Database) -> None:
    await _row(db, "note-a", age=timedelta(0))
    await _row(db, "note-b", age=timedelta(days=10))
    worker, qdrant, _, consolidator = _worker(db, Settings(openai_api_key="test"))
    qdrant.hits = [{"id": "note-b", "content": "text of note-b", "score": 0.99}]

    report = await worker.run_consolidation(user_id="u1")

    assert report.duplicates_merged == 1
    assert consolidator.calls == [("text of note-a", ["note-b"])]
    assert await _ids(db, "SELECT id FROM memories") == {"note-b"}
    assert await _ids(db, "SELECT content FROM memories WHERE id = 'note-b'") == {"merged text"}
    assert await _ids(db, "SELECT id FROM memories_fts") == {"note-b"}
    assert qdrant.deleted == [("note-a", "u1")]


async def test_status_says_whether_decay_cleanup_can_delete(tmp_path, db: Database) -> None:
    from remembra.api.v1 import admin
    from tests.security_harness import secure_app

    off = _worker(db, Settings(openai_api_key="test"))[0]
    on = _worker(db, Settings(openai_api_key="test", sleep_time_decay_cleanup_enabled=True))[0]
    for name, state, expected in (("off", {"sleep_worker": off}, False), ("on", {"sleep_worker": on}, True), ("none", {}, False)):
        (tmp_path / name).mkdir()
        async with secure_app(tmp_path / name, [admin.router], state=state) as h:
            key, _ = await h.api_key(await h.create_user(f"{name}@example.com"), "viewer")
            r = await h.client.get("/api/v1/admin/sleep-time/status", headers={"X-API-Key": key})
            assert r.status_code == 200, r.text
            assert r.json()["decay_cleanup_enabled"] is expected


async def test_real_vector_store_keeps_every_point_by_default_and_loses_only_the_decayed_note(db: Database) -> None:
    """The same rule on the real QdrantStore (in-memory Qdrant): vectors go only with a decayed note."""
    from uuid import uuid4

    from qdrant_client import AsyncQdrantClient

    from remembra.models.memory import Memory
    from remembra.storage.qdrant import QdrantStore

    store = QdrantStore(Settings(openai_api_key="t", embedding_dimensions=4, qdrant_collection="sleep_retention"))
    store._client = AsyncQdrantClient(location=":memory:")
    await store.init_collection(4)
    note, handoff, today = str(uuid4()), str(uuid4()), str(uuid4())
    rows = ((note, None, {}, OLD), (handoff, "handoff", RELAY, OLD), (today, None, {}, timedelta(0)))
    for memory_id, memory_type, metadata, age in rows:
        await _row(db, memory_id, memory_type=memory_type, metadata=metadata, age=age)
        await store.upsert(
            Memory(
                id=memory_id,
                user_id="u1",
                content=f"text of {memory_id}",
                memory_type=memory_type,
                embedding=[0.1, 0.2, 0.3, 0.4],
            )
        )

    async def points() -> set[str]:
        client = await store._get_client()
        found, _ = await client.scroll(collection_name=store.collection_name, limit=10)
        return {str(p.id) for p in found}

    service = SimpleNamespace(
        db=db, qdrant=store, embeddings=FakeEmbeddings(), consolidator=FakeConsolidator(), entity_matcher=None
    )
    default = SleepTimeWorker(settings=Settings(openai_api_key="test"), memory_service=service)
    assert (await default.run_consolidation(user_id="u1")).memories_decayed == 0
    assert await points() == {note, handoff, today}

    enabled = SleepTimeWorker(
        settings=Settings(openai_api_key="test", sleep_time_decay_cleanup_enabled=True), memory_service=service
    )
    assert (await enabled.run_consolidation(user_id="u1")).memories_decayed == 1
    assert await points() == {handoff, today}
    assert await _ids(db, "SELECT id FROM memories") == {handoff, today}


def test_docs_name_only_real_sleep_time_settings_with_their_defaults() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "guides" / "sleep-time-compute.md").read_text()
    reference = (root / "docs" / "reference" / "configuration.md").read_text()
    fields = Settings.model_fields
    for text in (guide, reference):
        for var in set(re.findall(r"REMEMBRA_(SLEEP_TIME_[A-Z_]+)", text)):
            assert var.lower() in fields, var  # the old guide named INTERVAL and IDLE_THRESHOLD, which never existed
        for name, shown in (("sleep_time_enabled", "true"), ("sleep_time_decay_cleanup_enabled", "false")):
            assert fields[name].default is (shown == "true")
            assert f"| `REMEMBRA_{name.upper()}` | `{shown}` |" in text
        assert f"| `REMEMBRA_SLEEP_TIME_DECAY_CLEANUP_DAYS` | `{fields['sleep_time_decay_cleanup_days'].default}` |" in text
