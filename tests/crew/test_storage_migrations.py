"""WP-1: migration runner generalisation, main-DB v5 (agent_inbox scoping + backfill) and crew.db v1.

Everything runs against real SQLite files through the production code paths
(``Database.init_schema``, ``CrewDatabase.init_schema``, ``InboxManager``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from remembra.crew.db import (
    CREW_MIGRATION_RUNNER,
    CREW_MIGRATIONS,
    CREW_TABLES,
    CrewDatabase,
    open_crew_db,
    resolve_crew_db_path,
)
from remembra.inbox.manager import InboxManager
from remembra.storage.database import (
    AGENT_INBOX_BASE_DDL,
    VERSIONED_MIGRATIONS,
    Database,
    MigrationRunner,
    main_migrations,
)

V5_COLUMNS = {"project_id", "crew_id", "kind", "sender_kind", "sender_verified"}


def sync_rows(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    """Read through a separate connection: proves what was actually committed to the file."""
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


async def columns(conn, table: str) -> dict[str, tuple]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {r[1]: (r[2], r[3], r[4]) for r in await cursor.fetchall()}


# ---------------------------------------------------------------------------
# MigrationRunner
# ---------------------------------------------------------------------------


def test_runner_rejects_bad_version_lists() -> None:
    with pytest.raises(ValueError):
        MigrationRunner([(2, "b", []), (1, "a", [])], label="x")
    with pytest.raises(ValueError):
        MigrationRunner([(1, "a", []), (1, "b", [])], label="x")
    with pytest.raises(ValueError):
        MigrationRunner([(0, "a", [])], label="x")
    with pytest.raises(ValueError):
        MigrationRunner([(1, "a", [])], label="x", table="bad name; drop")


def test_main_and_crew_lists_are_versioned_append_only() -> None:
    assert [v for v, _, _ in VERSIONED_MIGRATIONS] == [1, 2, 3, 4, 5, 6, 7, 8, 9]  # 6-9 shipped before crew merged
    assert main_migrations().latest_version == 9
    assert [v for v, _, _ in CREW_MIGRATIONS] == [1]
    assert CREW_MIGRATION_RUNNER.latest_version == 1
    # One runner class serves both files.
    assert type(main_migrations()) is type(CREW_MIGRATION_RUNNER) is MigrationRunner


async def test_runner_failed_step_rolls_back_the_whole_version(tmp_path: Path) -> None:
    db = CrewDatabase(str(tmp_path / "r.db"))
    await db.connect()
    try:

        async def boom(conn) -> None:
            raise RuntimeError("backfill failed")

        runner = MigrationRunner(
            [
                (1, "ok", ["CREATE TABLE a (x INTEGER)"]),
                (2, "bad", ["CREATE TABLE b (x INTEGER)", "INSERT INTO a VALUES (1)", boom]),
            ],
            label="t",
        )
        with pytest.raises(RuntimeError, match="backfill failed"):
            await runner.apply(db.conn, db.transaction)
        assert await runner.current_version(db.conn) == 1
        # Version 2's table and row were rolled back with it.
        assert sync_rows(tmp_path / "r.db", "SELECT name FROM sqlite_master WHERE name = 'b'") == []
        assert sync_rows(tmp_path / "r.db", "SELECT COUNT(*) FROM a") == [(0,)]

        fixed = MigrationRunner(
            [(1, "ok", ["CREATE TABLE a (x INTEGER)"]), (2, "fixed", ["CREATE TABLE b (x INTEGER)"])], label="t"
        )
        assert await fixed.apply(db.conn, db.transaction) == [2]
        assert await fixed.apply(db.conn, db.transaction) == []
    finally:
        await db.close()


async def test_runner_tolerates_only_duplicate_add_column(tmp_path: Path) -> None:
    db = CrewDatabase(str(tmp_path / "r.db"))
    await db.connect()
    try:
        await db.conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        await db.conn.commit()
        ok = MigrationRunner([(1, "add", ["ALTER TABLE t ADD COLUMN b TEXT", "ALTER TABLE t ADD COLUMN c TEXT"])], label="t")
        assert await ok.apply(db.conn, db.transaction) == [1]
        assert set(await columns(db.conn, "t")) == {"a", "b", "c"}

        bad = MigrationRunner([(1, "x", []), (2, "missing", ["ALTER TABLE nope ADD COLUMN z TEXT"])], label="t")
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await bad.apply(db.conn, db.transaction)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Main DB v5
# ---------------------------------------------------------------------------


async def test_v5_on_fresh_db_creates_agent_inbox_before_the_inbox_manager(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "main.db"))
    await db.connect()
    try:
        await db.init_schema()  # main.py:147 — runs BEFORE InboxManager.init_schema
        assert await db.get_schema_version() == main_migrations().latest_version
        cols = await columns(db.conn, "agent_inbox")
        assert set(cols) >= V5_COLUMNS
        assert cols["kind"][2] == "'directive'"
        assert cols["sender_kind"][2] == "'agent'"
        assert cols["sender_verified"][2] == "0"
        indexes = sync_rows(tmp_path / "main.db", "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agent_inbox'")
        assert ("idx_agent_inbox_project",) in indexes

        manager = InboxManager(db)
        await manager.init_schema()  # main.py:313 — must still work on the pre-created table
        sent = await manager.send("u1", "claude-code", "codex", "subj", "body")
        row = await manager.get_one("u1", sent["inbox_id"])
        assert row is not None
        assert row["kind"] == "directive"
        assert row["sender_kind"] == "agent"
        assert row["sender_verified"] == 0
        assert row["project_id"] is None and row["crew_id"] is None
    finally:
        await db.close()


async def test_v5_ddl_matches_the_inbox_manager_ddl(tmp_path: Path) -> None:
    """v5 must create the exact table InboxManager creates (spec §3.1)."""
    via_manager = Database(str(tmp_path / "a.db"))
    await via_manager.connect()
    try:
        await InboxManager(via_manager).init_schema()
        manager_cols = await columns(via_manager.conn, "agent_inbox")
    finally:
        await via_manager.close()
    async with aiosqlite.connect(tmp_path / "b.db") as conn:
        await conn.execute(AGENT_INBOX_BASE_DDL)
        ddl_cols = await columns(conn, "agent_inbox")
    assert ddl_cols == manager_cols


async def _v4_database(tmp_path: Path, name: str = "legacy.db") -> Database:
    """A database at v4 whose agent_inbox was created by InboxManager (the pre-crew shape)."""
    db = Database(str(tmp_path / name))
    await db.connect()
    await db.init_schema()
    await db.conn.execute("DROP INDEX IF EXISTS idx_agent_inbox_project")
    await db.conn.execute("DROP TABLE agent_inbox")
    await db.conn.execute("DELETE FROM schema_version WHERE version = 5")
    await db.conn.commit()
    await InboxManager(db).init_schema()
    assert V5_COLUMNS.isdisjoint(await columns(db.conn, "agent_inbox"))
    return db


async def _inbox_row(db: Database, inbox_id: str, owner: str, frm: str, to: str, metadata: dict | None = None) -> None:
    await db.conn.execute(
        """INSERT INTO agent_inbox (inbox_id, owner_user_id, from_agent, to_agent, subject, body, metadata, created_at)
           VALUES (?, ?, ?, ?, 's', 'b', ?, '2026-09-01T00:00:00')""",
        (inbox_id, owner, frm, to, json.dumps(metadata or {})),
    )


async def _relay_memory(db: Database, mid: str, owner: str, project: str, agent: str) -> None:
    await db.conn.execute(
        """INSERT INTO memories (id, user_id, project_id, content, metadata, memory_type, created_at, updated_at)
           VALUES (?, ?, ?, 'handoff', ?, 'handoff', '2026-09-01T00:00:00', '2026-09-01T00:00:00')""",
        (mid, owner, project, json.dumps({"agent_id": agent, "source": "relay"})),
    )


async def test_v5_on_v4_db_with_rows_backfills_only_unambiguous_projects(tmp_path: Path) -> None:
    db = await _v4_database(tmp_path)
    try:
        # Owner u1: codex only ever worked on yaadbooks; gemini on two projects.
        await _relay_memory(db, "m1", "u1", "yaadbooks", "codex")
        await _relay_memory(db, "m2", "u1", "yaadbooks", "codex")
        await _relay_memory(db, "m3", "u1", "yaadbooks", "gemini")
        await _relay_memory(db, "m4", "u1", "trademind", "gemini")
        await _relay_memory(db, "m5", "u1", "trademind", "cursor")
        # Another tenant's memories never influence u1's rows.
        await _relay_memory(db, "m6", "u2", "other", "claude-code")
        # Agent-bound key restricted to one project (relay G + RBAC project_ids).
        await db.save_api_key("k1", "hash1", "u1", "qwen key", agent_id="qwen")
        await db.conn.execute(
            "CREATE TABLE IF NOT EXISTS api_key_roles"
            " (api_key_id TEXT PRIMARY KEY, role TEXT, scopes TEXT, project_ids TEXT DEFAULT '')"
        )
        await db.conn.execute("INSERT INTO api_key_roles VALUES ('k1', 'editor', '', 'posapp')")
        # An unrestricted agent key says nothing about the project.
        await db.save_api_key("k2", "hash2", "u1", "kimi key", agent_id="kimi")
        await db.conn.execute("INSERT INTO api_key_roles VALUES ('k2', 'admin', '', '')")

        await _inbox_row(db, "i_meta", "u1", "gemini", "cursor", {"project_id": "explicit"})
        await _inbox_row(db, "i_one", "u1", "mani", "codex")  # to_agent -> yaadbooks only
        await _inbox_row(db, "i_from", "u1", "codex", "somebody")  # from_agent -> yaadbooks only
        await _inbox_row(db, "i_ambiguous", "u1", "mani", "gemini")  # two projects
        await _inbox_row(db, "i_union", "u1", "codex", "cursor")  # yaadbooks + trademind
        await _inbox_row(db, "i_unknown", "u1", "mani", "nobody")
        await _inbox_row(db, "i_key", "u1", "mani", "qwen")  # restricted agent key
        await _inbox_row(db, "i_open_key", "u1", "mani", "kimi")  # unrestricted key
        await _inbox_row(db, "i_tenant", "u1", "mani", "claude-code")  # only u2 knows claude-code
        await _inbox_row(db, "i_blank_meta", "u1", "mani", "nobody2", {"project_id": "  "})
        await db.conn.commit()

        await db._apply_versioned_migrations()

        assert await db.get_schema_version() == main_migrations().latest_version
        got = dict(sync_rows(tmp_path / "legacy.db", "SELECT inbox_id, project_id FROM agent_inbox"))
        assert got == {
            "i_meta": "explicit",
            "i_one": "yaadbooks",
            "i_from": "yaadbooks",
            "i_ambiguous": None,
            "i_union": None,
            "i_unknown": None,
            "i_key": "posapp",
            "i_open_key": None,
            "i_tenant": None,
            "i_blank_meta": None,
        }
        # Existing rows are never promoted to human or verified.
        assert set(sync_rows(tmp_path / "legacy.db", "SELECT DISTINCT kind, sender_kind, sender_verified FROM agent_inbox")) == {
            ("directive", "agent", 0)
        }
    finally:
        await db.close()


async def test_v5_twice_and_rerun_over_existing_columns_is_a_no_op(tmp_path: Path) -> None:
    db = await _v4_database(tmp_path)
    try:
        await _relay_memory(db, "m1", "u1", "yaadbooks", "codex")
        await _inbox_row(db, "i1", "u1", "mani", "codex")
        await db.conn.commit()
        await db._apply_versioned_migrations()
        await db._apply_versioned_migrations()  # twice: nothing to do
        assert sync_rows(tmp_path / "legacy.db", "SELECT COUNT(*) FROM schema_version WHERE version = 5") == [(1,)]

        # A human later scopes a row by hand; re-running v5 over existing columns
        # (version row lost) must tolerate the duplicate columns and keep data.
        await db.conn.execute("UPDATE agent_inbox SET project_id = 'manual' WHERE inbox_id = 'i1'")
        await db.conn.execute("DELETE FROM schema_version WHERE version = 5")
        await db.conn.commit()
        await db._apply_versioned_migrations()
        assert await db.get_schema_version() == main_migrations().latest_version
        assert sync_rows(tmp_path / "legacy.db", "SELECT project_id FROM agent_inbox WHERE inbox_id = 'i1'") == [("manual",)]
    finally:
        await db.close()


async def test_full_init_schema_is_idempotent_across_restarts(tmp_path: Path) -> None:
    for _ in range(2):
        db = Database(str(tmp_path / "boot.db"))
        await db.connect()
        try:
            await db.init_schema()
            await InboxManager(db).init_schema()
            assert await db.get_schema_version() == main_migrations().latest_version
        finally:
            await db.close()
    versions = sync_rows(tmp_path / "boot.db", "SELECT version FROM schema_version ORDER BY version")
    assert versions == [(v,) for v in range(1, 10)]


# ---------------------------------------------------------------------------
# crew.db v1
# ---------------------------------------------------------------------------


async def test_crew_db_v1_fresh_creates_every_table_and_index(tmp_path: Path) -> None:
    path = tmp_path / "crew.db"
    db = CrewDatabase(str(path))
    try:
        assert await db.init_schema() == [1]
        assert await db.get_schema_version() == 1
    finally:
        await db.close()
    tables = {r[0] for r in sync_rows(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(CREW_TABLES) <= tables
    indexes = {r[0] for r in sync_rows(path, "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
    for name in (
        "uq_claim_exclusive",
        "uq_collision_open",
        "uq_report_current",
        "uq_inbox_open",
        "uq_events_idem",
        "uq_crew_callsign_live",
        "idx_crew_sessions_reap",
        "idx_claims_lease",
        "idx_events_moment",
        "idx_project_shares_team",
    ):
        assert name in indexes, name
    # The crew file holds no main-DB table, and the main DB holds no crew table.
    assert "memories" not in tables and "agent_inbox" not in tables


async def test_crew_db_v1_twice_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "crew.db"
    db = await open_crew_db(str(tmp_path / "remembra.db"))
    try:
        assert db.db_path == str(path)
        await db.conn.execute(
            "INSERT INTO crews (id, owner_user_id, project_id, created_at, updated_at) VALUES ('crw_x', 'u', 'p', 't', 't')"
        )
        await db.conn.commit()
    finally:
        await db.close()
    db = await open_crew_db(str(tmp_path / "remembra.db"))
    try:
        assert await db.init_schema() == []
        assert (await db.fetchone("SELECT project_id FROM crews WHERE id = 'crw_x'")) == {"project_id": "p"}
    finally:
        await db.close()
    assert sync_rows(path, "SELECT COUNT(*) FROM schema_version") == [(1,)]


async def test_two_processes_migrating_one_crew_file_apply_v1_once(tmp_path: Path) -> None:
    path = str(tmp_path / "crew.db")
    a, b = CrewDatabase(path), CrewDatabase(path)
    try:
        results = await asyncio.gather(a.init_schema(), b.init_schema())
        assert sorted(results) == [[], [1]]
    finally:
        await a.close()
        await b.close()


async def test_crew_db_connection_settings(tmp_path: Path) -> None:
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    try:
        assert (await db.fetchone("PRAGMA journal_mode"))["journal_mode"] == "wal"
        assert (await db.fetchone("PRAGMA busy_timeout"))["timeout"] == 5000
    finally:
        await db.close()


async def test_crew_constraints_are_enforced(tmp_path: Path) -> None:
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    try:

        async def claim(cid: str, mode: str, state: str, zone: str = "zn_pos") -> None:
            await db.conn.execute(
                """INSERT INTO crew_claims (id, crew_id, zone_id, mode, holder_kind, state, source, created_at, updated_at)
                   VALUES (?, 'crw_1', ?, ?, 'session', ?, 'first_write', 't', 't')""",
                (cid, zone, mode, state),
            )
            await db.conn.commit()

        await claim("clm_1", "exclusive", "active")
        for state in ("active", "offered", "reserved"):
            with pytest.raises(sqlite3.IntegrityError):
                await claim(f"clm_dup_{state}", "exclusive", state)
        await claim("clm_2", "shared", "active")
        await claim("clm_3", "exclusive", "released")  # ended claims do not hold the zone
        await claim("clm_4", "exclusive", "active", zone="zn_reports")
        with pytest.raises(sqlite3.IntegrityError):
            await claim("clm_5", "bogus", "active", zone="zn_x")

        with pytest.raises(sqlite3.IntegrityError):
            await db.conn.execute("INSERT INTO crew_members VALUES ('crw_1', 'u', 'superuser', NULL, 't')")
        await db.conn.rollback()

        async def report(rid: str, current: int) -> None:
            await db.conn.execute(
                """INSERT INTO crew_reports (id, crew_id, task_id, session_id, kind, facts_source, facts_hash,
                                            is_current, created_at)
                   VALUES (?, 'crw_1', 'tsk_1', 'cs_1', 'stalled', 'server-inferred', ?, ?, 't')""",
                (rid, rid, current),
            )
            await db.conn.commit()

        await report("rpt_1", 1)
        with pytest.raises(sqlite3.IntegrityError):
            await report("rpt_2", 1)
        await report("rpt_3", 0)

        async def inbox(iid: str, state: str) -> None:
            await db.conn.execute(
                """INSERT INTO crew_inbox_items (id, crew_id, audience, kind, title, state, dedupe_key, created_at, updated_at)
                   VALUES (?, 'crw_1', 'project', 'baton_available', 'x', ?, 'baton:T-1', 't', 't')""",
                (iid, state),
            )
            await db.conn.commit()

        await inbox("inb_1", "open")
        with pytest.raises(sqlite3.IntegrityError):
            await inbox("inb_2", "seen")
        await inbox("inb_3", "resolved")

        async def session(sid: str, state: str) -> None:
            await db.conn.execute(
                """INSERT INTO crew_sessions (id, crew_id, user_id, agent_id, session_id, member_key, callsign, state,
                                              joined_at, token_hash) VALUES (?, 'crw_1', 'u', 'codex', ?, 'codex:mbp:a1b2c3d4',
                                              'codex-1', ?, 't', 'h')""",
                (sid, sid, state),
            )
            await db.conn.commit()

        await session("cs_1", "active")
        with pytest.raises(sqlite3.IntegrityError):
            await session("cs_2", "idle")
        await db.conn.execute("UPDATE crew_sessions SET state = 'ended' WHERE id = 'cs_1'")
        await db.conn.commit()
        await session("cs_3", "joining")  # callsign reusable once the holder ended
    finally:
        await db.close()


def test_resolve_crew_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMEMBRA_CREW_DB_PATH", raising=False)
    assert resolve_crew_db_path("sqlite+aiosqlite:////data/remembra.db") == "/data/crew.db"
    assert resolve_crew_db_path("sqlite+aiosqlite:///remembra.db") == "crew.db"
    assert resolve_crew_db_path(str(tmp_path / "x" / "main.db")) == str(tmp_path / "x" / "crew.db")
    assert resolve_crew_db_path(":memory:") == ":memory:"
    assert resolve_crew_db_path("/data/remembra.db", "/elsewhere/c.db") == "/elsewhere/c.db"
    monkeypatch.setenv("REMEMBRA_CREW_DB_PATH", "/env/crew.db")
    assert resolve_crew_db_path("/data/remembra.db") == "/env/crew.db"


async def test_in_memory_crew_db(tmp_path: Path) -> None:
    db = await open_crew_db(":memory:")
    try:
        assert await db.get_schema_version() == 1
    finally:
        await db.close()
