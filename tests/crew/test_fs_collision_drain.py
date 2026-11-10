"""F13: collisions drain when the overlap is over (fault survey 2026-11-10).

A collision row used to stay open for ever: nothing closed it when the other session ended or
went silent, when the writer adopted the zone it had breached, when the holder released the
zone a human commit touched, or when the commit turned out to be a crew session's own. Every
test drives the real ``crew.db``, the real event log and the real collision code.
"""

from __future__ import annotations

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

DIRTY = {"state": "dirty", "attribution": "certain"}


@pytest.fixture
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    ops, _ = make_ops(db)
    a, _ = await seed_session(db, "cs_a", callsign="codex-1", agent_id="codex")
    b, _ = await seed_session(db, "cs_b", callsign="codex-8", agent_id="codex")
    await Z.upload_zones_file(ops, CREW, Z.Principal.for_session(a), yaml_text=ZONES_YML, sha="s1", branch="main")
    try:
        yield db, ops, a, b
    finally:
        await assert_chain_ok(db)
        await db.close()


async def _fp(ops, session, *fps):
    async with ops.log.transaction() as tx:
        return await CO.record_footprints(ops, tx, CREW, session, list(fps))


async def _open(db, kind=None):
    rows = await db.fetchall("SELECT * FROM crew_collisions WHERE crew_id = ? AND state IN ('open','acknowledged')", (CREW,))
    return [r for r in rows if kind is None or r["kind"] == kind]


async def _open_notices(db):
    return [i for i in await inbox(db, kind="collision_notice") if i["state"] not in ("resolved", "dismissed")]


async def _set(db, sql, params=()):
    async with db.transaction():
        await db.conn.execute(sql, params)


def _ago(**kw) -> str:
    return now_iso(C.utcnow() - timedelta(**kw))


async def _end(db, session_id, **ago):
    await _set(db, "UPDATE crew_sessions SET state = 'ended', ended_at = ? WHERE id = ?", (_ago(**ago), session_id))


async def _collide(ops, a, b, path="src/lib/money.ts"):
    await _fp(ops, a, {"path": path, **DIRTY})
    opened = await _fp(ops, b, {"path": path, **DIRTY})
    assert [r["kind"] for r in opened] == ["same_file"]
    return opened[0]


# ---------------------------------------------------------------------------
# The other session is gone
# ---------------------------------------------------------------------------


async def test_same_file_drains_when_the_other_session_ended_a_day_ago(env):
    db, ops, a, b = env
    await _collide(ops, a, b)
    await _end(db, "cs_b", hours=25)
    # an unrelated write by the survivor is enough: the row is about this session, not about that path
    assert await _fp(ops, a, {"path": "src/lib/other.ts", **DIRTY}) == []
    assert await _open(db) == []
    assert await _open_notices(db) == []
    closed = await events(db, type_prefix="collision.resolved")
    assert [e["payload"]["collision"]["resolution"] for e in closed] == ["auto"]


async def test_a_session_that_ended_an_hour_ago_still_counts(env):
    db, ops, a, b = env
    await _collide(ops, a, b)
    await _end(db, "cs_b", hours=1)  # its uncommitted work may still be on disk
    await _fp(ops, a, {"path": "src/lib/other.ts", **DIRTY})
    assert [r["kind"] for r in await _open(db)] == ["same_file"]


async def test_a_lost_session_silent_for_days_neither_opens_nor_keeps_collisions(env):
    db, ops, a, b = env
    await _collide(ops, a, b)
    # lost, never ended (it holds a reserved baton, so the reaper keeps the lane): silent for nine days
    await _set(
        db,
        "UPDATE crew_sessions SET state = 'lost', last_seen_at = ?, last_heartbeat_at = NULL WHERE id = 'cs_b'",
        (_ago(days=9),),
    )
    c, _ = await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex")
    assert await _fp(ops, c, {"path": "src/lib/money.ts", **DIRTY}) != []  # a (live) and c do overlap
    assert all("cs_b" not in (r["session_a"], r["session_b"]) for r in await _open(db))
    # a session lost a moment ago is still a party
    await _set(db, "UPDATE crew_sessions SET state = 'lost', last_seen_at = ? WHERE id = 'cs_a'", (_ago(minutes=5),))
    d, _ = await seed_session(db, "cs_d", callsign="codex-10", agent_id="codex")
    opened = await _fp(ops, d, {"path": "src/lib/money.ts", **DIRTY})
    assert {r["session_b"] for r in opened} == {"cs_a", "cs_c"}


