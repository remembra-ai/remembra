"""Main-DB migrations 6 (agent_inbox.trust_score, R-16) and 7 (relay_pickups, R-18), and Crew mode's 5.

Both are additive and must apply to a production-shaped database: one at
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
        assert {6, 7} <= {r[0] for r in conn.execute("SELECT version FROM schema_version")}
        assert "trust_score" in _columns(conn, "agent_inbox")
        assert conn.execute("SELECT body, trust_score FROM agent_inbox WHERE inbox_id = 'inbox_old'").fetchone() == (
            "kept as is",
            None,
        )
        assert _columns(conn, "relay_pickups") >= {"handoff_id", "reader_agent", "reader_session", "gap_seconds"}
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
        applied = sorted(r[0] for r in conn.execute("SELECT version FROM schema_version"))
        assert applied == sorted({5} | {v for v, _, _ in VERSIONED_MIGRATIONS}) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    finally:
        conn.close()


def test_versions_are_unique_and_5_is_crews():
    versions = [v for v, _, _ in VERSIONED_MIGRATIONS]
    assert versions == sorted(set(versions)) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    # Crew mode's 5 merged after the releases shipped 6-10 (0.16.1 added 10); w2/account's migration was
    # renumbered from 6 to 8 when the wave-2 lanes merged. Names and statements of shipped versions never change.
    names = {v: n for v, n, _ in VERSIONED_MIGRATIONS}
    assert names[5] == "crew_agent_inbox_scoping"
    assert names[11] == "pending_embedding_claim_tokens"
    assert names[12] == "durable_vector_erasure_markers"
    assert (names[6], names[7], names[8], names[9], names[10]) == (
        "agent_inbox_trust_score",
        "relay_pickups",
        "account_erasure_and_founding_holds",
        "memories_user_type_index",
        "account_reviews",
    )


async def _at_production_schema_10(path: Path, monkeypatch) -> None:
    """A database as production has it (0.16.1): versions 1-4 and 6-10 (released before Crew mode's 5)."""
    with monkeypatch.context() as m:
        m.setattr(database, "VERSIONED_MIGRATIONS", [mig for mig in VERSIONED_MIGRATIONS if mig[0] != 5 and mig[0] <= 10])
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()
        try:
            await db.init_schema()
            rows = [
                ("inbox_tagged", "codex", "claude-code", '{"project_id": "alpha"}'),
                ("inbox_untagged", "gemini", "qwen", "{}"),
                ("inbox_bad_meta", "gemini", "qwen", "{not json"),
                ("inbox_number_tag", "gemini", "qwen", '{"project_id": 7}'),
            ]
            for inbox_id, frm, to, meta in rows:
                await db.conn.execute(
                    "INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata,"
                    " created_at, trust_score) VALUES (?, 'u1', ?, ?, ?, 'b', ?, '2026-09-01T00:00:00+00:00', 0.9)",
                    (inbox_id, frm, to, inbox_id, meta),
                )
            await db.conn.commit()
        finally:
            await db.close()


async def test_production_schema_10_preserves_releases_and_adds_crew_and_erasure(tmp_path, monkeypatch):
    path = tmp_path / "prod10.db"
    await _at_production_schema_10(path, monkeypatch)
    before = sqlite3.connect(path)
    try:
        assert sorted(r[0] for r in before.execute("SELECT version FROM schema_version")) == [1, 2, 3, 4, 6, 7, 8, 9, 10]
        assert "project_id" not in _columns(before, "agent_inbox")
        stamps = dict(before.execute("SELECT version, applied_at FROM schema_version"))
    finally:
        before.close()
    conn = await _migrate(path)
    try:
        applied = dict(conn.execute("SELECT version, applied_at FROM schema_version"))
        assert sorted(applied) == list(range(1, 15))
        assert {v: applied[v] for v in stamps} == stamps  # 1-4 and 6-10 were not re-applied
        assert {"project_id", "crew_id", "kind", "sender_kind", "sender_verified", "trust_score"} <= _columns(conn, "agent_inbox")
        got = {
            r[0]: r[1:]
            for r in conn.execute("SELECT inbox_id, project_id, sender_kind, sender_verified, kind, trust_score FROM agent_inbox")
        }
        assert got == {
            "inbox_tagged": ("alpha", "agent", 0, "directive", 0.9),
            "inbox_untagged": (None, "agent", 0, "directive", 0.9),
            "inbox_bad_meta": (None, "agent", 0, "directive", 0.9),
            "inbox_number_tag": (None, "agent", 0, "directive", 0.9),
        }
    finally:
        conn.close()
    again = await _migrate(path)  # idempotent: a second boot applies nothing
    try:
        assert dict(again.execute("SELECT version, applied_at FROM schema_version")) == applied
    finally:
        again.close()
