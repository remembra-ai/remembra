"""Final review fixes, server side: fenced-holder footprints (E2E-f), unattributed commits, presence."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from remembra.crew import claims as C
from remembra.crew import collisions as CO
from remembra.crew import zones as Z
from remembra.crew.store import now_iso, parse_iso
from tests.crew.wp5_support import CREW, OWNER, ZONES_YML, assert_chain_ok, make_ops, open_db, seed_crew, seed_session, zone_id

HUMAN = Z.Principal.human(OWNER)


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, audit = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="cc-1", worktree_id="wt-a")
    c, _ = await seed_session(db, "cs_c", callsign="cc-3", worktree_id="wt-c")
    await Z.upload_zones_file(ops, CREW, Z.Principal.for_session(a), yaml_text=ZONES_YML, sha="s1", branch="main")
    try:
        yield db, ops, a, c
    finally:
        await assert_chain_ok(db)
        await db.close()


async def _fp(ops, session, *fps):
    async with ops.log.transaction() as tx:
        return await CO.record_footprints(ops, tx, CREW, session, list(fps))


async def _live(db, session_a=None):
    rows = await db.fetchall("SELECT * FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged')", (CREW,))
    return [r for r in rows if session_a is None or r["session_a"] == session_a]


async def _handed_to_c_after_offline(db, ops, a, c, *, horizon_in_s: float = 30.0, held_s: float = 2.2):
    """A holds POS (epoch 1) for ``held_s``, stops renewing (network cut) and is reserved offline;
    Mani hands POS to C (epoch 2). A's lease horizon is ``horizon_in_s`` after the grant."""
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    claim = got.claim
    assert claim is not None and claim["epoch"] == 1
    lease = C.utcnow() + timedelta(seconds=horizon_in_s + C.FENCE_MARGIN_S)
    await db.conn.execute("UPDATE crew_claims SET lease_expires_at = ? WHERE id = ?", (now_iso(lease), claim["id"]))
    await db.conn.commit()
    await asyncio.sleep(held_s)  # A keeps editing while it holds POS; crewd cannot deliver the footprints
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "offline")
    fresh = await C.get_claim(db.conn, claim["id"])
    await C.override(ops, fresh, HUMAN, action="transfer", to="cs_c", reason="A is offline")
    moved = await C.get_claim(db.conn, claim["id"])
    assert moved["holder_session_id"] == "cs_c" and moved["epoch"] == 2
    return claim


def _pos(path: str, **kw):
    return {"path": f"src/app/pos/{path}", "state": "dirty", "attribution": "certain", **kw}


async def test_fenced_holders_pre_horizon_writes_arriving_late_are_not_collisions(env):
    """E2E-f: A's edits made while it held POS reach the server after the transfer (epoch 1 < 2): no collision."""
    db, ops, a, c = env
    await _handed_to_c_after_offline(db, ops, a, c)
    fp = CO.heartbeat_footprints([_pos("split.ts", claim_epoch=1, age_s=1)])  # written 1 s before arriving
    assert fp[0]["age_s"] == 1
    assert await _fp(ops, a, *fp) == []
    assert await _live(db, "cs_a") == []


async def test_writes_without_a_write_time_or_under_another_epoch_still_collide(env):
    db, ops, a, c = env
    await _handed_to_c_after_offline(db, ops, a, c)
    # no write time (an older crewd): the server cannot tell when it was written, so it files both
    opened = await _fp(ops, a, _pos("cart.ts", claim_epoch=1))
    assert {r["kind"] for r in opened} == {"exclusive_breach", "stale_epoch_write"}
    # written after the transfer (age 0): outside A's window
    opened = await _fp(ops, a, _pos("receipt.ts", claim_epoch=1, age_s=0))
    assert {r["kind"] for r in opened} == {"exclusive_breach", "stale_epoch_write"}
    # an epoch A never held is a breach whatever its time
    opened = await _fp(ops, a, _pos("tender.ts", claim_epoch=7, age_s=1))
    assert {r["kind"] for r in opened} == {"exclusive_breach"}


async def test_write_after_the_holders_own_horizon_is_stale(env):
    """A's horizon passed 1 s after the grant: a write after it (the gate denies those) stays a collision."""
    db, ops, a, c = env
    await _handed_to_c_after_offline(db, ops, a, c, horizon_in_s=1.0)
    opened = await _fp(ops, a, _pos("split.ts", claim_epoch=1, age_s=0))  # ~2.2 s after the grant
    assert "stale_epoch_write" in {r["kind"] for r in opened}
    assert await _fp(ops, a, _pos("refund.ts", claim_epoch=1, age_s=2)) == []  # ~0.2 s after the grant


async def test_holding_window_reads_the_zone_claim_events(env):
    db, ops, a, c = env
    claim = await _handed_to_c_after_offline(db, ops, a, c, held_s=0.5)
    zid = str(claim["zone_id"])
    rows = await db.fetchall(
        "SELECT ts FROM crew_events WHERE crew_id = ? AND type = 'claim.granted' AND zone_id = ?", (CREW, zid)
    )
    granted = parse_iso(rows[0]["ts"])
    ok = CO._written_while_held
    assert await ok(db.conn, CREW, zid, "cs_a", 1, granted + timedelta(seconds=0.2))
    assert not await ok(db.conn, CREW, zid, "cs_a", 1, granted - timedelta(seconds=5))  # before the grant
    assert not await ok(db.conn, CREW, zid, "cs_a", 1, granted + timedelta(seconds=5))  # after the reservation
    assert not await ok(db.conn, CREW, zid, "cs_c", 1, granted + timedelta(seconds=0.2))  # C never held epoch 1
    assert await ok(db.conn, CREW, zid, "cs_c", 2, C.utcnow())  # C holds epoch 2 now (open window)


