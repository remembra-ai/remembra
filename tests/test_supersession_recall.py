"""Supersession is wired end-to-end at the storage layer.

The bug this guards against: a memory could be marked superseded (via the
explicit ``supersede()`` API or the VERSION conflict strategy) yet still surface
in recall, because recall never consulted the marker. A memory product that
keeps returning "I use Stripe" after the user switched to Paddle is broken.

These tests exercise the source-of-truth path that recall now relies on:
``mark_memory_superseded`` sets the queryable columns, and
``filter_active_memory_ids`` excludes retired memories in one indexed query.
"""

import tempfile
from datetime import datetime, UTC
from pathlib import Path

import pytest

from remembra.storage.database import Database


async def _fresh_db() -> Database:
    tmp = Path(tempfile.mkdtemp()) / "supersession.db"
    db = Database(str(tmp))
    await db.connect()
    await db.init_schema()
    return db


async def _store(db: Database, mem_id: str, content: str) -> None:
    await db.save_memory_metadata(
        memory_id=mem_id,
        user_id="u1",
        project_id="default",
        content=content,
        extracted_facts=[content],
        metadata={},
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_migration_adds_supersession_columns():
    db = await _fresh_db()
    try:
        cursor = await db.conn.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "superseded_by" in cols
        assert "superseded_at" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_active_filter_excludes_superseded_keeps_current():
    db = await _fresh_db()
    try:
        await _store(db, "old-stripe", "I use Stripe for billing")
        await _store(db, "new-paddle", "I use Paddle for billing")

        # Before supersession, both are active.
        active = await db.filter_active_memory_ids(["old-stripe", "new-paddle"])
        assert active == {"old-stripe", "new-paddle"}

        # Retire the old belief.
        await db.mark_memory_superseded("old-stripe", "new-paddle")

        # Now recall's candidate filter drops only the stale one.
        active = await db.filter_active_memory_ids(["old-stripe", "new-paddle"])
        assert active == {"new-paddle"}
        assert "old-stripe" not in active
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_superseded_memory_is_retained_not_deleted():
    """History stays queryable — supersession marks, it does not delete."""
    db = await _fresh_db()
    try:
        await _store(db, "old-stripe", "I use Stripe")
        await db.mark_memory_superseded("old-stripe", "new-paddle")

        row = await db.get_memory("old-stripe")
        assert row is not None  # still there
        assert row["superseded_by"] == "new-paddle"
        assert row["superseded_at"] is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_filter_handles_empty_and_unknown_ids():
    db = await _fresh_db()
    try:
        assert await db.filter_active_memory_ids([]) == set()
        # Unknown ids are treated as active (caller already holds them live).
        assert await db.filter_active_memory_ids(["ghost"]) == {"ghost"}
    finally:
        await db.close()
