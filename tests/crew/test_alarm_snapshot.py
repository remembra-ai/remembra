"""Alarm preflight uses committed reads and revalidates any mutation."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
from datetime import UTC, datetime, timedelta

from remembra.crew import alarms
from remembra.crew.events import CrewEventLog, format_ts
from remembra.crew.settings import default_settings
from tests.crew.wp5_support import open_db, seed_crew, seed_session


def counters():
    return {"checkpoint_missed": 0, "stuck": 0, "unstuck": 0, "contested": 0, "uncontested": 0}


async def settings_for(_):
    return default_settings()


async def test_unchanged_alarm_preflight_finishes_while_an_unrelated_writer_is_open(tmp_path):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        await seed_session(db, "active-session")
        log = CrewEventLog(db, None)
        out = counters()
        async with db.transaction():
            await db.conn.execute("UPDATE crew_sessions SET callsign='pending' WHERE id='active-session'")
            await asyncio.wait_for(
                asyncio.create_task(alarms._stuck(log, datetime.now(UTC), settings_for, out), context=Context()), timeout=1
            )
        assert out == counters()
    finally:
        await db.close()


async def test_checkpoint_refresh_before_alarm_writer_lock_prevents_a_stale_nudge(tmp_path, monkeypatch):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        await seed_session(db, "active-session")
        now = datetime.now(UTC)
        async with db.transaction():
            await db.conn.execute(
                "UPDATE crew_sessions SET next_checkpoint_due_at=? WHERE id='active-session'",
                (format_ts(now - timedelta(hours=1)),),
            )
        log = CrewEventLog(db, None)
        original = log.transaction

        @asynccontextmanager
        async def refreshed_transaction():
            async with db.transaction():
                await db.conn.execute(
                    "UPDATE crew_sessions SET next_checkpoint_due_at=? WHERE id='active-session'",
                    (format_ts(now + timedelta(minutes=10)),),
                )
            async with original() as tx:
                yield tx

        monkeypatch.setattr(log, "transaction", refreshed_transaction)
        out = counters()
        await alarms._stuck(log, now, settings_for, out)
        assert out == counters()
        assert not await db.fetchall("SELECT * FROM crew_events WHERE type='checkpoint.missed'")
    finally:
        await db.close()
