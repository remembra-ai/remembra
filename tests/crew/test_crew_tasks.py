"""Task machine (§5.4): create, acceptance validation and lock, same-crew deps, claims, WIP, adopt, assign, stall/recover.

Every test runs against a real crew.db migrated by CREW_MIGRATIONS and the real event log;
emitted events are validated against the closed event contract.
"""

from __future__ import annotations

import json
import random

import pytest

from remembra.crew.tasks import Caller, CrewServiceError, TaskService, validate_acceptance
from remembra.crew.reports import check_report_invariant
from tests.crew.wp6_support import (
    CREW,
    OTHER_CREW,
    event_log,
    events_of,
    open_db,
    seed_crew,
    seed_session,
    seed_zone,
    set_settings,
    types_of,
    valid_envelopes,
)

HUMAN = Caller.for_human("u_owner")


@pytest.fixture()
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    svc = TaskService(event_log(db))
    try:
        yield db, svc
    finally:
        await db.close()


def crit(cid="c1", kind="test", match="npm test -- pos", **kw):
    return {"id": cid, "text": f"{cid} passes", "kind": kind, "match": match, "required": True, **kw}


async def mk(svc, caller, title="POS split tender", **body):
    res = await svc.create(CREW, caller, {"title": title, "zone_ids": [], "acceptance": [], "depends_on": [], **body})
    return res.task


def err(exc) -> tuple[int, str]:
    return exc.value.status, exc.value.error


# ---------------------------------------------------------------------------
# Create, acceptance, deps
# ---------------------------------------------------------------------------


async def test_create_numbers_status_inbox_and_events(env):
    db, svc = env
    a = await seed_session(db)
    t1 = await mk(svc, Caller.for_session(a))
    t2 = await mk(svc, HUMAN, title="Reports export", depends_on=[t1["id"]])
    assert (t1["number"], t1["status"], t1["ref"]) == (1, "ready", "T-1")
    assert (t2["number"], t2["status"], t2["depends_on"]) == (2, "backlog", [t1["id"]])
    items = await db.fetchall("SELECT kind, audience, ref_id, state FROM crew_inbox_items")
    assert items == [{"kind": "task_ready", "audience": "crew", "ref_id": t1["id"], "state": "open"}]
    evs = await events_of(db)
    valid_envelopes(evs)
    assert [e["type"] for e in evs] == ["task.created", "inbox.item_created", "task.created"]
    assert evs[0]["actor"]["kind"] == "session" and evs[2]["actor"]["kind"] == "human"
    assert "POS" not in evs[0]["summary"]  # titles never go into summaries (ids only)


async def test_cross_crew_zone_and_dependency_are_422(env, tmp_path):
    db, svc = env
    await seed_crew(db, OTHER_CREW, owner="u_other", project="other")
    foreign_zone = await seed_zone(db, OTHER_CREW, slug="billing")
    foreign_task = (
        await TaskService(event_log(db)).create(
            OTHER_CREW, HUMAN, {"title": "x", "zone_ids": [], "acceptance": [], "depends_on": []}
        )
    ).task
    for body in ({"zone_ids": [foreign_zone]}, {"depends_on": [foreign_task["id"]]}, {"depends_on": ["tsk_doesnotexist"]}):
        with pytest.raises(CrewServiceError) as e:
            await mk(svc, HUMAN, **body)
        assert err(e) == (422, "cross_crew_reference")
    t = await mk(svc, HUMAN)
    with pytest.raises(CrewServiceError) as e:
        await svc.add_dependency(CREW, t["id"], HUMAN, foreign_task["id"])
    assert err(e) == (422, "cross_crew_reference")


