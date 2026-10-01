"""Claim policy aggregates retain reservation, identity and strict threshold rules."""

import pytest

from remembra.crew import claims as C
from remembra.crew import zones as Z
from tests.crew.wp5_support import CREW, OWNER, inbox, make_ops, open_db, seed_crew, seed_session


async def test_caps_keep_session_and_agent_distinctions(tmp_path):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        a, _ = await seed_session(db, "cs_a", callsign="cc-1")
        b, _ = await seed_session(db, "cs_b", callsign="cc-2")
        ops, _ = make_ops(db)
        granted = await C.request_claim(ops, CREW, Z.Principal.for_session(a), path_glob="scripts/a.py")
        claim = granted.claim
        assert claim is not None
        # The session cap includes its own reserved baton; the agent cap only
        # includes active/offered session-held claims. Other agents/users and
        # micro-leases must never inflate these counters.
        cases = [
            ({}, (1, 1)),
            ({"state": "offered"}, (1, 1)),
            ({"state": "reserved", "reserved_for": a["id"]}, (1, 0)),
            ({"state": "reserved", "reserved_for": b["id"]}, (0, 0)),
            ({"state": "released"}, (0, 0)),
            ({"state": "queued"}, (0, 0)),
            ({"mode": "shared"}, (0, 0)),
            ({"mode": "watch"}, (0, 0)),
            ({"source": "micro_lease"}, (0, 0)),
            ({"holder_session_id": b["id"]}, (0, 1)),
            ({"holder_session_id": b["id"], "holder_agent_id": "codex"}, (0, 0)),
            ({"holder_session_id": b["id"], "holder_user_id": "another-user"}, (0, 0)),
            ({"holder_kind": "human", "holder_session_id": None}, (0, 0)),
        ]
        baseline = {
            k: claim[k]
            for k in (
                "state",
                "reserved_for",
                "mode",
                "source",
                "holder_kind",
                "holder_session_id",
                "holder_agent_id",
                "holder_user_id",
            )
        }
        async with ops.log.transaction() as tx:
            for changes, expected in cases:
                values = {**baseline, **changes}
                await tx.conn.execute(
                    "UPDATE crew_claims SET " + ", ".join(f"{key} = ?" for key in values) + " WHERE id = ?",
                    (*values.values(), claim["id"]),
                )
                assert await C.exclusive_counts(tx.conn, a) == expected, changes
    finally:
        await db.close()


@pytest.mark.parametrize("held_files,alarm", [(50, False), (51, True)])
async def test_hoarding_is_strictly_more_than_half_of_nonarchived_nonbuiltin_files(tmp_path, held_files, alarm):
    db = await open_db(tmp_path)
    try:
        await seed_crew(db)
        session, _ = await seed_session(db, "cs_a", callsign="cc-1")
        ops, _ = make_ops(db)
        human = Z.Principal.human(OWNER, privileged=True)
        hot = await Z.create_zone(ops, CREW, human, {"slug": "hot", "include": ["hot/**"]})
        cold = await Z.create_zone(ops, CREW, human, {"slug": "cold", "include": ["cold/**"]})
        archived = await Z.create_zone(ops, CREW, human, {"slug": "archived", "include": ["archived/**"]})
        async with ops.log.transaction() as tx:
            for zone, estimate in ((hot, held_files), (cold, 100 - held_files), (archived, 1000)):
                await tx.conn.execute("UPDATE crew_zones SET files_estimate = ? WHERE id = ?", (estimate, zone["id"]))
            await tx.conn.execute("UPDATE crew_zones SET archived_at = '2026-10-01T00:00:00Z' WHERE id = ?", (archived["id"],))
            await tx.conn.execute("UPDATE crew_zones SET files_estimate = 1000 WHERE crew_id = ? AND builtin = 1", (CREW,))
        assert (await C.request_claim(ops, CREW, Z.Principal.for_session(session), zone_id=hot["id"])).status == "granted"
        notices = await inbox(db, kind="zone_hoarding")
        assert bool(notices) is alarm
    finally:
        await db.close()
