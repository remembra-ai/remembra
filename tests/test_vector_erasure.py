"""Deletion ordering and crash recovery with real SQLite, Qdrant and HTTP auth.

Only the embedding provider is faked. No customer data or paid calls.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import pytest
from qdrant_client import models as qm

from remembra.account.erasure import AccountEraser
from remembra.core.time import utcnow
from remembra.extraction import background
from remembra.storage.database import Database
from remembra.storage.memory_rows import memory_from_row
from remembra.storage.pending_embeddings import PendingEmbeddingQueue, PendingEmbeddingWorker
from remembra.storage.vector_mutations import VectorErasureReconciler, vector_mutation_lock
from tests._ret_harness import quota_error
from tests.test_ret_api_degraded import _app


@pytest.fixture
async def stack(tmp_path):
    ctx, h, service, qdrant, emb, headers = await _app(tmp_path)
    try:
        yield h, service, qdrant, emb, headers
    finally:
        await background.drain(timeout=5)
        await qdrant.close()
        await ctx.__aexit__(None, None, None)


async def _pending(stack):
    h, service, qdrant, emb, headers = stack
    emb.fail = quota_error()
    response = await h.client.post(
        "/api/v1/memories",
        headers=headers,
        json={"content": "Synthetic erase-me decision", "project_id": "p", "skip_extraction": True},
    )
    assert response.status_code == 201 and response.json()["status"] == "pending", response.text
    emb.fail = None
    return response.json()["id"]


@pytest.mark.parametrize("pause", ["embedding", "upsert"])
@pytest.mark.parametrize("target", ["memory", "project", "all", "account", "ttl"])
async def test_delete_orders_against_dispatched_worker(stack, monkeypatch, pause, target):
    h, service, qdrant, emb, headers = stack
    mid = await _pending(stack)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = emb.embed if pause == "embedding" else qdrant.upsert

    async def paused(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(emb if pause == "embedding" else qdrant, "embed" if pause == "embedding" else "upsert", paused)
    other = Database(h.db.db_path)
    await other.connect()
    worker = asyncio.create_task(PendingEmbeddingWorker(PendingEmbeddingQueue(other), other, qdrant, emb).run_once())
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 3)

        async def delete():
            if target == "account":
                await AccountEraser(h.db, qdrant).erase("tenant-a")
            elif target == "ttl":
                await h.db.conn.execute(
                    "UPDATE memories SET expires_at = ? WHERE id = ?", ((utcnow() - timedelta(days=1)).isoformat(), mid)
                )
                await h.db.conn.commit()
                assert await service.cleanup_expired("tenant-a", "p") == 1
            else:
                params = (
                    {"memory_id": mid}
                    if target == "memory"
                    else {"project_id": "p"}
                    if target == "project"
                    else {"all_memories": "true"}
                )
                response = await h.client.delete("/api/v1/memories", params=params, headers=headers)
                assert response.status_code == 200, response.text

        deletion = asyncio.create_task(delete())
        if pause == "upsert":
            # Deletion cannot report success while the local writer still runs.
            await asyncio.sleep(0.03)
            assert not deletion.done()
            resume.set()
        await asyncio.wait_for(deletion, 3)
        resume.set()
        await asyncio.wait_for(worker, 3)
        assert await h.db.get_memory(mid) is None
        assert not await qdrant.existing_ids([mid])
        cursor = await h.db.conn.execute("SELECT COUNT(*) FROM memories_fts WHERE id = ?", (mid,))
        assert (await cursor.fetchone())[0] == 0
        cursor = await h.db.conn.execute("SELECT COUNT(*) FROM vector_erasure_markers WHERE memory_id = ?", (mid,))
        assert (await cursor.fetchone())[0] == 1
    finally:
        resume.set()
        await asyncio.gather(worker, *([deletion] if deletion else []), return_exceptions=True)
        await other.close()


async def test_marker_survives_restart_and_repeated_late_remote_writes(stack, monkeypatch):
    h, service, qdrant, emb, headers = stack
    mid = await _pending(stack)
    row = await h.db.get_memory(mid)
    memory = memory_from_row(row)
    memory.embedding = await emb.embed(memory.content)
    await qdrant.upsert(memory)
    await h.db.delete_memory(mid, user_id="tenant-a")
    other = Database(h.db.db_path)
    await other.connect()
    try:
        recovery = VectorErasureReconciler(other, qdrant)
        original = qdrant.delete_ids_everywhere

        async def unavailable(ids):
            raise ConnectionError("synthetic transport failure")

        monkeypatch.setattr(qdrant, "delete_ids_everywhere", unavailable)
        with pytest.raises(ConnectionError):
            await recovery.run_once()
        assert await qdrant.existing_ids([mid]) == {mid}
        monkeypatch.setattr(qdrant, "delete_ids_everywhere", original)
        assert await recovery.run_once() == 1
        assert not await qdrant.existing_ids([mid])
        # A request sent before a process died may commit after an earlier sweep.
        await qdrant.upsert(memory)
        assert await VectorErasureReconciler(other, qdrant).run_once() == 1
        assert not await qdrant.existing_ids([mid])
    finally:
        await other.close()


async def test_reconciliation_keeps_live_and_unrelated_collection_points(stack):
    h, service, qdrant, emb, headers = stack
    mid = await _pending(stack)
    row = await h.db.get_memory(mid)
    memory = memory_from_row(row)
    memory.embedding = await emb.embed(memory.content)
    client = await qdrant._get_client()
    rollback = qdrant.collection_name + "__rb_test_previous"
    for name in (rollback, "unrelated_app"):
        await client.create_collection(
            name, vectors_config=qm.VectorParams(size=len(memory.embedding), distance=qm.Distance.COSINE)
        )
        await client.upsert(name, points=[qm.PointStruct(id=mid, vector=memory.embedding, payload={"user_id": "tenant-a"})])
    await qdrant.upsert(memory)
    await h.db.delete_memory(mid)
    await VectorErasureReconciler(h.db, qdrant).run_once()
    assert not await qdrant.existing_ids([mid])
    assert (await client.count(rollback)).count == 0
    assert (await client.count("unrelated_app")).count == 1
    # Restore the canonical row and its vector; the old marker must not erase it.
    await h.db.save_memory_metadata(
        memory_id=mid,
        user_id=row["user_id"],
        project_id=row["project_id"],
        content=row["content"],
        extracted_facts=[],
        metadata={},
        created_at=utcnow(),
    )
    await qdrant.upsert(memory)
    assert await VectorErasureReconciler(h.db, qdrant).run_once() == 0
    assert await qdrant.existing_ids([mid]) == {mid}


async def test_separate_connections_share_mutation_lock_and_cancel_releases_it(stack):
    h, *_ = stack
    other = Database(h.db.db_path)
    await other.connect()
    entered = asyncio.Event()

    async def contender():
        async with vector_mutation_lock(other):
            entered.set()

    try:
        async with vector_mutation_lock(h.db):
            task = asyncio.create_task(contender())
            await asyncio.sleep(0.03)
            assert not entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(contender(), 3)
        assert entered.is_set()
    finally:
        await other.close()


async def test_process_death_releases_shared_filesystem_lock(stack):
    h, *_ = stack
    code = """
