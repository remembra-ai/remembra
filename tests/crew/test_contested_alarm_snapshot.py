"""Contested alarms use committed reads and fresh writer-side decisions."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
from datetime import UTC, datetime, timedelta

import pytest

from remembra.crew import alarms, claims as C, zones as Z
from remembra.crew.events import Actor
from remembra.crew.store import now_iso
from tests.crew.test_alarm_snapshot import counters
from tests.crew.wp5_support import CREW, ZONES_YML, make_ops, open_db, seed_crew, seed_session, zone_id


@pytest.fixture
async def world(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    a, _ = await seed_session(db, "cs_holder", worktree_id="wt-a")
    b, _ = await seed_session(db, "cs_waiter", worktree_id="wt-b", callsign="cc-2")
    await Z.upload_zones_file(ops, CREW, Z.Principal.for_session(a), yaml_text=ZONES_YML, sha="s1", branch="main")
    zid = await zone_id(db, "pos")
    claim = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=zid)
    assert claim.claim is not None
    try:
        yield db, ops, a, b, zid
    finally:
        await db.close()


async def wait_long(world, now):
    db, ops, _a, b, zid = world
    queued = await C.request_claim(ops, CREW, Z.Principal.for_session(b), zone_id=zid, wait=True)
    assert queued.claim is not None
    async with db.transaction():
        await db.conn.execute(
            "UPDATE crew_claims SET created_at=? WHERE id=?", (now_iso(now - timedelta(minutes=6)), queued.claim["id"])
        )
    return queued.claim["id"]


async def test_uncontested_scan_completes_while_writer_is_open(world):
    db, ops, *_ = world
    out = counters()
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET callsign='pending' WHERE id='cs_holder'")
        await asyncio.wait_for(
            asyncio.create_task(alarms._contested(ops.log, datetime.now(UTC), out), context=Context()), timeout=1
        )
    assert out == counters()


async def test_existing_contested_alarm_does_not_enter_writer_queue(world):
    db, ops, *_ = world
    now = datetime.now(UTC)
    await wait_long(world, now)
    first = counters()
    await alarms._contested(ops.log, now, first)
    assert first["contested"] == 1
    out = counters()
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET callsign='pending' WHERE id='cs_holder'")
        await asyncio.wait_for(asyncio.create_task(alarms._contested(ops.log, now, out), context=Context()), timeout=1)
    assert out == counters()


async def test_queue_canceled_before_writer_lock_does_not_raise_stale_alarm(world, monkeypatch):
    db, ops, *_ = world
    now = datetime.now(UTC)
    qid = await wait_long(world, now)
    original = ops.log.transaction

    @asynccontextmanager
    async def changed_transaction():
        async with db.transaction():
            await db.conn.execute("UPDATE crew_claims SET state='released' WHERE id=?", (qid,))
        async with original() as tx:
            yield tx

    monkeypatch.setattr(ops.log, "transaction", changed_transaction)
    out = counters()
    await alarms._contested(ops.log, now, out)
    assert out["contested"] == 0
    assert not await db.fetchall("SELECT * FROM crew_inbox_items WHERE kind='zone_contested'")


async def test_new_waiter_before_resolution_keeps_alarm_open(world, monkeypatch):
    db, ops, _a, _b, zid = world
    now = datetime.now(UTC)
    qid = await wait_long(world, now)
    async with db.transaction():
        await db.conn.execute("UPDATE crew_claims SET state='released' WHERE id=?", (qid,))
    async with ops.log.transaction() as tx:
        await Z.raise_inbox_item(
            tx,
            CREW,
            audience="project",
            kind="zone_contested",
            title="zone pos contested",
            dedupe_key=f"contested:{zid}",
            ref_type="zone",
            ref_id=zid,
            priority=1,
            primary_action="hand_baton",
            actor=Actor.system(),
        )
    original = ops.log.transaction

    @asynccontextmanager
    async def changed_transaction():
        async with db.transaction():
            await db.conn.execute("UPDATE crew_claims SET state='queued' WHERE id=?", (qid,))
        async with original() as tx:
            yield tx

    monkeypatch.setattr(ops.log, "transaction", changed_transaction)
    out = counters()
    await alarms._contested(ops.log, now, out)
    assert out["uncontested"] == 0
    items = await db.fetchall("SELECT state FROM crew_inbox_items WHERE kind='zone_contested'")
    assert items == [{"state": "open"}]
