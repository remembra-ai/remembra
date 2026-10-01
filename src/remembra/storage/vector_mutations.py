"""Order pending writes and deletes on the API's shared local SQLite filesystem.

No SQLite transaction spans network I/O. The persistent sidecar must not be
unlinked while the service runs. POSIX locks release on process death; they do
not retract a request already sent to Qdrant. Content-free deletion markers
therefore remain in SQLite and are revisited by the recovery loop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

import structlog

from remembra.core.time import utcnow

log = structlog.get_logger(__name__)
_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def vector_mutation_lock(db: Any) -> AsyncIterator[None]:
    """Serialize cooperating API workers sharing the same local database file."""
    path = getattr(db, "db_path", None)
    disk_path = Path(path).resolve() if isinstance(path, str) and path != ":memory:" else None
    key = str(disk_path) if disk_path else f"memory:{id(db)}"
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    async with lock:
        if disk_path is None:
            yield
            return
        import fcntl

        fd = os.open(
            disk_path.with_name(disk_path.name + ".vectors.lock"),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        acquired = False
        try:
            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            yield
        finally:
            try:
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class VectorErasureReconciler:
    """Repeatedly delete absent canonical IDs, including after late remote writes.

    Markers contain only opaque memory IDs and maintenance timestamps, never
    owner IDs or text. They survive acknowledgements: clearing one after a
    successful delete would miss an older request committed remotely later.
    Restored/live canonical IDs are excluded. This is eventual reconciliation
    while Qdrant is available, not a cross-database atomic transaction.
    """

    def __init__(self, db: Any, qdrant: Any, *, batch_size: int = 100) -> None:
        self.db = db
        self.qdrant = qdrant
        self.batch_size = batch_size

    async def run_once(self) -> int:
        async with vector_mutation_lock(self.db):
            cursor = await self.db.conn.execute(
                "SELECT memory_id FROM vector_erasure_markers "
                "WHERE NOT EXISTS (SELECT 1 FROM memories WHERE memories.id = memory_id) "
                "ORDER BY last_swept_at, memory_id LIMIT ?",
                (self.batch_size,),
            )
            ids = [row[0] for row in await cursor.fetchall()]
            if not ids:
                return 0
            await self.qdrant.delete_ids_everywhere(ids)
            await self.db.conn.executemany(
                "UPDATE vector_erasure_markers SET last_swept_at = ? WHERE memory_id = ?",
                [(utcnow().isoformat(), mid) for mid in ids],
            )
            await self.db.conn.commit()
            return len(ids)

    async def run_forever(self, *, poll_seconds: float = 5.0) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("vector_erasure_reconciliation_failed", error_type=type(exc).__name__)
            await asyncio.sleep(poll_seconds)
