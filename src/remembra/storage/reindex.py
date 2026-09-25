"""Re-indexing: rebuild the vector collection from SQLite (REL-9).

The old implementation overwrote vectors in place (``update_vectors``), paged
with OFFSET while rows changed underneath it, re-embedded verbatim source rows
that are never supposed to have vectors, kept job state in memory, and ran as
an untracked task. During the job the collection held a mix of old- and
new-model vectors, and a dimension change broke it outright. It also could not
rebuild a lost Qdrant from SQLite.

``mode="rebuild"`` (default for global jobs):

1. Creates a NEW physical collection sized for the target embedding model.
2. Walks ``memories`` with keyset pagination (``id > cursor``), skipping source
   rows, embeds in batches with the target embedder, and upserts the FULL
   payload (content, facts, metadata, entities, timestamps) — so it doubles as
   disaster recovery: an empty/lost Qdrant is rebuilt from SQLite.
3. Persists ``cursor``/counters after every batch in ``reindex_jobs``. A job cut
   short (crash, deploy, provider outage) is ``interrupted``/``paused`` and
   :meth:`ReindexManager.resume` continues from the cursor.
4. Catch-up pass over rows updated since the job started, then an atomic swap:
   ``QdrantStore.collection_name`` points at the new collection and the choice
   is persisted in ``vector_store_state`` (read at boot by
   :func:`apply_active_collection`). A post-swap pass re-upserts anything
   written during the swap. The old collection is kept (never deleted) for
   rollback; its name is recorded on the job.

``mode="in_place"`` keeps the legacy vector-only update for user/project-scoped
jobs (keyset paging, source rows skipped).

Wiring note (wave 2 / SEC-4): the embeddings switch endpoint currently switches
the live provider *before* starting the job, so live traffic already uses the
new model against the old collection until the swap. For a clean cut-over,
pass ``target_embeddings`` (a separate EmbeddingService for the new model) and
switch the live service in ``on_swap``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from remembra.storage.memory_rows import is_source_row, memory_from_row

logger = logging.getLogger(__name__)

ACTIVE_COLLECTION_KEY = "active_collection"


# ---------------------------------------------------------------------------
# Active collection pointer
# ---------------------------------------------------------------------------


async def _ensure_state_table(db: Any) -> None:
    await db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_store_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    await db.conn.commit()


async def get_active_collection(db: Any) -> str | None:
    """The collection the app should use, if a rebuild has swapped it."""
    await _ensure_state_table(db)
    cursor = await db.conn.execute("SELECT value FROM vector_store_state WHERE key = ?", (ACTIVE_COLLECTION_KEY,))
    row = await cursor.fetchone()
    return str(row[0]) if row else None


async def set_active_collection(db: Any, name: str) -> None:
    await _ensure_state_table(db)
    await db.conn.execute(
        """
        INSERT INTO vector_store_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (ACTIVE_COLLECTION_KEY, name, datetime.now(UTC).isoformat()),
    )
    await db.conn.commit()


async def apply_active_collection(db: Any, qdrant: Any) -> str:
    """Point ``qdrant`` at the persisted active collection (call before init_collection)."""
    active = await get_active_collection(db)
    if active:
        qdrant.collection_name = active
    return str(qdrant.collection_name)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:40] or "model"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ReindexJob:
    """Represents a re-indexing job."""

    id: str = field(default_factory=lambda: f"reindex_{uuid4().hex[:12]}")
    old_provider: str = ""
    old_model: str = ""
    new_provider: str = ""
    new_model: str = ""
    total_memories: int = 0
    processed: int = 0
    failed: int = 0
    status: str = "pending"  # pending | running | completed | failed | cancelled | paused | interrupted
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    mode: str = "rebuild"  # rebuild | in_place
    source_collection: str | None = None
    target_collection: str | None = None
    cursor: str | None = None  # last memory id processed (keyset)
    phase: str = "copy"  # copy | catchup | swapped | done
    user_id: str | None = None
    project_id: str | None = None


_JOB_COLUMNS = (
    "id, old_provider, old_model, new_provider, new_model, total_memories, processed, failed, "
    "status, started_at, completed_at, error, mode, source_collection, target_collection, cursor, "
    "phase, user_id, project_id"
)


def _job_from_row(row: Any) -> ReindexJob:
    return ReindexJob(
        id=row[0],
        old_provider=row[1],
        old_model=row[2],
        new_provider=row[3],
        new_model=row[4],
        total_memories=row[5] or 0,
        processed=row[6] or 0,
        failed=row[7] or 0,
        status=row[8],
        started_at=row[9],
        completed_at=row[10],
        error=row[11],
        mode=row[12] or "in_place",
        source_collection=row[13],
        target_collection=row[14],
        cursor=row[15],
        phase=row[16] or "copy",
        user_id=row[17],
        project_id=row[18],
    )


