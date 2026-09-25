"""Durable queue of memories whose vector is missing (REL-4 / REL-10 primitive).

When the embedding provider is down or out of quota, a memory can still be
written to SQLite (+ FTS, so keyword recall finds it) and its id enqueued
here. ``PendingEmbeddingWorker`` re-embeds queued rows and upserts the full
payload to Qdrant once the embedding circuit breaker lets traffic through.

Table ``pending_embeddings`` is created by versioned migration 2
(``storage/database.py``). One row per memory id; lifecycle::

    pending --claim--> in_progress --mark_done--> (row deleted)
                           |--mark_failed(retryable, attempts < max)--> pending (+backoff)
                           |--mark_failed(not retryable / attempts >= max)--> failed
                           |--release (breaker open; attempt not counted)--> pending
    in_progress rows whose lease expired (worker crashed) are re-claimable.

All state changes are single statements or ``db.transaction()`` blocks, so
they are safe to call from inside a caller's transaction (they join it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog

from remembra.core.metrics import PENDING_EMBEDDINGS, PENDING_EMBEDDINGS_PROCESSED
from remembra.core.provider_errors import ProviderErrorKind
from remembra.core.time import utcnow
from remembra.storage.memory_rows import is_source_row, memory_from_row

log = structlog.get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_FAILED = "failed"


@dataclass
class PendingEmbedding:
    memory_id: str
    user_id: str
    project_id: str
    reason: str | None
    status: str
    attempts: int
    next_attempt_at: str
    claimed_at: str | None
    last_error: str | None
    last_error_kind: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> PendingEmbedding:
        d = dict(row)
        return cls(
            memory_id=d["memory_id"],
            user_id=d["user_id"],
            project_id=d["project_id"],
            reason=d.get("reason"),
            status=d["status"],
            attempts=int(d["attempts"]),
            next_attempt_at=d["next_attempt_at"],
            claimed_at=d.get("claimed_at"),
            last_error=d.get("last_error"),
            last_error_kind=d.get("last_error_kind"),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )


class PendingEmbeddingQueue:
    """SQLite-backed retry queue for missing embeddings."""

    def __init__(
        self,
        db: Any,
        *,
        max_attempts: int = 12,
        base_backoff_seconds: float = 30.0,
        max_backoff_seconds: float = 3600.0,
        lease_seconds: float = 600.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.max_attempts = max(1, max_attempts)
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.lease_seconds = lease_seconds
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock()

    def backoff_seconds(self, attempts: int) -> float:
        """Exponential backoff for the Nth failed attempt (1-based), capped."""
        return float(min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** max(0, attempts - 1))))

    async def enqueue(
        self,
        memory_id: str,
        user_id: str,
        project_id: str = "default",
        reason: str = "embedding_failed",
        delay_seconds: float = 0.0,
    ) -> None:
        """Queue (or re-queue) a memory for embedding. Idempotent per memory id.

        Re-enqueueing a failed/dead-lettered row resets it to pending with a
        fresh attempt budget; re-enqueueing an in-flight row leaves it alone.
        """
        now = self._now()
        due = (now + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
        await self.db.conn.execute(
            """
            INSERT INTO pending_embeddings
                (memory_id, user_id, project_id, reason, status, attempts, next_attempt_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                reason = excluded.reason,
                status = CASE WHEN pending_embeddings.status = 'in_progress'
                              THEN 'in_progress' ELSE 'pending' END,
                attempts = CASE WHEN pending_embeddings.status = 'failed'
                                THEN 0 ELSE pending_embeddings.attempts END,
                next_attempt_at = CASE WHEN pending_embeddings.status = 'in_progress'
                                       THEN pending_embeddings.next_attempt_at
                                       ELSE excluded.next_attempt_at END,
                updated_at = excluded.updated_at
            """,
            (memory_id, user_id, project_id, reason, due, now.isoformat(), now.isoformat()),
        )
        await self.db.conn.commit()
        log.info("pending_embedding_enqueued", memory_id=memory_id, reason=reason)

    async def claim(self, limit: int = 20) -> list[PendingEmbedding]:
        """Atomically claim up to ``limit`` due rows (and rows with an expired lease)."""
        now = self._now()
        lease_cutoff = (now - timedelta(seconds=self.lease_seconds)).isoformat()
        async with self.db.transaction():
            cursor = await self.db.conn.execute(
                """
                SELECT * FROM pending_embeddings
                WHERE (status = 'pending' AND next_attempt_at <= ?)
                   OR (status = 'in_progress' AND claimed_at <= ?)
                ORDER BY next_attempt_at, created_at
                LIMIT ?
                """,
                (now.isoformat(), lease_cutoff, max(1, limit)),
            )
            rows = [PendingEmbedding.from_row(r) for r in await cursor.fetchall()]
            if rows:
                placeholders = ",".join("?" for _ in rows)
                await self.db.conn.execute(
                    f"UPDATE pending_embeddings SET status = 'in_progress', claimed_at = ?, updated_at = ?"
                    f" WHERE memory_id IN ({placeholders})",
                    (now.isoformat(), now.isoformat(), *[r.memory_id for r in rows]),
                )
        for r in rows:
            r.status = STATUS_IN_PROGRESS
            r.claimed_at = now.isoformat()
        return rows

    async def mark_done(self, memory_id: str) -> None:
        """The memory now has its vector: drop it from the queue."""
        await self.db.conn.execute("DELETE FROM pending_embeddings WHERE memory_id = ?", (memory_id,))
        await self.db.conn.commit()

    async def mark_failed(
        self,
        memory_id: str,
        error: str,
        kind: str | None = None,
        *,
        retryable: bool = True,
    ) -> str:
        """Record a failed attempt. Returns the new status (pending or failed)."""
        now = self._now()
        async with self.db.transaction():
            cursor = await self.db.conn.execute("SELECT attempts FROM pending_embeddings WHERE memory_id = ?", (memory_id,))
            row = await cursor.fetchone()
            if row is None:
                return "missing"
            attempts = int(row[0]) + 1
            if retryable and attempts < self.max_attempts:
                status = STATUS_PENDING
                due = (now + timedelta(seconds=self.backoff_seconds(attempts))).isoformat()
            else:
                status = STATUS_FAILED
                due = now.isoformat()
            await self.db.conn.execute(
                """
                UPDATE pending_embeddings
                SET status = ?, attempts = ?, next_attempt_at = ?, claimed_at = NULL,
                    last_error = ?, last_error_kind = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (status, attempts, due, error[:500], kind, now.isoformat(), memory_id),
            )
        if status == STATUS_FAILED:
            log.error("pending_embedding_dead_lettered", memory_id=memory_id, attempts=attempts, kind=kind)
        return status

    async def release(self, memory_id: str, delay_seconds: float, kind: str | None = None) -> None:
        """Put a claimed row back without counting an attempt (provider circuit open)."""
        now = self._now()
        due = (now + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
        await self.db.conn.execute(
            """
            UPDATE pending_embeddings
            SET status = 'pending', next_attempt_at = ?, claimed_at = NULL,
                last_error_kind = COALESCE(?, last_error_kind), updated_at = ?
            WHERE memory_id = ?
            """,
            (due, kind, now.isoformat(), memory_id),
        )
        await self.db.conn.commit()

    async def get(self, memory_id: str) -> PendingEmbedding | None:
        cursor = await self.db.conn.execute("SELECT * FROM pending_embeddings WHERE memory_id = ?", (memory_id,))
        row = await cursor.fetchone()
        return PendingEmbedding.from_row(row) if row else None

    async def requeue_failed(self) -> int:
        """Move every dead-lettered row back to pending with a fresh attempt budget."""
        now = self._now().isoformat()
        cursor = await self.db.conn.execute(
            """
            UPDATE pending_embeddings
            SET status = 'pending', attempts = 0, next_attempt_at = ?, updated_at = ?
            WHERE status = 'failed'
            """,
            (now, now),
        )
        await self.db.conn.commit()
        return int(cursor.rowcount or 0)

    async def stats(self) -> dict[str, Any]:
        """Counts by status plus the age (seconds) of the oldest pending row."""
        cursor = await self.db.conn.execute("SELECT status, COUNT(*) FROM pending_embeddings GROUP BY status")
        counts = {STATUS_PENDING: 0, STATUS_IN_PROGRESS: 0, STATUS_FAILED: 0}
        for status, n in await cursor.fetchall():
            counts[str(status)] = int(n)
        cursor = await self.db.conn.execute(
            "SELECT MIN(created_at) FROM pending_embeddings WHERE status IN ('pending', 'in_progress')"
        )
        row = await cursor.fetchone()
        oldest_age = None
        if row and row[0]:
            try:
                oldest_age = max(0.0, (self._now() - datetime.fromisoformat(row[0])).total_seconds())
            except ValueError:
                oldest_age = None
        for status, n in counts.items():
            PENDING_EMBEDDINGS.set(n, status=status)
        return {**counts, "oldest_pending_age_seconds": oldest_age}


# Provider kinds that mean "the provider as a whole can't serve right now":
# put the item back without spending an attempt, stop the batch.
_PROVIDER_DOWN_KINDS = {
    ProviderErrorKind.QUOTA_EXHAUSTED.value,
    ProviderErrorKind.RATE_LIMITED.value,
    ProviderErrorKind.UNAVAILABLE.value,
    ProviderErrorKind.AUTH.value,
}


class PendingEmbeddingWorker:
    """Drains ``pending_embeddings``: re-embed from SQLite, upsert to Qdrant, re-index FTS."""

    def __init__(
        self,
        queue: PendingEmbeddingQueue,
        db: Any,
        qdrant: Any,
        embeddings: Any,
        *,
        batch_size: int = 20,
        poll_seconds: float = 5.0,
        provider_down_delay_seconds: float = 60.0,
    ) -> None:
        self.queue = queue
        self.db = db
        self.qdrant = qdrant
        self.embeddings = embeddings
        self.batch_size = max(1, batch_size)
        self.poll_seconds = poll_seconds
        self.provider_down_delay_seconds = provider_down_delay_seconds
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self.last_run: dict[str, Any] | None = None

    def _breaker_allows(self) -> bool:
        breaker = getattr(self.embeddings, "breaker", None)
        return True if breaker is None else bool(breaker.allows_requests())

    async def run_once(self) -> dict[str, int]:
        """Process one batch. Returns outcome counts."""
        result = {"claimed": 0, "done": 0, "retry": 0, "dead": 0, "deferred": 0, "dropped": 0, "skipped": 0}
        if not self._breaker_allows():
            result["skipped"] = 1
            self.last_run = result
            return result

        items = await self.queue.claim(self.batch_size)
        result["claimed"] = len(items)
        for index, item in enumerate(items):
            outcome = await self._process(item)
            result[outcome] += 1
            if outcome == "deferred":
                # Provider is down: hand the rest of the batch back untouched.
                for rest in items[index + 1 :]:
                    await self.queue.release(rest.memory_id, self.provider_down_delay_seconds)
                    result["deferred"] += 1
                break
        for outcome, n in result.items():
            if n and outcome not in ("claimed", "skipped"):
                PENDING_EMBEDDINGS_PROCESSED.inc(n, outcome=outcome)
        await self.queue.stats()
        self.last_run = result
        if result["claimed"]:
            log.info("pending_embeddings_batch", **result)
        return result

    async def _process(self, item: PendingEmbedding) -> str:
        from remembra.storage.embeddings import EmbeddingProviderError

        row = await self.db.get_memory(item.memory_id)
        if row is None or is_source_row(row):
            # Deleted/archived since it was queued, or a source record (SQLite-only by design).
            await self.queue.mark_done(item.memory_id)
            return "dropped"

        memory = memory_from_row(row, await self.db.get_memory_entities(item.memory_id))
        try:
            memory.embedding = await self.embeddings.embed(memory.content)
        except EmbeddingProviderError as e:
            kind = e.kind.value
            if e.circuit_open or kind in _PROVIDER_DOWN_KINDS:
                delay = max(self.provider_down_delay_seconds, float(e.retry_after or 0.0))
                await self.queue.release(item.memory_id, delay, kind=kind)
                return "deferred"
            status = await self.queue.mark_failed(item.memory_id, str(e), kind, retryable=False)
            return "dead" if status == STATUS_FAILED else "retry"
        except Exception as e:
            status = await self.queue.mark_failed(item.memory_id, f"{type(e).__name__}: {e}", "internal")
            return "dead" if status == STATUS_FAILED else "retry"

        try:
            await self.qdrant.upsert(memory)
            await self.db.index_memory_fts(memory.id, memory.user_id, memory.project_id, memory.content)
        except Exception as e:
            status = await self.queue.mark_failed(item.memory_id, f"{type(e).__name__}: {e}", "vector_store")
            return "dead" if status == STATUS_FAILED else "retry"

        await self.queue.mark_done(item.memory_id)
        return "done"

    async def run_forever(self) -> None:
        log.info("pending_embedding_worker_started", poll_seconds=self.poll_seconds)
        while not self._stop.is_set():
            try:
                result = await self.run_once()
                busy = result["claimed"] >= self.batch_size and not result["deferred"]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("pending_embedding_worker_error", error=str(e))
                busy = False
            if busy:
                await asyncio.sleep(0)  # more due work: keep draining
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
        log.info("pending_embedding_worker_stopped")

    def start(self, registry: Any) -> asyncio.Task[Any]:
        """Start the loop as a tracked background task."""
        self._stop = asyncio.Event()
        self._task = registry.spawn(self.run_forever(), name="pending-embedding-worker", loop_task=True)
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