async def test_lost_session_with_no_timestamps_does_not_count(env):
    db, ops, a, b = env
    await _fp(ops, a, {"path": "src/lib/money.ts", **DIRTY})
    await _set(
        db,
        "UPDATE crew_sessions SET state = 'lost', last_seen_at = NULL, last_heartbeat_at = NULL, joined_at = ? WHERE id = 'cs_a'",
        (_ago(days=3),),
    )
    assert await _fp(ops, b, {"path": "src/lib/money.ts", **DIRTY}) == []


async def test_an_empty_checkpoint_drains_the_callers_rows(env):
    db, ops, a, b = env
    await _collide(ops, a, b)
    await _end(db, "cs_b", hours=30)
    svc = CheckpointService(CrewEventLog(db, None))
    res = await svc.ingest(CREW, Caller.for_session(a), {"session_id": "cs_a", "trigger": "turn", "facts": {"files_changed": []}})
    assert res.created
    assert await _open(db) == [] and await _open_notices(db) == []
    # a replay (unchanged checkpoint) drains too
    await _set(db, "UPDATE crew_sessions SET state = 'active', ended_at = NULL WHERE id = 'cs_b'")
    await _collide(ops, a, b, "src/lib/tax.ts")
    await _end(db, "cs_b", hours=30)
    empty = {"session_id": "cs_a", "trigger": "turn", "facts": {"files_changed": []}}
    again = await svc.ingest(CREW, Caller.for_session(a), empty)
    assert not again.created
    assert await _open(db) == []


# ---------------------------------------------------------------------------
# Declared and committed files have a shelf life
# ---------------------------------------------------------------------------


async def test_a_file_committed_more_than_a_day_ago_is_no_longer_an_overlap(env):
    db, ops, a, b = env
    await _collide(ops, a, b)
    await _fp(ops, b, {"path": "src/lib/money.ts", "state": "committed", "attribution": "certain", "last_commit": "abc1234"})
    assert len(await _open(db)) == 1  # committed a moment ago: other checkouts have not merged it yet
    await _set(db, "UPDATE crew_footprints SET last_at = ? WHERE session_id = 'cs_b'", (_ago(hours=25),))
    await _fp(ops, a, {"path": "src/lib/other.ts", **DIRTY})
    assert await _open(db) == []
    # and it opens nothing new against a later writer
    c, _ = await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex")
    opened = await _fp(ops, c, {"path": "src/lib/money.ts", **DIRTY})
    assert {r["session_b"] for r in opened} == {"cs_a"}


async def test_an_mcp_declared_file_expires_but_a_tracked_dirty_file_does_not(env):
    db, ops, a, b = env
    await _set(db, "UPDATE crew_sessions SET client_kind = 'mcp', worktree_id = NULL WHERE id = 'cs_b'")
    await _collide(ops, a, b)
    await _set(db, "UPDATE crew_footprints SET last_at = ?", (_ago(hours=30),))
    c, _ = await seed_session(db, "cs_c", callsign="cc-3")
    opened = await _fp(ops, c, {"path": "src/lib/money.ts", **DIRTY})
    # a is a hook session (crewd tracks its dirty file): still an overlap. b only declared it 30 h ago.
    assert {r["session_b"] for r in opened} == {"cs_a"}
    left = await _open(db)
    assert all("cs_b" not in (r["session_a"], r["session_b"]) for r in left) and len(left) == 1


# ---------------------------------------------------------------------------
# Claims: adoption, release
# ---------------------------------------------------------------------------


async def test_breach_resolves_when_the_writer_adopts_the_zone(env):
    db, ops, a, b = env
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    opened = await _fp(ops, b, {"path": "src/app/pos/cart.ts", **DIRTY})
    assert [r["kind"] for r in opened] == ["exclusive_breach"]
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "quota")
        await C.record_baton_offer(ops, tx, CREW, got.claim["id"], "cs_b")
    await C.adopt(ops, got.claim, Z.Principal.for_session(b))
    # the writer now holds the very claim it breached; its next write under that claim closes the row
    assert await _fp(ops, b, {"path": "src/app/pos/tender.ts", **DIRTY}) == []
    assert await _open(db, "exclusive_breach") == []
    assert await _open_notices(db) == []


