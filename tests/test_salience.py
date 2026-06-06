"""Tests for salience-aware memory.

`pinned` memories are never pruned by TTL expiry or decay. `importance` (0..1)
feeds the decay model so higher-importance memories retain relevance longer.
"""

import os
import tempfile
from datetime import timedelta

import pytest

from remembra.core.time import utcnow
from remembra.storage.database import Database
from remembra.temporal.decay import calculate_memory_decay_info


@pytest.fixture
async def db():
    tmp = tempfile.mktemp(suffix=".db")
    database = Database(f"sqlite+aiosqlite:///{tmp}")
    await database.connect()
    await database.init_schema()
    yield database
    await database.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass


async def _save(db, mid, *, pinned=False, importance=None, expires_at=None, created_at=None):
    await db.save_memory_metadata(
        memory_id=mid,
        user_id="u1",
        project_id="default",
        content="x",
        extracted_facts=[],
        metadata={},
        created_at=created_at or utcnow(),
        expires_at=expires_at,
        pinned=pinned,
        importance=importance,
    )


async def test_pinned_excluded_from_ttl_expiry(db):
    past = utcnow() - timedelta(days=1)
    await _save(db, "m_pinned", pinned=True, expires_at=past)
    await _save(db, "m_normal", pinned=False, expires_at=past)
    expired = await db.get_expired_memories(user_id="u1", project_id="default")
    assert "m_normal" in expired
    assert "m_pinned" not in expired


async def test_set_pin_and_unpin_toggles_protection(db):
    past = utcnow() - timedelta(days=1)
    await _save(db, "m1", pinned=False, expires_at=past)
    assert "m1" in await db.get_expired_memories(user_id="u1", project_id="default")

    assert await db.set_memory_pin("m1", "u1", True) is True
    assert "m1" not in await db.get_expired_memories(user_id="u1", project_id="default")

    assert await db.set_memory_pin("m1", "u1", False) is True
    assert "m1" in await db.get_expired_memories(user_id="u1", project_id="default")


async def test_pin_is_user_scoped(db):
    await _save(db, "m1")
    # A different user cannot pin someone else's memory.
    assert await db.set_memory_pin("m1", "other_user", True) is False


async def test_pinned_never_prunes_in_decay_info():
    old = utcnow() - timedelta(days=3650)  # ancient; would otherwise decay to ~0
    base = {"created_at": old, "last_accessed": old, "access_count": 0, "importance": 0.0}
    assert calculate_memory_decay_info({**base, "pinned": False})["should_prune"] is True
    pinned = calculate_memory_decay_info({**base, "pinned": True})
    assert pinned["should_prune"] is False
    assert pinned["pinned"] is True


async def test_importance_slows_decay():
    old = utcnow() - timedelta(days=60)
    low = calculate_memory_decay_info({"created_at": old, "last_accessed": old, "importance": 0.0})
    high = calculate_memory_decay_info({"created_at": old, "last_accessed": old, "importance": 1.0})
    assert high["relevance_score"] > low["relevance_score"]


async def test_set_importance_clamped(db):
    await _save(db, "m1")
    assert await db.set_memory_importance("m1", "u1", 5.0) is True  # clamped to 1.0
    cur = await db.conn.execute("SELECT importance FROM memories WHERE id='m1'")
    assert (await cur.fetchone())[0] == 1.0