# ---------------------------------------------------------------------------
# lease_ttl_s below the host-silence threshold (§10.1: host-wide silence is not a stall)
# ---------------------------------------------------------------------------


def _presence(session_ago: int, host_ago: int, lease_ttl_s: int):
    from remembra.crew.events import format_ts
    from remembra.crew.reaper import presence_target
    from remembra.crew.settings import default_settings

    now = C.utcnow()
    ts = lambda s: format_ts(now - timedelta(seconds=s))  # noqa: E731
    row = {"host_id": "hst_1", "joined_at": ts(4000), "last_heartbeat_at": ts(session_ago), "last_activity_at": ts(session_ago)}
    settings = {**default_settings(), "lease_ttl_s": lease_ttl_s}
    t = presence_target(row, {"last_seen_at": ts(host_ago)}, settings, now=now, boot_at=now - timedelta(days=1))
    return t.state, t.quiet_reason, t.lost_reason


@pytest.mark.parametrize("silent_s", [121, 150, 179])
def test_short_lease_host_wide_silence_is_unreachable_not_lost(silent_s):
    """lease_ttl_s=120: a 2-3 min network blip of the whole host never marks its sessions lost."""
    assert _presence(silent_s, silent_s, 120) == ("quiet", "host_unreachable", None)


def test_short_lease_session_missing_from_a_live_hosts_heartbeats_is_lost():
    """The host keeps heartbeating without this session: that session's lease expiry is a real loss."""
    assert _presence(130, 5, 120) == ("lost", None, "lease_expired")
    assert _presence(700, 30, 600) == ("lost", None, "lease_expired")


def test_host_lost_after_still_applies_with_a_short_lease():
    assert _presence(1900, 1900, 120) == ("lost", None, "host_lost")


# ---------------------------------------------------------------------------
# F3: killing crewd by its pid is tamper (§5.2 row 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "pids"),
    [
        ("kill -9 4242", {4242}),
        ("kill 4242", {4242}),
        ("kill -KILL 4242 17", {4242, 17}),
        ("kill -s TERM 4242", {4242}),
        ("kill -n 9 4242", {4242}),
        ("kill -- -4242", {4242}),
        ("kill -9 -4242", {4242}),
        ("kill -1 4242", {4242}),  # -1 first is SIGHUP
        ("kill -9 -1", {-1}),  # every process
        ("kill %1", set()),
        ("kill -l", set()),
        ("sleep 1; kill -9 4242 && git commit -qam x", {4242}),
    ],
)
def test_kill_targets_are_parsed(command, pids):
    from remembra.crew import gatecore as G

    assert G.parse_bash_full(command).kill_pids == pids


@pytest.mark.parametrize("command", ["kill -9 4242", "kill 4242", "kill -s KILL 4242", "kill -- -4242", "kill -9 -1"])
def test_kill_of_crewds_pid_is_denied_as_tamper(command):
    from tests.crew.gatecore_support import run

    v = run("Bash", {"command": command}, protected_pids={4242})
    assert (v.decision, v.rule, v.tamper_kinds) == ("deny", 2, ("crewd_kill",)), v
    assert "crew daemon" in (v.reason or "")


def test_kill_of_other_pids_is_not_tamper():
    from tests.crew.gatecore_support import run

    assert run("Bash", {"command": "kill -9 999"}, protected_pids={4242}).decision == "allow"
    assert run("Bash", {"command": "kill -9 4242"}).decision == "allow"  # the gate did not know crewd's pid


@pytest.mark.parametrize(
    "command", ["pkill -f remembra", "killall remembra-crew", "pkill -f relay.crew", "pkill -9 -f remembra.relay"]
)
def test_pkill_patterns_that_match_crewd_are_tamper(command):
    from remembra.crew import gatecore as G

    assert "crewd_kill" in G.parse_bash_full(command).tamper


@pytest.mark.parametrize("command", ["pkill -f vite", "killall node", "pkill -f rem"])
def test_pkill_of_other_processes_is_not_tamper(command):
    from remembra.crew import gatecore as G

    assert "crewd_kill" not in G.parse_bash_full(command).tamper


# ---------------------------------------------------------------------------
# Baton restore outcome (§13.3 step 7): crew_batons.restored and baton.restored
# ---------------------------------------------------------------------------


async def _baton(db, *, to_session="cs_c", task_id="tsk_1"):
    from remembra.crew.store import new_id

    from tests.crew.wp5_support import seed_task

    await seed_task(db, task_id, 1, status="claimed")
    baton_id = new_id("baton")
    async with db.transaction():
        await db.conn.execute(
            """INSERT INTO crew_batons (id, crew_id, task_id, from_session, to_session, kind, baton_ref, restored, zone_ids,
                   seq, created_at) VALUES (?, ?, ?, 'cs_a', ?, 'adopt', 'refs/remembra/baton/T-1/1', NULL, '[]', 1, ?)""",
            (baton_id, CREW, task_id, to_session, now_iso()),
        )
    return baton_id


async def test_restore_success_sets_the_row_and_emits_baton_restored(env):
    from remembra.crew.tasks import record_baton_restore
    from tests.crew.wp5_support import events, inbox

    db, ops, a, c = env
    bid = await _baton(db)
    out = await record_baton_restore(ops.log, CREW, c, bid, restored=True, status="restored", files=3)
    assert out["restored"] is True and out["duplicate"] is False
    assert (await db.fetchone("SELECT restored FROM crew_batons WHERE id = ?", (bid,)))["restored"] == 1
    ev = [e for e in await events(db) if e["type"] == "baton.restored"]
    assert len(ev) == 1 and ev[0]["payload"] == {
        "baton_id": bid,
        "task_id": "tsk_1",
        "to_session": "cs_c",
        "baton_ref": "refs/remembra/baton/T-1/1",
        "restored": True,
        "status": "restored",
        "files": 3,
    }
    assert await inbox(db, kind="baton_restore_failed") == []
    again = await record_baton_restore(ops.log, CREW, c, bid, restored=True, status="restored", files=3)
    assert again["duplicate"] is True and len([e for e in await events(db) if e["type"] == "baton.restored"]) == 1