async def test_breach_stays_while_the_holder_keeps_the_zone(env):
    db, ops, a, b = env
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    await _fp(ops, b, {"path": "src/app/pos/cart.ts", **DIRTY})
    await _fp(ops, b, {"path": "src/lib/elsewhere.ts", **DIRTY})
    await _set(db, "UPDATE crew_footprints SET last_at = ?", (_ago(days=3),))  # a breach does not age out
    async with ops.log.transaction() as tx:
        assert await CO.auto_resolve(ops, tx, CREW, session_id="cs_b") == []
    assert [r["kind"] for r in await _open(db)] == ["exclusive_breach"]


async def test_unattributed_change_is_one_notice_per_commit_and_closes_with_the_claim(env):
    db, ops, a, b = env
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    files = ["src/app/pos/a.ts", "src/app/pos/b.ts", "src/app/pos/c.ts", "src/app/pos/d.ts", "README.md"]
    async with ops.log.transaction() as tx:
        un = await CO.record_unattributed_change(ops, tx, CREW, files, commit="deadbeefcafe")
        again = await CO.record_unattributed_change(ops, tx, CREW, files, commit="deadbeefcafe")
    assert [r["kind"] for r in un] == ["unattributed_change"] and again == []
    assert un[0]["subject"] == "src/app/pos/a.ts" and un[0]["session_b"] == "cs_a"
    ev = loads(un[0]["evidence"])
    assert ev["commit"] == "deadbeefcafe" and ev["files"] == files[:4] and ev["file_count"] == 4
    assert len([i for i in await _open_notices(db) if i["recipient"] == "cs_a"]) == 1
    await C.release_claim(ops, got.claim, Z.Principal.for_session(a))
    assert await _open(db, "unattributed_change") == []
    assert await _open_notices(db) == []


async def test_a_commit_a_crew_session_reported_is_not_a_humans(env):
    db, ops, a, b = env
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    sha = "0123456789abcdef0123456789abcdef01234567"
    await _fp(ops, a, {"path": "src/app/pos/a.ts", "state": "committed", "attribution": "certain", "last_commit": sha})
    async with ops.log.transaction() as tx:
        assert await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts"], commit=sha) == []
        # no sha, an unusable sha and an unknown sha are still reported
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/b.ts"], commit=None)) == 1
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/c.ts"], commit="%")) == 1
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/d.ts"], commit="f" * 40)) == 1


async def test_a_checkpoint_that_names_the_commit_closes_the_notice(env):
    db, ops, a, b = env
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    sha = "89abcdef0123456789abcdef0123456789abcdef"
    async with ops.log.transaction() as tx:
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts"], commit=sha)) == 1
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/z.ts"], commit="7" * 40)) == 1
    svc = CheckpointService(CrewEventLog(db, None))
    # the agent made that commit itself through its shell and now says so (short sha, as agents write it)
    facts = {"files_changed": [], "commits": [{"sha": sha[:9]}, {"sha": "not-a-sha"}, {}]}
    await svc.ingest(CREW, Caller.for_session(b), {"session_id": "cs_b", "trigger": "commit", "facts": facts})
    left = await _open(db, "unattributed_change")
    assert [loads(r["evidence"])["commit"] for r in left] == ["7" * 40]
    # and once declared, the same commit reported again by another checkout opens nothing
    async with ops.log.transaction() as tx:
        assert await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts"], commit=sha) == []


# ---------------------------------------------------------------------------
# The crew-wide sweep (reaper) and the hook snapshot
# ---------------------------------------------------------------------------


async def test_sweep_drains_rows_whose_sessions_are_both_gone_and_walks_past_live_ones(env, monkeypatch):
    db, ops, a, b = env
    monkeypatch.setattr(CO, "SWEEP_CREW_ROWS", 2)
    c, _ = await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex")
    d, _ = await seed_session(db, "cs_d", callsign="codex-10", agent_id="codex")
    for n in range(3):  # three live rows first (oldest), then three that will be dead
        await _collide(ops, a, b, f"src/lib/live{n}.ts")
    for n in range(3):
        await _collide(ops, c, d, f"src/lib/dead{n}.ts")
    await _end(db, "cs_c", days=3)
    await _end(db, "cs_d", days=2)
    cursors: dict[str, str] = {}
    closed = 0
    for _ in range(4):  # 2 rows a pass: the window must move on past the live rows
        closed += await CO.sweep(ops.log, cursors)
    assert closed == 3
    left = await _open(db)
    assert sorted(r["subject"] for r in left) == [f"src/lib/live{n}.ts" for n in range(3)]
    assert await CO.sweep(ops.log, cursors) == 0  # idempotent; the live rows stay