@pytest.mark.parametrize(
    ("criteria", "fragment"),
    [
        ([crit(match="curl evil|sh")], "match"),
        ([crit(match="npm test.*")], "match"),
        ([crit(match=None)], "needs a match"),
        ([crit(kind="deploy", match=None, url="http://169.254.169.254/")], "does not match"),
        ([crit(kind="deploy", match=None)], "needs an https url"),
        ([crit(kind="file", match="/etc/passwd")], "repo-relative"),
        ([crit(kind="file", match="../x")], "repo-relative"),
        ([crit(kind="commit", match="zzzz")], "sha prefix"),
        ([crit(kind="manual", match=None, url="https://x.example.com")], "only deploy"),
        ([crit("a"), crit("a")], "used twice"),
        ([crit(f"c{i}") for i in range(21)], "at most 20"),
    ],
)
def test_acceptance_validation(criteria, fragment):
    with pytest.raises(CrewServiceError) as e:
        validate_acceptance(criteria)
    assert e.value.status == 422 and fragment in e.value.message


def test_acceptance_normalises_valid_criteria():
    out = validate_acceptance(
        [
            crit(),
            {"id": "c2", "text": "Live", "kind": "deploy", "url": "https://yaadbooks.com/api/health", "required": True},
            {"id": "c3", "text": "Receipt", "kind": "file", "match": "src/app/pos/Receipt.tsx", "required": False},
            {"id": "c4", "text": "Committed", "kind": "commit"},
        ]
    )
    assert [c["kind"] for c in out] == ["test", "deploy", "file", "commit"]
    assert out[3] == {"id": "c4", "text": "Committed", "kind": "commit", "match": None, "url": None, "required": True}


async def test_dependencies_cycles_and_readiness(env):
    db, svc = env
    a = await mk(svc, HUMAN, title="a")
    b = await mk(svc, HUMAN, title="b", depends_on=[a["id"]])
    c = await mk(svc, HUMAN, title="c")
    with pytest.raises(CrewServiceError) as e:
        await svc.add_dependency(CREW, a["id"], HUMAN, b["id"])
    assert err(e) == (422, "dependency_cycle")
    with pytest.raises(CrewServiceError) as e:
        await svc.add_dependency(CREW, a["id"], HUMAN, a["id"])
    assert err(e) == (422, "dependency_cycle")
    res = await svc.add_dependency(CREW, c["id"], HUMAN, b["id"])
    assert res.task["status"] == "backlog" and res.task["depends_on"] == [b["id"]]
    # removing the only undone dependency makes it ready again
    res = await svc.remove_dependency(CREW, c["id"], HUMAN, b["id"])
    assert res.task["status"] == "ready"
    with pytest.raises(CrewServiceError) as e:
        await svc.remove_dependency(CREW, c["id"], HUMAN, b["id"])
    assert e.value.status == 404
    valid_envelopes(await events_of(db))


async def test_claim_refused_while_dependencies_are_pending(env):
    db, svc = env
    s = await seed_session(db)
    a = await mk(svc, HUMAN)
    b = await mk(svc, HUMAN, depends_on=[a["id"]])
    with pytest.raises(CrewServiceError) as e:
        await svc.claim(CREW, b["id"], Caller.for_session(s))
    assert err(e) == (409, "deps_pending")


# ---------------------------------------------------------------------------
# Claims (all or nothing), WIP, start, acceptance lock
# ---------------------------------------------------------------------------