async def test_failed_restore_is_a_needs_you_safety_item_until_a_retry_succeeds(env):
    from remembra.crew import schemas as S
    from remembra.crew.tasks import record_baton_restore
    from tests.crew.wp5_support import events, inbox

    db, ops, a, c = env
    bid = await _baton(db)
    out = await record_baton_restore(ops.log, CREW, c, bid, restored=False, status="dirty_tree", files=0)
    assert out["restored"] is False
    assert (await db.fetchone("SELECT restored FROM crew_batons WHERE id = ?", (bid,)))["restored"] == 0
    items = await inbox(db, kind="baton_restore_failed")
    assert len(items) == 1 and items[0]["audience"] == "project" and items[0]["state"] == "open"
    assert items[0]["ref_type"] == "task" and items[0]["ref_id"] == "tsk_1" and "dirty_tree" in items[0]["title"]
    assert "baton_restore_failed" in S.SAFETY_INBOX_KINDS
    # the same failure reported again does not stack
    await record_baton_restore(ops.log, CREW, c, bid, restored=False, status="dirty_tree", files=0)
    assert len([e for e in await events(db) if e["type"] == "baton.restored"]) == 1
    # `adopt --restore-only` after cleaning the tree: success replaces the failure and resolves the item
    await record_baton_restore(ops.log, CREW, c, bid, restored=True, status="restored", files=2)
    assert (await db.fetchone("SELECT restored FROM crew_batons WHERE id = ?", (bid,)))["restored"] == 1
    assert (await inbox(db, kind="baton_restore_failed"))[0]["state"] == "resolved"


async def test_only_the_adopting_session_may_report_a_restore(env):
    from remembra.crew.tasks import BatonRestoreError, record_baton_restore

    db, ops, a, c = env
    bid = await _baton(db)
    with pytest.raises(BatonRestoreError) as e:
        await record_baton_restore(ops.log, CREW, a, bid, restored=True, status="restored", files=1)
    assert e.value.status == 404
    with pytest.raises(BatonRestoreError) as e:
        await record_baton_restore(ops.log, CREW, c, "bat_missing", restored=True, status="restored", files=1)
    assert e.value.status == 404
    with pytest.raises(BatonRestoreError) as e:
        await record_baton_restore(ops.log, CREW, c, bid, restored=True, status="dirty_tree", files=1)
    assert e.value.status == 422


def test_reducers_fold_the_restore_onto_its_baton_pass():
    from remembra.crew import reducer as R

    state = R.empty_state()
    passed = {"seq": 1, "type": "baton.passed", "payload": {"baton_id": "bat_1", "to_session": "cs_c", "restored": None}}
    restored = {"seq": 2, "type": "baton.restored", "payload": {"baton_id": "bat_1", "restored": False, "status": "dirty_tree"}}
    state = R.apply_event(state, {**passed, "refs": {}, "actor": {}})
    state = R.apply_event(state, {**restored, "refs": {}, "actor": {}})
    assert state["batons"][-1]["restored"] is False and state["batons"][-1]["restore_status"] == "dirty_tree"


# ---------------------------------------------------------------------------
# Needs-you safety items: tamper, missing git gate, false-deny storm, stuck, contested (§5.8, §10.3)
# ---------------------------------------------------------------------------


async def _ingest(ops, session, *items):
    from remembra.crew.events import ingest_client_events
    from remembra.crew.sessions import session_actor
    from remembra.crew.store import new_id

    events_ = [{"id": new_id("event")[-20:].lower().replace("_", "x"), **it} for it in items]
    return await ingest_client_events(ops.log, crew_id=CREW, actor=session_actor(session), items=events_)


def _blocked(zone="pos", decision="deny"):
    return {
        "type": "guard.blocked",
        "payload": {
            "path_rel": f"src/app/{zone}/x.ts",
            "zone": zone,
            "holder": "cc-1",
            "rule": 9,
            "op": "write",
            "decision": decision,
            "surface": "pretool",
            "coalesced": 1,
        },
    }


async def test_client_tamper_and_missing_githook_events_raise_needs_you_items(env):
    from tests.crew.wp5_support import inbox

    db, ops, a, c = env
    res = await _ingest(
        ops,
        c,
        {"type": "guard.tamper_blocked", "payload": {"kind": "no_verify", "surface": "pretool"}},
        {"type": "githook.missing", "payload": {"hook": "pre-push", "state": "missing", "worktree_id": None}},
        {"type": "githook.missing", "payload": {"hook": "pre-commit", "state": "chained", "worktree_id": None}},
    )
    assert [r.status for r in res] == ["accepted", "accepted", "accepted"], res
    tamper = await inbox(db, kind="tamper_blocked")
    assert len(tamper) == 1 and tamper[0]["audience"] == "project" and tamper[0]["ref_id"] == "cs_c", tamper
    assert "no_verify" in tamper[0]["title"] and tamper[0]["origin"] == "server"
    hooks = await inbox(db, kind="githook_missing")
    assert len(hooks) == 1 and "pre-push" in hooks[0]["title"], hooks  # "chained" is not missing