async def test_reaper_sweep_runs_the_collision_drain(tmp_path):
    from remembra.crew.reaper import CrewReaper
    from tests.crew.sessions_support import make_env

    senv = await make_env(tmp_path, name="reaper.db")
    try:
        ops = Z.CrewOps(senv.svc.log)
        crew = await seed_crew(senv.db)
        a, _ = await seed_session(senv.db, "cs_a", crew_id=crew, callsign="codex-1")
        b, _ = await seed_session(senv.db, "cs_b", crew_id=crew, callsign="codex-8")
        async with ops.log.transaction() as tx:
            await CO.record_footprints(ops, tx, crew, a, [{"path": "src/x.ts", **DIRTY}])
            assert len(await CO.record_footprints(ops, tx, crew, b, [{"path": "src/x.ts", **DIRTY}])) == 1
        old = now_iso(C.utcnow() - timedelta(days=4))
        async with senv.db.transaction():
            await senv.db.conn.execute("UPDATE crew_sessions SET state = 'ended', ended_at = ?", (old,))
        report = await CrewReaper(senv.svc).sweep()
        assert report.errors == [] and report.collisions_resolved == 1
        rows = await senv.db.fetchall("SELECT state, resolution FROM crew_collisions")
        assert rows == [{"state": "resolved", "resolution": "auto"}]
    finally:
        await senv.db.close()


async def test_hook_snapshot_keeps_the_newest_collisions_when_over_the_cap(env, monkeypatch):
    db, ops, a, b = env
    from remembra.crew import core as core_mod

    for n in range(4):
        await _collide(ops, a, b, f"src/lib/old{n}.ts")
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    newest = (await _fp(ops, b, {"path": "src/app/pos/cart.ts", **DIRTY}))[0]
    assert newest["kind"] == "exclusive_breach"
    monkeypatch.setitem(core_mod.SNAPSHOT_LIMITS, "collisions", 3)
    snap = await CrewCore(db).snapshot(CREW)
    got = snap["collisions"]
    assert len(got) == 3 and got[-1]["id"] == newest["id"], [c["subject"] for c in got]
    assert [c["subject"] for c in got[:2]] == ["src/lib/old2.ts", "src/lib/old3.ts"]  # still oldest first


# ---------------------------------------------------------------------------
# Review round: a zone handed to a third session, arrival order of commit reports, lost sessions
# ---------------------------------------------------------------------------


async def _hand_over(db, ops, claim, to_id):
    """cs_a's claim becomes a reserved baton, is offered to ``to_id`` and adopted (the claim id is kept)."""
    target = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (to_id,))
    async with ops.log.transaction() as tx:
        await C.reserve_session_claims(ops, tx, CREW, "cs_a", "quota")
        await C.record_baton_offer(ops, tx, CREW, claim["id"], to_id)
    await C.adopt(ops, claim, Z.Principal.for_session(target))


async def _empty_checkpoint(db, session):
    svc = CheckpointService(CrewEventLog(db, None))
    await svc.ingest(CREW, Caller.for_session(session), {"session_id": session["id"], "trigger": "turn", "facts": {}})


