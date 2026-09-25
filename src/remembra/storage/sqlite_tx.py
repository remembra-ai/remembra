"""Transaction isolation for the single shared aiosqlite connection (REL-11).

Remembra keeps ONE aiosqlite connection per process. Python's sqlite3 opens
an implicit transaction on the first write and ~60 methods call ``commit()``,
so before this module any request's ``commit()`` could commit another
request's half-finished multi-statement write, and one ``rollback()`` could
discard someone else's work.

``TxCoordinator`` fixes that without rewriting every query method:

* ``Database.conn`` returns a :class:`GuardedConnection` proxy. Every
  ``execute``/``executemany``/``executescript``/``commit``/``rollback`` issued
  from *outside* the active transaction waits on an ``asyncio.Lock`` — so no
  foreign statement, commit or rollback can land inside an open transaction.
* ``Database.transaction()`` takes the lock, flushes any implicit transaction
  left by legacy code, issues ``BEGIN IMMEDIATE`` (write lock up front — no
  mid-transaction SQLITE_BUSY upgrade failures), and COMMITs on success /
  ROLLBACKs on any exception, including cancellation.
* Inside a transaction, legacy ``commit()`` calls are deferred to the
  transaction end, so existing multi-step methods become atomic simply by being
  called inside ``async with db.transaction():``. ``rollback()`` inside a
  transaction marks it rollback-only.
* Ownership is tracked with a ``ContextVar``: nested ``transaction()`` calls
  join the outer one, and tasks spawned inside inherit ownership (no
  self-deadlock on ``gather``/``wait_for``).

Rules for callers: keep transaction bodies to SQLite statements. Never await
network I/O (Qdrant, embeddings, HTTP) while holding a transaction — every
other request's DB access waits on it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import aiosqlite
import structlog

log = structlog.get_logger(__name__)


class TransactionRollbackOnly(RuntimeError):
    """Raised when a transaction body called ``rollback()`` and then returned normally."""


class _TxToken:
    __slots__ = ("rollback_only",)

    def __init__(self) -> None:
        self.rollback_only = False


_active_tx: ContextVar[_TxToken | None] = ContextVar("remembra_sqlite_tx", default=None)


class TxCoordinator:
    """Serializes statements on one connection around explicit transactions."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._current: _TxToken | None = None

    @property
    def lock(self) -> asyncio.Lock:
        # asyncio.Lock binds to the running loop on first contention; recreate
        # it if the Database is reused on a new loop (test suites do this).
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def owns(self) -> bool:
        token = _active_tx.get()
        return token is not None and token is self._current

    @property
    def in_transaction(self) -> bool:
        return self._current is not None

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run one statement: directly if we own the transaction, else under the lock."""
        if self.owns():
            return await factory()
        async with self.lock:
            return await factory()

    @asynccontextmanager
    async def transaction(self, raw: aiosqlite.Connection) -> AsyncIterator[None]:
        if self.owns():
            # Nested: join the outer transaction; the outermost block commits.
            yield
            return

        async with self.lock:
            token = _TxToken()
            self._current = token
            ctx_token = _active_tx.set(token)
            try:
                if raw.in_transaction:
                    # A legacy method issued a write and has not reached its
                    # commit() yet; that commit would be a no-op once we BEGIN.
                    await raw.commit()
                await raw.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    await _safe_rollback(raw)
                    raise
                if token.rollback_only:
                    await _safe_rollback(raw)
                    raise TransactionRollbackOnly("transaction body requested rollback")
                try:
                    await raw.commit()
                except BaseException:
                    await _safe_rollback(raw)
                    raise
            finally:
                self._current = None
                _active_tx.reset(ctx_token)


async def _safe_rollback(raw: aiosqlite.Connection) -> None:
    try:
        await raw.rollback()
    except Exception as e:  # never mask the original error
        log.error("sqlite_rollback_failed", error=str(e))


class _GuardedResult:
    """Awaitable (and async-context-manager) result of a guarded execute."""

    __slots__ = ("_coord", "_factory", "_cursor")

    def __init__(self, coord: TxCoordinator, factory: Callable[[], Awaitable[Any]]) -> None:
        self._coord = coord
        self._factory = factory
        self._cursor: Any = None

    def __await__(self) -> Generator[Any, None, Any]:
        return self._coord.run(self._factory).__await__()

    async def __aenter__(self) -> Any:
        self._cursor = await self._coord.run(self._factory)
        return self._cursor

    async def __aexit__(self, *exc: object) -> None:
        if self._cursor is not None:
            await self._cursor.close()


class GuardedConnection:
    """Proxy for ``aiosqlite.Connection`` enforcing :class:`TxCoordinator` rules.

    Anything not overridden (``row_factory``, ``close``, ``in_transaction``,
    ``total_changes`` ...) passes straight through to the raw connection.
    """

    __slots__ = ("_raw", "_coord")

    def __init__(self, raw: aiosqlite.Connection, coord: TxCoordinator) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_coord", coord)

    @property
    def raw(self) -> aiosqlite.Connection:
        raw: aiosqlite.Connection = object.__getattribute__(self, "_raw")
        return raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._raw, name, value)

    def execute(self, sql: str, parameters: Iterable[Any] | None = None) -> _GuardedResult:
        raw = self._raw
        if parameters is None:
            return _GuardedResult(self._coord, lambda: raw.execute(sql))
        return _GuardedResult(self._coord, lambda: raw.execute(sql, parameters))

    def executemany(self, sql: str, parameters: Iterable[Iterable[Any]]) -> _GuardedResult:
        raw = self._raw
        return _GuardedResult(self._coord, lambda: raw.executemany(sql, parameters))

    def executescript(self, sql_script: str) -> _GuardedResult:
        if self._coord.owns():
            # sqlite3.executescript() COMMITs any open transaction first, which
            # would silently end ours.
            raise RuntimeError("executescript() cannot run inside db.transaction(); use execute()")
        raw = self._raw
        return _GuardedResult(self._coord, lambda: raw.executescript(sql_script))

    async def commit(self) -> None:
        if self._coord.owns():
            return  # deferred to the end of the enclosing transaction
        await self._coord.run(self._raw.commit)

    async def rollback(self) -> None:
        if self._coord.owns():
            token = _active_tx.get()
            if token is not None:
                token.rollback_only = True
            log.warning("sqlite_rollback_inside_transaction_marked_rollback_only")
            return
        await self._coord.run(self._raw.rollback)
