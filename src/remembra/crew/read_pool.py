"""Bounded, query-only WAL readers for request authorization.

Each borrow starts and ends its own snapshot. Connections are reused so frequent
Crew requests do not start a SQLite worker thread for every read. State changes
remain on CrewDatabase's serialized writer connection.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from remembra.crew.db import CREW_BUSY_TIMEOUT_MS


class CrewReadPool:
    def __init__(self, size: int = 4) -> None:
        if size < 1:
            raise ValueError("Crew reader pool size must be positive")
        self._size = size
        self._slots = asyncio.Semaphore(size)
        self._available: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._readers: list[aiosqlite.Connection] = []
        self._uri: str | None = None
        self._path: str | None = None
        self._file_identity: tuple[int, int] | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def _open(self, uri: str) -> aiosqlite.Connection:
        # Shield connection creation so cancellation cannot orphan its worker.
        connect = asyncio.ensure_future(aiosqlite.connect(uri, uri=True))
        try:
            reader = await asyncio.shield(connect)
        except asyncio.CancelledError:
            reader = await connect
            await reader.close()
            raise
        try:
            reader.row_factory = aiosqlite.Row
            await reader.execute(f"PRAGMA busy_timeout = {CREW_BUSY_TIMEOUT_MS}")
            await reader.execute("PRAGMA query_only = ON")
        except BaseException:
            await reader.close()
            raise
        self._readers.append(reader)
        return reader

    @asynccontextmanager
    async def snapshot(self, path: str) -> AsyncIterator[aiosqlite.Connection]:
        # Resolve the configured path once, rather than walking each parent
        # directory on every authorization lookup. Still detect replacement:
        # reusing an old connection after a file swap could mix auth databases.
        if self._file_identity is None:
            stat = os.stat(path)
            self._file_identity = (stat.st_dev, stat.st_ino)
        if self._path != path:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            if self._uri is not None and self._uri != uri:
                raise RuntimeError("A Crew reader pool cannot switch databases")
            self._uri, self._path = uri, path
        assert self._uri is not None
        uri = self._uri
        async with self._slots:
            if self._closed:
                raise RuntimeError("Crew reader pool is closed")
            try:
                reader = self._available.get_nowait()
            except asyncio.QueueEmpty:
                reader = await self._open(uri)
            try:
                await reader.execute("BEGIN")
                # A borrower may wait for a slot or connection creation. Check
                # after those awaits, before exposing an authorization reader.
                stat = os.stat(path)
                if self._file_identity != (stat.st_dev, stat.st_ino):
                    raise RuntimeError("Crew reader database file changed; restart required")
                yield reader
            finally:
                try:
                    await reader.rollback()
                except BaseException:
                    self._readers.remove(reader)
                    await reader.close()
                    raise
                else:
                    self._available.put_nowait(reader)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        acquired = 0
        try:
            # Borrowers finish before their connections are closed. Waiters see
            # the closed flag when they acquire a slot and cannot start a read.
            for _ in range(self._size):
                await self._slots.acquire()
                acquired += 1
            await asyncio.gather(*(reader.close() for reader in self._readers))
            self._readers.clear()
        finally:
            for _ in range(acquired):
                self._slots.release()
