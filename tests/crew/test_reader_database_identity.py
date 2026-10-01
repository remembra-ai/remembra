"""Reused authorization readers remain bound to one database file."""

import os
from pathlib import Path

import pytest

from remembra.crew.read_pool import CrewReadPool
from tests.crew.wp5_support import open_db


async def test_repeated_borrow_does_not_rewalk_unchanged_database_path(tmp_path, monkeypatch):
    db = await open_db(tmp_path)
    pool = CrewReadPool()
    original = Path.resolve
    resolved = []

    def measured(path, *args, **kwargs):
        resolved.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", measured)
    try:
        for _ in range(10):
            async with pool.snapshot(db.db_path) as reader:
                assert (await (await reader.execute("SELECT 1")).fetchone())[0] == 1
        assert resolved == [db.db_path]
    finally:
        await pool.close()
        await db.close()


async def test_replacing_database_file_cannot_mix_old_and_new_authorization(tmp_path):
    db = await open_db(tmp_path)
    pool = CrewReadPool()
    try:
        async with pool.snapshot(db.db_path):
            pass
        replacement = tmp_path / "replacement.db"
        replacement.write_bytes(b"")
        os.replace(replacement, db.db_path)
        with pytest.raises(RuntimeError, match="database file changed"):
            async with pool.snapshot(db.db_path):
                pytest.fail("Reused auth reader silently accepted a different database file")
    finally:
        await pool.close()
        await db.close()
