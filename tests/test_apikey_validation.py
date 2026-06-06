"""Tests for O(1) API key validation with lazy backfill (security hardening).

Validation used to load every active key and bcrypt-check the candidate against
each one on a cache miss — an O(n) scan that doubled as a CPU-exhaustion vector
(invalid keys never cache, so every probe forced a full scan). These tests pin
the new behavior: indexed lookup by a deterministic hash, lazy backfill of legacy
keys, and O(1) rejection of unknown keys once migration completes.
"""

import os
import tempfile

import pytest

from remembra.auth.keys import APIKeyManager, _key_cache
from remembra.auth.rbac import RoleManager
from remembra.storage.database import Database


@pytest.fixture
async def db():
    tmp = tempfile.mktemp(suffix=".db")
    database = Database(f"sqlite+aiosqlite:///{tmp}")
    await database.connect()
    await database.init_schema()
    # api_key_roles lives in the RBAC schema, created at app startup in production.
    await RoleManager(database).init_schema()
    yield database
    await database.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clear_cache():
    _key_cache.clear()
    yield
    _key_cache.clear()


async def test_create_and_validate_o1(db):
    mgr = APIKeyManager(db)
    k = await mgr.create_key(user_id="u1", name="test")
    assert k.key.startswith("rem_")

    _key_cache.clear()  # force the DB path, not the in-memory cache
    info = await mgr.validate_key(k.key)
    assert info is not None and info["user_id"] == "u1"

    # The deterministic lookup hash is persisted at creation time.
    cur = await db.conn.execute("SELECT key_lookup FROM api_keys WHERE id=?", (k.id,))
    row = await cur.fetchone()
    assert row[0] == APIKeyManager.compute_lookup(k.key)


async def test_invalid_key_rejected_without_scan(db):
    mgr = APIKeyManager(db)
    await mgr.create_key(user_id="u1")
    _key_cache.clear()

    assert await mgr.validate_key("rem_not_a_real_key_aaaaaaaaaaaaaaaaaaaa") is None
    # No legacy keys remain, so unknown keys are rejected in O(1) (no bcrypt scan).
    assert await db.get_unmigrated_active_api_keys() == []


async def test_non_prefixed_key_rejected(db):
    mgr = APIKeyManager(db)
    assert await mgr.validate_key("not-a-remembra-key") is None


async def test_legacy_key_lazy_backfill(db):
    mgr = APIKeyManager(db)
    k = await mgr.create_key(user_id="u2", name="legacy")
    # Simulate a key created before the key_lookup column existed.
    await db.conn.execute("UPDATE api_keys SET key_lookup=NULL WHERE id=?", (k.id,))
    await db.conn.commit()
    _key_cache.clear()

    assert len(await db.get_unmigrated_active_api_keys()) == 1
    info = await mgr.validate_key(k.key)
    assert info is not None and info["user_id"] == "u2"

    # First successful validation backfills the lookup → O(1) thereafter.
    cur = await db.conn.execute("SELECT key_lookup FROM api_keys WHERE id=?", (k.id,))
    assert (await cur.fetchone())[0] is not None
    assert await db.get_unmigrated_active_api_keys() == []


async def test_revoked_key_rejected(db):
    mgr = APIKeyManager(db)
    k = await mgr.create_key(user_id="u1")
    await mgr.revoke_key(k.id, "u1")
    _key_cache.clear()
    assert await mgr.validate_key(k.key) is None


async def test_roles_and_scopes_normalized(db):
    """Role/scopes/project_ids from api_key_roles are surfaced and parsed to lists."""
    mgr = APIKeyManager(db)
    role_mgr = RoleManager(db)
    k = await mgr.create_key(user_id="u3", name="scoped")
    from remembra.auth.rbac import Role

    await role_mgr.assign_role(api_key_id=k.id, role=Role.VIEWER, project_ids=["projA", "projB"])
    _key_cache.clear()

    info = await mgr.validate_key(k.key)
    assert info is not None
    assert info["role"] == "viewer"
    assert info["project_ids"] == ["projA", "projB"]