async def test_githook_item_resolves_when_the_heartbeat_reports_the_gate_back(tmp_path):
    """Through the WP-4 heartbeat: githook_state ok again resolves the git-gate-missing item."""
    from tests.crew.sessions_support import OWNER as S_OWNER, PROJECT, hb_item, host, join_req, make_env
    from remembra.crew.store import crew_id_for

    env = await make_env(tmp_path)
    try:
        h, tok = await host(env)
        joined = await env.svc.join(user_id=S_OWNER, req=join_req("s-a", host_id=h["id"]), host_token=tok)
        crew = crew_id_for(S_OWNER, PROJECT)
        sid = joined.session["id"]
        await env.svc.heartbeat(
            user_id=S_OWNER,
            host=h,
            body={"batch_id": "b1", "sessions": [hb_item(joined.session, joined.session_token, githook_state="missing")]},
        )
        from remembra.crew.events import ingest_client_events
        from remembra.crew.sessions import get_session, session_actor

        row = await get_session(env.db.conn, sid)
        await ingest_client_events(
            env.svc.log,
            crew_id=crew,
            actor=session_actor(row),
            items=[
                {
                    "id": "gh000001",
                    "type": "githook.missing",
                    "payload": {"hook": "pre-commit", "state": "missing", "worktree_id": None},
                }
            ],
        )
        items = await env.db.fetchall("SELECT state FROM crew_inbox_items WHERE kind = 'githook_missing'")
        assert [i["state"] for i in items] == ["open"]
        await env.svc.heartbeat(
            user_id=S_OWNER,
            host=h,
            body={"batch_id": "b2", "sessions": [hb_item(joined.session, joined.session_token, githook_state="ok")]},
        )
        items = await env.db.fetchall("SELECT state FROM crew_inbox_items WHERE kind = 'githook_missing'")
        assert [i["state"] for i in items] == ["resolved"]
    finally:
        await env.db.close()


async def test_false_deny_storm_alarm_at_five_denies_in_ten_minutes(env):
    from remembra.crew import alarms
    from tests.crew.wp5_support import inbox

    alarms._blocks.clear()
    db, ops, a, c = env
    res = await _ingest(ops, c, *[_blocked() for _ in range(4)], _blocked(decision="would_deny"))
    assert [r.status for r in res][0] == "accepted" and {r.status for r in res[1:]} == {"coalesced"}  # one event, 5 items
    assert await inbox(db, kind="false_deny_alarm") == []  # 4 denies (the would_deny is observe mode)
    await _ingest(ops, c, _blocked())
    items = await inbox(db, kind="false_deny_alarm")
    assert len(items) == 1, items
    item = items[0]
    assert (item["audience"], item["ref_type"], item["ref_id"], item["primary_action"]) == (
        "project",
        "session",
        "cs_c",
        "bypass",
    )
    assert "cc-3 was blocked 5 times" in item["title"]
    await _ingest(ops, c, _blocked(zone="reports"))
    assert len(await inbox(db, kind="false_deny_alarm")) == 1  # coalesced into the open item
    # after a restart (no in-memory counts) the event log still counts as a floor
    alarms._blocks.clear()
    async with ops.log.transaction() as tx:
        assert not await alarms.note_guard_block(tx, CREW, "cs_c", "cc-3", now=C.utcnow())  # 2 logged + 1 < 5


async def _sweep(ops, now=None):
    from remembra.crew import alarms
    from remembra.crew.settings import default_settings

    async def settings_for(_crew):
        return default_settings()

    return await alarms.sweep(ops.log, now or C.utcnow(), settings_for)


async def test_a_busy_hooked_agent_without_checkpoints_is_stuck_until_it_checkpoints(env):
    from tests.crew.wp5_support import events, inbox

    db, ops, a, c = env
    now = C.utcnow()
    await db.conn.execute(
        "UPDATE crew_sessions SET host_id = 'hst_1', state = 'active', next_checkpoint_due_at = ? WHERE id = 'cs_c'",
        (now_iso(now - timedelta(minutes=25)),),  # last checkpoint 35 min ago (interval 10 min): missed, not yet ×2
    )
    await db.conn.commit()
    got = await _sweep(ops, now)
    assert got["checkpoint_missed"] == 1 and got["stuck"] == 0
    assert (await _sweep(ops, now))["checkpoint_missed"] == 0  # once per checkpoint period
    await db.conn.execute(
        "UPDATE crew_sessions SET next_checkpoint_due_at = ? WHERE id = 'cs_c'", (now_iso(now - timedelta(minutes=31)),)
    )
    await db.conn.commit()
    got = await _sweep(ops, now)
    assert got["stuck"] == 1
    assert (await db.fetchone("SELECT stuck FROM crew_sessions WHERE id = 'cs_c'"))["stuck"] == 1
    stuck_ev = [e for e in await events(db) if e["type"] == "session.stuck"]
    assert stuck_ev[-1]["payload"] == {"signal": "checkpoint_missed", "stuck": True}
    items = await inbox(db, kind="stuck_agent")
    assert len(items) == 1 and items[0]["primary_action"] == "checkpoint" and items[0]["ref_id"] == "cs_c"
    assert (await _sweep(ops, now))["stuck"] == 0  # idempotent
    # a checkpoint arrives: due moves into the future → no longer stuck, the item resolves
    await db.conn.execute(
        "UPDATE crew_sessions SET next_checkpoint_due_at = ? WHERE id = 'cs_c'", (now_iso(now + timedelta(minutes=10)),)
    )
    await db.conn.commit()
    assert (await _sweep(ops, now))["unstuck"] == 1
    assert (await db.fetchone("SELECT stuck FROM crew_sessions WHERE id = 'cs_c'"))["stuck"] == 0
    assert (await inbox(db, kind="stuck_agent"))[0]["state"] == "resolved"
    assert [e for e in await events(db) if e["type"] == "session.stuck"][-1]["payload"]["stuck"] is False