@pytest.mark.parametrize("via", ["checkpoint", "sweep"])
async def test_breach_follows_the_zone_to_a_third_holder_while_the_write_stands(env, via):
    db, ops, a, b = env
    await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex")
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    opened = await _fp(ops, b, {"path": "src/app/pos/cart.ts", **DIRTY})
    assert [(r["kind"], r["session_b"]) for r in opened] == [("exclusive_breach", "cs_a")]
    await _hand_over(db, ops, got.claim, "cs_c")
    holder = await db.fetchone("SELECT state, holder_session_id FROM crew_claims WHERE id = ?", (got.claim["id"],))
    assert (holder["state"], holder["holder_session_id"]) == ("active", "cs_c")

    if via == "checkpoint":
        await _empty_checkpoint(db, b)  # crewd sends no footprint again: the file did not change
    else:
        assert await CO.sweep(ops.log, {}) == 1
    left = await _open(db)
    assert [(r["kind"], r["severity"], r["session_a"], r["session_b"]) for r in left] == [
        ("exclusive_breach", "high", "cs_b", "cs_c")
    ]
    assert left[0]["id"] != opened[0]["id"] and left[0]["claim_id"] == got.claim["id"]
    assert left[0]["subject"] == "src/app/pos/cart.ts" and left[0]["attribution"] == "certain"
    assert loads(left[0]["evidence"])["moved_from"] == "cs_a"
    old = await db.fetchone("SELECT state, resolution FROM crew_collisions WHERE id = ?", (opened[0]["id"],))
    assert (old["state"], old["resolution"]) == ("resolved", "auto")
    # the new holder and the writer are told; the old holder's notice is gone; Needs-you has the new row
    assert sorted(i["recipient"] for i in await _open_notices(db)) == ["cs_b", "cs_c"]
    escalated = [i for i in await inbox(db, kind="collision_escalated") if i["state"] not in ("resolved", "dismissed")]
    assert [i["ref_id"] for i in escalated] == [left[0]["id"]]
    detected = await events(db, type_prefix="collision.detected")
    assert len(detected) == 2 and detected[-1]["moment"] and detected[-1]["payload"]["collision"]["session_b"] == "cs_c"
    # the Stop gate's snapshot carries the breach
    snap = await CrewCore(db).snapshot(CREW)
    assert [(c["kind"], c["session_a"], c["session_b"]) for c in snap["collisions"]] == [("exclusive_breach", "cs_b", "cs_c")]

    # stable: further checkpoints and sweeps neither close nor duplicate it
    await _empty_checkpoint(db, b)
    assert await CO.sweep(ops.log, {}) == 0
    assert [r["id"] for r in await _open(db)] == [left[0]["id"]]
    # and it ends like any breach: the write lands
    async with ops.log.transaction() as tx:
        await CO.set_footprint_state(ops, tx, CREW, "cs_b", ["src/app/pos/cart.ts"], "landed")
    assert await _open(db) == [] and await _open_notices(db) == []


async def test_breach_does_not_follow_the_zone_once_the_write_landed_or_the_writer_is_gone(env):
    db, ops, a, b = env
    await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex")
    got = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    await _fp(ops, b, {"path": "src/app/pos/cart.ts", **DIRTY}, {"path": "src/app/pos/tender.ts", **DIRTY})
    assert len(await _open(db, "exclusive_breach")) == 2
    await _hand_over(db, ops, got.claim, "cs_c")
    await _set(db, "UPDATE crew_footprints SET state = 'landed' WHERE session_id = 'cs_b' AND path = 'src/app/pos/cart.ts'")
    await _empty_checkpoint(db, b)
    assert [(r["subject"], r["session_b"]) for r in await _open(db)] == [("src/app/pos/tender.ts", "cs_c")]
    await _set(db, "UPDATE crew_sessions SET state = 'ended', ended_at = ? WHERE id = 'cs_b'", (_ago(days=2),))
    assert await CO.sweep(ops.log, {}) == 1
    assert await _open(db) == [] and await _open_notices(db) == []


async def test_shared_zone_notice_follows_the_claim_and_a_humans_commit_notice_does_not(env):
    db, ops, a, b = env
    await seed_session(db, "cs_c", callsign="codex-9", agent_id="codex")
    pos = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    async with ops.log.transaction() as tx:
        un = await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts"], commit="deadbeefcafe")
    assert [(r["kind"], r["session_b"]) for r in un] == [("unattributed_change", "cs_a")]
    shared = await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "reports"), mode="shared")
    opened = await _fp(ops, b, {"path": "src/app/reports/daily.ts", **DIRTY})
    assert [(r["kind"], r["session_b"]) for r in opened] == [("same_zone_shared", "cs_a")]
    await _set(db, "UPDATE crew_claims SET holder_session_id = 'cs_c' WHERE id IN (?, ?)", (pos.claim["id"], shared.claim["id"]))
    assert await CO.sweep(ops.log, {}) == 2
    # the notice about a commit was for whoever held the zone then; the shared-zone overlap is with whoever holds it now
    assert [(r["kind"], r["session_a"], r["session_b"]) for r in await _open(db)] == [("same_zone_shared", "cs_b", "cs_c")]


