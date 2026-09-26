"""Pre-migration backups: startup copies the SQLite database before any schema
change, once per build, and refuses to migrate when the copy cannot be made."""

import os
import sqlite3
import stat
from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient

from remembra.storage import backup as backup_mod
from remembra.storage.backup import BACKUP_PREFIX, BackupError, backup_label, pre_migration_backup
from remembra.storage.database import VERSIONED_MIGRATIONS, Database
from remembra.storage.qdrant import QdrantStore

LATEST_VERSION = max(v for v, _, _ in VERSIONED_MIGRATIONS)
# The version before the newest entry (numbers may skip: 5 belongs to feat/crew).
PREVIOUS_VERSION = max(v for v, _, _ in VERSIONED_MIGRATIONS[:-1])


def _make_db(path: Path, rows: int = 25) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    conn.executemany("INSERT INTO notes (body) VALUES (?)", [(f"note {i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{BACKUP_PREFIX}*.db"))


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_backup_copies_the_database_with_private_permissions(tmp_path):
    db = tmp_path / "remembra.db"
    _make_db(db, rows=25)

    dest = pre_migration_backup(str(db), label="abc123")

    assert dest is not None and dest.parent == tmp_path / "backups"
    assert dest.name.startswith(f"{BACKUP_PREFIX}abc123-")
    assert _count(dest, "notes") == 25
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700
    assert not list(dest.parent.glob("*.partial"))


def test_backup_includes_rows_still_in_the_wal(tmp_path):
    db = tmp_path / "remembra.db"
    _make_db(db, rows=3)
    live = sqlite3.connect(db)  # keeps the WAL un-checkpointed while we back up
    live.execute("INSERT INTO notes (body) VALUES ('only in the wal')")
    live.commit()
    try:
        dest = pre_migration_backup(str(db), label="wal")
    finally:
        live.close()
    assert dest is not None
    assert _count(dest, "notes") == 4


def test_same_build_is_backed_up_once(tmp_path):
    db = tmp_path / "remembra.db"
    _make_db(db)

    first = pre_migration_backup(str(db), label="build1")
    again = pre_migration_backup(str(db), label="build1")

    assert first is not None and again is None
    assert len(_backups(tmp_path / "backups")) == 1


def test_new_builds_are_pruned_to_keep(tmp_path):
    db = tmp_path / "remembra.db"
    _make_db(db)
    made = []
    for i in range(5):
        path = pre_migration_backup(str(db), label=f"build{i}", keep=3)
        assert path is not None
        os.utime(path, (1_000_000 + i, 1_000_000 + i))  # deterministic newest-first order
        made.append(path)

    remaining = _backups(tmp_path / "backups")
    assert len(remaining) == 3
    assert {p.name for p in remaining} == {p.name for p in made[-3:]}


@pytest.mark.parametrize("path", [":memory:", "file::memory:?cache=shared", ""])
def test_in_memory_databases_are_skipped(path):
    assert pre_migration_backup(path, label="x") is None


def test_missing_or_empty_file_is_skipped(tmp_path):
    assert pre_migration_backup(str(tmp_path / "absent.db"), label="x") is None
    empty = tmp_path / "empty.db"
    empty.touch()
    assert pre_migration_backup(str(empty), label="x") is None
    assert not (tmp_path / "backups").exists()


def test_custom_backup_dir(tmp_path):
    db = tmp_path / "remembra.db"
    _make_db(db)
    target = tmp_path / "elsewhere" / "nested"
    dest = pre_migration_backup(str(db), label="x", backup_dir=str(target))
    assert dest is not None and dest.parent == target


def test_not_enough_disk_raises_and_leaves_nothing(tmp_path, monkeypatch):
    db = tmp_path / "remembra.db"
    _make_db(db)

    class Tiny:
        free = 1024

    monkeypatch.setattr(backup_mod.shutil, "disk_usage", lambda _p: Tiny())
    with pytest.raises(BackupError, match="not enough free space"):
        pre_migration_backup(str(db), label="x")
    assert not _backups(tmp_path / "backups")
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_copy_failure_raises_and_removes_the_partial_file(tmp_path, monkeypatch):
    db = tmp_path / "remembra.db"
    _make_db(db)
    real_connect = sqlite3.connect

    class Exploding:
        def __init__(self, conn):
            self._conn = conn

        def backup(self, _target):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            self._conn.close()

    calls = {"n": 0}

    def connect(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        calls["n"] += 1
        return Exploding(conn) if calls["n"] == 1 else conn

    monkeypatch.setattr(backup_mod.sqlite3, "connect", connect)
    with pytest.raises(BackupError, match="disk I/O error"):
        pre_migration_backup(str(db), label="x")
    assert not list((tmp_path / "backups").iterdir())


def test_keep_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        pre_migration_backup(str(tmp_path / "x.db"), label="x", keep=0)


def test_backup_label_is_filesystem_safe():
    assert backup_label("0123456789abcdef0123", "0.16.0") == "0123456789ab"
    assert backup_label(None, "0.16.0") == "0.16.0"
    assert backup_label("  ", "") == "unknown"
    assert backup_label("../../etc/passwd", "v") == ".._.._etc_pa"


async def _legacy_database(path: Path, monkeypatch) -> None:
    """A database at the previous schema (every versioned migration except the
    newest applied), holding a user row we can look for in the backup."""
    import remembra.storage.database as database

    with monkeypatch.context() as m:
        m.setattr(database, "VERSIONED_MIGRATIONS", VERSIONED_MIGRATIONS[:-1])
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()
        try:
            await db.init_schema()
            await db.conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES ('u-1', 'a@example.com', 'x', '2026-01-01')"
            )
            await db.conn.commit()
            assert await db.get_schema_version() == PREVIOUS_VERSION
        finally:
            await db.close()


async def test_init_schema_backs_up_before_migrating(tmp_path, monkeypatch):
    path = tmp_path / "remembra.db"
    await _legacy_database(path, monkeypatch)

    db = Database(f"sqlite+aiosqlite:///{path}", backup_before_migrate=True, backup_label="deploy1")
    await db.connect()
    try:
        await db.init_schema()
        assert await db.get_schema_version() == LATEST_VERSION
    finally:
        await db.close()

    [copy] = _backups(tmp_path / "backups")
    conn = sqlite3.connect(copy)
    try:
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
        users = conn.execute("SELECT id FROM users").fetchall()
    finally:
        conn.close()
    # The copy is the database as it was BEFORE the pending migration ran.
    assert LATEST_VERSION not in applied
    assert users == [("u-1",)]


async def test_init_schema_refuses_to_migrate_when_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / "remembra.db"
    await _legacy_database(path, monkeypatch)

    def fail(*_a, **_k):
        raise BackupError("no space")

    monkeypatch.setattr("remembra.storage.database.pre_migration_backup", fail)
    db = Database(f"sqlite+aiosqlite:///{path}", backup_before_migrate=True, backup_label="deploy1")
    await db.connect()
    try:
        with pytest.raises(BackupError):
            await db.init_schema()
        assert await db.get_schema_version() == PREVIOUS_VERSION  # untouched
    finally:
        await db.close()


async def test_library_default_takes_no_backup(tmp_path, monkeypatch):
    path = tmp_path / "remembra.db"
    await _legacy_database(path, monkeypatch)
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.connect()
    try:
        await db.init_schema()
    finally:
        await db.close()
    assert not (tmp_path / "backups").exists()


async def test_app_startup_backs_up_an_existing_database(tmp_path, monkeypatch):
    import remembra.config
    import remembra.main as main

    path = tmp_path / "prod.db"
    await _legacy_database(path, monkeypatch)
    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "backup_boot_test")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_BUILD_SHA", "feedfacecafe1234")
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self):  # noqa: ANN001
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)

    app = main.create_app()
    async with app.router.lifespan_context(app):
        assert await app.state.db.get_schema_version() == LATEST_VERSION
    # Same build restarting: no second copy.
    app = main.create_app()
    async with app.router.lifespan_context(app):
        pass
    monkeypatch.setattr(remembra.config, "_settings", None)

    copies = _backups(tmp_path / "backups")
    assert [c.name.split("-")[2] for c in copies] == ["feedfacecafe"]
