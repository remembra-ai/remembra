"""WP-5 footprints with attribution and every collision kind, delivery, dedupe and auto-resolution."""

from __future__ import annotations

from datetime import timedelta

import pytest

from remembra.crew import claims as C
from remembra.crew import collisions as CO
from remembra.crew import zones as Z
from remembra.crew.store import now_iso
from tests.crew.wp5_support import (
    CREW,
    OWNER,
    ZONES_YML,
    assert_chain_ok,
    events,
    inbox,
    make_ops,
    open_db,
    seed_crew,
    seed_session,
    zone_id,
)

HUMAN = Z.Principal.human(OWNER, privileged=True)


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, audit = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-1", worktree_id="wt-a")
    b, _ = await seed_session(db, "cs_b", callsign="cc-2", worktree_id="wt-b")
    await Z.upload_zones_file(ops, CREW, Z.Principal.for_session(a), yaml_text=ZONES_YML, sha="s1", branch="main")
    try:
        yield db, ops, audit, a, b
    finally:
        await assert_chain_ok(db)
        await db.close()


async def _fp(ops, session, *fps):
    async with ops.log.transaction() as tx:
        return await CO.record_footprints(ops, tx, CREW, session, list(fps))


async def _open(db, kind=None):
    rows = await db.fetchall("SELECT * FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged')", (CREW,))
    return [r for r in rows if kind is None or r["kind"] == kind]


async def test_same_file_and_same_worktree(env):
    db, ops, _, a, b = env
    await _fp(ops, a, {"path": "src/lib/money.ts", "state": "dirty", "attribution": "certain"})
    opened = await _fp(ops, b, {"path": "src/lib/money.ts", "state": "dirty", "attribution": "certain"})
    assert [r["kind"] for r in opened] == ["same_file"] and opened[0]["severity"] == "medium"
    assert opened[0]["session_a"] == "cs_b" and opened[0]["session_b"] == "cs_a"
    shared, _ = await seed_session(db, "cs_c", callsign="cc-3", worktree_id="wt-a")
    opened = await _fp(ops, shared, {"path": "src/lib/money.ts", "state": "dirty", "attribution": "certain"})
    kinds = {r["kind"] for r in opened}
    assert "same_worktree_file" in kinds
    crit = [
        e for e in await events(db, type_prefix="collision.detected") if e["payload"]["collision"]["kind"] == "same_worktree_file"
    ]
    assert crit[0]["moment"] == 1 and crit[0]["payload"]["collision"]["escalated"] is True
    assert await inbox(db, kind="collision_escalated")
    notices = {i["recipient"] for i in await inbox(db, kind="collision_notice")}
    assert {"cs_a", "cs_b", "cs_c"} <= notices
    # the same overlap reported again opens nothing new
    assert await _fp(ops, b, {"path": "src/lib/money.ts", "state": "dirty", "attribution": "certain"}) == []
    # landing A's and C's file resolves their collisions automatically
    async with ops.log.transaction() as tx:
        await CO.set_footprint_state(ops, tx, CREW, "cs_a", ["src/lib/money.ts"], "landed", last_commit="abc1234")
    remaining = await _open(db)
    assert all("cs_a" not in (r["session_a"], r["session_b"]) for r in remaining)
    assert any(e["payload"]["collision"]["resolution"] == "auto" for e in await events(db, type_prefix="collision.resolved"))


async def test_exclusive_breach_attribution_and_resolution_on_release(env):
    db, ops, _, a, b = env
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    opened = await _fp(ops, b, {"path": "src/app/pos/cart.ts", "state": "dirty", "attribution": "probable"})
    breach = [r for r in opened if r["kind"] == "exclusive_breach"]
    assert breach and breach[0]["attribution"] == "probable" and breach[0]["session_b"] == "cs_a"
    assert breach[0]["claim_id"] == got.claim["id"]  # type: ignore[index]
    # a later certain report upgrades the footprint; certain is never downgraded
    await _fp(ops, b, {"path": "src/app/pos/cart.ts", "state": "dirty", "attribution": "certain"})
    await _fp(ops, b, {"path": "src/app/pos/cart.ts", "state": "dirty", "attribution": "probable"})
    fp = await db.fetchone(
        "SELECT attribution, touches FROM crew_footprints WHERE session_id = 'cs_b' AND path = 'src/app/pos/cart.ts'"
    )
    assert fp == {"attribution": "certain", "touches": 3}
    # the holder's own writes are not a breach
    assert await _fp(ops, a, {"path": "src/app/pos/tender.ts", "state": "dirty", "attribution": "certain"}) == []
    await C.release_claim(ops, got.claim, Z.Principal.for_session(a))  # type: ignore[arg-type]
    assert await _open(db, "exclusive_breach") == []