class ProviderUnavailable(Exception):
    """Embedding provider can't serve (quota/rate limit/outage): pause the job."""


# ---------------------------------------------------------------------------
# Reindex Manager
# ---------------------------------------------------------------------------


class ReindexManager:
    """Manages background re-indexing of memory embeddings.

    Args:
        db: The application's Database instance.
        qdrant: The live QdrantStore.
        embeddings: The live EmbeddingService.
    """

    BATCH_SIZE = 50

    def __init__(self, db: Any, qdrant: Any, embeddings: Any) -> None:
        self._db = db
        self._qdrant = qdrant
        self._embeddings = embeddings
        self._current_job: ReindexJob | None = None
        self._cancel_requested = False
        self._task: asyncio.Task[Any] | None = None

    async def init_schema(self) -> None:
        """Create/upgrade the reindex_jobs table; mark jobs cut off by a restart."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS reindex_jobs (
                id TEXT PRIMARY KEY,
                old_provider TEXT NOT NULL,
                old_model TEXT NOT NULL,
                new_provider TEXT NOT NULL,
                new_model TEXT NOT NULL,
                total_memories INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT,
                error TEXT
            );
        """)
        for column in (
            "mode TEXT",
            "source_collection TEXT",
            "target_collection TEXT",
            "cursor TEXT",
            "phase TEXT",
            "user_id TEXT",
            "project_id TEXT",
        ):
            try:
                await self._db.conn.execute(f"ALTER TABLE reindex_jobs ADD COLUMN {column}")
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        await self._db.conn.execute(
            "UPDATE reindex_jobs SET status = 'interrupted' WHERE status = 'running'",
        )
        await self._db.conn.commit()
        await _ensure_state_table(self._db)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def start_reindex(
        self,
        old_provider: str,
        old_model: str,
        new_provider: str,
        new_model: str,
        user_id: str | None = None,
        project_id: str | None = None,
        *,
        mode: str | None = None,
        target_embeddings: Any = None,
        on_swap: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> ReindexJob:
        """Start a re-indexing job (runs as a tracked background task).

        Global jobs default to ``rebuild``; user/project-scoped jobs can only
        run ``in_place`` (a scoped rebuild would drop everyone else's vectors
        from the new collection).
        """
        if self._current_job and self._current_job.status == "running":
            raise RuntimeError("A re-indexing job is already running")
        scoped = user_id is not None or project_id is not None
        mode = mode or ("in_place" if scoped else "rebuild")
        if mode == "rebuild" and scoped:
            raise ValueError("rebuild mode is global; use mode='in_place' for scoped jobs")
        if mode not in ("rebuild", "in_place"):
            raise ValueError(f"unknown reindex mode: {mode}")

        total = await self._count_memories(user_id, project_id)
        started = datetime.now(UTC).isoformat()
        job = ReindexJob(
            old_provider=old_provider,
            old_model=old_model,
            new_provider=new_provider,
            new_model=new_model,
            total_memories=total,
            status="running",
            started_at=started,
            mode=mode,
            source_collection=self._qdrant.collection_name,
            user_id=user_id,
            project_id=project_id,
        )
        if mode == "rebuild":
            base = re.sub(r"__rb_.*$", "", self._qdrant.collection_name)
            job.target_collection = f"{base}__rb_{_slug(new_model)}_{uuid4().hex[:8]}"
        self._current_job = job
        self._cancel_requested = False
        await self._save_job(job)
        self._launch(job, target_embeddings, on_swap)
        logger.info(
            "Reindex started: id=%s mode=%s total=%d %s/%s -> %s/%s target=%s",
            job.id,
            mode,
            total,
            old_provider,
            old_model,
            new_provider,
            new_model,
            job.target_collection,
        )
        return job

    async def resume(
        self,
        job_id: str,
        *,
        target_embeddings: Any = None,
        on_swap: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> ReindexJob:
        """Continue an interrupted/paused/failed job from its persisted cursor."""
        if self._current_job and self._current_job.status == "running":
            raise RuntimeError("A re-indexing job is already running")
        job = await self.get_status(job_id)
        if job is None:
            raise LookupError(job_id)
        if job.status not in ("interrupted", "paused", "failed", "cancelled"):
            raise RuntimeError(f"job {job_id} is {job.status}; only interrupted/paused/failed/cancelled resume")
        job.status = "running"
        job.error = None
        job.completed_at = None
        self._current_job = job
        self._cancel_requested = False
        await self._save_job(job)
        self._launch(job, target_embeddings, on_swap)
        return job

    async def wait(self) -> None:
        """Await the running job's task (tests / CLI)."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def cancel(self) -> bool:
        """Request cancellation of the running reindex job (resumable later)."""
        if self._current_job and self._current_job.status == "running":
            self._cancel_requested = True
            logger.info("Reindex cancellation requested: %s", self._current_job.id)
            return True
        return False

    async def get_status(self, job_id: str | None = None) -> ReindexJob | None:
        """Get the status of a reindex job (defaults to current)."""
        if job_id is None and self._current_job:
            return self._current_job
        if job_id is None:
            return None
        cursor = await self._db.conn.execute(f"SELECT {_JOB_COLUMNS} FROM reindex_jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return _job_from_row(row) if row else None

    async def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent reindex jobs."""
        cursor = await self._db.conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM reindex_jobs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        jobs = [_job_from_row(r) for r in await cursor.fetchall()]
        return [
            {
                "id": j.id,
                "old_provider": j.old_provider,
                "old_model": j.old_model,
                "new_provider": j.new_provider,
                "new_model": j.new_model,
                "total_memories": j.total_memories,
                "processed": j.processed,
                "failed": j.failed,
                "status": j.status,
                "started_at": j.started_at,
                "completed_at": j.completed_at,
                "mode": j.mode,
                "source_collection": j.source_collection,
                "target_collection": j.target_collection,
                "phase": j.phase,
            }
            for j in jobs
        ]

    # -----------------------------------------------------------------------
    # Internal processing
    # -----------------------------------------------------------------------

    def _launch(self, job: ReindexJob, target_embeddings: Any, on_swap: Any) -> None:
        from remembra.core.tasks import get_task_registry

        self._task = get_task_registry().spawn(
            self._run(job, target_embeddings or self._embeddings, on_swap),
            name=f"reindex:{job.id}",
        )

    async def _run(self, job: ReindexJob, embedder: Any, on_swap: Any) -> None:
        try:
            if job.mode == "rebuild":
                await self._run_rebuild(job, embedder, on_swap)
            else:
                await self._run_in_place(job, embedder)
            if job.status == "running":
                job.status = "completed"
                job.phase = "done"
        except ProviderUnavailable as e:
            job.status = "paused"
            job.error = f"embedding provider unavailable: {e}"
            logger.warning("Reindex %s paused at cursor=%s: %s", job.id, job.cursor, e)
        except asyncio.CancelledError:
            job.status = "interrupted"
            raise
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error("Reindex job failed: %s", e)
        finally:
            if job.status != "running":
                job.completed_at = datetime.now(UTC).isoformat()
            await self._save_job(job)
            logger.info(
                "Reindex %s: processed=%d failed=%d status=%s phase=%s",
                job.id,
                job.processed,
                job.failed,
                job.status,
                job.phase,
            )

    def _target_store(self, name: str) -> Any:
        target = copy.copy(self._qdrant)  # shares the client + encryptor
        target.collection_name = name
        return target

    async def _ensure_target_collection(self, job: ReindexJob, dims: int) -> None:
        assert job.target_collection
        client = await self._qdrant._get_client()
        if not await client.collection_exists(job.target_collection):
            await self._qdrant.create_collection(job.target_collection, dims)
            logger.info("Reindex %s created collection %s (%d dims)", job.id, job.target_collection, dims)

    async def _run_rebuild(self, job: ReindexJob, embedder: Any, on_swap: Any) -> None:
        assert job.target_collection
        target = self._target_store(job.target_collection)

        if job.phase == "copy":
            await self._copy_pass(job, embedder, target, since=None)
            if self._cancel_requested:
                job.status = "cancelled"
                return
            job.phase = "catchup"
            job.cursor = None
            await self._save_job(job)

        if job.phase == "catchup":
            # Rows written/updated while the copy pass ran.
            catchup_started = datetime.now(UTC).isoformat()
            await self._copy_pass(job, embedder, target, since=job.started_at, count=False)
            if self._cancel_requested:
                job.status = "cancelled"
                return
            if job.total_memories and not job.processed and job.failed:
                raise RuntimeError("every memory failed to embed; refusing to swap to an empty collection")

            # Atomic swap: in-process pointer + persisted pointer for next boot.
            self._qdrant.collection_name = job.target_collection
            await set_active_collection(self._db, job.target_collection)
            job.phase = "swapped"
            job.cursor = None
            await self._save_job(job)
            logger.info(
                "Reindex %s swapped active collection %s -> %s (old kept for rollback)",
                job.id,
                job.source_collection,
                job.target_collection,
            )
            if on_swap is not None:
                maybe = on_swap(job.target_collection)
                if asyncio.iscoroutine(maybe):
                    await maybe
            # Anything that landed in the old collection during the swap.
            await self._copy_pass(job, embedder, target, since=catchup_started, count=False)

    async def _copy_pass(self, job: ReindexJob, embedder: Any, target: Any, since: str | None, count: bool = True) -> None:
        from remembra.storage.embeddings import EmbeddingProviderError

        while not self._cancel_requested:
            rows = await self._fetch_rows(job.cursor, self.BATCH_SIZE, job.user_id, job.project_id, since)
            if not rows:
                break
            memories = []
            for row in rows:
                if is_source_row(row):
                    continue
                memories.append(memory_from_row(row, await self._db.get_memory_entities(row["id"])))

            if memories:
                try:
                    vectors = await embedder.embed_batch([m.content for m in memories])
                except EmbeddingProviderError as e:
                    if e.circuit_open or e.kind.value in ("quota_exhausted", "rate_limited", "unavailable", "auth"):
                        raise ProviderUnavailable(e.kind.value) from e
                    vectors = await self._embed_individually(job, embedder, memories, count)
                ok = [(m, v) for m, v in zip(memories, vectors, strict=True) if v is not None]
                for m, v in ok:
                    m.embedding = v
                if ok:
                    await self._ensure_target_collection(job, len(ok[0][1]))
                    await target.upsert_batch([m for m, _ in ok])
                if count:
                    job.processed += len(ok)

            job.cursor = rows[-1]["id"]
            await self._save_job(job)

        if job.target_collection:
            await self._ensure_target_collection(job, int(embedder.dimensions))

    async def _embed_individually(self, job: ReindexJob, embedder: Any, memories: list[Any], count: bool = True) -> list[Any]:
        """A batch was rejected as bad input: find and skip the offending rows."""
        from remembra.storage.embeddings import EmbeddingProviderError

        vectors: list[Any] = []
        for m in memories:
            try:
                vectors.append((await embedder.embed_batch([m.content]))[0])
            except EmbeddingProviderError as e:
                if e.circuit_open or e.kind.value != "bad_request":
                    raise ProviderUnavailable(e.kind.value) from e
                if count:
                    job.failed += 1
                logger.warning("Reindex %s: memory %s rejected by provider", job.id, m.id)
                vectors.append(None)
        return vectors

    async def _run_in_place(self, job: ReindexJob, embedder: Any) -> None:
        from remembra.storage.embeddings import EmbeddingProviderError

        while not self._cancel_requested:
            rows = await self._fetch_rows(job.cursor, self.BATCH_SIZE, job.user_id, job.project_id, None)
            if not rows:
                break
            for row in rows:
                if self._cancel_requested:
                    break
                if is_source_row(row):
                    job.cursor = row["id"]
                    continue
                try:
                    vector = await embedder.embed(row["content"])
                    await self._qdrant.upsert_vector(row["id"], vector)
                    job.processed += 1
                except EmbeddingProviderError as e:
                    if e.circuit_open or e.kind.value != "bad_request":
                        raise ProviderUnavailable(e.kind.value) from e
                    job.failed += 1
                except Exception as e:
                    job.failed += 1
                    logger.warning("Reindex failed for memory %s: %s", row["id"], e)
                job.cursor = row["id"]
            await self._save_job(job)
        if self._cancel_requested:
            job.status = "cancelled"

    async def _fetch_rows(
        self,
        after_id: str | None,
        limit: int,
        user_id: str | None,
        project_id: str | None,
        updated_since: str | None,
    ) -> list[dict[str, Any]]:
        """Keyset page of memory rows ordered by id (stable while rows change)."""
        conditions = []
        params: list[Any] = []
        if after_id is not None:
            conditions.append("id > ?")
            params.append(after_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if updated_since:
            # ISO strings; stored naive-UTC, job timestamps aware-UTC -> compare prefix-safe
            conditions.append("updated_at >= ?")
            params.append(updated_since[:19])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.conn.execute(
            f"SELECT * FROM memories {where} ORDER BY id LIMIT ?",
            (*params, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def _count_memories(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Count memories that will get a vector (source rows excluded)."""
        conditions = ["(memory_type IS NULL OR memory_type != 'source')"]
        params: list[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        cursor = await self._db.conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE {' AND '.join(conditions)}",
            params,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def _save_job(self, job: ReindexJob) -> None:
        """Persist job state to SQLite (every batch, so jobs survive restarts)."""
        await self._db.conn.execute(
            f"""
            INSERT INTO reindex_jobs ({_JOB_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                processed = excluded.processed,
                failed = excluded.failed,
                status = excluded.status,
                completed_at = excluded.completed_at,
                error = excluded.error,
                target_collection = excluded.target_collection,
                cursor = excluded.cursor,
                phase = excluded.phase
            """,
            (
                job.id,
                job.old_provider,
                job.old_model,
                job.new_provider,
                job.new_model,
                job.total_memories,
                job.processed,
                job.failed,
                job.status,
                job.started_at,
                job.completed_at,
                job.error,
                job.mode,
                job.source_collection,
                job.target_collection,
                job.cursor,
                job.phase,
                job.user_id,
                job.project_id,
            ),
        )
        await self._db.conn.commit()
