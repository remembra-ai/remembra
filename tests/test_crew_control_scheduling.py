"""Crew lease scheduling must stay fair, cancellable and transaction isolated."""

import asyncio
from contextlib import nullcontext

import aiosqlite
import pytest

from remembra.storage.sqlite_tx import GuardedConnection, TxCoordinator, sqlite_control_lane


async def queued_job(coord, order, label, control=False):
    async def record():
        order.append(label)

    with sqlite_control_lane() if control else nullcontext():
        await coord.run(record)


async def test_control_priority_preserves_fifo_and_bounded_normal_progress():
    coord = TxCoordinator(control_priorities=True)
    order = []
    async with coord.lock:
        jobs = [asyncio.create_task(queued_job(coord, order, f"n{i}")) for i in range(2)]
        jobs += [asyncio.create_task(queued_job(coord, order, f"c{i}", True)) for i in range(20)]
        await asyncio.sleep(0)
        assert order == []  # priority cannot interrupt the existing holder
    await asyncio.wait_for(asyncio.gather(*jobs), 2)
    assert order == [
        *[f"c{i}" for i in range(8)],
        "n0",
        *[f"c{i}" for i in range(8, 16)],
        "n1",
        *[f"c{i}" for i in range(16, 20)],
    ]


async def test_main_database_keeps_fifo_even_for_control_context():
    coord = TxCoordinator()
    order = []
    async with coord.lock:
        jobs = [
            asyncio.create_task(queued_job(coord, order, "normal")),
            asyncio.create_task(queued_job(coord, order, "control", True)),
        ]
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(*jobs), 2)
    assert order == ["normal", "control"]


@pytest.mark.parametrize("cancel_after_grant", [False, True])
async def test_cancelled_control_waiter_never_strands_writer(cancel_after_grant):
    coord = TxCoordinator(control_priorities=True)
    order = []
    await coord.lock.acquire()
    cancelled = asyncio.create_task(queued_job(coord, order, "cancelled", True))
    normal = asyncio.create_task(queued_job(coord, order, "normal"))
    await asyncio.sleep(0)
    if cancel_after_grant:
        coord.lock.release()
        cancelled.cancel()  # grant happened, but the coroutine has not resumed
    else:
        cancelled.cancel()
        coord.lock.release()  # also exercises cancellation before queue cleanup runs
    result = await asyncio.gather(cancelled, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    await asyncio.wait_for(normal, 2)
    await asyncio.wait_for(queued_job(coord, order, "next"), 2)
    assert order == ["normal", "next"]


async def test_control_reader_cannot_enter_foreign_transaction(tmp_path):
    coord = TxCoordinator(control_priorities=True)
    async with aiosqlite.connect(tmp_path / "control.db") as raw:
        conn = GuardedConnection(raw, coord)
        await conn.execute("CREATE TABLE example (value TEXT)")
        await conn.commit()
        pending = asyncio.Event()
        release = asyncio.Event()

        async def writer():
            async with coord.transaction(raw):
                await conn.execute("INSERT INTO example VALUES ('pending')")
                async with coord.transaction(raw):
                    assert (await conn.execute_fetchall("SELECT value FROM example"))[0][0] == "pending"
                pending.set()
                await release.wait()
                raise RuntimeError("rollback")

        async def reader():
            await pending.wait()
            with sqlite_control_lane():
                return await conn.execute_fetchall("SELECT value FROM example")

        writing = asyncio.create_task(writer())
        reading = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(pending.wait(), 2)
            await asyncio.sleep(0.02)
            assert not reading.done()
        finally:
            release.set()
            result = await asyncio.gather(writing, reading, return_exceptions=True)
        assert isinstance(result[0], RuntimeError)
        assert result[1] == []
