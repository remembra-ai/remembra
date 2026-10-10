"""The alarm sweep's discovery reads are batched; every mutation is still decided under the writer lock.

One reaper pass used to ask ``crew_events`` once per overdue session (``checkpoint.missed``) and once per
crew with a held zone (``guard.blocked``), and ``crew_claims`` once per held zone (its queue). With 50
crews of 5 sessions that is about 350 statements per pass. The pass now asks once per bounded batch.
The answers must be the same as before: a session is nudged only when it has no ``checkpoint.missed``
since *its own* last checkpoint, one crew's blocks or queue never count for another crew's zone, and a
contested alarm is raised only if the zone is still contested when the writer gets to it.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from remembra.crew import alarms, claims as C, zones as Z
from remembra.crew.events import Actor, CrewEventLog, format_ts
from remembra.crew.settings import default_settings
from tests.crew.wp5_support import ZONES_YML, make_ops, open_db, seed_crew, seed_session, zone_id


def counters():
    return {"checkpoint_missed": 0, "stuck": 0, "unstuck": 0, "contested": 0, "uncontested": 0}


async def settings_for(_):
    return default_settings()


def crew_name(i: int) -> str:
    return f"crw_{i:016x}"


def record_reads(monkeypatch) -> list[tuple[str, tuple]]:
    """Every statement the alarms module sends through its read helpers, in order, with its values."""
    seen: list[tuple[str, tuple]] = []
    real_all, real_one = alarms.fetchall, alarms.fetchone

    async def counted_all(conn, sql, params=()):
        seen.append((sql, tuple(params)))
        return await real_all(conn, sql, params)

    async def counted_one(conn, sql, params=()):
        seen.append((sql, tuple(params)))
        return await real_one(conn, sql, params)

    monkeypatch.setattr(alarms, "fetchall", counted_all)
    monkeypatch.setattr(alarms, "fetchone", counted_one)
    return seen


def event_reads(seen) -> int:
    return sum("crew_events" in sql for sql, _ in seen)


def queue_reads(seen) -> int:
    return sum("state = 'queued'" in sql for sql, _ in seen)


async def overdue_sessions(db, crews: int, per_crew: int, now: datetime) -> list[str]:
    """Active sessions whose checkpoint is 25 minutes old (default interval 10 min: missed, not yet stuck)."""
    ids = []
    for c in range(crews):
        crew_id = await seed_crew(db, crew_name(c + 1), project=f"project-{c}")
        for s in range(per_crew):
            sid = f"cs_{c}_{s}"
            await seed_session(db, sid, crew_id=crew_id, callsign=f"cc-{s + 1}")
            ids.append(sid)
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET next_checkpoint_due_at = ?", (format_ts(now - timedelta(minutes=15)),))
    return ids


async def steady_state_stuck_reads(tmp_path, monkeypatch, crews: int, per_crew: int) -> int:
    db = await open_db(tmp_path)
    try:
        now = datetime.now(UTC)
        ids = await overdue_sessions(db, crews, per_crew, now)
        log = CrewEventLog(db, None)
        first = counters()
        await alarms._stuck(log, now, settings_for, first)
        assert first["checkpoint_missed"] == len(ids)  # every overdue session got its one nudge
        seen = record_reads(monkeypatch)
        again = counters()
        await alarms._stuck(log, now + timedelta(seconds=30), settings_for, again)
        assert again == counters()  # already nudged: nothing to change, no transaction
        return event_reads(seen)
    finally:
        await db.close()


async def test_unchanged_stuck_scan_event_reads_do_not_grow_with_sessions(tmp_path, monkeypatch):
    small = await steady_state_stuck_reads(tmp_path / "small", monkeypatch, crews=2, per_crew=2)
    large = await steady_state_stuck_reads(tmp_path / "large", monkeypatch, crews=6, per_crew=4)
    assert small == large, f"event reads grew from {small} (4 sessions) to {large} (24 sessions)"
    assert large <= 1


async def test_batched_lookup_judges_each_session_by_its_own_last_checkpoint(tmp_path):
    db = await open_db(tmp_path)
    try:
        start = datetime.now(UTC) - timedelta(hours=3)
        await overdue_sessions(db, crews=1, per_crew=2, now=start)
        log = CrewEventLog(db, None)
        first = counters()
        await alarms._stuck(log, start, settings_for, first)
        assert first["checkpoint_missed"] == 2
        # cs_0_1 checkpoints after its nudge and then goes quiet again; cs_0_0 never checkpoints.
        later = start + timedelta(hours=2)
        async with db.transaction():
            await db.conn.execute(
                "UPDATE crew_sessions SET next_checkpoint_due_at = ? WHERE id = 'cs_0_1'",
                (format_ts(later - timedelta(minutes=15)),),
            )
        out = counters()
        await alarms._stuck(log, later, settings_for, out)
        assert out["checkpoint_missed"] == 1
        rows = await db.fetchall(
            "SELECT session_id, COUNT(*) AS n FROM crew_events WHERE type = 'checkpoint.missed' GROUP BY session_id"
        )
        assert {r["session_id"]: r["n"] for r in rows} == {"cs_0_0": 1, "cs_0_1": 2}
    finally:
        await db.close()


async def test_small_session_batches_give_the_same_nudges(tmp_path, monkeypatch):
    monkeypatch.setattr(alarms, "DISCOVERY_SESSIONS_PER_READ", 2, raising=False)
    db = await open_db(tmp_path)
    try:
        now = datetime.now(UTC)
        ids = await overdue_sessions(db, crews=3, per_crew=3, now=now)
        log = CrewEventLog(db, None)
        first = counters()
        await alarms._stuck(log, now, settings_for, first)
        assert first["checkpoint_missed"] == len(ids) == 9
        again = counters()
        await alarms._stuck(log, now + timedelta(seconds=30), settings_for, again)
        assert again == counters()
    finally:
        await db.close()


async def test_a_sweep_with_nothing_to_watch_reads_no_events(tmp_path, monkeypatch):
    db = await open_db(tmp_path)
    try:
        crew_id = await seed_crew(db, crew_name(1), project="project-0")
        await seed_session(db, "cs_fresh", crew_id=crew_id)  # joined just now: not overdue
        seen = record_reads(monkeypatch)
        assert await alarms.sweep(CrewEventLog(db, None), datetime.now(UTC), settings_for) == counters()
        assert event_reads(seen) == 0 and queue_reads(seen) == 0
    finally:
        await db.close()


async def held_zone_crews(db, crews: int):
    """Each crew: a holder with the ``pos`` zone and a second session. Returns [(crew_id, ops, waiter, zone_id)]."""
    out = []
    for c in range(crews):
        crew_id = await seed_crew(db, crew_name(c + 1), project=f"project-{c}")
        ops, _ = make_ops(db)
        holder, _ = await seed_session(db, f"cs_holder_{c}", crew_id=crew_id, worktree_id=f"wt-a-{c}")
        waiter, _ = await seed_session(db, f"cs_waiter_{c}", crew_id=crew_id, worktree_id=f"wt-b-{c}", callsign="cc-2")
        await Z.upload_zones_file(ops, crew_id, Z.Principal.for_session(holder), yaml_text=ZONES_YML, sha=f"s{c}", branch="main")
        zid = await zone_id(db, "pos", crew_id)
        got = await C.request_claim(ops, crew_id, Z.Principal.for_session(holder), zone_id=zid)
        assert got.claim is not None
        out.append((crew_id, ops, waiter, zid))
    return out


async def uncontested_reads(tmp_path, monkeypatch, crews: int) -> tuple[int, int]:
    db = await open_db(tmp_path)
    try:
        world = await held_zone_crews(db, crews)
        seen = record_reads(monkeypatch)
        out = counters()
        await alarms._contested(world[0][1].log, datetime.now(UTC), out)
        assert out == counters()
        return event_reads(seen), queue_reads(seen)
    finally:
        await db.close()


async def test_unchanged_contested_scan_reads_do_not_grow_with_crews(tmp_path, monkeypatch):
    small = await uncontested_reads(tmp_path / "small", monkeypatch, crews=2)
    large = await uncontested_reads(tmp_path / "large", monkeypatch, crews=7)
    assert small == large, f"(event, queue) reads grew from {small} (2 crews) to {large} (7 crews)"
    assert large == (1, 1)


async def blocked(ops, crew_id: str, session_id: str, at: datetime) -> None:
    async with ops.log.transaction() as tx:
        await tx.emit(
            crew_id=crew_id,
            type="guard.blocked",
            actor=Actor.system(),
            payload={"zone": "pos", "decision": "deny", "rule": 3, "op": "write", "surface": "pretool", "coalesced": 1},
            summary="blocked on pos",
            refs={"session_id": session_id},
            now=at,
        )


@pytest.mark.parametrize("crews_per_read", [None, 1])
async def test_one_crews_blocks_never_contest_another_crews_zone(tmp_path, monkeypatch, crews_per_read):
    if crews_per_read is not None:  # the smallest batch: every crew in its own read
        monkeypatch.setattr(alarms, "DISCOVERY_CREWS_PER_READ", crews_per_read, raising=False)
    db = await open_db(tmp_path)
    try:
        world = await held_zone_crews(db, 3)
        now = datetime.now(UTC)
        crew_id, ops, waiter, zid = world[1]  # only the middle crew is blocked, for six minutes
        await blocked(ops, crew_id, waiter["id"], now - timedelta(minutes=7))
        await blocked(ops, crew_id, waiter["id"], now - timedelta(minutes=1))
        out = counters()
        await alarms._contested(ops.log, now, out)
        assert out["contested"] == 1
        items = await db.fetchall("SELECT crew_id, ref_id, title FROM crew_inbox_items WHERE kind = 'zone_contested'")
        assert items == [{"crew_id": crew_id, "ref_id": zid, "title": "zone pos contested for 6 min (1 waiting)"}]
    finally:
        await db.close()


async def queue_waiter(db, ops, crew_id: str, waiter, zid: str, since: datetime) -> None:
    got = await C.request_claim(ops, crew_id, Z.Principal.for_session(waiter), zone_id=zid, wait=True)
    assert got.claim is not None and got.claim["state"] == "queued", got
    async with db.transaction():
        await db.conn.execute("UPDATE crew_claims SET created_at = ? WHERE id = ?", (format_ts(since), got.claim["id"]))


async def test_one_crews_queue_never_contests_another_crews_zone(tmp_path):
    db = await open_db(tmp_path)
    try:
        world = await held_zone_crews(db, 3)
        now = datetime.now(UTC)
        crew_id, ops, waiter, zid = world[2]  # only the last crew has a waiter, queued for six minutes
        await queue_waiter(db, ops, crew_id, waiter, zid, now - timedelta(minutes=6))
        out = counters()
        await alarms._contested(ops.log, now, out)
        assert out["contested"] == 1
        items = await db.fetchall("SELECT crew_id, ref_id, title FROM crew_inbox_items WHERE kind = 'zone_contested'")
        assert items == [{"crew_id": crew_id, "ref_id": zid, "title": "zone pos contested for 6 min (1 waiting)"}]
        again = counters()  # the open alarm is neither raised twice nor cleared while the wait goes on
        await alarms._contested(ops.log, now + timedelta(seconds=30), again)
        assert again == counters()
    finally:
        await db.close()


async def test_a_queue_that_empties_before_the_writer_raises_no_alarm(tmp_path, monkeypatch):
    db = await open_db(tmp_path)
    try:
        ((crew_id, ops, waiter, zid),) = await held_zone_crews(db, 1)
        now = datetime.now(UTC)
        await queue_waiter(db, ops, crew_id, waiter, zid, now - timedelta(minutes=6))
        real = ops.log.transaction

        @asynccontextmanager
        async def after_the_waiter_left():
            # What a withdrawal committed between the sweep's reads and its own transaction looks like.
            async with real() as tx:
                await tx.conn.execute("UPDATE crew_claims SET state = 'released' WHERE state = 'queued'")
                yield tx

        monkeypatch.setattr(ops.log, "transaction", after_the_waiter_left)
        out = counters()
        await alarms._contested(ops.log, now, out)
        assert out == counters()
        assert await db.fetchall("SELECT id FROM crew_inbox_items WHERE kind = 'zone_contested'") == []
    finally:
        await db.close()


async def test_discovery_reads_seek_the_event_log_by_index(tmp_path, monkeypatch):
    db = await open_db(tmp_path)
    try:
        now = datetime.now(UTC)
        world = await held_zone_crews(db, 3)
        async with db.transaction():
            await db.conn.execute(
                "UPDATE crew_sessions SET next_checkpoint_due_at = ?", (format_ts(now - timedelta(minutes=15)),)
            )
        seen = record_reads(monkeypatch)
        await alarms.sweep(world[0][1].log, now, settings_for)
        batched = [(sql, params) for sql, params in seen if "crew_events" in sql and ("WITH due" in sql or " IN (" in sql)]
        assert len(batched) == 2, [sql for sql, _ in seen]
        for sql, params in batched:
            plan = [str(r["detail"]) for r in await db.fetchall("EXPLAIN QUERY PLAN " + sql, params)]
            walked = [step for step in plan if step.startswith("SCAN") and ("crew_events" in step or step.split()[1] == "e")]
            assert not walked, f"walks the whole event log: {plan}"
    finally:
        await db.close()
