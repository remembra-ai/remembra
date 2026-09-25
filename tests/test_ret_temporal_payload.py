"""UPG-1 (validity windows + as_of) and RET-7 (payload backfill)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from remembra.core.time import utcnow
from remembra.extraction import background
from remembra.models.memory import RecallRequest
from remembra.services.agent_session import AgentSessionService
from remembra.storage.database import Database
from remembra.storage.payload_backfill import backfill_payload
from tests._ret_harness import USER, make_stack, unit, with_cosine

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def stack(tmp_path):
    s = await make_stack(tmp_path)
    yield s
    await background.drain(timeout=5.0)
    await s.close()


async def _recall(stack, query: str, **kw):
    return await stack.service.recall(RecallRequest(query=query, user_id=USER, project_id="p", retrieval_mode="balanced", **kw))


# ---------------------------------------------------------------------------
# Required regression (7): as_of returns the superseded value in the past
# ---------------------------------------------------------------------------


async def test_regression_as_of_returns_superseded_value_then_new_value(stack) -> None:
    stack.emb.vectors["where is remembra deployed"] = unit(1)
    stack.emb.vectors["Remembra is deployed on Fly.io"] = with_cosine(0.9)
    stack.emb.vectors["Remembra is deployed on Coolify"] = with_cosine(0.9)
    old = await stack.seed("Remembra is deployed on Fly.io")
    await stack.backdate(old, 10)

    sup = await stack.service.supersede(old, USER, "Remembra is deployed on Coolify", reason="migrated")
    new = sup.new_memory_id

    old_row, new_row = await stack.row(old), await stack.row(new)
    assert old_row["superseded_by"] == new
    assert old_row["valid_to"] == new_row["valid_from"]  # the window closes when the new one opens

    past = await _recall(stack, "where is remembra deployed", as_of=utcnow() - timedelta(days=5))
    assert [m.content for m in past.memories] == ["Remembra is deployed on Fly.io"]
    assert past.memories[0].superseded_by == new and past.memories[0].valid_to is not None

    now = await _recall(stack, "where is remembra deployed")
    assert [m.content for m in now.memories] == ["Remembra is deployed on Coolify"]

    before_anything = await _recall(stack, "where is remembra deployed", as_of=utcnow() - timedelta(days=30))
    assert before_anything.memories == []

    # The temporal service entry point uses the same semantics.
    via_helper = await stack.service.recall_as_of(USER, "where is remembra deployed", utcnow() - timedelta(days=5), "p")
    assert [m.id for m in via_helper.memories] == [old]


async def test_status_upsert_closes_previous_window(stack) -> None:
    session = AgentSessionService(stack.db, stack.service)
    first = await session.upsert_status(USER, "p", "deploy:remembra-api", "pushed, NOT deployed")
    await stack.backdate(first["memory_id"], 3)
    second = await session.upsert_status(USER, "p", "deploy:remembra-api", "live")
    old_row, new_row = await stack.row(first["memory_id"]), await stack.row(second["memory_id"])
    assert old_row["valid_to"] == new_row["valid_from"]

    stack.emb.vectors["deploy remembra-api"] = unit(1)
    past = await _recall(stack, "deploy remembra-api", as_of=utcnow() - timedelta(days=1), enable_hybrid=True)
    assert [m.id for m in past.memories] == [first["memory_id"]]


async def test_validity_migration_backfills_existing_rows(tmp_path) -> None:
    db = Database(str(tmp_path / "legacy.db"))
    await db.connect()
    await db.init_schema()
    try:
        # Recreate a pre-migration database: no validity columns, version 3 unapplied.
        await db.conn.execute("DROP INDEX IF EXISTS idx_memories_valid")
        await db.conn.execute("ALTER TABLE memories DROP COLUMN valid_to")
        await db.conn.execute("ALTER TABLE memories DROP COLUMN valid_from")
        await db.conn.execute("DELETE FROM schema_version WHERE version = 3")
        await db.conn.commit()
        t_old, t_new, t_mark = "2026-01-01T00:00:00", "2026-02-01T00:00:00", "2026-02-01T00:00:05"
        for mid, created, sup_by, sup_at in [("old", t_old, "new", t_mark), ("new", t_new, None, None)]:
            await db.conn.execute(
                """INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at,
                                         superseded_by, superseded_at)
                   VALUES (?, 'u', 'p', ?, ?, ?, ?, ?)""",
                (mid, mid, created, created, sup_by, sup_at),
            )
        await db.conn.commit()

        await db._apply_versioned_migrations()

        rows = {r["id"]: dict(r) for r in await (await db.conn.execute("SELECT * FROM memories")).fetchall()}
        assert rows["old"]["valid_from"] == t_old and rows["old"]["valid_to"] == t_new
        assert rows["new"]["valid_from"] == t_new and rows["new"]["valid_to"] is None
        assert await db.get_schema_version() >= 3
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# RET-7: payload backfill (dry run by default, idempotent apply)
# ---------------------------------------------------------------------------


async def test_payload_backfill_dry_run_then_apply(stack) -> None:
    a = await stack.seed("Acme renewal signed", scope="work:acme", memory_type="fact")
    b = await stack.seed("Gym plan", scope="personal:health")
    # Simulate points written before RET-7: strip the new fields.
    for mid in (a, b):
        await stack.qdrant._client.delete_payload(  # type: ignore[union-attr]
            collection_name=stack.qdrant.collection_name,
            keys=["memory_type", "scope", "scope_prefixes", "valid_from", "valid_to"],
            points=[mid],
        )
    # A row whose vector is missing (e.g. queued) is reported, not invented.
    await stack.db.save_memory_metadata(
        memory_id="no-vector", user_id=USER, project_id="p", content="c", extracted_facts=[], metadata={}, created_at=utcnow()
    )

    dry = await backfill_payload(stack.db, stack.qdrant)
    assert dry.to_dict()["mode"] == "dry_run"
    assert (dry.needs_update, dry.updated, dry.missing_vector) == (2, 0, 1)
    assert "scope" not in (await stack.qdrant.get_raw_payloads([a]))[a]

    applied = await backfill_payload(stack.db, stack.qdrant, apply=True)
    assert (applied.needs_update, applied.updated, applied.errors) == (2, 2, 0)
    payload = (await stack.qdrant.get_raw_payloads([a]))[a]
    assert payload["memory_type"] == "fact" and payload["scope_prefixes"] == ["work", "work:acme"]
    assert payload["content"]  # untouched

    again = await backfill_payload(stack.db, stack.qdrant, apply=True)
    assert (again.needs_update, again.up_to_date) == (0, 2)

    # Server-side scope filter works on backfilled points.
    hits = await stack.qdrant.search(unit(1), USER, "p", limit=10, score_threshold=-1.0, must_match={"scope_prefixes": "work"})
    assert {mid for mid, _, _ in hits} == {a}
