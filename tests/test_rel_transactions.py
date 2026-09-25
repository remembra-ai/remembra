"""REL-11 / REL-12 / REL-15: transaction isolation on the shared connection,
atomic multi-step writes, versioned migrations, and the scoped entity merge.

Everything runs against real SQLite files (aiosqlite), with a second,
independent connection used as an outside observer of what is committed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiosqlite
import pytest

from remembra.core.time import utcnow
from remembra.storage import database as database_module
from remembra.storage.database import Database
from remembra.storage.sqlite_tx import TransactionRollbackOnly


@pytest.fixture()
async def db(tmp_path):
    database = Database(str(tmp_path / "tx.db"))
    await database.connect()
    await database.init_schema()
    yield database
    await database.close()


async def _observer_count(path: str, sql: str, params: tuple = ()) -> int:
    """Count rows as seen by an independent connection (= what is committed)."""
    async with aiosqlite.connect(path) as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0])


async def _insert_memory(db: Database, memory_id: str, user_id: str = "u1", project_id: str = "default") -> None:
    now = utcnow()
    await db.save_memory_metadata(
        memory_id=memory_id,
        user_id=user_id,
        project_id=project_id,
        content=f"content {memory_id}",
        extracted_facts=[],
        metadata={},
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Transaction primitive
# ---------------------------------------------------------------------------


async def test_transaction_commits_and_rolls_back(db: Database) -> None:
    async with db.transaction():
        await db.conn.execute("INSERT INTO schema_version VALUES (900, 'x', 'now')")
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version=900") == 1

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction():
            await db.conn.execute("INSERT INTO schema_version VALUES (901, 'x', 'now')")
            raise RuntimeError("boom")
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version=901") == 0


async def test_foreign_commit_cannot_commit_open_transaction(db: Database) -> None:
    """Request B's legacy execute+commit must not commit request A's half-done work,
    and A's rollback must not discard B's work."""
    a_started = asyncio.Event()
    a_proceed = asyncio.Event()

    async def request_a() -> None:
        async with db.transaction():
            await db.conn.execute("INSERT INTO schema_version VALUES (1001, 'a1', 'now')")
            a_started.set()
            await a_proceed.wait()
            await db.conn.execute("INSERT INTO schema_version VALUES (1002, 'a2', 'now')")
            raise RuntimeError("request A failed after its first write")

    async def request_b() -> None:
        await a_started.wait()
        # Legacy style: implicit transaction + explicit commit (and a stray rollback).
        await db.conn.execute("INSERT INTO schema_version VALUES (2001, 'b1', 'now')")
        await db.conn.commit()
        await db.conn.rollback()

    task_a = asyncio.create_task(request_a())
    task_b = asyncio.create_task(request_b())
    await a_started.wait()
    await asyncio.sleep(0.05)  # give B every chance to interleave
    # While A is open, nothing from A (or B, which is queued behind A) is committed.
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version>=1000") == 0
    assert not task_b.done()

    a_proceed.set()
    with pytest.raises(RuntimeError, match="request A failed"):
        await task_a
    await task_b

    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version IN (1001,1002)") == 0
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version=2001") == 1


async def test_concurrent_transactions_are_isolated(db: Database) -> None:
    async def worker(n: int, fail: bool) -> None:
        async with db.transaction():
            for i in range(5):
                await db.conn.execute("INSERT INTO schema_version VALUES (?, 'w', 'now')", (n * 100 + i,))
                await asyncio.sleep(0)
            if fail:
                raise ValueError(n)

    results = await asyncio.gather(
        *(worker(n, fail=(n % 2 == 0)) for n in range(30, 40)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ValueError) for r in results) == 5
    for n in range(30, 40):
        expected = 0 if n % 2 == 0 else 5
        got = await _observer_count(
            db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version BETWEEN ? AND ?", (n * 100, n * 100 + 4)
        )
        assert got == expected, n


async def test_nested_transaction_and_legacy_commit_are_deferred(db: Database) -> None:
    with pytest.raises(KeyError):
        async with db.transaction():
            await db.conn.execute("INSERT INTO schema_version VALUES (3001, 'x', 'now')")
            await db.conn.commit()  # legacy commit inside: deferred, not real
            async with db.transaction():  # nested joins
                await db.conn.execute("INSERT INTO schema_version VALUES (3002, 'x', 'now')")
            raise KeyError("outer fails")
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version>=3000") == 0


async def test_rollback_inside_transaction_marks_rollback_only(db: Database) -> None:
    with pytest.raises(TransactionRollbackOnly):
        async with db.transaction():
            await db.conn.execute("INSERT INTO schema_version VALUES (4001, 'x', 'now')")
            await db.conn.rollback()
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version=4001") == 0


async def test_executescript_inside_transaction_is_refused(db: Database) -> None:
    with pytest.raises(RuntimeError, match="executescript"):
        async with db.transaction():
            await db.conn.executescript("SELECT 1;")


async def test_cancelled_transaction_rolls_back(db: Database) -> None:
    started = asyncio.Event()

    async def body() -> None:
        async with db.transaction():
            await db.conn.execute("INSERT INTO schema_version VALUES (5001, 'x', 'now')")
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(body())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version=5001") == 0
    # connection still usable afterwards
    async with db.transaction():
        await db.conn.execute("INSERT INTO schema_version VALUES (5002, 'x', 'now')")
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version WHERE version=5002") == 1


async def test_busy_timeout_is_set(db: Database) -> None:
    cursor = await db.conn.execute("PRAGMA busy_timeout")
    assert (await cursor.fetchone())[0] == 5000


# ---------------------------------------------------------------------------
# Atomic multi-step methods
# ---------------------------------------------------------------------------


async def _seed_memory_graph(db: Database, memory_id: str) -> None:
    now = utcnow().isoformat()
    await _insert_memory(db, memory_id)
    await db.index_memory_fts(memory_id, "u1", "default", f"content {memory_id}")
    await db.conn.execute(
        "INSERT INTO entities (id, user_id, project_id, canonical_name, type, created_at, updated_at)"
        " VALUES ('e1','u1','default','A','person',?,?), ('e2','u1','default','B','person',?,?)",
        (now, now, now, now),
    )
    await db.conn.execute("INSERT INTO memory_entities (memory_id, entity_id) VALUES (?, 'e1')", (memory_id,))
    await db.conn.execute(
        "INSERT INTO relationships (id, from_entity_id, to_entity_id, type, source_memory_id, created_at)"
        " VALUES ('r1','e1','e2','knows',?,?)",
        (memory_id, now),
    )
    await db.conn.commit()


async def test_delete_memory_is_atomic(db: Database) -> None:
    await _seed_memory_graph(db, "m1")
    await db.conn.execute("CREATE TRIGGER block_delete BEFORE DELETE ON memories BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    await db.conn.commit()

    with pytest.raises(Exception, match="blocked"):
        await db.delete_memory("m1")
    # Earlier steps (relationships, links, FTS) were rolled back with it.
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM relationships WHERE id='r1'") == 1
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memory_entities WHERE memory_id='m1'") == 1
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memories_fts WHERE id='m1'") == 1

    await db.conn.execute("DROP TRIGGER block_delete")
    await db.conn.commit()
    assert await db.delete_memory("m1") is True
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memories_fts WHERE id='m1'") == 0
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM relationships") == 0


async def test_archive_memory_is_an_atomic_move(db: Database) -> None:
    await _seed_memory_graph(db, "m2")
    await db.conn.execute("CREATE TRIGGER block_delete BEFORE DELETE ON memories BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    await db.conn.commit()
    assert await db.archive_memory("m2", reason="manual") is False
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM archived_memories WHERE id='m2'") == 0
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memories WHERE id='m2'") == 1

    await db.conn.execute("DROP TRIGGER block_delete")
    await db.conn.commit()
    assert await db.archive_memory("m2", reason="manual") is True
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM archived_memories WHERE id='m2'") == 1
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memories WHERE id='m2'") == 0


async def test_delete_project_memories_is_atomic_and_scoped(db: Database) -> None:
    await _insert_memory(db, "p1", project_id="alpha")
    await _insert_memory(db, "p2", project_id="alpha")
    await _insert_memory(db, "p3", project_id="beta")
    for mid, proj in (("p1", "alpha"), ("p2", "alpha"), ("p3", "beta")):
        await db.index_memory_fts(mid, "u1", proj, "x")
    assert await db.delete_project_memories("u1", "alpha") == 2
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memories") == 1
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memories_fts") == 1


# ---------------------------------------------------------------------------
# Versioned migrations (REL-15)
# ---------------------------------------------------------------------------


async def test_schema_version_recorded_and_idempotent(db: Database) -> None:
    latest = max(v for v, _, _ in database_module.VERSIONED_MIGRATIONS)
    assert await db.get_schema_version() == latest
    await db.init_schema()  # second boot: no errors, nothing re-applied
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM schema_version") == len(database_module.VERSIONED_MIGRATIONS)
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM pending_embeddings") == 0


async def test_failing_versioned_migration_fails_boot_and_is_not_recorded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        database_module,
        "VERSIONED_MIGRATIONS",
        [*database_module.VERSIONED_MIGRATIONS, (99, "broken", ["CREATE TABLE t99 (a)", "ALTER TABLE nope ADD x"])],
    )
    database = Database(str(tmp_path / "bad.db"))
    await database.connect()
    with pytest.raises(Exception, match="nope"):
        await database.init_schema()
    await database.close()
    assert await _observer_count(str(tmp_path / "bad.db"), "SELECT COUNT(*) FROM schema_version WHERE version=99") == 0
    # the partial statement of the failed migration was rolled back too
    assert (
        await _observer_count(str(tmp_path / "bad.db"), "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='t99'")
        == 0
    )


# ---------------------------------------------------------------------------
# REL-12: entity merge is scoped and history-preserving
# ---------------------------------------------------------------------------


async def test_entity_merge_is_scoped_and_preserves_history(db: Database) -> None:
    from remembra.services.sleep_time import SleepTimeWorker

    now = utcnow().isoformat()
    ents = [
        ("keep", "u1"),
        ("dup", "u1"),
        ("acme", "u1"),
        ("x2", "u2"),
        ("y2", "u2"),
    ]
    for eid, uid in ents:
        await db.conn.execute(
            "INSERT INTO entities (id, user_id, canonical_name, aliases, type, created_at, updated_at)"
            " VALUES (?, ?, ?, 'alias', 'person', ?, ?)",
            (eid, uid, eid, now, now),
        )
    rels = [
        # u1: after merge, dup->acme duplicates keep->acme (both currently valid)
        ("r_keep", "keep", "acme", "works_at", "2026-01-01", None),
        ("r_dup", "dup", "acme", "works_at", "2026-06-01", None),
        # u1: historical edge (already ended) must be untouched
        ("r_hist", "dup", "acme", "works_at", "2024-01-01", "2025-01-01"),
        # u1: keep->dup becomes a self-reference after the merge
        ("r_self", "keep", "dup", "knows", "2026-01-01", None),
        # u2: unrelated exact duplicates — the old global DELETE removed one of these
        ("r_u2a", "x2", "y2", "knows", "2026-01-01", None),
        ("r_u2b", "x2", "y2", "knows", "2026-02-01", None),
    ]
    for rid, f, t, typ, vf, vt in rels:
        await db.conn.execute(
            "INSERT INTO relationships (id, from_entity_id, to_entity_id, type, created_at, valid_from, valid_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, f, t, typ, now, vf, vt),
        )
    await _insert_memory(db, "mem")
    await db.conn.execute("INSERT INTO memory_entities (memory_id, entity_id) VALUES ('mem','keep'), ('mem','dup')")
    await db.conn.commit()

    service = MagicMock()
    service.db = db
    worker = SleepTimeWorker(settings=MagicMock(consolidation_threshold=0.9), memory_service=service)
    await worker._merge_entities("keep", "dup")

    async def rel(rid: str) -> dict:
        cursor = await db.conn.execute("SELECT * FROM relationships WHERE id = ?", (rid,))
        row = await cursor.fetchone()
        return dict(row) if row else {}

    # No relationship rows were deleted at all.
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM relationships") == 6
    # Other tenant untouched.
    assert (await rel("r_u2a"))["valid_to"] is None
    assert (await rel("r_u2b"))["valid_to"] is None
    # Newest currently-valid edge survives; the older duplicate is closed and points at it.
    survivor, closed = await rel("r_dup"), await rel("r_keep")
    assert survivor["from_entity_id"] == "keep" and survivor["valid_to"] is None
    assert closed["valid_to"] is not None and closed["superseded_by"] == "r_dup"
    # Historical edge kept as history (re-pointed, window unchanged).
    hist = await rel("r_hist")
    assert hist["from_entity_id"] == "keep" and hist["valid_to"] == "2025-01-01" and hist["superseded_by"] is None
    # Self-reference closed, not deleted.
    assert (await rel("r_self"))["valid_to"] is not None
    # Entity gone, memory link collapsed onto the survivor without a PK conflict.
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM entities WHERE id='dup'") == 0
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM memory_entities WHERE memory_id='mem'") == 1


async def test_entity_merge_failure_rolls_back_only_itself(db: Database) -> None:
    from remembra.services.sleep_time import SleepTimeWorker

    now = utcnow().isoformat()
    for eid in ("k", "d", "o"):
        await db.conn.execute(
            "INSERT INTO entities (id, user_id, canonical_name, type, created_at, updated_at)"
            " VALUES (?, 'u1', ?, 'person', ?, ?)",
            (eid, eid, now, now),
        )
    await db.conn.execute(
        "INSERT INTO relationships (id, from_entity_id, to_entity_id, type, created_at) VALUES ('r','d','o','x',?)",
        (now,),
    )
    await db.conn.execute("CREATE TRIGGER block_entity_delete BEFORE DELETE ON entities BEGIN SELECT RAISE(ABORT, 'nope'); END")
    await db.conn.commit()
    service = MagicMock()
    service.db = db
    worker = SleepTimeWorker(settings=MagicMock(consolidation_threshold=0.9), memory_service=service)
    await worker._merge_entities("k", "d")  # logs + swallows, but must roll back
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM relationships WHERE from_entity_id='d'") == 1
    assert await _observer_count(db.db_path, "SELECT COUNT(*) FROM entities") == 3