async def test_mcp_only_sessions_are_nudged_never_stuck(env):
    from tests.crew.wp5_support import events, inbox

    db, ops, a, c = env
    now = C.utcnow()
    await db.conn.execute(
        "UPDATE crew_sessions SET host_id = NULL, state = 'active', next_checkpoint_due_at = ? WHERE id = 'cs_c'",
        (now_iso(now - timedelta(hours=2)),),
    )
    await db.conn.commit()
    got = await _sweep(ops, now)
    assert got["stuck"] == 0 and await inbox(db, kind="stuck_agent") == []
    missed = [e for e in await events(db) if e["type"] == "checkpoint.missed"]
    assert missed and missed[0]["payload"]["nudge"] is True


async def test_zone_contested_for_more_than_five_minutes(env):
    from tests.crew.wp5_support import inbox

    db, ops, a, c = env
    zid = await zone_id(db, "pos")
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=zid)
    assert got.claim is not None
    queued = await C.request_claim(ops, CREW, Z.Principal.for_session(c), zone_id=zid, wait=True)
    assert queued.claim is not None and queued.claim["state"] == "queued", queued
    now = C.utcnow()
    assert (await _sweep(ops, now))["contested"] == 0  # queued just now
    later = now + timedelta(minutes=6)
    got2 = await _sweep(ops, later)
    assert got2["contested"] == 1
    items = await inbox(db, kind="zone_contested")
    assert len(items) == 1 and items[0]["ref_type"] == "zone" and items[0]["ref_id"] == zid and "pos" in items[0]["title"]
    assert (await _sweep(ops, later))["contested"] == 0  # one item while it lasts
    await C.release_claim(ops, got.claim, Z.Principal.for_session(a))  # A releases; C's queued claim is granted
    assert (await _sweep(ops, later))["uncontested"] == 1
    assert (await inbox(db, kind="zone_contested"))[0]["state"] == "resolved"


# ---------------------------------------------------------------------------
# Memory promotions stay bounded: server-only quota, owner-wide cap, cap re-check, plan gate
# ---------------------------------------------------------------------------


@pytest.fixture
async def ckp(tmp_path):
    import dataclasses

    from remembra.crew.checkpoints import CheckpointService
    from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS
    from tests.crew.wp6_support import event_log, open_db as open6, seed_crew as crew6

    db = await open6(tmp_path)
    await crew6(db)
    cap = {"n": 1}

    async def resolver(owner):
        return dataclasses.replace(SELF_HOSTED_CREW_LIMITS, memory_promotions_per_day=cap["n"])

    svc = CheckpointService(event_log(db), limits_resolver=resolver)
    try:
        yield db, svc, resolver, cap
    finally:
        await db.close()


def _ck(session, trigger="turn", n=0):
    return {
        "session_id": session["id"],
        "trigger": trigger,
        "facts": {"head": f"{n:012x}", "commits": [], "n": n},
        "task_id": None,
    }


async def test_fifty_quota_checkpoints_from_one_session_are_refused(ckp):
    """The review's runtime proof, reversed: 50 client 'quota' checkpoints with cap 1 → none promoted."""
    from remembra.crew.tasks import Caller, CrewServiceError
    from tests.crew.wp6_support import CREW as C6, seed_session as sess6

    db, svc, _, _ = ckp
    s = await sess6(db)
    for i in range(50):
        with pytest.raises(CrewServiceError) as e:
            await svc.ingest(C6, Caller.for_session(s), _ck(s, "quota", i))
        assert e.value.error == "server_trigger"
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM crew_outbox"))["n"] == 0


async def test_the_daily_cap_is_per_owner_across_crews(ckp):
    from remembra.crew.tasks import Caller
    from tests.crew.wp6_support import CREW as C6, OTHER_CREW, seed_crew as crew6, seed_session as sess6

    db, svc, _, _ = ckp
    await crew6(db, OTHER_CREW, owner="u_owner", project="other-project")
    a = await sess6(db)
    b = await sess6(db, OTHER_CREW, callsign="cc-9")
    first = await svc.ingest(C6, Caller.for_session(a), _ck(a, n=1))
    other = await svc.ingest(OTHER_CREW, Caller.for_session(b), _ck(b, n=2))
    assert (first.promotion, other.promotion) == ("promoted", "deferred")


async def test_a_deferred_promotion_is_checked_against_the_cap_again_when_it_comes_due(ckp, tmp_path):
    from types import SimpleNamespace

    from remembra.crew.outbox import CrewOutboxWorker, memory_promotion_handler
    from remembra.crew.store import CrewStore
    from remembra.crew.tasks import Caller
    from tests.crew.wp6_support import CREW as C6, seed_session as sess6

    db, svc, resolver, _ = ckp
    a = await sess6(db)
    b = await sess6(db, callsign="cc-2")
    assert (await svc.ingest(C6, Caller.for_session(a), _ck(a, n=1))).promotion == "promoted"
    assert (await svc.ingest(C6, Caller.for_session(b), _ck(b, n=2))).promotion == "deferred"
    # the deferred row comes due while today's cap is already used (e.g. yesterday's backlog today)
    await db.conn.execute("UPDATE crew_outbox SET next_attempt_at = ? WHERE next_attempt_at IS NOT NULL", (now_iso(),))
    await db.conn.commit()
    stored = []

    class Memory:
        settings = SimpleNamespace(checkpoint_default_ttl="7d")

        async def store(self, request, **kw):
            stored.append(request)
            return SimpleNamespace(id=f"mem_{len(stored)}", status="stored")

    main = SimpleNamespace(conn=None)

    class _NoMain:
        async def execute(self, *a, **k):
            class C:
                async def fetchone(self):
                    return None

            return C()

    main.conn = _NoMain()
    store = CrewStore(db)
    handler = memory_promotion_handler(main, Memory(), crew_store=store, limits_for=resolver)
    worker = CrewOutboxWorker(store, {"memory_promotion": handler})
    counts = await worker.run_once()
    assert counts == {"done": 1, "retry": 1, "failed": 0} and len(stored) == 1
    rows = await db.fetchall("SELECT state, attempts, next_attempt_at, last_error FROM crew_outbox ORDER BY created_at")
    deferred = [r for r in rows if r["state"] == "pending"]
    assert len(deferred) == 1 and deferred[0]["attempts"] == 0, rows  # a plan cap is not a failed attempt
    tomorrow = (C.utcnow().date() + timedelta(days=1)).isoformat()
    assert (
        deferred[0]["next_attempt_at"].startswith(tomorrow + "T00:00:00")
        and "memory_promotions_per_day" in deferred[0]["last_error"]
    )


