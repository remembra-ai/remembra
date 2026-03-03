"""Background re-indexing when embedding provider or model changes.

When a user switches from e.g. OpenAI text-embedding-3-small to
Voyage voyage-code-3, all existing memories must be re-embedded so
that vector search continues to work.  This module provides a
``ReindexManager`` that:

1. Iterates over all memories in batches (SQLite source of truth)
2. Re-embeds content using the **new** embedding service
3. Upserts the updated vectors into Qdrant
4. Tracks progress in a ``reindex_jobs`` SQLite table
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


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
    status: str = "pending"  # pending | running | completed | failed | cancelled
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Reindex Manager
# ---------------------------------------------------------------------------


class ReindexManager:
    """Manages background re-indexing of memory embeddings.

    Args:
        db: The application's Database instance.
        qdrant: The QdrantStore for vector upserts.
        embeddings: The EmbeddingService (should already be pointed at the *new* model).
    """

    BATCH_SIZE = 50

    def __init__(self, db: Any, qdrant: Any, embeddings: Any) -> None:
        self._db = db
        self._qdrant = qdrant
        self._embeddings = embeddings
        self._current_job: ReindexJob | None = None
        self._cancel_requested = False

    async def init_schema(self) -> None:
        """Create the reindex_jobs tracking table."""
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
        await self._db.conn.commit()

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
    ) -> ReindexJob:
        """Start a re-indexing job (runs as a background task).

        Args:
            old_provider: Previous embedding provider name.
            old_model: Previous embedding model name.
            new_provider: New embedding provider name.
            new_model: New embedding model name.
            user_id: Optional scope to a single user.
            project_id: Optional scope to a single project.

        Returns:
            The created ReindexJob with initial metadata.
        """
        if self._current_job and self._current_job.status == "running":
            raise RuntimeError("A re-indexing job is already running")

        # Count memories to process
        total = await self._count_memories(user_id, project_id)

        job = ReindexJob(
            old_provider=old_provider,
            old_model=old_model,
            new_provider=new_provider,
            new_model=new_model,
            total_memories=total,
            status="running",
            started_at=datetime.now(UTC).isoformat(),
        )
        self._current_job = job
        self._cancel_requested = False

        # Persist job record
        await self._save_job(job)

        # Launch background processing
        asyncio.create_task(
            self._run_reindex(job, user_id=user_id, project_id=project_id)
        )

        logger.info(
            "Reindex started: id=%s total=%d %s/%s -> %s/%s",
            job.id, total, old_provider, old_model, new_provider, new_model,
        )
        return job

    async def cancel(self) -> bool:
        """Request cancellation of the running reindex job."""
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

        cursor = await self._db.conn.execute(
            "SELECT id, old_provider, old_model, new_provider, new_model, "
            "total_memories, processed, failed, status, started_at, completed_at, error "
            "FROM reindex_jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ReindexJob(
            id=row[0], old_provider=row[1], old_model=row[2],
            new_provider=row[3], new_model=row[4], total_memories=row[5],
            processed=row[6], failed=row[7], status=row[8],
            started_at=row[9], completed_at=row[10], error=row[11],
        )

    async def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent reindex jobs."""
        cursor = await self._db.conn.execute(
            "SELECT id, old_provider, old_model, new_provider, new_model, "
            "total_memories, processed, failed, status, started_at, completed_at "
            "FROM reindex_jobs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "old_provider": r[1], "old_model": r[2],
                "new_provider": r[3], "new_model": r[4],
                "total_memories": r[5], "processed": r[6], "failed": r[7],
                "status": r[8], "started_at": r[9], "completed_at": r[10],
            }
            for r in rows
        ]

    # -----------------------------------------------------------------------
    # Internal processing
    # -----------------------------------------------------------------------

    async def _run_reindex(
        self,
        job: ReindexJob,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Process all memories in batches."""
        try:
            offset = 0
            while True:
                if self._cancel_requested:
                    job.status = "cancelled"
                    break

                batch = await self._fetch_batch(
                    offset=offset,
                    limit=self.BATCH_SIZE,
                    user_id=user_id,
                    project_id=project_id,
                )
                if not batch:
                    break

                for memory_id, content in batch:
                    if self._cancel_requested:
                        break
                    try:
                        # Re-embed with new provider
                        new_vector = await self._embeddings.embed(content)
                        # Upsert into Qdrant
                        await self._qdrant.upsert_vector(memory_id, new_vector)
                        job.processed += 1
                    except Exception as e:
                        job.failed += 1
                        logger.warning(
                            "Reindex failed for memory %s: %s", memory_id, e,
                        )

                offset += self.BATCH_SIZE

                # Periodic status save
                if job.processed % (self.BATCH_SIZE * 5) == 0:
                    await self._save_job(job)
                    logger.info(
                        "Reindex progress: %d/%d (failed=%d)",
                        job.processed, job.total_memories, job.failed,
                    )

            if job.status != "cancelled":
                job.status = "completed"
            job.completed_at = datetime.now(UTC).isoformat()

        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.completed_at = datetime.now(UTC).isoformat()
            logger.error("Reindex job failed: %s", e)

        finally:
            await self._save_job(job)
            logger.info(
                "Reindex %s: processed=%d failed=%d status=%s",
                job.id, job.processed, job.failed, job.status,
            )

    async def _fetch_batch(
        self,
        offset: int,
        limit: int,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Fetch a batch of (memory_id, content) pairs."""
        conditions = []
        params: list[Any] = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT id, content FROM memories {where} ORDER BY created_at LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self._db.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    async def _count_memories(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Count total memories to reindex."""
        conditions = []
        params: list[Any] = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.conn.execute(
            f"SELECT COUNT(*) FROM memories {where}", params,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def _save_job(self, job: ReindexJob) -> None:
        """Persist job state to SQLite."""
        await self._db.conn.execute(
            """
            INSERT INTO reindex_jobs
                (id, old_provider, old_model, new_provider, new_model,
                 total_memories, processed, failed, status, started_at,
                 completed_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                processed = excluded.processed,
                failed = excluded.failed,
                status = excluded.status,
                completed_at = excluded.completed_at,
                error = excluded.error
            """,
            (
                job.id, job.old_provider, job.old_model,
                job.new_provider, job.new_model, job.total_memories,
                job.processed, job.failed, job.status,
                job.started_at, job.completed_at, job.error,
            ),
        )
        await self._db.conn.commit()
