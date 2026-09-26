"""Main-DB migration 6 (agent_inbox.trust_score, R-16).

It is additive and must apply to a production-shaped database: one at
schema 4 (feat/relay-launch before this lane) holding inbox rows, one where
the inbox table does not exist yet, and one where feat/crew's version 5 has
already altered agent_inbox (the branches merge in either order).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import remembra.storage.database as database
from remembra.storage.database import VERSIONED_MIGRATIONS, Database


async def _at_version_4(path: Path, monkeypatch, with_inbox: bool, crew_v5: bool = False) -> None:
    with monkeypatch.context() as m:
        m.setattr(database, "VERSIONED_MIGRATIONS", [mig for mig in VERSIONED_MIGRATIONS if mig[0] <= 4])
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()
        try:
            await db.init_schema()
            if with_inbox:
                await db.conn.executescript(database.AGENT_INBOX_BASE_DDL + ";")
                if crew_v5:  # what feat/crew's version 5 adds, recorded as applied
                    for column in ("project_id TEXT", "crew_id TEXT", "kind TEXT DEFAULT 'directive'"):
                        await db.conn.execute(f"ALTER TABLE agent_inbox ADD COLUMN {column}")
                    await db.conn.execute(
                        "INSERT INTO schema_version (version, name, applied_at) VALUES (5, 'crew_agent_inbox_scoping', 'x')"
                    )
                await db.conn.execute(
                    "INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, created_at)"
                    " VALUES ('inbox_old', 'u1', 'codex', 'claude-code', 'old', 'kept as is', '2026-09-01T00:00:00+00:00')"
                )
            await db.conn.commit()
            assert await db.get_schema_version() == (5 if crew_v5 else 4)
        finally:
            await db.close()


async def _migrate(path: Path) -> sqlite3.Connection:
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.connect()
    try:
        await db.init_schema()
    finally:
        await db.close()
    return sqlite3.connect(path)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


async def test_existing_inbox_rows_survive_and_get_a_null_score(tmp_path, monkeypatch):
    path = tmp_path / "prod.db"
    await _at_version_4(path, monkeypatch, with_inbox=True)
    conn = await _migrate(path)
    try:
        assert 6 in {r[0] for r in conn.execute("SELECT version FROM schema_version")}
        assert "trust_score" in _columns(conn, "agent_inbox")
        assert conn.execute("SELECT body, trust_score FROM agent_inbox WHERE inbox_id = 'inbox_old'").fetchone() == (
            "kept as is",
            None,
        )
    finally:
        conn.close()


async def test_fresh_database_without_an_inbox_table(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    await _at_version_4(path, monkeypatch, with_inbox=False)
    conn = await _migrate(path)
    try:
        assert "trust_score" in _columns(conn, "agent_inbox")
    finally:
        conn.close()


async def test_applies_after_crew_version_5(tmp_path, monkeypatch):
    path = tmp_path / "crew.db"
    await _at_version_4(path, monkeypatch, with_inbox=True, crew_v5=True)
    conn = await _migrate(path)
    try:
        assert {"project_id", "crew_id", "kind", "trust_score"} <= _columns(conn, "agent_inbox")
        assert sorted(r[0] for r in conn.execute("SELECT version FROM schema_version")) == [1, 2, 3, 4, 5, 6]
    finally:
        conn.close()


def test_versions_are_unique_and_leave_5_to_crew():
    versions = [v for v, _, _ in VERSIONED_MIGRATIONS]
    assert len(versions) == len(set(versions)) and 5 not in versions and 6 in versions
