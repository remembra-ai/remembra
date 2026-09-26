"""WP-5 claim state machine, caps, epochs, handover, adopt authorisation (D33), override and timers."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from remembra.crew import claims as C
from remembra.crew import zones as Z
from remembra.crew.store import now_iso
from tests.crew.wp5_support import (
    CREW,
    OTHER_CREW,
    OWNER,
    ZONES_YML,
    assert_chain_ok,
    events,
    inbox,
    make_ops,
    open_db,
    seed_crew,
    seed_session,
    seed_task,
    zone_id,
)

HUMAN = Z.Principal.human(OWNER)


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, audit = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-1", worktree_id="wt-a", checkout_fp="fp-a")
    b, _ = await seed_session(db, "cs_b", callsign="cc-2", worktree_id="wt-b", checkout_fp="fp-b")
    await Z.upload_zones_file(ops, CREW, Z.Principal.for_session(a), yaml_text=ZONES_YML, sha="s1", branch="main")
    try:
        yield db, ops, audit, Z.Principal.for_session(a), Z.Principal.for_session(b)
    finally:
        await assert_chain_ok(db)
        await db.close()


async def _claim(ops, who, db, slug, **kw):
    return await C.request_claim(ops, CREW, who, zone_id=await zone_id(db, slug), **kw)


async def test_grant_deny_queue_and_fifo_promotion(env):
    db, ops, _, a, b = env
    got = await _claim(ops, a, db, "pos")
    assert got.status == "granted" and got.http_status == 201
    assert got.claim["epoch"] == 1 and got.claim["lease_expires_at"] is not None  # type: ignore[index]
    again = await _claim(ops, a, db, "pos")
    assert again.status == "existing" and again.claim["id"] == got.claim["id"]  # type: ignore[index]
    denied = await _claim(ops, b, db, "pos")
    assert denied.status == "denied" and denied.http_status == 409 and denied.error == "conflict"
    body = denied.body()
    assert body["blockers"][0]["holder_callsign"] == "cc-1" and body["holder"] == "cc-1"
    assert "request_release" in body["how_to_request"]
    queued = await _claim(ops, b, db, "pos", wait=True)
    assert queued.status == "queued" and queued.http_status == 202
    third, _ = await seed_session(db, "cs_c", callsign="cc-3")
    q2 = await _claim(ops, Z.Principal.for_session(third), db, "pos", wait=True)
    assert q2.status == "queued"
    await C.release_claim(ops, got.claim, a)  # type: ignore[arg-type]
    first = await C.get_claim(db.conn, queued.claim["id"])  # type: ignore[index]
    second = await C.get_claim(db.conn, q2.claim["id"])  # type: ignore[index]
    assert first["state"] == "active" and first["epoch"] == 2  # per-zone epoch keeps rising
    assert second["state"] == "queued"  # FIFO: never overtakes
    notices = await inbox(db, kind="claim_granted")
    assert notices and notices[0]["recipient"] == "cs_b"
    kinds = [e["type"] for e in await events(db, type_prefix="claim.")]
    assert kinds == ["claim.granted", "claim.denied", "claim.queued", "claim.queued", "claim.released", "claim.granted"]


async def test_compatibility_matrix_hierarchy_overlap_globs_and_resources(env):
    db, ops, _, a, b = env
    assert (await _claim(ops, a, db, "reports", mode="shared")).status == "granted"
    assert (await _claim(ops, b, db, "reports", mode="shared")).status == "granted"
    s3, _ = await seed_session(db, "cs_c", callsign="cc-3")
    c = Z.Principal.for_session(s3)
    assert (await _claim(ops, c, db, "reports", mode="exclusive")).status == "denied"
    assert (await _claim(ops, c, db, "reports", mode="watch")).status == "granted"
    # holding the parent covers the children
    await seed_task(db, "tsk_1", 1)
    assert (await _claim(ops, a, db, "pos")).status == "granted"
    parent = await _claim(ops, c, db, "app", task_id="tsk_1")
    assert parent.status == "denied" and await zone_id(db, "pos") in {b_["zone_id"] for b_ in parent.blockers}
    # a path glob inside a held zone conflicts; outside it does not
    assert (await C.request_claim(ops, CREW, c, path_glob="src/app/pos/cart.ts")).status == "denied"
    assert (await C.request_claim(ops, CREW, c, path_glob="src/other/x.ts")).status == "granted"
    # a resource named in a held zone's services conflicts with that zone's exclusive holder
    assert (await _claim(ops, b, db, "billing")).status == "granted"
    assert (await C.request_claim(ops, CREW, c, resource="deploy:vercel")).status == "denied"
    assert (await C.request_claim(ops, CREW, c, resource="schema:main")).status == "granted"
    assert (await C.request_claim(ops, CREW, a, resource="schema:main")).status == "denied"


async def test_rules_for_zones_tasks_and_sessions(env):
    db, ops, _, a, b = env
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, a, db, "app")
    assert err.value.error == "task_required"
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, a, db, "crew-policy")
    assert err.value.status == 423 and err.value.error == "crew_policy"
    with pytest.raises(Z.CrewOpError) as err:
        await C.request_claim(ops, CREW, a, zone_id=await zone_id(db, "pos"), path_glob="x")
    assert err.value.status == 422
    await seed_crew(db, OTHER_CREW, project="elsewhere")
    await seed_task(db, "tsk_foreign", 1, crew_id=OTHER_CREW)
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, a, db, "pos", task_id="tsk_foreign")
    assert err.value.error == "cross_crew_reference"
    with pytest.raises(Z.CrewOpError):
        await C.request_claim(ops, CREW, a, path_glob="../etc/**")
    # protected zones and reserve_for (key-verified only)
    await Z.create_zone(ops, CREW, HUMAN, {"slug": "vault", "include": ["vault/**"], "protected": True})
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, a, db, "vault")
    assert err.value.error == "protected"
    assert (await _claim(ops, HUMAN, db, "vault")).status == "granted"
    await Z.create_zone(ops, CREW, HUMAN, {"slug": "codexzone", "include": ["cx/**"], "reserve_for": "codex"})
    fake, _ = await seed_session(db, "cs_fake", callsign="codex-1", agent_id="codex", verified=False)
    real, _ = await seed_session(db, "cs_real", callsign="codex-2", agent_id="codex", verified=True)
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, Z.Principal.for_session(fake), db, "codexzone")
    assert err.value.error == "reserved_for_agent"
    assert (await _claim(ops, Z.Principal.for_session(real), db, "codexzone")).status == "granted"
    # session states
    ended, _ = await seed_session(db, "cs_ended", callsign="cc-9", state="lost")
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, Z.Principal.for_session(ended), db, "reports")
    assert err.value.error == "session_not_live"
    paused, _ = await seed_session(db, "cs_paused", callsign="cc-8", state="paused")
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, Z.Principal.for_session(paused), db, "reports")
    assert err.value.status == 423
    await ops.store.patch_settings(CREW, {"require_verified_agents_for_claims": True}, if_match=1)
    with pytest.raises(Z.CrewOpError) as err:
        await _claim(ops, Z.Principal.for_session(fake), db, "reports")
    assert err.value.error == "unverified_agent"


async def test_caps_per_session_and_per_agent(env):
    db, ops, _, a, b = env
    for i in range(4):
        await Z.create_zone(ops, CREW, HUMAN, {"slug": f"z{i}", "include": [f"z{i}/**"]})
    for i in range(3):
        assert (await _claim(ops, a, db, f"z{i}")).status == "granted"
    capped = await _claim(ops, a, db, "z3")
    assert capped.status == "denied" and capped.error == "claim_cap"
    assert (await _claim(ops, a, db, "z3", mode="shared")).status == "granted"  # caps count exclusive claims only
    await ops.store.patch_settings(CREW, {"max_exclusive_claims_per_agent": 4}, if_match=1)
    assert (await _claim(ops, b, db, "pos")).status == "granted"  # 4th exclusive for agent claude-code
    capped = await _claim(ops, b, db, "billing")
    assert capped.error == "claim_cap"  # 5th exclusive for the same agent id across sessions
    denied = [e for e in await events(db, type_prefix="claim.denied")]
    assert len(denied) == 2 and denied[0]["payload"]["blockers"] == []


async def test_hoarding_alarm(env):
    db, ops, _, a, _b = env
    await ops.store.patch_settings(CREW, {"max_exclusive_claims_per_session": 6, "max_exclusive_claims_per_agent": 6}, if_match=1)
    for i in range(4):
        await Z.create_zone(ops, CREW, HUMAN, {"slug": f"h{i}", "include": [f"h{i}/**"]})
        await _claim(ops, a, db, f"h{i}")
    items = await inbox(db, kind="zone_hoarding")
    assert len(items) == 1 and items[0]["recipient"] is None and items[0]["ref_id"] == "cs_a"


async def test_concurrent_requests_grant_exactly_one(env):
    db, ops, _, _a, _b = env
    sessions = []
    for i in range(20):
        row, _ = await seed_session(db, f"cs_r{i:02d}", callsign=f"rc-{i + 1}")
        sessions.append(Z.Principal.for_session(row))
    zid = await zone_id(db, "reports")
    outcomes = await asyncio.gather(*(C.request_claim(ops, CREW, p, zone_id=zid) for p in sessions))
    assert sum(o.status == "granted" for o in outcomes) == 1
    live = await db.fetchall("SELECT id FROM crew_claims WHERE zone_id = ? AND state = 'active'", (zid,))
    assert len(live) == 1


async def test_release_as_baton_and_task_batons_never_expire(env):
    db, ops, _, a, b = env
    await seed_task(db, "tsk_1", 1)
    plain = await _claim(ops, a, db, "pos")
    res = await C.release_claim(ops, plain.claim, a, baton=True)  # type: ignore[arg-type]
    assert res["claim"]["state"] == "reserved" and res["claim"]["reserve_reason"] == "baton"
    row = await C.get_claim(db.conn, plain.claim["id"])  # type: ignore[index]
    assert row["reserve_expires_at"] is not None and row["reserved_for"] is None
    tasked = await _claim(ops, a, db, "reports", task_id="tsk_1")
    await C.release_claim(ops, tasked.claim, a, baton=True)  # type: ignore[arg-type]
    row = await C.get_claim(db.conn, tasked.claim["id"])  # type: ignore[index]
    assert row["reserve_expires_at"] is None  # D5
    assert len(await inbox(db, kind="baton_reserved")) == 2
    blocked = await _claim(ops, b, db, "pos")
    assert blocked.status == "denied" and "reserved" in blocked.body()["how_to_request"]
    assert "adopt" not in blocked.body()["how_to_request"]
    with pytest.raises(Z.CrewOpError) as err:
        await C.release_claim(ops, tasked.claim, b)  # type: ignore[arg-type]
    assert err.value.error == "not_holder"
    # the holder re-takes its own baton with a new epoch
    again = await _claim(ops, a, db, "pos")
    assert again.status == "retaken" and again.claim["epoch"] == 2  # type: ignore[index]


async def test_handover_accept_decline_and_timeout(env):
    db, ops, _, a, b = env
    got = await _claim(ops, a, db, "pos")
    offered = await C.handover(ops, got.claim, a, to="cs_b")  # type: ignore[arg-type]
    assert offered["claim"]["state"] == "offered" and offered["claim"]["offered_to"] == "cs_b"
    assert (await _claim(ops, b, db, "pos")).status == "denied"  # the holder still holds while offered
    s3, _ = await seed_session(db, "cs_c", callsign="cc-3")
    with pytest.raises(Z.CrewOpError):
        await C.accept_handover(ops, got.claim, Z.Principal.for_session(s3))  # type: ignore[arg-type]
    accepted = await C.accept_handover(ops, got.claim, b)  # type: ignore[arg-type]
    assert accepted["claim"]["holder_session_id"] == "cs_b" and accepted["claim"]["epoch"] == 2
    passed = await events(db, type_prefix="baton.passed")
    assert passed[-1]["payload"]["kind"] == "handover" and passed[-1]["moment"] == 1
    baton = await db.fetchone("SELECT * FROM crew_batons WHERE to_session = 'cs_b'")
    assert baton is not None and baton["from_session"] == "cs_a"
    # decline keeps it with the holder
    await C.handover(ops, got.claim, b, to="cs_c")  # type: ignore[arg-type]
    await C.decline_handover(ops, got.claim, Z.Principal.for_session(s3))  # type: ignore[arg-type]
    row = await C.get_claim(db.conn, got.claim["id"])  # type: ignore[index]
    assert row["state"] == "active" and row["holder_session_id"] == "cs_b"
    # timeout through the sweeper
    await C.handover(ops, got.claim, b, to="cs_c")  # type: ignore[arg-type]
    counts = await C.sweep(ops, now=C.utcnow() + timedelta(seconds=C.HANDOVER_TTL_S + 5))
    assert counts["handover_timeouts"] == 1
    declined = await events(db, type_prefix="claim.handover_declined")
    assert declined[-1]["payload"]["reason"] == "timeout"
    with pytest.raises(Z.CrewOpError):
        await C.handover(ops, got.claim, b, to="cs_b")  # type: ignore[arg-type]


async def _stall(ops, db, session_id, reason="quota"):
    async with ops.log.transaction() as tx:
        return await C.reserve_session_claims(ops, tx, CREW, session_id, reason, baton_ref="refs/remembra/baton/T-1/7")


async def test_adopt_needs_an_offer_across_checkouts(env):
    db, ops, audit, a, b = env
    await seed_task(db, "tsk_1", 1)
    c1 = await _claim(ops, a, db, "pos", task_id="tsk_1")
    c2 = await _claim(ops, a, db, "reports", task_id="tsk_1")
    reserved = await _stall(ops, db, "cs_a")
    assert set(reserved) == {c1.claim["id"], c2.claim["id"]}  # type: ignore[index]
    with pytest.raises(Z.CrewOpError) as err:
        await C.adopt(ops, c1.claim, b)  # type: ignore[arg-type]
    assert err.value.status == 403 and err.value.error == "not_offered"
    assert "remembra-crew adopt" not in err.value.message and "adopt T-" not in err.value.message
    async with ops.log.transaction() as tx:
        offer = await C.record_baton_offer(ops, tx, CREW, c1.claim["id"], "cs_b")  # type: ignore[index]
    res = await C.adopt(ops, c1.claim, b)  # type: ignore[arg-type]
    assert res["kind"] == "adopt" and res["cross_checkout"] is True
    assert {c["id"] for c in res["claims"]} == set(reserved)  # the task's batons move together
    assert all(c["holder_session_id"] == "cs_b" and c["epoch"] == 2 and c["state"] == "active" for c in res["claims"])
    used = await db.fetchone("SELECT used_at FROM crew_baton_offers WHERE id = ?", (offer["id"],))
    assert used is not None and used["used_at"] is not None
    adopted = await events(db, type_prefix="claim.adopted")
    assert len(adopted) == 2 and all(e["moment"] == 1 for e in adopted)
    passed = (await events(db, type_prefix="baton.passed"))[-1]["payload"]
    assert passed["kind"] == "adopt" and passed["baton_ref"] == "refs/remembra/baton/T-1/7" and passed["from_session"] == "cs_a"
    assert "crew.adopt_cross_checkout" in audit.actions()
    assert any(e["type"] == "claim.offered_in_brief" for e in await events(db))


async def test_adopt_same_checkout_reserved_for_and_setting(env):
    db, ops, _, a, _b = env
    same, _ = await seed_session(db, "cs_same", callsign="cc-5", worktree_id="wt-a", checkout_fp="fp-a")
    got = await _claim(ops, a, db, "pos")
    await _stall(ops, db, "cs_a", "lost")
    res = await C.adopt(ops, got.claim, Z.Principal.for_session(same))  # type: ignore[arg-type]
    assert res["kind"] == "same_checkout" and res["cross_checkout"] is False
    # reserved_for (offline): only the holder session itself; others need an offer
    other, _ = await seed_session(db, "cs_other", callsign="cc-6", worktree_id="wt-z")
    rep = await _claim(ops, Z.Principal.for_session(same), db, "reports")
    await _stall(ops, db, "cs_same", "offline")
    with pytest.raises(Z.CrewOpError):
        await C.adopt(ops, rep.claim, Z.Principal.for_session(other))  # type: ignore[arg-type]
    await ops.store.patch_settings(CREW, {"adopt_on_first_write": True}, if_match=1)
    res = await C.adopt(ops, rep.claim, Z.Principal.for_session(other))  # type: ignore[arg-type]
    assert res["kind"] == "first_write"
    # a human hold is never adoptable without a human hand-over
    hold = await _claim(ops, Z.Principal.for_session(other), db, "billing")
    await C.override(ops, hold.claim, HUMAN, action="hold", to=None, reason="wait for me")  # type: ignore[arg-type]
    with pytest.raises(Z.CrewOpError):
        await C.adopt(ops, hold.claim, Z.Principal.for_session(same))  # type: ignore[arg-type]


async def test_override_revoke_transfer_hold(env):
    db, ops, audit, a, b = env
    got = await _claim(ops, a, db, "pos")
    q = await _claim(ops, b, db, "pos", wait=True)
    res = await C.override(ops, got.claim, HUMAN, action="revoke", to=None, reason="stop")  # type: ignore[arg-type]
    assert res["claim"]["state"] == "revoked"
    assert (await C.get_claim(db.conn, q.claim["id"]))["state"] == "active"  # type: ignore[index]
    res = await C.override(ops, q.claim, HUMAN, action="transfer", to="cs_a", reason="Mani moves it")  # type: ignore[arg-type]
    assert res["claim"]["holder_session_id"] == "cs_a" and res["claim"]["source"] == "dashboard"
    overrides = [e for e in await events(db) if e["type"] == "human.override"]
    assert [e["payload"]["action"] for e in overrides] == ["revoke", "transfer"] and all(e["moment"] for e in overrides)
    notices = {i["recipient"] for i in await inbox(db, kind="override_notice")}
    assert {"cs_a", "cs_b"} <= notices
    assert (await events(db, type_prefix="baton.passed"))[-1]["payload"]["kind"] == "human_assign"
    with pytest.raises(Z.CrewOpError) as err:
        await C.override(ops, q.claim, HUMAN, action="transfer", to="cs_nope", reason="x")  # type: ignore[arg-type]
    assert err.value.error == "cross_crew_reference"
    await C.override(ops, q.claim, HUMAN, action="hold", to=None, reason="hold")  # type: ignore[arg-type]
    assert (await C.get_claim(db.conn, q.claim["id"]))["reserve_reason"] == "human_hold"  # type: ignore[index]
    assert {"crew.claim_revoke", "crew.claim_transfer", "crew.claim_hold"} <= set(audit.actions())


async def test_session_interface_for_wp4(env):
    db, ops, _, a, b = env
    got = await _claim(ops, a, db, "pos")
    before = (await C.get_claim(db.conn, got.claim["id"]))["lease_expires_at"]  # type: ignore[index]
    renewed = await C.renew_session_leases(db.conn, CREW, "cs_a", now=C.utcnow() + timedelta(minutes=5))
    assert renewed[got.claim["id"]]["lease_expires_at"] > before  # type: ignore[index]
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "idle")
    row = await C.get_claim(db.conn, got.claim["id"])  # type: ignore[index]
    assert row["state"] == "reserved" and row["reserved_for"] == "cs_a"
    assert (await _claim(ops, b, db, "pos")).status == "denied"
    async with ops.log.transaction() as tx:
        assert await C.retake_session_claims(ops, tx, CREW, "cs_a") == [got.claim["id"]]  # type: ignore[index]
    row = await C.get_claim(db.conn, got.claim["id"])  # type: ignore[index]
    assert row["state"] == "active" and row["epoch"] == 2
    async with ops.log.transaction() as tx:
        await C.release_session_claims(ops, tx, CREW, "cs_a", reason="left")
    assert (await C.get_claim(db.conn, got.claim["id"]))["state"] == "released"  # type: ignore[index]


async def test_sweeper_leases_reservations_and_boot_grace(env):
    db, ops, _, a, b = env
    await seed_task(db, "tsk_1", 1)
    got = await _claim(ops, a, db, "pos")
    later = C.utcnow() + timedelta(minutes=11)
    # boot grace: the server restarted 5 min later, so leases count from boot + lease_ttl
    counts = await C.sweep(ops, now=later, boot_at=C.utcnow() + timedelta(minutes=5))
    assert counts["leases_expired"] == 0
    counts = await C.sweep(ops, now=later)
    assert counts["leases_expired"] == 1
    row = await C.get_claim(db.conn, got.claim["id"])  # type: ignore[index]
    assert row["state"] == "reserved" and row["reserve_reason"] == "offline" and row["reserved_for"] == "cs_a"
    # a non-task reservation expires after reserve_ttl and wakes the queue; a task baton never does
    waiting = await _claim(ops, b, db, "pos", wait=True)
    tasked = await _claim(ops, b, db, "reports", task_id="tsk_1")
    await C.release_claim(ops, tasked.claim, b, baton=True)  # type: ignore[arg-type]
    much_later = C.utcnow() + timedelta(days=3)
    counts = await C.sweep(ops, now=much_later)
    assert counts["reservations_expired"] == 1
    assert (await C.get_claim(db.conn, got.claim["id"]))["state"] == "expired"  # type: ignore[index]
    assert (await C.get_claim(db.conn, tasked.claim["id"]))["state"] == "reserved"  # type: ignore[index]
    assert (await C.get_claim(db.conn, waiting.claim["id"]))["state"] == "active"  # type: ignore[index]
    # micro-leases expire instead of reserving
    ml = await C.request_claim(ops, CREW, a, path_glob="yarn.lock", source="micro_lease")
    assert ml.claim["lease_expires_at"] < now_iso(C.utcnow() + timedelta(seconds=C.MICRO_LEASE_S + 1))  # type: ignore[index]
    await C.sweep(ops, now=C.utcnow() + timedelta(seconds=C.MICRO_LEASE_S + 5))
    assert (await C.get_claim(db.conn, ml.claim["id"]))["state"] == "expired"  # type: ignore[index]
    # an expired freeze is lifted
    z = await Z.get_zone(db.conn, await zone_id(db, "billing"))
    await Z.freeze_zone(ops, z, HUMAN, reason="short", until=now_iso(C.utcnow() + timedelta(minutes=1)))
    counts = await C.sweep(ops, now=C.utcnow() + timedelta(minutes=2))
    assert counts["freezes_expired"] == 1
    assert (await Z.get_zone(db.conn, z["id"]))["frozen_by"] is None


async def test_long_poll_wait_returns_when_granted(env):
    db, ops, _, a, b = env
    got = await _claim(ops, a, db, "pos")
    queued = await _claim(ops, b, db, "pos", wait=True)

    async def release_soon() -> None:
        await asyncio.sleep(0.2)
        await C.release_claim(ops, got.claim, a)  # type: ignore[arg-type]

    task = asyncio.create_task(release_soon())
    loop = asyncio.get_running_loop()
    start = loop.time()
    row = await C.wait_for_claim(ops, queued.claim["id"], 10)  # type: ignore[index]
    await task
    assert row["state"] == "active" and loop.time() - start < 2
    still = await _claim(ops, a, db, "pos", wait=True)
    row = await C.wait_for_claim(ops, still.claim["id"], 0.3)  # type: ignore[index]
    assert row["state"] == "queued"


async def test_session_token_authentication(env):
    db, ops, _, _a, _b = env
    row, token = await seed_session(db, "cs_tok", callsign="cc-7")
    ok = await C.authenticate_session(db.conn, token, user_id=OWNER, crew_id=CREW)
    assert ok["id"] == "cs_tok"
    for kwargs in ({"user_id": "u_other"}, {"user_id": OWNER, "crew_id": OTHER_CREW}, {"user_id": OWNER, "session_id": "cs_a"}):
        with pytest.raises(Z.CrewOpError) as err:
            await C.authenticate_session(db.conn, token, **kwargs)
        assert err.value.status == 401
    with pytest.raises(Z.CrewOpError):
        await C.authenticate_session(db.conn, "wrong", user_id=OWNER)
    with pytest.raises(Z.CrewOpError):
        await C.authenticate_session(db.conn, None, user_id=OWNER)
    await db.conn.execute("UPDATE crew_sessions SET state = 'ended' WHERE id = 'cs_tok'")
    await db.conn.commit()
    with pytest.raises(Z.CrewOpError):
        await C.authenticate_session(db.conn, token, user_id=OWNER)