async def test_promotions_go_through_the_plan_gate_and_are_metered(tmp_path):
    """With a usage meter the outbox applies the same memory cap as POST /memories and meters the store."""
    from types import SimpleNamespace

    from remembra.cloud.metering import UsageMeter
    from remembra.crew.outbox import OutboxItem, OutboxPermanentError, memory_promotion_handler
    from remembra.storage.database import Database

    main = Database(str(tmp_path / "main.db"))
    await main.connect()
    await main.init_schema()
    try:
        meter = UsageMeter(main)
        await meter.init_schema()
        app = SimpleNamespace(state=SimpleNamespace(usage_meter=meter, db=main, tasks=None))
        stored = []

        class Memory:
            settings = SimpleNamespace(checkpoint_default_ttl="7d")

            async def store(self, request, **kw):
                stored.append(request)
                return SimpleNamespace(id=f"mem_{len(stored)}", status="stored")

        handler = memory_promotion_handler(main, Memory(), app=app)
        payload = {
            "user_id": "u_meter",
            "project_id": "p1",
            "memory_type": "checkpoint",
            "content": "[CHECKPOINT] x",
            "metadata": {"checkpoint_trigger": "turn"},
        }
        item = OutboxItem(id="obx_1", crew_id="crw_1", kind="memory_promotion", payload=payload, attempts=0, created_at=now_iso())
        assert await handler(item) == "mem_1"
        period = await meter.get_period_counters("u_meter", datetime_month_start())
        assert period["relay_events"] == 1  # a checkpoint is a relay event: metered, no credits
        # at the plan's memory cap: refused like POST /memories (429 → deferred an hour, not stored)
        account = await meter.get_account("u_meter")
        now = datetime_now_iso()
        await main.conn.executemany(
            "INSERT INTO memories (id, user_id, project_id, content, created_at, updated_at) VALUES (?, ?, 'p1', 'x', ?, ?)",
            [(f"fill-{i}", "u_meter", now, now) for i in range(int(account.memory_cap))],
        )
        await main.conn.commit()
        from remembra.crew.outbox import OutboxDeferred

        with pytest.raises(OutboxDeferred):
            await handler(
                OutboxItem(id="obx_2", crew_id="crw_1", kind="memory_promotion", payload=payload, attempts=0, created_at=now)
            )
        assert len(stored) == 1
        assert OutboxPermanentError  # imported for the 4xx path (non-429 refusals fail the item)
    finally:
        await main.close()


def datetime_month_start():
    from remembra.cloud.limits import _calendar_month_start

    return _calendar_month_start()


def datetime_now_iso():
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Bypass codes are scoped (D34): push, commit, write:<zone>
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "surface", "zone", "ok"),
    [
        ("push", "prepush", None, True),
        ("push", "precommit", None, False),
        ("push", "pretool", "pos", False),
        ("commit", "precommit", None, True),
        ("commit", "prepush", None, False),
        ("write:pos", "pretool", "pos", True),
        ("write:pos", "mcp", "pos", True),
        ("write:pos", "pretool", "reports", False),
        ("write:pos", "pretool", None, False),
        ("write:pos", "precommit", None, False),
        ("all", "pretool", "pos", True),  # the offline TTY grant (no code, server unreachable)
        ("push", "tty", None, True),  # stored in crewd, checked at use
        ("gate", "pretool", "pos", False),
    ],
)
def test_bypass_scope_table(scope, surface, zone, ok):
    from remembra.crew import schemas as S

    assert S.bypass_scope_matches(scope, surface, zone) is ok


async def test_a_push_code_does_not_open_a_commit_and_is_not_consumed_by_trying(env):
    from remembra.crew import bypass as B

    db, ops, a, c = env
    issued = await B.issue_code(ops, CREW, HUMAN, session_id="cs_c", scope="push", minutes=15)
    for surface, zone in (("precommit", None), ("pretool", "pos"), ("mcp", "pos")):
        with pytest.raises(Z.CrewOpError) as e:
            await B.redeem_code(ops, c, issued["code"], surface=surface, zone=zone)
        assert (e.value.status, e.value.error) == (403, "bypass_scope_mismatch")
    row = await db.fetchone("SELECT used_at FROM crew_bypass_codes WHERE id = ?", (issued["code_id"],))
    assert row["used_at"] is None  # a refused use does not burn the code
    used = await B.redeem_code(ops, c, issued["code"], surface="prepush")
    assert used["ok"] and used["scope"] == "push"