async def test_stale_epoch_after_adopt_and_fenced_writes(env):
    db, ops, _, a, b = env
    pa = Z.Principal.for_session(a)
    got = await C.request_claim(ops, CREW, pa, zone_id=await zone_id(db, "pos"))
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "quota")
        await C.record_baton_offer(ops, tx, CREW, got.claim["id"], "cs_b")  # type: ignore[index]
    await C.adopt(ops, got.claim, Z.Principal.for_session(b))  # type: ignore[arg-type]
    opened = await _fp(ops, a, {"path": "src/app/pos/cart.ts", "state": "dirty", "attribution": "certain", "claim_epoch": 1})
    stale = [r for r in opened if r["kind"] == "stale_epoch_write"]
    assert stale and stale[0]["severity"] == "high" and stale[0]["session_b"] == "cs_b"
    # own claim past its lease horizon: stale too
    rep = await C.request_claim(ops, CREW, Z.Principal.for_session(b), zone_id=await zone_id(db, "reports"))
    await db.conn.execute(
        "UPDATE crew_claims SET lease_expires_at = ? WHERE id = ?",
        (now_iso(C.utcnow() + timedelta(seconds=30)), rep.claim["id"]),  # type: ignore[index]
    )
    await db.conn.commit()
    opened = await _fp(ops, b, {"path": "src/app/reports/x.ts", "state": "dirty", "attribution": "certain"})
    assert [r["kind"] for r in opened] == ["stale_epoch_write"] and opened[0]["session_b"] is None


async def test_foreign_checkout_shared_zone_unattributed_and_merge_risk(env):
    db, ops, _, a, b = env
    opened = await _fp(ops, b, {"path": "src/x.ts", "state": "dirty", "attribution": "certain", "worktree_id": "wt-a"})
    assert [r["kind"] for r in opened] == ["foreign_checkout_write"] and opened[0]["severity"] == "critical"
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "billing"), mode="shared")
    opened = await _fp(ops, b, {"path": "src/billing/a.ts", "state": "dirty", "attribution": "certain"})
    assert [r["kind"] for r in opened] == ["same_zone_shared"]
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    async with ops.log.transaction() as tx:
        un = await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts", "README.md"], commit="deadbeef")
        risk = await CO.record_merge_conflict_risk(ops, tx, CREW, b, "cs_a", ["src/app/pos/a.ts"])
    assert [r["kind"] for r in un] == ["unattributed_change"] and un[0]["session_a"] is None and un[0]["severity"] == "notice"
    assert [r["kind"] for r in risk] == ["merge_conflict_risk"]
    with pytest.raises(Z.CrewOpError):
        async with ops.log.transaction() as tx:
            await CO.record_footprints(ops, tx, CREW, b, [{"path": "/abs/path", "state": "dirty", "attribution": "certain"}])


async def test_ack_resolve_dismiss(env):
    db, ops, audit, a, b = env
    await _fp(ops, a, {"path": "src/lib/m.ts", "state": "dirty", "attribution": "certain"})
    row = (await _fp(ops, b, {"path": "src/lib/m.ts", "state": "dirty", "attribution": "certain"}))[0]
    outsider, _ = await seed_session(db, "cs_z", callsign="cc-9")
    with pytest.raises(Z.CrewOpError) as err:
        await CO.acknowledge(ops, row, Z.Principal.for_session(outsider))
    assert err.value.status == 403
    assert (await CO.acknowledge(ops, row, Z.Principal.for_session(a)))["state"] == "acknowledged"
    assert (await CO.resolve(ops, row, Z.Principal.for_session(b), "rebased onto cc-1"))["state"] == "resolved"
    with pytest.raises(Z.CrewOpError):
        await CO.resolve(ops, row, Z.Principal.for_session(b), "again")
    await _fp(ops, a, {"path": "src/lib/n.ts", "state": "dirty", "attribution": "certain"})
    row2 = (await _fp(ops, b, {"path": "src/lib/n.ts", "state": "dirty", "attribution": "certain"}))[0]
    view = await CO.dismiss(ops, row2, HUMAN, "fine")
    assert view["state"] == "dismissed" and "crew.collision_dismissed" in audit.actions()
    listed = await CO.list_collisions(db.conn, CREW, "dismissed")
    assert [c["id"] for c in listed] == [row2["id"]]
    assert (await inbox(db, kind="collision_open"))[-1]["state"] == "resolved"