@pytest.mark.parametrize("trigger", ["turn", "commit", "close"])
async def test_a_checkpoint_that_named_the_commit_first_means_no_notice(env, trigger):
    """crewd's Stop checkpoint (trigger ``turn``, ``commits`` = plain shas) can land before its commit scan."""
    db, ops, a, b = env
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    sha = "89abcdef0123456789abcdef0123456789abcdef"
    svc = CheckpointService(CrewEventLog(db, None))
    await svc.ingest(
        CREW, Caller.for_session(a), {"session_id": "cs_a", "trigger": trigger, "facts": {"head": sha, "commits": [sha]}}
    )
    await svc.ingest(  # an MCP-style checkpoint with a short id
        CREW, Caller.for_session(b), {"session_id": "cs_b", "trigger": "turn", "facts": {"commits": [{"sha": "ABCDEF1"}]}}
    )
    async with ops.log.transaction() as tx:
        assert await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts"], commit=sha) == []
        assert await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/b.ts"], commit="abcdef1" + "0" * 33) == []
        # a commit no checkpoint names is still reported, also when a checkpoint merely mentions it elsewhere
        other = "7" * 40
        await svc_note(svc, a, other)
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/c.ts"], commit=other)) == 1
    assert [r["subject"] for r in await _open(db, "unattributed_change")] == ["src/app/pos/c.ts"]


async def svc_note(svc, session, sha):
    """A checkpoint whose facts carry ``sha`` outside ``commits`` (its head): not a report of that commit."""
    await svc.ingest(
        CREW, Caller.for_session(session), {"session_id": session["id"], "trigger": "turn", "facts": {"head": sha, "commits": []}}
    )


async def test_only_the_newest_checkpoints_are_searched_for_a_commit(env, monkeypatch):
    db, ops, a, b = env
    await C.request_claim(ops, CREW, Z.Principal.for_session(a), zone_id=await zone_id(db, "pos"))
    sha = "89abcdef0123456789abcdef0123456789abcdef"
    svc = CheckpointService(CrewEventLog(db, None))
    await svc.ingest(CREW, Caller.for_session(a), {"session_id": "cs_a", "trigger": "turn", "facts": {"commits": [sha]}})
    await _set(db, "UPDATE crew_checkpoints SET created_at = ?", (_ago(days=9),))
    for n in range(3):
        await svc.ingest(CREW, Caller.for_session(b), {"session_id": "cs_b", "trigger": "turn", "facts": {"n": n}})
    monkeypatch.setattr(CO, "COMMIT_LOOKBACK_CHECKPOINTS", 3)
    async with ops.log.transaction() as tx:
        assert len(await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/a.ts"], commit=sha)) == 1
    monkeypatch.setattr(CO, "COMMIT_LOOKBACK_CHECKPOINTS", 4)
    async with ops.log.transaction() as tx:
        assert await CO.record_unattributed_change(ops, tx, CREW, ["src/app/pos/b.ts"], commit=sha) == []


async def test_a_lost_session_counts_from_its_latest_sign_of_life(env):
    db, ops, a, b = env
    await _fp(ops, a, {"path": "src/lib/m.ts", **DIRTY})
    await _fp(ops, b, {"path": "src/lib/m.ts", **DIRTY})
    # a call advanced last_seen_at minutes ago although the last crewd heartbeat is days old
    await _set(
        db,
        "UPDATE crew_sessions SET state = 'lost', last_heartbeat_at = ?, last_seen_at = ? WHERE id = 'cs_b'",
        (_ago(days=3), _ago(minutes=5)),
    )
    assert await CO.sweep(ops.log, {}) == 0
    assert len(await _open(db, "same_file")) == 1
    await _set(db, "UPDATE crew_sessions SET last_seen_at = ?, joined_at = ? WHERE id = 'cs_b'", (_ago(days=2), _ago(days=5)))
    assert await CO.sweep(ops.log, {}) == 1
    assert await _open(db) == []
    # an unreadable timestamp is skipped, not fatal
    await _set(
        db, "UPDATE crew_sessions SET last_heartbeat_at = 'garbage', last_seen_at = ? WHERE id = 'cs_b'", (_ago(minutes=1),)
    )
    fresh = await db.fetchone("SELECT * FROM crew_sessions WHERE id = 'cs_b'")
    assert CO._recent(fresh) is True