async def test_a_zone_write_code_covers_only_that_zone(env):
    from remembra.crew import bypass as B

    db, ops, a, c = env
    issued = await B.issue_code(ops, CREW, HUMAN, session_id="cs_c", scope="write:pos", minutes=5)
    with pytest.raises(Z.CrewOpError):
        await B.redeem_code(ops, c, issued["code"], surface="pretool", zone="reports")
    with pytest.raises(Z.CrewOpError):
        await B.redeem_code(ops, c, issued["code"], surface="prepush")
    assert (await B.redeem_code(ops, c, issued["code"], surface="pretool", zone="pos"))["scope"] == "write:pos"


async def test_crewd_keeps_the_codes_scope_and_remaining_life(tmp_path, monkeypatch):
    """`remembra-crew bypass --code` at a TTY: crewd stores the code's own scope and at most its remaining
    lifetime (not scope 'all' and now + 15 min), and consumes it only for a matching use."""
    import time as _time
    from datetime import UTC, datetime

    from remembra.relay.crew import crewd as CD
    from remembra.relay.crew.gate import Layout

    d = CD.Crewd(Layout(tmp_path / "home"))
    sess = {"key": "k1", "session_id": "cs_c", "callsign": "cc-3", "crew_id": CREW, "agent_pid": 999999, "crew_url": "x"}
    d.sessions["k1"] = sess
    d.tokens["k1"] = "tok"
    left_s = 60
    expires = datetime.fromtimestamp(_time.time() + left_s, UTC).isoformat()

    class Resp:
        ok, status = True, 200
        body = {"ok": True, "code_id": "byp_1", "scope": "write:pos", "expires_at": expires}

    seen = {}

    async def fake_request(api, method, path, **kw):
        seen["body"] = kw.get("json_body")
        return Resp()

    monkeypatch.setattr(d, "request", fake_request)
    monkeypatch.setattr(d, "api_for", lambda s: None)
    monkeypatch.setattr(CD, "has_controlling_tty", lambda pid: True)
    monkeypatch.setattr(d, "resolve_peer", lambda peer, key=None: None if key is None else sess)
    peer = CD.Peer(pid=4242, uid=None, chain=(4242,))
    out = await d.bypass(peer, {"session": "cc-3", "code": "RCB-7K3QW-9ZX2M", "minutes": 15})
    assert out["ok"] and out["scope"] == "write:pos" and out["expires_in_s"] <= left_s
    assert seen["body"]["surface"] == "tty"
    grant = sess["bypass"]
    assert grant["scope"] == "write:pos" and grant["expires_at"] <= d.clock() + left_s
    assert d.bypass_consume(peer, {"key": "k1", "surface": "precommit"})["error"] == "scope_mismatch"
    assert d.bypass_consume(peer, {"key": "k1", "surface": "pretool", "zone": "reports"})["error"] == "scope_mismatch"
    assert d.bypass_consume(peer, {"key": "k1", "surface": "pretool", "zone": "pos"})["ok"] is True
    assert d.bypass_consume(peer, {"key": "k1", "surface": "pretool", "zone": "pos"})["error"] == "no_grant"  # single use


# ---------------------------------------------------------------------------
# Email notification targets need proof of ownership; subjects carry no agent text; daily cap
# ---------------------------------------------------------------------------


class _Mail:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send(self, message):
        from remembra.cloud.email import EmailResult

        self.sent.append(message)
        return EmailResult(success=self.ok, message_id=f"em-{len(self.sent)}", error=None if self.ok else "boom")


async def test_a_third_party_email_is_unverified_until_its_mailed_code_is_entered(tmp_path):
    import re

    from remembra.crew.notify import NotifyError, NotifyTargetNotFound, NotifyTargets
    from tests.crew.wp7_support import OWNER as O7, make_env

    env = await make_env(tmp_path)
    mail = _Mail()
    targets = NotifyTargets(env.db, email_backend=mail, account_email="mani@example.com")
    out = await targets.add(O7, "email", "Victim@Elsewhere.example")
    assert out["verified_at"] is None and out["confirmation"] == "sent"
    [confirm] = mail.sent
    assert confirm.to == "victim@elsewhere.example" and confirm.subject == "Confirm this address for Remembra crew alerts"
    code = re.search(r"<strong>([0-9A-Z]{8})</strong>", confirm.html).group(1)
    row = await env.db.fetchone("SELECT secret_hash FROM crew_notify_targets WHERE id = ?", (out["id"],))
    assert code not in str(row["secret_hash"])  # only a hash is stored
    with pytest.raises(NotifyError):
        await targets.confirm(O7, out["id"], "WRONG123")
    with pytest.raises(NotifyTargetNotFound):
        await targets.confirm("u_someone_else", out["id"], code)
    done = await targets.confirm(O7, out["id"], code.lower())
    assert done["verified_at"]
    # the account's own verified address needs no code and sends nothing
    own = await targets.add(O7, "email", "Mani@Example.com")
    assert own["verified_at"] and own["confirmation"] == "not_needed" and len(mail.sent) == 1
    await env.db.close()


async def test_an_expired_code_does_not_verify_and_unverified_targets_get_no_alerts(tmp_path):
    from remembra.crew.notify import KIND_CREW_NOTIFY, NotifyError, NotifyTargets, notify_handler
    from remembra.crew.outbox import CrewOutboxWorker, OutboxItem, OutboxPermanentError
    from remembra.crew.store import CrewStore
    from tests.crew.wp7_support import OWNER as O7, make_env

    env = await make_env(tmp_path)
    mail = _Mail()
    targets = NotifyTargets(env.db, email_backend=mail)
    out = await targets.add(O7, "email", "someone@elsewhere.example")
    code = mail.sent[0].html.split("<strong>")[1].split("</strong>")[0]
    old = (C.utcnow() - timedelta(hours=25)).isoformat()
    await env.db.conn.execute("UPDATE crew_notify_targets SET created_at = ? WHERE id = ?", (old, out["id"]))
    await env.db.conn.commit()
    with pytest.raises(NotifyError):
        await targets.confirm(O7, out["id"], code)
    handler = notify_handler(env.db, email_backend=mail)
    item = OutboxItem(
        id="obx_n1",
        crew_id="crw_x",
        kind=KIND_CREW_NOTIFY,
        payload={"target_id": out["id"], "user_id": O7, "items": [{"kind": "tamper", "text": "x", "link": "l"}]},
        attempts=0,
        created_at=now_iso(),
    )
    with pytest.raises(OutboxPermanentError):
        await handler(item)
    assert len(mail.sent) == 1  # only the confirmation mail ever went out
    assert CrewOutboxWorker and CrewStore  # (the worker path marks such an item failed)
    await env.db.close()