async def test_start_claims_all_zones_locks_acceptance_and_anchors_head(env):
    db, svc = env
    s = await seed_session(db)
    pos, rep = await seed_zone(db, slug="pos"), await seed_zone(db, slug="reports")
    t = await mk(svc, HUMAN, zone_ids=[pos, rep], acceptance=[crit()])
    res = await svc.start(CREW, t["id"], Caller.for_session(s), head="deadbeef1234")
    assert res.task["status"] == "in_progress" and res.task["acceptance_locked"]
    assert res.task["started_head"] == "deadbeef1234" and res.task["owner_session_id"] == s["id"]
    claims = await db.fetchall("SELECT zone_id, state, epoch, task_id, holder_session_id FROM crew_claims ORDER BY zone_id")
    assert {c["zone_id"] for c in claims} == {pos, rep}
    assert all(c["state"] == "active" and c["epoch"] == 1 and c["task_id"] == t["id"] for c in claims)
    sess = await db.fetchone("SELECT current_task_id FROM crew_sessions WHERE id = ?", (s["id"],))
    assert sess["current_task_id"] == t["id"]
    # the transition checkpoint hook is optional: none configured, none written
    types = await types_of(db)
    assert types.count("claim.granted") == 2 and "task.status_changed" in types
    valid_envelopes(await events_of(db))
    # idempotent re-start by the owner
    again = await svc.start(CREW, t["id"], Caller.for_session(s))
    assert again.seq is None