import asyncio, sys
from remembra.storage.database import Database
from remembra.storage.vector_mutations import vector_mutation_lock
async def main():
    db = Database(sys.argv[1])
    print('waiting', flush=True)
    async with vector_mutation_lock(db):
        print('acquired', flush=True)
        await asyncio.sleep(60)
asyncio.run(main())
"""
    process = None
    try:
        async with vector_mutation_lock(h.db):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code, h.db.db_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            assert await asyncio.wait_for(process.stdout.readline(), 3) == b"waiting\n"
            acquired = asyncio.create_task(process.stdout.readline())
            await asyncio.sleep(0.03)
            assert not acquired.done()
        assert await asyncio.wait_for(acquired, 3) == b"acquired\n"
        process.kill()
        await asyncio.wait_for(process.wait(), 3)
        async with asyncio.timeout(3):
            async with vector_mutation_lock(h.db):
                pass
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_authorized_store_cannot_recreate_account_after_erase(stack, monkeypatch):
    h, service, qdrant, emb, headers = stack
    entered, resume = asyncio.Event(), asyncio.Event()
    original = emb.embed

    async def paused(text):
        entered.set()
        await resume.wait()
        return await original(text)

    monkeypatch.setattr(emb, "embed", paused)
    task = asyncio.create_task(
        h.client.post(
            "/api/v1/memories",
            headers=headers,
            json={"content": "Synthetic request authorized before erasure", "project_id": "p", "skip_extraction": True},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await AccountEraser(h.db, qdrant).erase("tenant-a")
        resume.set()
        response = await asyncio.wait_for(task, 3)
        assert response.status_code == 400 and response.json()["detail"] == "Account has been erased"
        assert not await h.db.list_memories("tenant-a")
        assert (await (await qdrant._get_client()).count(qdrant.collection_name)).count == 0
        # The same statement-level fence covers bulk imports and survives restart.
        other = Database(h.db.db_path)
        await other.connect()
        try:
            with pytest.raises(ValueError, match="Account has been erased"):
                await other.save_memories_bulk(
                    [
                        {
                            "id": "recreated",
                            "user_id": "tenant-a",
                            "project_id": "p",
                            "content": "synthetic",
                            "created_at": utcnow(),
                        },
                        {
                            "id": "unaffected",
                            "user_id": "tenant-b",
                            "project_id": "p",
                            "content": "synthetic",
                            "created_at": utcnow(),
                        },
                    ]
                )
            assert await other.get_memory("recreated") is None and await other.get_memory("unaffected") is None
        finally:
            await other.close()
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_recovery_boots_with_embedding_worker_disabled(tmp_path, monkeypatch):
    import remembra.config
    import remembra.main as main
    from qdrant_client import AsyncQdrantClient

    from remembra.models.memory import Memory
    from remembra.storage.qdrant import QdrantStore

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'disabled-worker.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("REMEMBRA_EMBEDDING_DIMENSIONS", "4")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "erasure_boot")
    monkeypatch.setenv("REMEMBRA_PENDING_EMBEDDINGS_WORKER_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_PRE_MIGRATION_BACKUP", "false")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setattr(remembra.config, "_settings", None)

    client = AsyncQdrantClient(location=":memory:")

    async def local_client(self):
        return client

    original = VectorErasureReconciler.run_forever

    async def quick_recovery(self):
        await original(self, poll_seconds=0.01)

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    monkeypatch.setattr(VectorErasureReconciler, "run_forever", quick_recovery)
    app = main.create_app()
    async with app.router.lifespan_context(app):
        assert "pending-embedding-worker" not in app.state.tasks.names()
        assert "vector_erasure_reconciliation" in app.state.tasks.names()
        memory = Memory(user_id="synthetic", content="disposable recovery sample", embedding=[0.1] * 4)
        await app.state.db.save_memory_metadata(
            memory_id=memory.id,
            user_id=memory.user_id,
            project_id=memory.project_id,
            content=memory.content,
            extracted_facts=[],
            metadata={},
            created_at=memory.created_at,
        )
        await app.state.qdrant.upsert(memory)
        await app.state.db.delete_memory(memory.id)
        for _ in range(100):
            if not await app.state.qdrant.existing_ids([memory.id]):
                break
            await asyncio.sleep(0.01)
        assert not await app.state.qdrant.existing_ids([memory.id])
    monkeypatch.setattr(remembra.config, "_settings", None)


async def test_markers_do_not_retain_sqlite_only_identifiers(stack):
    h, *_ = stack
    identifier = "synthetic-user-identifier"
    await h.db.save_memory_metadata(
        memory_id=identifier,
        user_id=identifier,
        project_id="p",
        content="disposable SQLite-only row",
        extracted_facts=[],
        metadata={},
        created_at=utcnow(),
    )
    await h.db.delete_memory(identifier)
    cursor = await h.db.conn.execute("SELECT COUNT(*) FROM vector_erasure_markers")
    assert (await cursor.fetchone())[0] == 0
