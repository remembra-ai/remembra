"""WP-2 retention and digests (§4.5), on a real crew.db."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from types import SimpleNamespace

from remembra.cloud.plans import PlanTier, get_plan
from remembra.crew import retention
from remembra.crew.events import Actor, CrewEventLog, crew_head, fetch_events, format_ts, verify_crew_chain
from remembra.crew.limits import crew_limits_for_tier
from remembra.crew.retention import (
    ENTERPRISE_POLICY,
    FREE_POLICY,
    PRO_POLICY,
    SELF_HOSTED_POLICY,
    TEAM_POLICY,
    RetentionPolicy,
    policy_for_tier,
    policy_from_retention,
    prune_crew_events,
    run_retention,
    seconds_until_next_run,
    usage_meter_resolver,
)
from tests.crew.crewdb import CREW_A, open_crew_db, seed_crew, seed_session, state_changed

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
async def crewdb(tmp_path):
    db = await open_crew_db(tmp_path)
    await seed_crew(db, CREW_A)
    try:
        yield db
    finally:
        await db.close()


async def _free(_owner):
    return FREE_POLICY


async def _emit(log, days_ago, *, type="session.state_changed", payload=None, actor=None, origin="server", idem=None):
    return await log.emit(
        crew_id=CREW_A,
        type=type,
        actor=actor or Actor.system(),
        payload=payload if payload is not None else state_changed(),
        summary=type,
        origin=origin,
        idem_key=idem,
        now=NOW - timedelta(days=days_ago),
    )


BURST = {"files_touched": ["a.ts"], "command_verbs": ["npm"], "tests": {"pass": 0, "fail": 0}}


async def _seed_timeline(db):
    log = CrewEventLog(db)
    session = await seed_session(db, CREW_A, "cs_sess0001")
    await _emit(log, 30)  # raw, 30 d: pruned (free keeps 14 d)
    await _emit(log, 30, type="host.unreachable", payload={"host_id": "hst_1", "silent_s": 1, "session_ids": []})  # moment
    await _emit(log, 30, actor=Actor.human("owner-1"))  # human action
    await _emit(log, 20)  # pruned
    await _emit(log, 10, type="activity.burst", payload=BURST, actor=session, origin="client", idem="c:burst0000001")  # 10 d > 7
    await _emit(log, 10)  # raw, 10 d: kept
    await _emit(log, 1, type="activity.burst", payload=BURST, actor=session, origin="client", idem="c:burst0000002")  # kept
    return log


async def test_prunes_by_plan_windows_keeps_moments_and_humans_and_digests_first(crewdb):
    await _seed_timeline(crewdb)
    report = await run_retention(crewdb, resolve_policy=_free, now=NOW)
    assert report.errors == [] and report.chain_errors == {}
    assert report.events_pruned == 3
    remaining = await fetch_events(crewdb.conn, CREW_A, after_seq=0)
    assert [e["seq"] for e in remaining] == [2, 3, 6, 7]
    async with crewdb.conn.execute(
        "SELECT day, counts, moments FROM crew_digests WHERE crew_id = ? ORDER BY day", (CREW_A,)
    ) as cur:
        digests = [(r[0], json.loads(r[1]), json.loads(r[2])) for r in await cur.fetchall()]
    day = lambda d: format_ts(NOW - timedelta(days=d))[:10]  # noqa: E731
    assert digests == [
        (
            day(30),
            {"session.state_changed": 1},
            [{"seq": 2, "type": "host.unreachable"}, {"seq": 3, "type": "session.state_changed"}],
        ),
        (day(20), {"session.state_changed": 1}, []),
        (day(10), {"activity.burst": 1}, []),
    ]
    # the chain still verifies with pruned gaps; the head is intact
    chain = await verify_crew_chain(crewdb.conn, CREW_A)
    assert chain.ok and chain.pruned_gaps == 2
    assert (await crew_head(crewdb.conn, CREW_A)).last_seq == 7
    # a second run is a no-op
    again = await run_retention(crewdb, resolve_policy=_free, now=NOW)
    assert again.events_pruned == 0 and again.digests_written == 0


async def test_small_batches_merge_digest_counts_and_never_lose_events(crewdb):
    log = CrewEventLog(crewdb)
    for _ in range(7):
        await _emit(log, 40)
    await _emit(log, 40, type="host.unreachable", payload={"host_id": "hst_1", "silent_s": 1, "session_ids": []})
    report = retention.RetentionReport()
    pruned = await prune_crew_events(crewdb, CREW_A, FREE_POLICY, now=NOW, batch=2, report=report)
    assert pruned == 7 and report.digests_written == 1
    async with crewdb.conn.execute("SELECT counts FROM crew_digests") as cur:
        assert json.loads((await cur.fetchone())[0]) == {"session.state_changed": 7}
    assert [e["type"] for e in await fetch_events(crewdb.conn, CREW_A, after_seq=0)] == ["host.unreachable"]


async def test_prune_rolls_back_digest_when_delete_fails(crewdb, monkeypatch):
    log = CrewEventLog(crewdb)
    await _emit(log, 40)
    real_merge = retention._merge_digest

    async def merge_then_fail(*args, **kwargs):
        await real_merge(*args, **kwargs)
        raise RuntimeError("crash between digest and delete")

    monkeypatch.setattr(retention, "_merge_digest", merge_then_fail)
    with pytest.raises(RuntimeError):
        await prune_crew_events(crewdb, CREW_A, FREE_POLICY, now=NOW)
    async with crewdb.conn.execute("SELECT COUNT(*) FROM crew_digests") as cur:
        assert (await cur.fetchone())[0] == 0
    assert len(await fetch_events(crewdb.conn, CREW_A, after_seq=0)) == 1


async def test_table_rules_footprints_sessions_checkpoints_batons_idempotency(crewdb):
    old_end = format_ts(NOW - timedelta(days=100))
    recent_end = format_ts(NOW - timedelta(days=3))
    await seed_session(crewdb, CREW_A, "cs_oldended", state="ended", ended_at=old_end)
    await seed_session(crewdb, CREW_A, "cs_recentend", callsign="cc-2", state="ended", ended_at=recent_end)
    await seed_session(crewdb, CREW_A, "cs_live", callsign="cc-3", state="active")
    old = format_ts(NOW - timedelta(days=40))
    fresh = format_ts(NOW - timedelta(days=2))
    async with crewdb.transaction():
        c = crewdb.conn
        for sid in ("cs_oldended", "cs_recentend", "cs_live"):
            await c.execute("INSERT INTO crew_footprints (crew_id, session_id, path) VALUES (?, ?, 'src/a.ts')", (CREW_A, sid))
        for i, created in enumerate((old, fresh)):
            await c.execute(
                "INSERT INTO crew_checkpoints (id, crew_id, session_id, trigger, facts, facts_hash, headline, facts_source, "
                "created_at) VALUES (?, ?, 'cs_live', 'interval', '{\"x\":1}', ?, 'h', 'relay-cli', ?)",
                (f"ckp_{i}", CREW_A, f"fh{i}", created),
            )
            await c.execute(
                "INSERT INTO crew_batons (id, crew_id, to_session, kind, brief_text, created_at) "
                "VALUES (?, ?, 'cs_live', 'adopt', 'brief', ?)",
                (f"bat_{i}", CREW_A, created),
            )
        await c.execute(
            "INSERT INTO crew_idempotency (key, principal, response_json, created_at) VALUES ('old', 'p', '{}', ?)",
            (format_ts(NOW - timedelta(hours=73)),),
        )
        await c.execute(
            "INSERT INTO crew_idempotency (key, principal, response_json, created_at) VALUES ('new', 'p', '{}', ?)",
            (format_ts(NOW - timedelta(hours=1)),),
        )
    report = await run_retention(crewdb, resolve_policy=_free, now=NOW)
    assert report.errors == []

    async def col(sql):
        async with crewdb.conn.execute(sql) as cur:
            return [tuple(r) for r in await cur.fetchall()]

    # footprints of sessions ended > 7 d are pruned; the ended-100 d session row goes too (free: 90 d)
    assert await col("SELECT session_id FROM crew_footprints ORDER BY session_id") == [("cs_live",), ("cs_recentend",)]
    assert await col("SELECT id FROM crew_sessions ORDER BY id") == [("cs_live",), ("cs_recentend",)]
    # checkpoint facts after 14 d → headline + hash only; baton brief text after 30 d → cleared
    assert await col("SELECT id, facts IS NULL, headline, facts_hash FROM crew_checkpoints ORDER BY id") == [
        ("ckp_0", 1, "h", "fh0"),
        ("ckp_1", 0, "h", "fh1"),
    ]
    assert await col("SELECT id, brief_text FROM crew_batons ORDER BY id") == [("bat_0", None), ("bat_1", "brief")]
    assert await col("SELECT key FROM crew_idempotency") == [("new",)]
    assert (report.footprints_pruned, report.sessions_pruned, report.checkpoint_facts_cleared) == (1, 1, 1)
    assert (report.baton_briefs_cleared, report.idempotency_pruned) == (1, 1)


async def test_pro_plan_keeps_longer_and_broken_chain_is_reported(crewdb):
    await _seed_timeline(crewdb)

    async def pro(_owner):
        return PRO_POLICY

    await crewdb.conn.execute("UPDATE crew_events SET summary = 'forged' WHERE crew_id = ? AND seq = 1", (CREW_A,))
    await crewdb.conn.commit()
    report = await run_retention(crewdb, resolve_policy=pro, now=NOW)
    assert report.events_pruned == 0  # nothing is older than 180 d / 30 d
    assert report.chain_errors == {CREW_A: ["seq 1: hash mismatch"]}


async def test_policy_resolution():
    # §4.5 windows (raw events, bursts, checkpoint facts, ended sessions, baton brief text).
    assert RetentionPolicy(14, 7, 14, 90, 30) == FREE_POLICY
    assert RetentionPolicy(180, 30, 90, 365, 180) == PRO_POLICY
    assert RetentionPolicy(365, 60, 180, 365, 365) == TEAM_POLICY
    # One source of truth: every tier's windows are WP-14's crew limits for that plan.
    for tier in PlanTier:
        assert policy_for_tier(tier.value) == policy_from_retention(crew_limits_for_tier(tier).retention)
    solo = policy_for_tier("solo")
    assert FREE_POLICY.raw_event_days < solo.raw_event_days < PRO_POLICY.raw_event_days
    assert policy_for_tier("legacy_team_199").raw_event_days == 365 and policy_for_tier("bogus") == FREE_POLICY
    assert policy_for_tier(None) == FREE_POLICY

    class Meter:
        async def get_account(self, user_id):
            # get_account returns seat-scaled limits; crew windows never scale with seats.
            tier = PlanTier.PRO if user_id == "payer" else PlanTier.FREE
            return SimpleNamespace(limits=get_plan(tier).scaled(5))

    resolve = usage_meter_resolver(Meter())
    assert await resolve("payer") == PRO_POLICY and await resolve("freeloader") == FREE_POLICY
    # Self-hosted (no meter) gets the same defaults WP-14 gives its limits: Enterprise's.
    assert await usage_meter_resolver(None)("anyone") == SELF_HOSTED_POLICY == ENTERPRISE_POLICY


async def test_nightly_schedule_runs_and_survives_failures(crewdb):
    assert seconds_until_next_run(datetime(2026, 9, 25, 3, 0, tzinfo=UTC)) == 17 * 60
    assert seconds_until_next_run(datetime(2026, 9, 25, 3, 17, tzinfo=UTC)) == 24 * 3600
    log = CrewEventLog(crewdb)
    await _emit(log, 40)
    sleeps = []
    calls = {"n": 0}

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    async def flaky_policy(owner):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("billing down")
        return FREE_POLICY

    with pytest.raises(asyncio.CancelledError):
        await retention.retention_loop(crewdb, flaky_policy, clock=lambda: NOW, sleep=fake_sleep)
    assert len(sleeps) == 3 and calls["n"] == 2
    assert await fetch_events(crewdb.conn, CREW_A, after_seq=0) == []  # second night pruned it
