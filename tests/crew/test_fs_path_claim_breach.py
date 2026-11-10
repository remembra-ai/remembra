"""F10 x F13 review: a write into another session's exclusive FILE claim is an ``exclusive_breach``.

F10 made the server hand out file claims (``path_glob``, no ``zone_id``) inside temporary
auto-derived folder zones. Collision detection only read zone claims, so the write that was a
breach of the whole-folder claim before F10 opened nothing at all afterwards. Every test drives
the real ``crew.db``, the real event log, the real claim code and the real checkpoint service.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from remembra.crew import claims as C
from remembra.crew import collisions as CO
from remembra.crew import zones as Z
from remembra.crew.checkpoints import CheckpointService
from remembra.crew.core import CrewCore
from remembra.crew.events import CrewEventLog
from remembra.crew.store import loads, now_iso
from remembra.crew.tasks import Caller
from tests.crew.wp5_support import (
    CREW,
    OWNER,
    assert_chain_ok,
    events,
    inbox,
    make_ops,
    open_db,
    seed_crew,
    seed_session,
)

HUMAN = Z.Principal.human(OWNER, privileged=True)
DIRTY = {"state": "dirty", "attribution": "certain"}
FILE = "lib/team-access.ts"
TREE = {
    "name": ".",
    "files": 2,
    "children": [
        {"name": "lib", "files": 9, "children": []},
        {"name": "app", "files": 1, "children": [{"name": "api", "files": 6, "children": []}]},
    ],
}


@pytest.fixture
async def env(tmp_path):
    """No zones.yml: temporary folder zones. cs_a and cs_c are hook sessions, cs_b is MCP-only (checkpoints declare its files)."""
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-3", worktree_id="wt-a", checkout_fp="fp-a")
    await seed_session(db, "cs_b", callsign="codex-5", agent_id="codex")
    async with db.transaction():
        await db.conn.execute("UPDATE crew_sessions SET client_kind = 'mcp' WHERE id = 'cs_b'")
    b = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_b'")
    c, _ = await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex", worktree_id="wt-c", checkout_fp="fp-c")
    res = await Z.put_tree(ops, CREW, Z.Principal.for_session(a), TREE)
    assert res["bootstrap_zone_ids"]
    try:
        yield db, ops, a, b, c
    finally:
        await assert_chain_ok(db)
        await db.close()


def P(session) -> Z.Principal:
    return Z.Principal.for_session(session)


async def _claim_file(ops, session, path=FILE, mode="exclusive"):
    got = await C.request_claim(ops, CREW, P(session), path_glob=path, mode=mode)
    assert got.status == "granted" and got.claim["zone_id"] is None and got.claim["path_glob"] == path, got.body()
    return got.claim


async def _checkpoint(db, session, files, note="n"):
    svc = CheckpointService(CrewEventLog(db, None))
    body = {"session_id": session["id"], "trigger": "turn", "facts": {"files_changed": list(files), "summary": note}}
    return await svc.ingest(CREW, Caller.for_session(session), body)


async def _fp(ops, session, *fps):
    async with ops.log.transaction() as tx:
        return await CO.record_footprints(ops, tx, CREW, session, list(fps))


async def _open(db, kind=None):
    rows = await db.fetchall(
        "SELECT * FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged') ORDER BY created_at", (CREW,)
    )
    return [r for r in rows if kind is None or r["kind"] == kind]


async def _all(db):
    return await db.fetchall("SELECT * FROM crew_collisions WHERE crew_id = ?", (CREW,))


async def _open_notices(db):
    return [i for i in await inbox(db, kind="collision_notice") if i["state"] not in ("resolved", "dismissed")]


async def _set(db, sql, params=()):
    async with db.transaction():
        await db.conn.execute(sql, params)


# ---------------------------------------------------------------------------
# The backstop: a write into another session's exclusive file claim
# ---------------------------------------------------------------------------


async def test_a_checkpoint_that_changes_another_sessions_claimed_file_opens_one_breach(env):
    db, ops, a, b, c = env
    claim = await _claim_file(ops, a)
    res = await _checkpoint(db, b, [FILE])
    assert res.created
    rows = await _open(db)
    assert [(r["kind"], r["severity"], r["subject"], r["session_a"], r["session_b"]) for r in rows] == [
        ("exclusive_breach", "high", FILE, "cs_b", "cs_a")
    ]
    lib = next(z for z in await Z.load_zone_rows(db.conn, CREW) if z["slug"] == "lib")
    # the row names the folder zone the file is in (what the Stop gate and the dashboard show) and the file claim
    assert rows[0]["claim_id"] == claim["id"] and rows[0]["zone_id"] == lib["id"] and rows[0]["attribution"] == "certain"
    assert loads(rows[0]["evidence"]) == {"holder_kind": "session", "path_glob": FILE}
    # delivered like the whole-zone breach was: a moment, a notice for both parties, a Needs-you item
    detected = await events(db, type_prefix="collision.detected")
    assert len(detected) == 1 and detected[0]["moment"]
    assert sorted(i["recipient"] for i in await _open_notices(db)) == ["cs_a", "cs_b"]
    assert [i["ref_id"] for i in await inbox(db, kind="collision_escalated")] == [rows[0]["id"]]
    snap = await CrewCore(db).snapshot(CREW)
    assert [(x["kind"], x["session_a"], x["session_b"]) for x in snap["collisions"]] == [("exclusive_breach", "cs_b", "cs_a")]


async def test_the_breach_is_opened_once_and_neither_repeats_nor_drains_while_it_stands(env):
    db, ops, a, b, c = env
    await _claim_file(ops, a)
    await _checkpoint(db, b, [FILE])
    first = await _open(db)
    assert len(first) == 1
    # the same file again in a changed checkpoint, an unchanged replay, an empty checkpoint, a sweep, the holder's own checkpoint
    assert (await _checkpoint(db, b, [FILE, "lib/other.ts"], note="more")).created
    assert not (await _checkpoint(db, b, [FILE, "lib/other.ts"], note="more")).created
    assert (await _checkpoint(db, b, [], note="idle")).created
    assert await CO.sweep(ops.log, {}) == 0
    await _fp(ops, a, {"path": FILE, **DIRTY})
    assert [r["id"] for r in await _open(db, "exclusive_breach")] == [first[0]["id"]]
    assert len([r for r in await _all(db) if r["kind"] == "exclusive_breach"]) == 1  # no closed-and-reopened copies either
    assert len([e for e in await events(db, type_prefix="collision.detected") if "exclusive_breach" in e["summary"]]) == 1
    assert sorted(i["recipient"] for i in await _open_notices(db) if i["ref_id"] == first[0]["id"]) == ["cs_a", "cs_b"]


async def test_a_heartbeat_footprint_in_another_sessions_file_claim_is_a_breach_too(env):
    db, ops, a, b, c = env
    claim = await _claim_file(ops, a)
    opened = await _fp(ops, c, *CO.heartbeat_footprints([{"path": FILE, "state": "dirty"}]))
    assert [(r["kind"], r["session_a"], r["session_b"], r["claim_id"], r["attribution"]) for r in opened] == [
        ("exclusive_breach", "cs_c", "cs_a", claim["id"], "probable")
    ]


async def test_the_guards_first_write_file_claim_is_backed_the_same_way(env):
    db, ops, a, b, c = env
    first = await C.server_guard(ops, CREW, P(a), op="write", paths=[FILE])
    assert first["decision"] == "allow" and [x["path_glob"] for x in first["auto_claimed"]] == [FILE]
    await _checkpoint(db, b, [FILE])
    assert [(r["kind"], r["session_b"]) for r in await _open(db)] == [("exclusive_breach", "cs_a")]


# ---------------------------------------------------------------------------
# What is not a breach
# ---------------------------------------------------------------------------


async def test_own_claims_other_files_and_unclaimed_files_open_nothing(env):
    db, ops, a, b, c = env
    await _claim_file(ops, a)
    await _claim_file(ops, b, "lib/sales-research.ts")
    assert await _fp(ops, a, {"path": FILE, **DIRTY}) == []  # the holder's own file
    await _checkpoint(db, b, ["lib/sales-research.ts", "lib/brand-new.ts", "lib/team-access.test.ts"])
    assert await _open(db) == []


@pytest.mark.parametrize("mode", ["shared", "watch"])
async def test_shared_and_watch_file_claims_are_not_exclusive_breaches(env, mode):
    db, ops, a, b, c = env
    got = await C.request_claim(ops, CREW, P(a), path_glob=FILE, mode=mode)
    assert got.status == "granted" and got.claim["mode"] == mode and got.claim["zone_id"] is None, got.body()
    await _checkpoint(db, b, [FILE])
    assert await _open(db, "exclusive_breach") == []


async def test_a_serialize_micro_lease_orders_writes_and_is_not_a_claim_of_work(env):
    db, ops, a, b, c = env
    claim = await _claim_file(ops, a)
    await _set(db, "UPDATE crew_claims SET source = 'micro_lease' WHERE id = ?", (claim["id"],))
    await _checkpoint(db, b, [FILE])
    assert await _open(db, "exclusive_breach") == []


async def test_a_released_or_ended_file_claim_no_longer_breaches_and_closes_the_row(env):
    db, ops, a, b, c = env
    claim = await _claim_file(ops, a)
    await _checkpoint(db, b, [FILE])
    assert len(await _open(db, "exclusive_breach")) == 1
    await C.release_claim(ops, await C.get_claim(db.conn, claim["id"]), P(a))
    assert await _open(db) == [] and await _open_notices(db) == []
    row = (await _all(db))[0]
    assert (row["state"], row["resolution"]) == ("resolved", "auto")
    await _checkpoint(db, b, [FILE, "lib/x.ts"], note="again")
    assert await _open(db) == []


async def test_the_breach_closes_when_the_write_lands(env):
    db, ops, a, b, c = env
    await _claim_file(ops, a)
    await _fp(ops, c, {"path": FILE, **DIRTY})
    assert len(await _open(db, "exclusive_breach")) == 1
    async with ops.log.transaction() as tx:
        await CO.set_footprint_state(ops, tx, CREW, "cs_c", [FILE], "landed")
    assert await _open(db) == [] and await _open_notices(db) == []


async def test_the_breach_follows_the_file_claim_to_a_new_holder(env):
    db, ops, a, b, c = env
    claim = await _claim_file(ops, a)
    await _checkpoint(db, b, [FILE])
    old = (await _open(db))[0]
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "quota")
        await C.record_baton_offer(ops, tx, CREW, claim["id"], "cs_c")
    await C.adopt(ops, await C.get_claim(db.conn, claim["id"]), P(c))
    await _checkpoint(db, b, [], note="idle")
    left = await _open(db)
    assert [(r["kind"], r["session_a"], r["session_b"], r["claim_id"]) for r in left] == [
        ("exclusive_breach", "cs_b", "cs_c", claim["id"])
    ]
    assert left[0]["id"] != old["id"]
    # and when the writer itself is given the file, the breach is over
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_c", "quota")
        await C.record_baton_offer(ops, tx, CREW, claim["id"], "cs_b")
    await C.adopt(ops, await C.get_claim(db.conn, claim["id"]), P(b))
    await _checkpoint(db, b, [FILE], note="mine now")
    assert await _open(db) == []


# ---------------------------------------------------------------------------
# Holders that ended or were lost: the same rule as a zone claim (the live claim decides)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("holder", ["lost_now", "lost_long_ago", "ended_long_ago", "reserved"])
async def test_a_file_claim_of_an_ended_or_lost_holder_follows_the_zone_claim_rule(env, holder):
    db, ops, a, b, c = env
    zones = {r["slug"]: r for r in await Z.load_zone_rows(db.conn, CREW)}
    zone_claim = await C.request_claim(ops, CREW, P(a), zone_id=zones["api"]["id"])
    assert zone_claim.status == "granted"
    file_claim = await _claim_file(ops, a)
    long_ago = now_iso(C.utcnow() - timedelta(days=9))
    if holder == "lost_now":
        await _set(db, "UPDATE crew_sessions SET state = 'lost' WHERE id = 'cs_a'")
    elif holder == "lost_long_ago":
        lost = "UPDATE crew_sessions SET state = 'lost', last_seen_at = ?, last_heartbeat_at = NULL WHERE id = 'cs_a'"
        await _set(db, lost, (long_ago,))
    elif holder == "ended_long_ago":
        await _set(db, "UPDATE crew_sessions SET state = 'ended', ended_at = ? WHERE id = 'cs_a'", (long_ago,))
    else:
        async with ops.log.transaction() as tx:
            await C.reserve_session_claims(ops, tx, CREW, "cs_a", "quota")
    await _checkpoint(db, b, ["app/api/route.ts", FILE])
    got = {r["claim_id"]: r["kind"] for r in await _open(db)}
    # whatever the whole-zone claim does for this holder, the file claim does
    assert got.get(file_claim["id"]) == got.get(zone_claim.claim["id"]) == "exclusive_breach", got
    await _set(db, "UPDATE crew_claims SET state = 'expired', ended_at = ? WHERE crew_id = ?", (now_iso(), CREW))
    assert await CO.sweep(ops.log, {}) == 2
    assert await _open(db) == []


# ---------------------------------------------------------------------------
# E2E-f for file claims: the old holder's own writes that arrive late
# ---------------------------------------------------------------------------


async def test_the_old_holders_writes_made_while_it_held_the_file_are_not_a_breach(env):
    db, ops, a, b, c = env
    claim = await _claim_file(ops, a)
    assert claim["epoch"] == 1
    lease = C.utcnow() + timedelta(seconds=30 + C.FENCE_MARGIN_S)
    await _set(db, "UPDATE crew_claims SET lease_expires_at = ? WHERE id = ?", (now_iso(lease), claim["id"]))
    await asyncio.sleep(2.2)  # A keeps editing while it holds the file; crewd is cut off and cannot deliver the footprints
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "offline")
    await C.override(ops, await C.get_claim(db.conn, claim["id"]), HUMAN, action="transfer", to="cs_c", reason="A is offline")
    moved = await C.get_claim(db.conn, claim["id"])
    assert moved["holder_session_id"] == "cs_c" and moved["epoch"] == 2

    def late(path_age):
        fp = {"path": FILE, "state": "dirty", "attribution": "certain", "claim_epoch": 1}
        return CO.heartbeat_footprints([{**fp, **({} if path_age is None else {"age_s": path_age})}])

    # written under epoch 1 one second before it arrived, while A still held the file: A's own work, not a breach
    assert await _fp(ops, a, *late(1)) == []
    assert await _open(db) == []
    # written after the file moved on: a breach of the new holder's claim, like a zone's
    assert [(r["kind"], r["session_b"]) for r in await _fp(ops, a, *late(0))] == [("exclusive_breach", "cs_c")]
