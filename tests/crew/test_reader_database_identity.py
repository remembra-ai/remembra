"""Reused authorization readers remain bound to one database file."""

import asyncio
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


async def test_queued_reader_rechecks_identity_after_waiting_for_slot(tmp_path):
    db = await open_db(tmp_path)
    pool = CrewReadPool(size=1)

    async def borrow():
        async with pool.snapshot(db.db_path):
            return "authorized from the stale database"

    waiter = None
    try:
        async with pool.snapshot(db.db_path):
            waiter = asyncio.create_task(borrow())
            await asyncio.sleep(0)
            replacement = tmp_path / "replacement.db"
            replacement.write_bytes(b"")
            os.replace(replacement, db.db_path)
        with pytest.raises(RuntimeError, match="database file changed"):
            await asyncio.wait_for(waiter, 1)
    finally:
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        await pool.close()
        await db.close()


async def test_database_swap_during_connection_open_is_rejected(tmp_path, monkeypatch):
    db = await open_db(tmp_path)
    pool = CrewReadPool(size=1)
    original = pool._open

    async def swapped(uri):
        reader = await original(uri)
        replacement = tmp_path / "replacement.db"
        replacement.write_bytes(b"")
        os.replace(replacement, db.db_path)
        return reader

    monkeypatch.setattr(pool, "_open", swapped)
    try:
        with pytest.raises(RuntimeError, match="database file changed"):
            async with pool.snapshot(db.db_path):
                pytest.fail("Database changed during connection creation was accepted")
    finally:
        await pool.close()
        await db.close()
