"""Regression: init_schema() must succeed on a pre-0.14 (existing) database.

The first 0.14.0 deploy crashed at startup with:
    sqlite3.OperationalError: no such column: key_lookup

Cause: an index on the new `api_keys.key_lookup` column was placed in SCHEMA_SQL,
which runs (via executescript) BEFORE the ALTER TABLE that adds the column. On a
fresh database CREATE TABLE includes the column so the index builds fine — which is
exactly why the fresh-DB unit tests passed. On an existing database CREATE TABLE
IF NOT EXISTS is a no-op, the column doesn't exist yet, and the index build raises.

This test recreates the legacy table shape and asserts the migration path is
crash-free and lands the new column + index.
"""

import os
import tempfile

import aiosqlite

from remembra.auth.keys import APIKeyManager, _key_cache
from remembra.auth.rbac import RoleManager
from remembra.storage.database import Database

# api_keys exactly as it shipped before 0.14 — no key_lookup column.
LEGACY_API_KEYS = """
CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    active BOOLEAN DEFAULT TRUE,
    rate_limit_tier TEXT DEFAULT 'standard'
);
"""


async def test_init_schema_on_legacy_database():
    tmp = tempfile.mktemp(suffix=".db")
    # Stand up a pre-0.14 database (api_keys without key_lookup).
    conn = await aiosqlite.connect(tmp)
    await conn.executescript(LEGACY_API_KEYS)
    await conn.commit()
    await conn.close()

    # This is the call that crashed in production before the fix.
    db = Database(f"sqlite+aiosqlite:///{tmp}")
    await db.connect()
    await db.init_schema()  # must NOT raise

    # The migration added the column and (later) the index.
    cur = await db.conn.execute("PRAGMA table_info(api_keys)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "key_lookup" in cols, "key_lookup column should be added by migration"

    cur = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_api_keys_lookup'"
    )
    assert await cur.fetchone() is not None, "lookup index should exist after migration"

    # End-to-end: key creation + O(1) validation works on the migrated DB.
    await RoleManager(db).init_schema()
    _key_cache.clear()
    mgr = APIKeyManager(db)
    k = await mgr.create_key(user_id="u1", name="t")
    _key_cache.clear()
    info = await mgr.validate_key(k.key)
    assert info is not None and info["user_id"] == "u1"

    await db.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass
