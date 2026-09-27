"""Checkpoint ingest, redaction, idempotency, session bookkeeping and counted promotion (§5.5, D15)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from remembra.crew.checkpoints import CheckpointService, headline
from remembra.crew.limits import SELF_HOSTED_CREW_LIMITS
from remembra.crew.outbox import CrewOutboxWorker, memory_promotion_handler
from remembra.crew.store import CrewStore, now_iso
from remembra.crew.tasks import Caller, CrewServiceError, TaskService
from remembra.storage.database import Database
from tests.crew.wp6_support import CREW, OTHER_CREW, event_log, events_of, open_db, seed_crew, seed_session, valid_envelopes

FACTS = {
    "branch": "feat/pos",
    "head": "a1b2c3d4e5f6",
    "commits": [{"sha": "a1b2c3d4e5f6", "subject": "split tender"}],
    "uncommitted_files": ["src/app/pos/tender.ts"],
    "tests": [{"command": "npm test -- pos", "passed": 41, "failed": 0}],
    "unpushed_commits": 1,
    "last_error": "curl -u admin:PUdUmKHYVuQv6pZNgb https://staging.example.com",
    "commands": [
        {"cmd": "export OPENAI_API_KEY=sk-proj-M2kVvumKAA5y9VEEySJ2AoLmrdZxyP8MM2kVvumK && npm run build", "exit_code": 0}
    ],
    "stdout": "SECRET=abc",
}


def limits(cap: int):
    return dataclasses.replace(SELF_HOSTED_CREW_LIMITS, memory_promotions_per_day=cap)


@pytest.fixture()
async def env(tmp_path):
    db = await open_db(tmp_path)
    await seed_crew(db)
    cap = {"n": 100}

    async def resolver(owner):
        return limits(cap["n"])

    svc = CheckpointService(event_log(db), limits_resolver=resolver)
    svc._cap = cap  # test handle
    try:
        yield db, svc
    finally:
        await db.close()


def body(session, trigger="turn", facts=None, task_id=None):
    return {"session_id": session["id"], "trigger": trigger, "facts": FACTS if facts is None else facts, "task_id": task_id}


async def test_ingest_redacts_stores_emits_and_updates_the_session(env):
    db, svc = env
    s = await seed_session(db)
    res = await svc.ingest(CREW, Caller.for_session(s), body(s))
    assert res.created and res.seq and res.promotion == "promoted"
    stored = json.dumps(res.checkpoint["facts"])
    for secret in ("PUdUmKHYVuQv6pZNgb", "sk-proj-M2kVvumKAA5y9VEEySJ2AoLmrdZxyP8MM2kVvumK", "SECRET=abc"):
        assert secret not in stored
    assert res.checkpoint["facts"]["commands"] == [{"verb": "npm", "exit_code": 0}]
    assert res.checkpoint["facts"]["tests"][0]["command"] == "npm test -- pos"
    assert res.checkpoint["facts_source"] == "relay-cli"
    assert res.checkpoint["headline"] == "cc-1 turn: 1 commits, 1 dirty, tests 1 pass 0 fail, 1 unpushed"
    sess = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (s["id"],))
    assert sess["last_checkpoint_id"] == res.checkpoint["id"] and sess["calls_since_checkpoint"] == 0
    assert sess["checkpoint_streak"] == 1 and sess["next_checkpoint_due_at"] > now_iso()
    evs = await events_of(db)
    valid_envelopes(evs)
    assert [e["type"] for e in evs] == ["checkpoint.created"]
    assert evs[0]["payload"]["checkpoint"]["id"] == res.checkpoint["id"]


async def test_replay_is_idempotent(env):
    db, svc = env
    s = await seed_session(db)
    first = await svc.ingest(CREW, Caller.for_session(s), body(s))
    again = await svc.ingest(CREW, Caller.for_session(s), body(s))
    assert not again.created and again.promotion == "replay" and again.checkpoint["id"] == first.checkpoint["id"]
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM crew_checkpoints"))["n"] == 1
    assert len(await events_of(db)) == 1


async def test_mcp_sessions_are_agent_declared(env):
    db, svc = env
    s = await seed_session(db, client_kind="mcp", adapter="mcp")
    res = await svc.ingest(CREW, Caller.for_session(s), body(s))
    assert res.checkpoint["facts_source"] == "agent-declared"


@pytest.mark.parametrize(
    ("mutate", "status", "error"),
    [
        (lambda b, s: b.update(trigger="task"), 422, "server_trigger"),
        (lambda b, s: b.update(trigger="lost"), 422, "server_trigger"),
        (lambda b, s: b.update(trigger="bogus"), 422, "invalid_checkpoint"),
        (lambda b, s: b.update(session_id="cs_someoneelse"), 403, "session_mismatch"),
        (lambda b, s: b.update(facts="not an object"), 422, "invalid_facts"),
        (lambda b, s: b.update(facts={"blob": "x" * 17_000}), 413, "facts_too_large"),
        (lambda b, s: b.update(task_id="tsk_notinthiscrew"), 422, "cross_crew_reference"),
    ],
)
async def test_ingest_refusals(env, mutate, status, error):
    db, svc = env
    s = await seed_session(db)
    b = body(s)
    mutate(b, s)
    with pytest.raises(CrewServiceError) as e:
        await svc.ingest(CREW, Caller.for_session(s), b)
    assert (e.value.status, e.value.error) == (status, error)


async def test_ingest_needs_a_live_session_of_this_crew(env):
    db, svc = env
    ended = await seed_session(db, state="ended")
    with pytest.raises(CrewServiceError) as e:
        await svc.ingest(CREW, Caller.for_session(ended), body(ended))
    assert e.value.error == "session_ended"
    with pytest.raises(CrewServiceError) as e:
        await svc.ingest(CREW, Caller.for_human("u_owner", privileged=True), body(ended))
    assert e.value.error == "session_required"
    await seed_crew(db, OTHER_CREW, owner="u_x", project="x")
    foreign = await seed_session(db, OTHER_CREW)
    with pytest.raises(CrewServiceError) as e:
        await svc.ingest(CREW, Caller.for_session(foreign), body(foreign))
    assert e.value.status == 404


async def _promotions(db):
    rows = await db.fetchall(
        "SELECT payload, next_attempt_at FROM crew_outbox WHERE kind = 'memory_promotion' ORDER BY created_at"
    )
    return [(json.loads(r["payload"]), r["next_attempt_at"]) for r in rows]


async def _server_quota(svc, db, s, facts):
    """A ``quota`` checkpoint as the stall flow records it (never from ``POST /checkpoints``)."""
    async with svc.events.transaction() as tx:
        fresh = await db.fetchone("SELECT * FROM crew_sessions WHERE id = ?", (s["id"],))
        return await svc.record_in_tx(tx, CREW, fresh, trigger="quota", facts=facts, task_id=None, facts_source="relay-cli")


async def test_a_session_cannot_submit_a_quota_checkpoint(env):
    """quota promotes without the cap or spacing, so only the stall flow records it (review finding)."""
    db, svc = env
    s = await seed_session(db)
    with pytest.raises(CrewServiceError) as e:
        await svc.ingest(CREW, Caller.for_session(s), body(s, "quota"))
    assert (e.value.status, e.value.error) == (422, "server_trigger")


async def test_promotion_spacing_always_triggers_and_close_coalescing(env):
    db, svc = env
    s = await seed_session(db)
    c = Caller.for_session(s)
    r1 = await svc.ingest(CREW, c, body(s, facts={**FACTS, "n": 1}))
    r2 = await svc.ingest(CREW, c, body(s, "commit", facts={**FACTS, "n": 2}))
    r3 = await _server_quota(svc, db, s, {**FACTS, "n": 3})
    r4 = await svc.ingest(CREW, c, body(s, "close", facts={**FACTS, "commits": []}))
    r5 = await svc.ingest(CREW, c, body(s, "close", facts={**FACTS, "n": 5}))
    assert [r.promotion for r in (r1, r2, r3, r4, r5)] == [
        "promoted",
        "skipped:spacing",
        "promoted",
        "skipped:no_commits",
        "promoted",
    ]
    short = await seed_session(db, callsign="cc-2", joined_at=now_iso())
    r6 = await svc.ingest(CREW, Caller.for_session(short), body(short, "close"))
    assert r6.promotion == "skipped:short_session"
    promos = await _promotions(db)
    assert [p["metadata"]["checkpoint_trigger"] for p, _ in promos] == ["turn", "quota", "close"]
    payload = promos[0][0]
    assert payload["memory_type"] == "checkpoint" and payload["user_id"] == "u_owner" and payload["project_id"] == "yaadbooks"
    assert payload["metadata"]["checkpoint_id"] == r1.checkpoint["id"]
    assert "PUdUmKHYVuQv6pZNgb" not in payload["content"] and "[CHECKPOINT] cc-1 (claude-code)" in payload["content"]


async def test_daily_cap_defers_to_the_next_day_except_quota_and_lost(env):
    db, svc = env
    svc._cap["n"] = 1
    a = await seed_session(db, callsign="cc-1")
    b = await seed_session(db, callsign="cc-2")
    first = await svc.ingest(CREW, Caller.for_session(a), body(a))
    second = await svc.ingest(CREW, Caller.for_session(b), body(b))
    quota = await _server_quota(svc, db, b, {**FACTS, "x": 1})
    assert (first.promotion, second.promotion, quota.promotion) == ("promoted", "deferred", "promoted")
    promos = await _promotions(db)
    today = datetime.now(UTC).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert promos[0][0]["metadata"]["promotion_day"] == today.isoformat()
    assert promos[1][0]["metadata"]["promotion_day"] == tomorrow and promos[1][1].startswith(tomorrow + "T00:00:00")
    assert promos[2][0]["metadata"]["promotion_day"] == today.isoformat()  # quota always promotes and counts
    due = await CrewStore(db).due_outbox()
    assert {json.loads(r["payload"])["metadata"]["checkpoint_trigger"] for r in due} == {"turn", "quota"}


async def test_promotion_payload_is_stored_by_the_real_outbox_handler(env, tmp_path):
    """The queued payload passes WP-1's memory_promotion handler into a real main database."""
    db, svc = env
    s = await seed_session(db)
    await svc.ingest(CREW, Caller.for_session(s), body(s))
    main = Database(str(tmp_path / "main.db"))
    await main.connect()
    await main.init_schema()
    stored = []

    class Memory:
        settings = SimpleNamespace(checkpoint_default_ttl="7d")

        async def store(self, request, **kw):
            stored.append(request)
            return SimpleNamespace(id=f"mem_{len(stored)}", status="stored")

    try:
        worker = CrewOutboxWorker(CrewStore(db), {"memory_promotion": memory_promotion_handler(main, Memory())})
        counts = await worker.run_once()
        assert counts == {"done": 1, "retry": 0, "failed": 0}
        req = stored[0]
        assert req.memory_type == "checkpoint" and req.project_id == "yaadbooks" and req.metadata["source"] == "crew"
    finally:
        await main.close()


async def test_task_transitions_record_a_task_checkpoint(env):
    db, svc = env
    s = await seed_session(db)
    tasks = TaskService(svc.events, on_transition=svc.on_task_transition)
    t = (
        await tasks.create(
            CREW, Caller.for_human("u_owner", privileged=True), {"title": "T", "zone_ids": [], "acceptance": [], "depends_on": []}
        )
    ).task
    await tasks.start(CREW, t["id"], Caller.for_session(s))
    await tasks.release(CREW, t["id"], Caller.for_session(s))
    rows = await db.fetchall("SELECT trigger, facts_source, task_id, facts FROM crew_checkpoints ORDER BY created_at, rowid")
    assert [(r["trigger"], r["facts_source"], r["task_id"]) for r in rows] == [
        ("task", "server-inferred", t["id"]),
        ("task", "server-inferred", t["id"]),
    ]
    assert [json.loads(r["facts"])["task"]["to"] for r in rows] == ["in_progress", "stalled"]
    promos = await _promotions(db)
    assert len(promos) == 1 and promos[0][0]["metadata"]["checkpoint_trigger"] == "task"  # only the finishing transition
    valid_envelopes(await events_of(db))


async def test_list_filters(env):
    db, svc = env
    a = await seed_session(db, callsign="cc-1")
    b = await seed_session(db, callsign="cc-2")
    await svc.ingest(CREW, Caller.for_session(a), body(a))
    await svc.ingest(CREW, Caller.for_session(b), body(b))
    assert len(await svc.list_checkpoints(CREW)) == 2
    only_a = await svc.list_checkpoints(CREW, session_id=a["id"])
    assert [c["session_id"] for c in only_a] == [a["id"]]
    assert await svc.list_checkpoints(OTHER_CREW) == []


def test_headline_is_ids_and_counts_only():
    text = headline("cc-1", "commit", 12, {"commits": ["a"], "dirty": ["x", "y"], "tests": [{"cmd": "t", "passed": False}]})
    assert text == "cc-1 commit T-12: 1 commits, 2 dirty, tests 0 pass 1 fail"