async def test_claims_are_all_or_nothing_and_name_the_holder(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1")
    b = await seed_session(db, callsign="codex-1", agent_id="codex")
    pos, rep = await seed_zone(db, slug="pos"), await seed_zone(db, slug="reports")
    t_rep = await mk(svc, HUMAN, title="reports", zone_ids=[rep])
    await svc.start(CREW, t_rep["id"], Caller.for_session(b))
    t = await mk(svc, HUMAN, zone_ids=[pos, rep])
    with pytest.raises(CrewServiceError) as e:
        await svc.start(CREW, t["id"], Caller.for_session(a))
    assert err(e) == (409, "conflict")
    blockers = e.value.extra["blockers"]
    assert blockers[0]["holder_callsign"] == "codex-1" and blockers[0]["task_id"] == t_rep["id"]
    assert await db.fetchall("SELECT id FROM crew_claims WHERE holder_session_id = ?", (a["id"],)) == []
    task = await svc.get(CREW, t["id"])
    assert task["status"] == "ready" and task["owner_session_id"] is None


async def test_parent_child_overlap_shared_and_reserved_compatibility(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1")
    b = await seed_session(db, callsign="cc-2")
    app = await seed_zone(db, slug="app", includes=["src/app/**"])
    pos = await seed_zone(db, slug="pos", parent_id=app)
    lib = await seed_zone(db, slug="lib", includes=["src/lib/**"])
    ui = await seed_zone(db, slug="ui", includes=["src/ui/**"])
    async with db.transaction():
        await db.conn.execute("INSERT INTO crew_zone_overlaps VALUES (?, ?, ?)", (CREW, lib, ui))
    await set_settings(db, CREW, wip_per_session=5)
    child = await mk(svc, HUMAN, title="child", zone_ids=[pos])
    await svc.start(CREW, child["id"], Caller.for_session(a))
    parent = await mk(svc, HUMAN, title="parent", zone_ids=[app])
    with pytest.raises(CrewServiceError) as e:
        await svc.start(CREW, parent["id"], Caller.for_session(b))
    assert e.value.error == "conflict"  # holding a child blocks the parent
    t_lib = await mk(svc, HUMAN, title="lib", zone_ids=[lib])
    await svc.claim(CREW, t_lib["id"], Caller.for_session(a))
    t_ui = await mk(svc, HUMAN, title="ui", zone_ids=[ui])
    with pytest.raises(CrewServiceError) as e:
        await svc.claim(CREW, t_ui["id"], Caller.for_session(b))
    assert e.value.error == "conflict"  # overlapping zones conflict
    # shared + shared is compatible
    docs = await seed_zone(db, slug="docs", mode="shared")
    s1 = await mk(svc, HUMAN, title="s1", zone_ids=[docs])
    s2 = await mk(svc, HUMAN, title="s2", zone_ids=[docs])
    async with db.transaction():
        await db.conn.execute("UPDATE crew_tasks SET claim_mode = 'shared' WHERE id IN (?, ?)", (s1["id"], s2["id"]))
    await svc.claim(CREW, s1["id"], Caller.for_session(a))
    await svc.claim(CREW, s2["id"], Caller.for_session(b))


@pytest.mark.parametrize(
    ("zone_kw", "error", "status"),
    [
        ({"builtin": True, "slug": "crew-policy"}, "crew_policy", 423),
        ({"frozen_by": "u_owner"}, "frozen", 423),
        ({"protected": True}, "protected", 423),
        ({"reserve_for": "codex"}, "reserved_for_agent", 423),
    ],
)
async def test_policy_zones_cannot_be_claimed(env, zone_kw, error, status):
    db, svc = env
    s = await seed_session(db)
    z = await seed_zone(db, **zone_kw)
    t = await mk(svc, HUMAN, zone_ids=[z])
    with pytest.raises(CrewServiceError) as e:
        await svc.start(CREW, t["id"], Caller.for_session(s))
    assert err(e) == (status, error)


async def test_reserve_for_needs_a_key_verified_agent(env):
    db, svc = env
    fake = await seed_session(db, callsign="codex-1", agent_id="codex", verified=False)
    real = await seed_session(db, callsign="codex-2", agent_id="codex", verified=True)
    z = await seed_zone(db, reserve_for="codex")
    t = await mk(svc, HUMAN, zone_ids=[z])
    with pytest.raises(CrewServiceError) as e:
        await svc.start(CREW, t["id"], Caller.for_session(fake))
    assert e.value.error == "reserved_for_agent"
    await svc.start(CREW, t["id"], Caller.for_session(real))


async def test_wip_and_claim_caps(env):
    db, svc = env
    s = await seed_session(db)
    zones = [await seed_zone(db, slug=f"z{i}") for i in range(5)]
    t1 = await mk(svc, HUMAN, zone_ids=zones[:1])
    t2 = await mk(svc, HUMAN, zone_ids=zones[1:2])
    await svc.start(CREW, t1["id"], Caller.for_session(s))
    with pytest.raises(CrewServiceError) as e:
        await svc.claim(CREW, t2["id"], Caller.for_session(s))
    assert err(e) == (409, "wip_limit")
    await set_settings(db, CREW, wip_per_session=5)
    big = await mk(svc, HUMAN, zone_ids=zones[1:4])  # 1 held + 3 new > 3 per session
    with pytest.raises(CrewServiceError) as e:
        await svc.claim(CREW, big["id"], Caller.for_session(s))
    assert err(e) == (409, "claim_cap")


async def test_verified_agents_setting(env):
    db, svc = env
    s = await seed_session(db, verified=False)
    await set_settings(db, CREW, require_verified_agents_for_claims=True)
    t = await mk(svc, HUMAN, zone_ids=[await seed_zone(db)])
    with pytest.raises(CrewServiceError) as e:
        await svc.claim(CREW, t["id"], Caller.for_session(s))
    assert err(e) == (403, "unverified_agent")


async def test_acceptance_lock_patch_rules_and_if_match(env):
    db, svc = env
    s = await seed_session(db)
    t = await mk(svc, HUMAN, acceptance=[crit()])
    # before the lock an agent may edit criteria
    res = await svc.patch(CREW, t["id"], Caller.for_session(s), {"acceptance": [crit(match="npm test")]}, if_match=t["version"])
    assert res.task["acceptance"][0]["match"] == "npm test"
    started = await svc.start(CREW, t["id"], Caller.for_session(s))
    v = started.task["version"]
    with pytest.raises(CrewServiceError) as e:
        await svc.patch(CREW, t["id"], Caller.for_session(s), {"acceptance": [crit()]}, if_match=v)
    assert err(e) == (403, "human_only")
    with pytest.raises(CrewServiceError) as e:
        await svc.patch(CREW, t["id"], HUMAN, {"title": "x"}, if_match=v - 1)
    assert err(e) == (412, "version_mismatch")
    with pytest.raises(CrewServiceError) as e:
        await svc.patch(CREW, t["id"], HUMAN, {"status": "done"}, if_match=v)
    assert err(e) == (409, "report_required")
    with pytest.raises(CrewServiceError) as e:
        await svc.patch(CREW, t["id"], HUMAN, {"status": "in_progress"}, if_match=v)
    assert err(e) == (422, "invalid_transition")
    with pytest.raises(CrewServiceError) as e:
        await svc.patch(CREW, t["id"], HUMAN, {"zone_ids": []}, if_match=v)
    assert err(e) == (409, "task_active")
    res = await svc.patch(CREW, t["id"], HUMAN, {"acceptance": [crit(match="npm test -- pos --ci")]}, if_match=v)
    assert res.extra["acceptance_changed_after_lock"]
    last = (await events_of(db))[-1]
    assert last["type"] == "task.acceptance_changed" and last["moment"] is True and last["actor"]["kind"] == "human"
    no_change = await svc.patch(CREW, t["id"], HUMAN, {"title": res.task["title"]}, if_match=res.task["version"])
    assert no_change.seq is None


async def test_block_unblock_and_owner_only(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1")
    b = await seed_session(db, callsign="cc-2")
    t = await mk(svc, HUMAN)
    await svc.start(CREW, t["id"], Caller.for_session(a))
    with pytest.raises(CrewServiceError) as e:
        await svc.block(CREW, t["id"], Caller.for_session(b), "waiting")
    assert err(e) == (403, "not_task_owner")
    res = await svc.block(CREW, t["id"], Caller.for_session(a), "waiting on API keys")
    assert res.task["status"] == "blocked" and res.task["blocked_reason"] == "waiting on API keys"
    res = await svc.unblock(CREW, t["id"], Caller.for_session(a))
    assert res.task["status"] == "in_progress" and res.task["blocked_reason"] is None
    items = await db.fetchall("SELECT kind, state FROM crew_inbox_items WHERE kind = 'task_blocked'")
    assert items == [{"kind": "task_blocked", "state": "resolved"}]


# ---------------------------------------------------------------------------
# Release, stall, adopt, assign, recover
# ---------------------------------------------------------------------------


async def test_release_before_start_returns_to_ready(env):
    db, svc = env
    s = await seed_session(db)
    z = await seed_zone(db)
    t = await mk(svc, HUMAN, zone_ids=[z])
    await svc.claim(CREW, t["id"], Caller.for_session(s))
    res = await svc.release(CREW, t["id"], Caller.for_session(s))
    assert res.task["status"] == "ready" and res.task["owner_session_id"] is None
    assert (await db.fetchone("SELECT state FROM crew_claims"))["state"] == "released"
    assert await check_report_invariant(db.conn) == []


async def test_release_after_start_stalls_with_a_baton_and_a_current_report(env):
    db, svc = env
    s = await seed_session(db)
    z = await seed_zone(db)
    t = await mk(svc, HUMAN, zone_ids=[z])
    await svc.start(CREW, t["id"], Caller.for_session(s))
    res = await svc.release(CREW, t["id"], Caller.for_session(s))
    assert res.task["status"] == "stalled" and res.task["status_before_stall"] == "in_progress"
    claim = await db.fetchone("SELECT * FROM crew_claims")
    assert (claim["state"], claim["reserve_reason"], claim["reserved_for"]) == ("reserved", "baton", s["id"])
    report = await db.fetchone("SELECT * FROM crew_reports WHERE is_current = 1")
    assert report["kind"] == "stalled" and report["facts_source"] == "server-inferred"
    assert res.task["current_report_id"] == report["id"]
    item = await db.fetchone("SELECT * FROM crew_inbox_items WHERE kind = 'baton_available'")
    assert item["audience"] == "project" and item["state"] == "open"
    assert await check_report_invariant(db.conn) == []
    valid_envelopes(await events_of(db))


async def test_adopt_requires_an_offer_and_moves_claims_with_epoch(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1", checkout_fp="fp-a", worktree_id="wt-a")
    c = await seed_session(db, callsign="cc-3", checkout_fp="fp-c", worktree_id="wt-c")
    z = await seed_zone(db)
    t = await mk(svc, HUMAN, zone_ids=[z])
    await svc.start(CREW, t["id"], Caller.for_session(a))
    await svc.release(CREW, t["id"], Caller.for_session(a))
    with pytest.raises(CrewServiceError) as e:
        await svc.adopt(CREW, t["id"], Caller.for_session(c))
    assert err(e) == (403, "adopt_not_offered")
    claim = await db.fetchone("SELECT * FROM crew_claims")
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO crew_baton_offers (id, crew_id, claim_id, task_id, to_session, via, created_at)"
            " VALUES ('off_1', ?, ?, ?, ?, 'brief', '2026-09-25T00:00:00.000Z')",
            (CREW, claim["id"], t["id"], c["id"]),
        )
    before = await db.fetchone("SELECT last_seq FROM crews")
    res = await svc.adopt(CREW, t["id"], Caller.for_session(c))
    assert res.task["status"] == "claimed" and res.task["owner_session_id"] == c["id"]
    assert res.extra["cross_checkout"] is True
    claim = await db.fetchone("SELECT * FROM crew_claims")
    assert (claim["holder_session_id"], claim["state"], claim["epoch"], claim["source"]) == (c["id"], "active", 2, "adopt")
    baton = await db.fetchone("SELECT * FROM crew_batons")
    assert (baton["from_session"], baton["to_session"], baton["kind"], baton["offer_id"]) == (a["id"], c["id"], "adopt", "off_1")
    offer = await db.fetchone("SELECT used_at FROM crew_baton_offers")
    assert offer["used_at"]
    evs = await events_of(db, after=before["last_seq"])
    kinds = [e["type"] for e in evs]
    assert "claim.adopted" in kinds and "baton.passed" in kinds
    adopted = next(e for e in evs if e["type"] == "claim.adopted")
    assert adopted["moment"] is True  # cross-checkout adopt is a moment
    assert next(e for e in evs if e["type"] == "baton.passed")["moment"] is True
    valid_envelopes(evs)
    # the stalled report stays current while claimed (at most one current)
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM crew_reports WHERE is_current = 1"))["n"] == 1


async def test_same_checkout_and_first_write_adoption(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1", checkout_fp="fp", worktree_id="wt")
    same = await seed_session(db, callsign="cc-2", checkout_fp="fp", worktree_id="wt")
    other = await seed_session(db, callsign="cc-3", checkout_fp="fp2", worktree_id="wt2")
    z1, z2 = await seed_zone(db, slug="pos"), await seed_zone(db, slug="rep")
    t1 = await mk(svc, HUMAN, zone_ids=[z1])
    await svc.start(CREW, t1["id"], Caller.for_session(a))
    await svc.release(CREW, t1["id"], Caller.for_session(a))
    res = await svc.adopt(CREW, t1["id"], Caller.for_session(same))
    assert res.extra["baton"]["kind"] == "same_checkout" and res.extra["cross_checkout"] is False
    t2 = await mk(svc, HUMAN, zone_ids=[z2])
    await set_settings(db, CREW, wip_per_session=3)
    await svc.start(CREW, t2["id"], Caller.for_session(a))
    await svc.release(CREW, t2["id"], Caller.for_session(a))
    await set_settings(db, CREW, adopt_on_first_write=True)
    res = await svc.adopt(CREW, t2["id"], Caller.for_session(other))
    assert res.extra["baton"]["kind"] == "first_write"


async def test_assign_is_human_only_and_transfers_claims(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1")
    b = await seed_session(db, callsign="cc-2")
    z = await seed_zone(db)
    t = await mk(svc, HUMAN, zone_ids=[z])
    await svc.start(CREW, t["id"], Caller.for_session(a))
    with pytest.raises(CrewServiceError) as e:
        await svc.assign(CREW, t["id"], Caller.for_session(b), b["id"])
    assert err(e) == (403, "human_only")
    with pytest.raises(CrewServiceError) as e:
        await svc.assign(CREW, t["id"], HUMAN, "cs_nope")
    assert err(e) == (422, "cross_crew_reference")
    res = await svc.assign(CREW, t["id"], HUMAN, b["id"])
    assert res.task["owner_session_id"] == b["id"] and res.task["status"] == "in_progress"
    claim = await db.fetchone("SELECT * FROM crew_claims")
    assert (claim["holder_session_id"], claim["epoch"], claim["source"]) == (b["id"], 2, "dashboard")
    evs = await events_of(db)
    assert {"claim.transferred", "baton.passed", "task.assigned"} <= {e["type"] for e in evs}
    assigned = next(e for e in evs if e["type"] == "task.assigned")
    assert assigned["moment"] is True and assigned["payload"]["to_session"] == b["id"]
    valid_envelopes(evs)
    # assign overrides pending dependencies
    dep = await mk(svc, HUMAN, title="dep")
    blocked = await mk(svc, HUMAN, title="blocked", depends_on=[dep["id"]])
    await set_settings(db, CREW, wip_per_session=5)
    res = await svc.assign(CREW, blocked["id"], HUMAN, a["id"])
    assert res.task["status"] == "claimed"


async def test_stall_and_recover_session_tasks(env):
    db, svc = env
    s = await seed_session(db)
    z = await seed_zone(db)
    t = await mk(svc, HUMAN, zone_ids=[z])
    await svc.start(CREW, t["id"], Caller.for_session(s))
    await svc.block(CREW, t["id"], Caller.for_session(s), "api down")
    log = svc.events
    async with log.transaction() as tx:
        stalled = await svc.stall_session_tasks(
            tx, CREW, s["id"], reason="quota:billing_error", reserve_reason="quota", baton_ref="refs/remembra/baton/T-1/1"
        )
    assert stalled == [t["id"]]
    task = await svc.get(CREW, t["id"])
    assert task["status"] == "stalled" and task["status_before_stall"] == "blocked"
    claim = await db.fetchone("SELECT * FROM crew_claims")
    assert (claim["state"], claim["reserve_reason"], claim["baton_ref"]) == ("reserved", "quota", "refs/remembra/baton/T-1/1")
    report = await db.fetchone("SELECT * FROM crew_reports WHERE is_current = 1")
    assert report["baton_ref"] == "refs/remembra/baton/T-1/1"
    assert await check_report_invariant(db.conn) == []
    async with log.transaction() as tx:
        out = await svc.recover_session_tasks(tx, CREW, s["id"])
    assert out["tasks_restored"] == [t["id"]] and out["superseded_report_ids"] == [report["id"]]
    assert out["claims_retaken"] == [claim["id"]]
    task = await svc.get(CREW, t["id"])
    assert task["status"] == "blocked" and task["status_before_stall"] is None and task["current_report_id"] is None
    claim = await db.fetchone("SELECT * FROM crew_claims")
    assert (claim["state"], claim["epoch"]) == ("active", 2)
    rep = await db.fetchone("SELECT is_current, superseded_reason FROM crew_reports")
    assert (rep["is_current"], rep["superseded_reason"]) == (0, "recovered")
    baton_item = await db.fetchone("SELECT state FROM crew_inbox_items WHERE kind = 'baton_available'")
    assert baton_item["state"] == "resolved"
    valid_envelopes(await events_of(db))


async def test_recovery_property_random_lost_recovered_sequences(env):
    """§13.1: random lost → recovered sequences keep the invariant, restore status and resolve inbox items."""
    db, svc = env
    rng = random.Random(20260925)
    await set_settings(db, CREW, wip_per_session=5, max_exclusive_claims_per_session=10, max_exclusive_claims_per_agent=30)
    sessions = [await seed_session(db, callsign=f"cc-{i + 1}") for i in range(3)]
    tasks = []
    for i in range(6):
        z = await seed_zone(db, slug=f"z{i}")
        t = await mk(svc, HUMAN, title=f"t{i}", zone_ids=[z])
        owner = sessions[i % 3]
        await svc.start(CREW, t["id"], Caller.for_session(owner))
        if rng.random() < 0.3:
            await svc.block(CREW, t["id"], Caller.for_session(owner), "blocked")
        tasks.append((t["id"], owner))
    expected = {tid: (await svc.get(CREW, tid))["status"] for tid, _ in tasks}
    for _ in range(40):
        s = rng.choice(sessions)
        async with svc.events.transaction() as tx:
            if rng.random() < 0.5:
                await svc.stall_session_tasks(tx, CREW, s["id"], reason="lost", reserve_reason="lost")
            else:
                await svc.recover_session_tasks(tx, CREW, s["id"])
        assert await check_report_invariant(db.conn) == []
        for tid, owner in tasks:
            task = await svc.get(CREW, tid)
            assert task["status"] in ("stalled", expected[tid]) and task["owner_session_id"] == owner["id"]
            live_items = await db.fetchall(
                "SELECT id FROM crew_inbox_items WHERE ref_id = ? AND kind = 'baton_available' AND state = 'open'", (tid,)
            )
            assert (task["status"] == "stalled") == bool(live_items)
    for s in sessions:
        async with svc.events.transaction() as tx:
            await svc.recover_session_tasks(tx, CREW, s["id"])
    for tid, _ in tasks:
        assert (await svc.get(CREW, tid))["status"] == expected[tid]
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM crew_reports WHERE is_current = 1"))["n"] == 0
    valid_envelopes(await events_of(db))


async def test_cancel_keeps_the_invariant_and_reopen(env):
    db, svc = env
    s = await seed_session(db)
    t = await mk(svc, HUMAN, zone_ids=[await seed_zone(db)])
    started = await svc.start(CREW, t["id"], Caller.for_session(s))
    other = await seed_session(db, callsign="cc-9")
    with pytest.raises(CrewServiceError) as e:
        await svc.cancel(CREW, t["id"], Caller.for_session(other), if_match=started.task["version"])
    assert err(e) == (403, "not_task_owner")
    res = await svc.patch(CREW, t["id"], HUMAN, {"status": "cancelled"}, if_match=started.task["version"])
    assert res.task["status"] == "cancelled"
    assert (await db.fetchone("SELECT state FROM crew_claims"))["state"] == "released"
    assert await check_report_invariant(db.conn) == []
    with pytest.raises(CrewServiceError) as e:
        await svc.reopen(CREW, t["id"], HUMAN)
    assert e.value.status == 409
    # an unstarted task cancels without a report
    t2 = await mk(svc, HUMAN)
    await svc.cancel(CREW, t2["id"], HUMAN, if_match=t2["version"])
    assert await db.fetchall("SELECT id FROM crew_reports WHERE task_id = ?", (t2["id"],)) == []


async def test_events_for_every_transition_validate_against_the_contract(env):
    db, svc = env
    evs = await events_of(db)
    assert evs == []  # sanity: the fixture starts clean
    s = await seed_session(db)
    t = await mk(svc, HUMAN, acceptance=[crit()])
    await svc.claim(CREW, t["id"], Caller.for_session(s))
    await svc.start(CREW, t["id"], Caller.for_session(s))
    evs = await events_of(db)
    valid_envelopes(evs)
    payloads = [e["payload"] for e in evs if e["type"].startswith("task.")]
    assert all("task" in p for p in payloads)
    assert json.dumps(evs).count('"acceptance_locked": true') >= 1
