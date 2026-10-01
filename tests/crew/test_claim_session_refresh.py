"""Claim decisions and audit attribution use the current transactional session."""

import json

import pytest

from remembra.crew import claims as C
from remembra.crew import zones as Z
from tests.crew.wp5_support import CREW, events, make_ops, open_db, seed_crew, seed_session


async def test_removed_session_cannot_claim_using_an_earlier_principal(tmp_path):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        row, _ = await seed_session(db, "cs_removed")
        principal = Z.Principal.for_session(row)
        ops, _ = make_ops(db)
        ops.limits = None  # Supported uncapped service path still requires a live session.
        async with db.transaction():
            await db.conn.execute("DELETE FROM crew_sessions WHERE id=?", (row["id"],))
        with pytest.raises(Z.CrewOpError) as error:
            await C.request_claim(ops, CREW, principal, resource="service:isolated-test")
        assert error.value.status == 409 and error.value.error == "session_not_live"
        assert not await db.fetchall("SELECT * FROM crew_claims")
        assert not await events(db)
    finally:
        await db.close()


async def test_claim_event_actor_uses_fresh_callsign(tmp_path):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        row, _ = await seed_session(db, "cs_refreshed", verified=True)
        principal = Z.Principal.for_session(row)
        ops, _ = make_ops(db)
        async with db.transaction():
            await db.conn.execute("UPDATE crew_sessions SET callsign='cc-2' WHERE id=?", (row["id"],))
        result = await C.request_claim(ops, CREW, principal, resource="service:isolated-test")
        assert result.status == "granted"
        event = await db.fetchone("SELECT actor FROM crew_events WHERE type='claim.granted'")
        actor = json.loads(event["actor"])
        assert actor["verified"] is True
        assert actor["callsign"] == "cc-2"
    finally:
        await db.close()