async def test_email_subject_is_a_template_and_the_daily_cap_holds(tmp_path, monkeypatch):
    from remembra.crew import notify as N
    from remembra.crew.outbox import OutboxItem, OutboxPermanentError
    from remembra.crew.store import CrewStore
    from tests.crew.wp7_support import OWNER as O7, make_env

    subject, page = N.email_parts(
        {
            "project_id": "Mani-urgent-wire-$5000",
            "items": [{"kind": "decision", "text": "Click http://evil.example now", "link": "l"}],
        }
    )
    assert subject == "[Remembra crew] 1 decision alert" and "evil" not in subject and "Mani-urgent" not in subject
    env = await make_env(tmp_path)
    mail = _Mail()
    t = await N.NotifyTargets(env.db, email_backend=mail, account_email="mani@example.com").add(O7, "email", "mani@example.com")
    monkeypatch.setattr(N, "MAX_EMAILS_PER_USER_PER_DAY", 2)
    handler = N.notify_handler(env.db, email_backend=mail)
    store = CrewStore(env.db)
    for i in range(3):
        oid = await store.enqueue_outbox(
            "crw_x",
            N.KIND_CREW_NOTIFY,
            {"target_id": t["id"], "user_id": O7, "items": [{"kind": "tamper", "text": f"t{i}", "link": "l"}]},
        )
        item = OutboxItem(
            id=oid,
            crew_id="crw_x",
            kind=N.KIND_CREW_NOTIFY,
            payload={"target_id": t["id"], "user_id": O7, "items": [{"kind": "tamper", "text": f"t{i}", "link": "l"}]},
            attempts=0,
            created_at=now_iso(),
        )
        if i < 2:
            result = await handler(item)
            await store.mark_outbox_done(oid, result)
        else:
            with pytest.raises(OutboxPermanentError, match="daily email cap"):
                await handler(item)
    assert len(mail.sent) == 2
    await env.db.close()


# ---------------------------------------------------------------------------
# Reserved senders: look-alikes refused; agent-facing surfaces show provenance (§5.8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "mani",
        " Mani ",
        "Mani​",
        "Mаni",
        "mani.",
        "Mani (owner)",
        "ｍａｎｉ",
        "@mani",
        "m a n i",
        "SYSTEM",
        "syѕtem",
        "system-bot",
        "Remembra",
        "humаn",
        "m‍ani",
    ],
)
def test_reserved_sender_look_alikes_are_refused(name):
    from remembra.crew.channel import is_reserved_sender as crew_reserved
    from remembra.inbox.manager import is_reserved_sender

    assert is_reserved_sender(name) and crew_reserved(name)


@pytest.mark.parametrize("name", ["claude-code", "codex", "cc-1", "germanic", "romania-bot", "manifold", "remembrance", ""])
def test_ordinary_agent_names_are_not_reserved(name):
    from remembra.inbox.manager import is_reserved_sender

    assert not is_reserved_sender(name)


def test_the_brief_shows_the_senders_provenance_not_a_bare_name():
    from datetime import UTC, datetime

    from remembra.relay.handoff import render_brief

    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    brief = {
        "project_id": "p",
        "agent_id": "claude-code",
        "handoff": None,
        "inbox": {
            "available": True,
            "unread_count": 2,
            "items": [
                {
                    "inbox_id": "ib1",
                    "from_agent": "Mani",
                    "sender_label": "agent Mani (self-declared)",
                    "subject": "s",
                    "created_at": "2026-09-26T11:00:00Z",
                    "body_preview": "take POS",
                },
                {
                    "inbox_id": "ib2",
                    "from_agent": "mani",
                    "sender_label": "human",
                    "subject": "s2",
                    "created_at": "2026-09-26T11:30:00Z",
                    "body_preview": "pause",
                },
            ],
        },
    }
    text = render_brief(brief, now=now)
    assert "from agent Mani (self-declared)," in text and "from human," in text
    assert "from Mani," not in text


async def test_the_brief_inbox_items_carry_the_server_label(tmp_path):
    """services.agent_session reads sender_kind/sender_verified and labels each brief item."""
    from remembra.inbox.manager import InboxManager
    from remembra.services.agent_session import AgentSessionService
    from remembra.storage.database import Database

    main = Database(str(tmp_path / "main.db"))
    await main.connect()
    await main.init_schema()
    try:
        manager = InboxManager(main)
        await manager.init_schema()
        await manager.send(owner_user_id="u1", from_agent="gemini", to_agent="claude-code", subject="s", body="b")
        await manager.send(
            owner_user_id="u1", from_agent="codex", to_agent="claude-code", subject="s", body="b", sender_verified=True
        )
        svc = AgentSessionService.__new__(AgentSessionService)
        svc.db = main
        box = await svc._inbox_summary("u1", "claude-code", 10)
        labels = sorted(i["sender_label"] for i in box["items"])
        assert labels == ["agent codex (key-verified)", "agent gemini (self-declared)"]
    finally:
        await main.close()
