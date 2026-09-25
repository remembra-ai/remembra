"""SQLite <-> Qdrant <-> FTS drift detection and repair (REL-10 primitive).

Store/delete paths write three places (SQLite row, Qdrant vector, FTS row)
without a shared transaction, so partial failures leave drift:

* ``missing_vectors`` — SQLite memory (non-source) with no Qdrant point:
  invisible to semantic recall. **Repair:** enqueue in ``pending_embeddings``
  (the worker re-embeds from SQLite and upserts the full payload).
* ``orphan_vectors``  — Qdrant point with no SQLite row. May be the *only copy*
  of a memory whose SQLite write failed after the vector write, or a memory
  whose Qdrant delete failed. The two can't be told apart safely, so these
  are **reported only** (never deleted or resurrected automatically).
* ``missing_fts``     — SQLite memory (non-source) absent from FTS:
  invisible to keyword recall. **Repair:** index it.
* ``orphan_fts``      — FTS row with no SQLite memory (derived index only).
  **Repair:** delete the FTS row.

Run from the app (``reconcile(db, qdrant, ...)``) or the CLI::

    python -m remembra.storage.reconcile            # report (JSON)
    python -m remembra.storage.reconcile --repair   # report + repair
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from remembra.core.metrics import RECONCILE_DRIFT
from remembra.storage.memory_rows import is_source_row

log = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    collection: str = ""
    sqlite_rows: int = 0
    qdrant_points: int = 0
    fts_rows: int = 0
    missing_vectors: int = 0
    orphan_vectors: int = 0
    missing_fts: int = 0
    orphan_fts: int = 0
    repaired: bool = False
    requeued: int = 0
    fts_indexed: int = 0
    fts_removed: int = 0
    samples: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _sqlite_rows(db: Any, user_id: str | None, batch_size: int) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    cursor_id = ""
    while True:
        params: list[Any] = [cursor_id]
        where = "id > ?"
        if user_id:
            where += " AND user_id = ?"
            params.append(user_id)
        cursor = await db.conn.execute(
            f"SELECT id, user_id, project_id, content, memory_type, metadata FROM memories WHERE {where} ORDER BY id LIMIT ?",
            (*params, batch_size),
        )
        page = [dict(r) for r in await cursor.fetchall()]
        if not page:
            return rows
        for r in page:
            rows[r["id"]] = r
        cursor_id = page[-1]["id"]


async def _qdrant_ids(qdrant: Any, user_id: str | None, batch_size: int) -> set[str]:
    from qdrant_client.http import models as qmodels

    client = await qdrant._get_client()
    scroll_filter = None
    if user_id:
        scroll_filter = qmodels.Filter(must=[qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id))])
    ids: set[str] = set()
    offset: Any = None
    while True:
        points, offset = await client.scroll(
            collection_name=qdrant.collection_name,
            scroll_filter=scroll_filter,
            limit=batch_size,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.update(str(p.id) for p in points)
        if offset is None:
            return ids


async def _fts_ids(db: Any, user_id: str | None) -> set[str]:
    if user_id:
        cursor = await db.conn.execute("SELECT id FROM memories_fts WHERE user_id = ?", (user_id,))
    else:
        cursor = await db.conn.execute("SELECT id FROM memories_fts")
    return {str(r[0]) for r in await cursor.fetchall()}


async def reconcile(
    db: Any,
    qdrant: Any,
    *,
    repair: bool = False,
    queue: Any = None,
    user_id: str | None = None,
    batch_size: int = 500,
    sample_limit: int = 20,
) -> ReconcileReport:
    """Compare SQLite, Qdrant and FTS; optionally repair (see module docstring).

    Args:
        repair: re-queue missing vectors, index missing FTS rows, drop orphan
            FTS rows. Orphan vectors are always report-only.
        queue: PendingEmbeddingQueue used for re-queueing (built from ``db``
            when omitted).
        user_id: limit the scan to one tenant.
    """
    rows = await _sqlite_rows(db, user_id, batch_size)
    vector_ids = await _qdrant_ids(qdrant, user_id, batch_size)
    fts_ids = await _fts_ids(db, user_id)

    expects_index = {mid for mid, r in rows.items() if not is_source_row(r)}
    missing_vectors = sorted(expects_index - vector_ids)
    orphan_vectors = sorted(vector_ids - set(rows))
    missing_fts = sorted(expects_index - fts_ids)
    orphan_fts = sorted(fts_ids - set(rows))

    report = ReconcileReport(
        collection=str(qdrant.collection_name),
        sqlite_rows=len(rows),
        qdrant_points=len(vector_ids),
        fts_rows=len(fts_ids),
        missing_vectors=len(missing_vectors),
        orphan_vectors=len(orphan_vectors),
        missing_fts=len(missing_fts),
        orphan_fts=len(orphan_fts),
        samples={
            "missing_vectors": missing_vectors[:sample_limit],
            "orphan_vectors": orphan_vectors[:sample_limit],
            "missing_fts": missing_fts[:sample_limit],
            "orphan_fts": orphan_fts[:sample_limit],
        },
    )

    if repair:
        if queue is None:
            from remembra.storage.pending_embeddings import PendingEmbeddingQueue

            queue = PendingEmbeddingQueue(db)
        for mid in missing_vectors:
            r = rows[mid]
            await queue.enqueue(mid, r["user_id"], r["project_id"] or "default", reason="reconcile_missing_vector")
            report.requeued += 1
        for mid in missing_fts:
            r = rows[mid]
            await db.index_memory_fts(mid, r["user_id"], r["project_id"] or "default", r["content"])
            report.fts_indexed += 1
        for mid in orphan_fts:
            await db.delete_memory_fts(mid)
            report.fts_removed += 1
        report.repaired = True

    if user_id is None:
        for kind in ("missing_vectors", "orphan_vectors", "missing_fts", "orphan_fts"):
            RECONCILE_DRIFT.set(getattr(report, kind), type=kind)
    log.info(
        "reconcile_completed",
        collection=report.collection,
        missing_vectors=report.missing_vectors,
        orphan_vectors=report.orphan_vectors,
        missing_fts=report.missing_fts,
        orphan_fts=report.orphan_fts,
        repaired=repair,
    )
    return report


async def run_reconcile_loop(db: Any, qdrant: Any, interval_seconds: float) -> None:
    """Periodic report-only reconcile (feeds the remembra_reconcile_drift gauge)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reconcile(db, qdrant, repair=False)
        except Exception as e:
            log.error("reconcile_loop_error", error=str(e))


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report (and optionally repair) SQLite/Qdrant/FTS drift.")
    parser.add_argument("--repair", action="store_true", help="re-queue missing vectors and fix FTS drift")
    parser.add_argument("--user-id", default=None, help="limit to one user")
    args = parser.parse_args(argv)
    # stdout carries the JSON report; logs go to stderr for the duration.
    import sys

    previous = structlog.get_config()
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    try:
        return await _run_cli(args)
    finally:
        structlog.configure(**previous)


async def _run_cli(args: argparse.Namespace) -> int:

    from remembra.config import get_settings
    from remembra.storage.database import Database
    from remembra.storage.qdrant import QdrantStore
    from remembra.storage.reindex import apply_active_collection

    settings = get_settings()
    db = Database(settings.database_url)
    await db.connect()
    await db.init_schema()
    qdrant = QdrantStore(settings)
    try:
        await apply_active_collection(db, qdrant)
        report = await reconcile(db, qdrant, repair=args.repair, user_id=args.user_id)
        print(json.dumps(report.to_dict(), indent=2))
    finally:
        await qdrant.close()
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
