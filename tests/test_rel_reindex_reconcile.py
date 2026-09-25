"""REL-9 rebuild/alias-free swap reindex and REL-10 reconcile.

Real SQLite + real in-process Qdrant (qdrant_client local mode) + a fake
embedder. Covers: rebuild-from-SQLite (disaster recovery), keyset paging over
multiple pages, source rows skipped, dimension change, pause on provider quota
+ resume, cancel + resume, restart -> interrupted -> resume, writes during
the job caught up, and drift report/repair.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from remembra.config import Settings
from remembra.core.provider_errors import ProviderErrorKind
from remembra.core.time import utcnow
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingProviderError
from remembra.storage.pending_embeddings import PendingEmbeddingQueue, PendingEmbeddingWorker
from remembra.storage.qdrant import QdrantStore
from remembra.storage.reconcile import reconcile
from remembra.storage.reindex import ReindexManager, apply_active_collection, get_active_collection


class FakeEmbedder:
    def __init__(self, dims: int = 4) -> None:
        self.dimensions = dims
        self.calls = 0
        self.fail_after: int | None = None
        self.fail_kind = ProviderErrorKind.QUOTA_EXHAUSTED
        self.reject: set[str] = set()
        self.on_call = None

    def _vec(self, text: str) -> list[float]:
        h = abs(hash(text))
        return [((h >> (i * 3)) % 97) / 97.0 + 0.01 for i in range(self.dimensions)]

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.on_call:
            await self.on_call(self.calls)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise EmbeddingProviderError("x", 429, kind=self.fail_kind)
        if any(t in self.reject for t in texts):
            raise EmbeddingProviderError("too long", 400, kind=ProviderErrorKind.BAD_REQUEST)
        return [self._vec(t) for t in texts]


@pytest.fixture()
async def env(tmp_path):
    settings = Settings(openai_api_key="t", embedding_dimensions=4, qdrant_collection="memories")
    db = Database(str(tmp_path / "ri.db"))
    await db.connect()
    await db.init_schema()
    qdrant = QdrantStore(settings)
    client = AsyncQdrantClient(location=":memory:")
    qdrant._client = client
    await qdrant.init_collection()
    manager = ReindexManager(db=db, qdrant=qdrant, embeddings=FakeEmbedder(4))
    await manager.init_schema()
    yield {"db": db, "qdrant": qdrant, "client": client, "manager": manager, "settings": settings}
    await qdrant.close()
    await db.close()


async def _save(db: Database, content: str, memory_type: str | None = None, user_id: str = "u1") -> str:
    mid = str(uuid.uuid4())
    await db.save_memory_metadata(
        memory_id=mid,
        user_id=user_id,
        project_id="default",
        content=content,
        extracted_facts=[content],
        metadata={"n": content},
        created_at=utcnow(),
        memory_type=memory_type,
    )
    return mid


async def _count(client: AsyncQdrantClient, name: str) -> int:
    return (await client.count(name, exact=True)).count


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------


async def test_rebuild_restores_lost_qdrant_from_sqlite_and_swaps(env) -> None:
    db, qdrant, client, manager = env["db"], env["qdrant"], env["client"], env["manager"]
    ids = [await _save(db, f"memory {i}") for i in range(120)]  # 3 keyset pages of 50
    source_id = await _save(db, "verbatim source text", memory_type="source")
    assert await _count(client, "memories") == 0  # "lost" vector store

    job = await manager.start_reindex("openai", "m", "openai", "m")
    await manager.wait()
    assert job.status == "completed", job.error
    assert job.processed == 120 and job.total_memories == 120
    assert qdrant.collection_name == job.target_collection != "memories"
    assert await get_active_collection(db) == job.target_collection
    assert await _count(client, job.target_collection) == 120
    assert await client.collection_exists("memories")  # old collection kept for rollback

    point = await qdrant.get_by_id(ids[7])
    assert point["content"] == "memory 7" and point["metadata"] == {"n": "memory 7"}
    assert await qdrant.get_by_id(source_id) is None  # source rows never get vectors

    # Next boot follows the persisted pointer.
    fresh = QdrantStore(env["settings"])
    assert await apply_active_collection(db, fresh) == job.target_collection


async def test_rebuild_supports_dimension_change(env) -> None:
    db, client, manager = env["db"], env["client"], env["manager"]
    for i in range(5):
        await _save(db, f"m{i}")
    job = await manager.start_reindex("openai", "small", "voyage", "voyage-3", target_embeddings=FakeEmbedder(6))
    await manager.wait()
    assert job.status == "completed"
    info = await client.get_collection(job.target_collection)
    assert info.config.params.vectors.size == 6
    assert (await client.get_collection("memories")).config.params.vectors.size == 4


async def test_rebuild_pauses_on_quota_and_resumes_without_duplicates(env) -> None:
    db, client, manager = env["db"], env["client"], env["manager"]
    for i in range(130):
        await _save(db, f"fact {i}")
    embedder = FakeEmbedder(4)
    embedder.fail_after = 1  # first batch of 50 succeeds, then quota runs out
    job = await manager.start_reindex("openai", "m", "openai", "m", target_embeddings=embedder)
    await manager.wait()
    assert job.status == "paused" and "quota_exhausted" in (job.error or "")
    assert job.processed == 50 and job.cursor is not None
    assert (await manager.get_status(job.id)).cursor == job.cursor  # persisted

    embedder.fail_after = None
    resumed = await manager.resume(job.id, target_embeddings=embedder)
    await manager.wait()
    assert resumed.status == "completed"
    assert resumed.processed == 130
    assert await _count(client, resumed.target_collection) == 130


async def test_restart_marks_running_job_interrupted_and_resume_completes(env) -> None:
    db, qdrant, manager = env["db"], env["qdrant"], env["manager"]
    for i in range(60):
        await _save(db, f"row {i}")
    embedder = FakeEmbedder(4)

    async def cancel_mid_job(call: int) -> None:
        if call == 1:
            await manager.cancel()

    embedder.on_call = cancel_mid_job
    job = await manager.start_reindex("openai", "m", "openai", "m", target_embeddings=embedder)
    await manager.wait()
    assert job.status == "cancelled" and job.processed == 50

    # Simulate a crash mid-run: status left as 'running' in SQLite, new process boots.
    await db.conn.execute("UPDATE reindex_jobs SET status = 'running' WHERE id = ?", (job.id,))
    await db.conn.commit()
    rebooted = ReindexManager(db=db, qdrant=qdrant, embeddings=embedder)
    await rebooted.init_schema()
    assert (await rebooted.get_status(job.id)).status == "interrupted"
    embedder.on_call = None
    resumed = await rebooted.resume(job.id, target_embeddings=embedder)
    await rebooted.wait()
    assert resumed.status == "completed" and resumed.processed == 60


async def test_writes_during_rebuild_are_caught_up(env) -> None:
    db, qdrant, manager = env["db"], env["qdrant"], env["manager"]
    for i in range(10):
        await _save(db, f"before {i}")
    embedder = FakeEmbedder(4)
    late: list[str] = []

    async def concurrent_store(call: int) -> None:
        if call == 1:
            late.append(await _save(db, "stored while the rebuild was running"))

    embedder.on_call = concurrent_store
    job = await manager.start_reindex("openai", "m", "openai", "m", target_embeddings=embedder)
    await manager.wait()
    assert job.status == "completed"
    point = await qdrant.get_by_id(late[0])  # qdrant now points at the new collection
    assert point is not None and point["content"] == "stored while the rebuild was running"


async def test_bad_input_row_is_skipped_not_fatal(env) -> None:
    db, manager = env["db"], env["manager"]
    for i in range(4):
        await _save(db, f"ok {i}")
    await _save(db, "poison row")
    embedder = FakeEmbedder(4)
    embedder.reject = {"poison row"}
    job = await manager.start_reindex("openai", "m", "openai", "m", target_embeddings=embedder)
    await manager.wait()
    assert job.status == "completed"
    assert job.processed == 4 and job.failed == 1


async def test_scoped_jobs_run_in_place_and_skip_source_rows(env) -> None:
    db, qdrant, client, manager = env["db"], env["qdrant"], env["client"], env["manager"]
    embedder = FakeEmbedder(4)
    mid = await _save(db, "mine", user_id="u9")
    await _save(db, "mine too, but verbatim", memory_type="source", user_id="u9")
    from remembra.storage.memory_rows import memory_from_row

    m = memory_from_row(await db.get_memory(mid))
    m.embedding = [0.5, 0.5, 0.5, 0.5]
    await qdrant.upsert(m)

    with pytest.raises(ValueError, match="global"):
        await manager.start_reindex("a", "b", "c", "d", user_id="u9", mode="rebuild")
    job = await manager.start_reindex("a", "b", "c", "d", user_id="u9", target_embeddings=embedder)
    await manager.wait()
    assert job.mode == "in_place" and job.status == "completed"
    assert job.processed == 1 and embedder.calls == 1
    assert qdrant.collection_name == "memories"
    points = await client.retrieve("memories", ids=[mid], with_vectors=True)
    assert points[0].vector != [0.5, 0.5, 0.5, 0.5]


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


async def test_reconcile_reports_and_repairs_drift(env) -> None:
    db, qdrant, manager = env["db"], env["qdrant"], env["manager"]
    from remembra.storage.memory_rows import memory_from_row

    embedder = FakeEmbedder(4)
    healthy = await _save(db, "healthy memory")
    no_vector = await _save(db, "memory whose vector write failed")
    no_fts = await _save(db, "memory missing from keyword index")
    await _save(db, "verbatim source", memory_type="source")  # SQLite-only by design

    for mid in (healthy, no_fts):
        m = memory_from_row(await db.get_memory(mid))
        m.embedding = await embedder.embed(m.content)
        await qdrant.upsert(m)
    for mid in (healthy, no_vector):
        row = await db.get_memory(mid)
        await db.index_memory_fts(mid, "u1", "default", row["content"])
    orphan_vector = str(uuid.uuid4())
    ghost = memory_from_row(
        {"id": orphan_vector, "user_id": "u1", "content": "vector with no row", "created_at": utcnow().isoformat()}
    )
    ghost.embedding = await embedder.embed(ghost.content)
    await qdrant.upsert(ghost)
    await db.index_memory_fts("fts-orphan", "u1", "default", "stale keyword row")

    report = await reconcile(db, qdrant)
    assert (report.missing_vectors, report.orphan_vectors, report.missing_fts, report.orphan_fts) == (1, 1, 1, 1)
    assert report.samples["missing_vectors"] == [no_vector]
    assert report.samples["orphan_vectors"] == [orphan_vector]
    assert report.repaired is False

    queue = PendingEmbeddingQueue(db)
    report = await reconcile(db, qdrant, repair=True, queue=queue)
    assert (report.requeued, report.fts_indexed, report.fts_removed) == (1, 1, 1)
    assert (await queue.get(no_vector)).reason == "reconcile_missing_vector"

    await PendingEmbeddingWorker(queue, db, qdrant, embedder).run_once()
    after = await reconcile(db, qdrant)
    assert (after.missing_vectors, after.missing_fts, after.orphan_fts) == (0, 0, 0)
    assert after.orphan_vectors == 1  # report-only: may be the only copy of a memory
    assert await qdrant.get_by_id(orphan_vector) is not None

    scoped = await reconcile(db, qdrant, user_id="someone-else")
    assert scoped.sqlite_rows == 0 and scoped.qdrant_points == 0
    del manager


async def test_reconcile_cli_reports_json(tmp_path, monkeypatch, capsys) -> None:
    import json

    import remembra.config
    from remembra.storage import reconcile as reconcile_module

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "cli_test")
    monkeypatch.setenv("REMEMBRA_EMBEDDING_DIMENSIONS", "4")
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")
    await local.create_collection("cli_test", vectors_config={"size": 4, "distance": "Cosine"})

    async def local_client(self):  # noqa: ANN001
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    seed = Database(str(tmp_path / "cli.db"))
    await seed.connect()
    await seed.init_schema()
    await _save(seed, "needs a vector")
    await seed.close()

    assert await reconcile_module._main(["--repair"]) == 0
    captured = capsys.readouterr().out
    # In-process, loggers cached by earlier tests may still print to stdout;
    # decode the JSON report object wherever it starts.
    out, _ = json.JSONDecoder().raw_decode(captured[captured.index("{\n") :])
    assert out["missing_vectors"] == 1 and out["requeued"] == 1 and out["repaired"] is True
