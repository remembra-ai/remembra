"""Authorization reads see committed state without joining the writer queue."""

import asyncio
from contextvars import Context

import aiosqlite
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from remembra.api.v1 import crew_zones
from remembra.auth.middleware import AuthenticatedUser
from remembra.crew import access
from tests.crew.wp5_support import CREW, OWNER, open_db, seed_crew, seed_session


@pytest.fixture
async def world(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    session, token = await seed_session(db, "cs_reader")
    app = FastAPI()
    app.state.crew_db = db
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "app": app,
            "path_params": {"crew_id": CREW, "session_id": session["id"]},
            "headers": [(b"x-remembra-crew-session", token.encode())],
        }
    )
    user = AuthenticatedUser(OWNER, "test_key", "standard")
    try:
        async with crew_zones.router.lifespan_context(app):
            yield db, request, user
    finally:
        await db.close()


@pytest.mark.parametrize("entity", [False, True])
async def test_access_checks_do_not_wait_for_unrelated_writer(world, entity):
    db, request, user = world
    dep = access.crew_entity("session", "crew:read") if entity else access.crew_access("crew:read")
    async with db.transaction():
        await db.conn.execute("UPDATE crews SET name='uncommitted' WHERE id=?", (CREW,))
        result = await asyncio.wait_for(asyncio.create_task(dep(request, user), context=Context()), timeout=1)
    crew = result.access.crew if entity else result.crew
    assert crew.name == "yaadbooks"


async def test_principal_lookup_does_not_wait_for_unrelated_writer(world):
    db, request, user = world
    granted = await access.crew_access("crew:claim")(request, user)
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET callsign='uncommitted' WHERE id='cs_reader'")
        principal = await asyncio.wait_for(
            asyncio.create_task(crew_zones.principal_for(request, granted), context=Context()), timeout=1
        )
    assert principal.session["callsign"] == "cc-1"


async def test_committed_revocation_is_seen_on_next_principal_lookup(world):
    db, request, user = world
    granted = await access.crew_access("crew:claim")(request, user)
    assert (await crew_zones.principal_for(request, granted)).session_id == "cs_reader"
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET state='ended' WHERE id='cs_reader'")
    with pytest.raises(HTTPException) as error:
        await crew_zones.principal_for(request, granted)
    assert error.value.status_code == 401


async def test_project_restriction_still_returns_generic_404(world):
    _, request, _ = world
    user = AuthenticatedUser(OWNER, "restricted_key", "standard", project_ids=["another-project"])
    with pytest.raises(HTTPException) as error:
        await access.crew_access("crew:read")(request, user)
    assert error.value.status_code == 404
    assert error.value.detail == {"error": "not_found", "message": "Not found."}


async def test_repeated_access_reuses_reader_instead_of_starting_a_thread_per_request(world, monkeypatch):
    _, request, user = world
    original = aiosqlite.connect
    opened = []

    def measured(*args, **kwargs):
        opened.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", measured)
    for _ in range(20):
        assert (await access.crew_access("crew:read")(request, user)).crew_id == CREW
    assert len(opened) == 1


async def test_reader_is_query_only_and_closed_by_router_lifespan(tmp_path):
    db = await open_db(tmp_path)
    app = FastAPI()
    app.state.crew_db = db
    request = Request({"type": "http", "method": "GET", "path": "/", "app": app, "headers": []})
    try:
        async with crew_zones.router.lifespan_context(app), access.access_snapshot(request) as reader:
            with pytest.raises(aiosqlite.OperationalError, match="readonly"):
                await reader.execute("UPDATE crews SET name='forbidden'")
        with pytest.raises(ValueError, match="no active connection"):
            await reader.execute("SELECT 1")
    finally:
        await db.close()


async def test_pool_bounds_readers_and_cancellation_releases_slot(tmp_path):
    from remembra.crew.read_pool import CrewReadPool

    db = await open_db(tmp_path)
    pool = CrewReadPool(size=2)
    ready = asyncio.Queue()
    release = asyncio.Event()

    async def borrow():
        async with pool.snapshot(db.db_path) as reader:
            await reader.execute("SELECT 1")
            await ready.put(reader)
            await release.wait()

    first = asyncio.create_task(borrow())
    second = asyncio.create_task(borrow())
    waiter = asyncio.create_task(borrow())
    try:
        assert await asyncio.wait_for(ready.get(), 1) is not await asyncio.wait_for(ready.get(), 1)
        assert ready.empty()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        async with asyncio.timeout(1), pool.snapshot(db.db_path) as reader:
            assert (await (await reader.execute("SELECT 42")).fetchone())[0] == 42
    finally:
        release.set()
        await asyncio.gather(first, second, waiter, return_exceptions=True)
        await pool.close()
        await db.close()


async def test_cancelled_shutdown_can_be_awaited_again(tmp_path):
    from remembra.crew.read_pool import CrewReadPool

    db = await open_db(tmp_path)
    pool = CrewReadPool(size=1)
    try:
        async with pool.snapshot(db.db_path) as reader:
            close = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close
            assert (await (await reader.execute("SELECT 1")).fetchone())[0] == 1
        await asyncio.wait_for(pool.close(), 1)
        with pytest.raises(ValueError, match="no active connection"):
            await reader.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="closed"):
            async with pool.snapshot(db.db_path):
                pass
    finally:
        await pool.close()
        await db.close()
