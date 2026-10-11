"""Main-DB migration 14 (``marshal_desk``): the Marshal desk's spend ledger, holds and opt-out.

A fresh database reaches 14 with the four tables and the held-reservation
index; a production database at 13 applies only 14 (its rows are kept); and
applying the list again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import remembra.storage.database as database
from remembra.storage.database import VERSIONED_MIGRATIONS, Database, main_migrations

TABLES = {"marshal_budget", "marshal_user_day", "marshal_reservations", "marshal_prefs"}


def _schema(path: Path) -> tuple[set[str], set[str]]:
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        conn.close()
    return tables, indexes


def test_version_14_is_the_latest_and_is_appended() -> None:
    assert main_migrations().latest_version == 14
    version, name, steps = VERSIONED_MIGRATIONS[-1]
    assert (version, name) == (14, "marshal_desk")
    assert [v for v, _, _ in VERSIONED_MIGRATIONS][:13] == list(range(1, 14))


async def test_a_fresh_database_reaches_14_with_every_table_and_the_index(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    db = Database(str(path))
    await db.connect()
    try:
        await db.init_schema()
        assert await db.get_schema_version() == 14
        # The CHECK constraints hold: no negative money, no unknown state.
        with pytest.raises(sqlite3.IntegrityError):
            await db.conn.execute(
                "INSERT INTO marshal_budget (period, reserved_micro, updated_at) VALUES ('day:2026-10-12', -1, 'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await db.conn.execute(
                "INSERT INTO marshal_reservations (id, user_id, day, month, reserved_micro, state, created_at)"
                " VALUES ('r', 'u', '2026-10-12', '2026-10', 20000, 'lost', 'x')"
            )
    finally:
        await db.close()
    tables, indexes = _schema(path)
    assert tables >= TABLES
    assert "idx_marshal_reservations_held" in indexes


async def test_a_database_at_13_applies_only_14_and_keeps_its_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "prod.db"
    with monkeypatch.context() as m:
        m.setattr(database, "VERSIONED_MIGRATIONS", [mig for mig in VERSIONED_MIGRATIONS if mig[0] <= 13])
        db = Database(str(path))
        await db.connect()
        try:
            await db.init_schema()
            assert await db.get_schema_version() == 13
            await db.conn.execute(
                "INSERT INTO relay_pickups (user_id, project_id, handoff_id, reader_agent, picked_up_at)"
                " VALUES ('u1', 'widget', 'h1', 'codex', '2026-10-01T00:00:00+00:00')"
            )
            await db.conn.commit()
        finally:
            await db.close()
    assert not (_schema(path)[0] & TABLES)

    db = Database(str(path))
    await db.connect()
    try:
        await db.conn.executescript(database.SCHEMA_SQL)
        applied = await main_migrations().apply(db.conn, db.transaction)
        assert applied == [14]
        # Applying again is a no-op.
        assert await main_migrations().apply(db.conn, db.transaction) == []
        await db.init_schema()
        assert await db.get_schema_version() == 14
        cursor = await db.conn.execute("SELECT COUNT(*) FROM relay_pickups WHERE handoff_id = 'h1'")
        assert (await cursor.fetchone())[0] == 1
        cursor = await db.conn.execute("SELECT name FROM schema_version WHERE version = 14")
        assert (await cursor.fetchone())[0] == "marshal_desk"
    finally:
        await db.close()
    assert _schema(path)[0] >= TABLES
