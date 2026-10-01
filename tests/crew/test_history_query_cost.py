"""Historical rows must not make target fencing and alarm probes scan the log."""

from datetime import UTC, datetime

import pytest

from remembra.crew import alarms
from remembra.crew import claims as C
from tests.crew.wp5_support import CREW, OTHER_CREW, OWNER, open_db, seed_crew


@pytest.mark.parametrize("kind", ["zone_id", "path_glob", "resource"])
async def test_next_epoch_cost_bounded_by_target_not_crew_history(tmp_path, kind):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        await seed_crew(db, OTHER_CREW, project="other-project")
        stamp = "2026-10-01T00:00:00+00:00"
        async with db.transaction():
            await db.conn.executemany(
                f"INSERT INTO crew_claims(id,crew_id,{kind},mode,holder_kind,state,source,epoch,created_at,updated_at)"
                " VALUES(?,?,?,'exclusive','human',?,'explicit',?,?,?)",
                [(f"history-{i}", CREW, f"other-{i}", "released", i, stamp, stamp) for i in range(2000)]
                + [
                    (f"target-{i}", CREW, "target", state, epoch, stamp, stamp)
                    for i, (state, epoch) in enumerate([("released", 11), ("expired", 14), ("revoked", 18)])
                ]
                + [("foreign", OTHER_CREW, "target", "released", 99, stamp, stamp)],
            )
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return 0

        await db.conn.set_progress_handler(progress, 100)
        try:

            def target(value):
                return C.Target(zone={"id": value}) if kind == "zone_id" else C.Target(**{kind: value})

            assert await C.next_epoch(db.conn, CREW, target("target")) == 19
            assert await C.next_epoch(db.conn, CREW, target("absent")) == 1
        finally:
            await db.conn.set_progress_handler(None, 0)
        assert steps < 500, f"Target fencing scanned historical rows: {steps} SQLite VM operations"
    finally:
        await db.close()


async def test_checkpoint_missed_probe_does_not_scan_unrelated_event_history(tmp_path):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        stamp = "2026-10-01T00:00:00+00:00"
        async with db.transaction():
            await db.conn.executemany(
                "INSERT INTO crew_events(crew_id,seq,id,owner_user_id,project_id,ts,type,summary,payload,session_id)"
                " VALUES(?,?,?,?,?,?,'heartbeat','synthetic history','{}','session-target')",
                [(CREW, i, f"event-{i}", OWNER, "yaadbooks", stamp) for i in range(2000)],
            )
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return 0

        await db.conn.set_progress_handler(progress, 100)
        try:
            assert not await alarms._missed_since(
                db.conn, {"crew_id": CREW, "id": "session-target"}, datetime(2026, 10, 1, tzinfo=UTC)
            )
        finally:
            await db.conn.set_progress_handler(None, 0)
        assert steps < 500, f"Alarm probe scanned historical rows: {steps} SQLite VM operations"
    finally:
        await db.close()
