"""WP-1: crew outbox worker (D35) over a real crew.db and a real main-DB memory stack.

The memory side uses ``tests._ret_harness.make_stack``: the shipped
``MemoryService`` on a temp SQLite file and in-process Qdrant, with only the
embedding provider faked. Relay handoffs go through the real ``RelayService``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remembra.crew.db import CrewDatabase
from remembra.crew.outbox import (
    KIND_MEMORY_PROMOTION,
    KIND_RELAY_HANDOFF,
    CrewOutboxWorker,
    OutboxItem,
    OutboxPermanentError,
    default_handlers,
)
from remembra.crew.store import CrewStore, now_iso, parse_iso
from remembra.services.relay import RelayService, relay_key
from tests._ret_harness import make_stack


@pytest.fixture()
async def crew(tmp_path: Path):
    db = CrewDatabase(str(tmp_path / "crew.db"))
    await db.init_schema()
    store = CrewStore(db)
    row, _ = await store.ensure_crew("u1", "p")
    store.crew_id = row["id"]  # type: ignore[attr-defined]
    try:
        yield store
    finally:
        await db.close()


@pytest.fixture()
async def stack(tmp_path: Path):
    s = await make_stack(tmp_path)
    try:
        yield s
    finally:
        await s.close()


async def force_due(store: CrewStore, outbox_id: str) -> None:
    await store.db.conn.execute(
        "UPDATE crew_outbox SET next_attempt_at = ? WHERE id = ?",
        (now_iso(datetime.now(UTC) - timedelta(seconds=1)), outbox_id),
    )
    await store.db.conn.commit()


async def reset_pending(store: CrewStore, outbox_id: str) -> None:
    """Simulate a crash after the main-DB write but before the item was marked done."""
    await store.db.conn.execute(
        "UPDATE crew_outbox SET state = 'pending', result_id = NULL, next_attempt_at = ? WHERE id = ?", (now_iso(), outbox_id)
    )
    await store.db.conn.commit()


# ---------------------------------------------------------------------------
# worker mechanics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("separate_connection", [False, True])
async def test_separate_workers_do_not_apply_the_same_pending_effect(crew: CrewStore, separate_connection: bool) -> None:
    """Per-worker asyncio locks must not let two API workers apply one effect."""
    other_db = CrewDatabase(crew.db.db_path) if separate_connection else crew.db
    if separate_connection:
        await other_db.connect()
    entered = asyncio.Event()
    finish = asyncio.Event()
    effects: list[str] = []

    async def first_handler(item: OutboxItem) -> str:
        effects.append(item.id)
        entered.set()
        await finish.wait()
        return "first-effect"

    async def second_handler(item: OutboxItem) -> str:
        effects.append(item.id)
        return "duplicate-effect"

    outbox_id = await crew.enqueue_outbox(crew.crew_id, "k", {})
    first = asyncio.create_task(CrewOutboxWorker(crew, {"k": first_handler}).run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await asyncio.wait_for(CrewOutboxWorker(CrewStore(other_db), {"k": second_handler}).run_once(), timeout=5)
    finally:
        finish.set()
        await first
        if separate_connection:
            await other_db.close()
    assert second == {"done": 0, "retry": 0, "failed": 0}
    assert effects == [outbox_id]
    assert (await crew.get_outbox(outbox_id))["result_id"] == "first-effect"


async def test_other_process_is_excluded_and_a_killed_worker_does_not_strand_pending_work(
    crew: CrewStore, tmp_path: Path
) -> None:
    outbox_id = await crew.enqueue_outbox(crew.crew_id, "k", {})
    ready = tmp_path / "worker-entered"
    child_code = """
import asyncio, sys
from pathlib import Path
from remembra.crew.db import CrewDatabase
from remembra.crew.store import CrewStore
from remembra.crew.outbox import CrewOutboxWorker
async def main():
    db = CrewDatabase(sys.argv[1])
    await db.connect()
    async def blocked(item):
        Path(sys.argv[2]).write_text(item.id)
        await asyncio.Event().wait()
    await CrewOutboxWorker(CrewStore(db), {"k": blocked}).run_once()
asyncio.run(main())
"""
    effects: list[str] = []

    async def handler(item: OutboxItem) -> str:
        effects.append(item.id)
        return "recovered"

    child = subprocess.Popen([sys.executable, "-c", child_code, crew.db.db_path, str(ready)])
    worker = CrewOutboxWorker(crew, {"k": handler})
    try:
        async with asyncio.timeout(10):
            while not ready.exists():
                assert child.poll() is None, "worker process exited before handling the item"
                await asyncio.sleep(0.02)
        excluded = await worker.run_once()
    finally:
        child.kill()
        child.wait(timeout=5)
    assert excluded == {"done": 0, "retry": 0, "failed": 0}
    assert effects == []
    pending = await crew.get_outbox(outbox_id)
    assert pending["state"] == "pending" and pending["attempts"] == 0
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    assert effects == [outbox_id]


async def test_cancelling_a_handler_releases_the_lock_for_another_connection(crew: CrewStore) -> None:
    entered = asyncio.Event()

    async def blocked(item: OutboxItem) -> None:
        entered.set()
        await asyncio.Event().wait()

    outbox_id = await crew.enqueue_outbox(crew.crew_id, "k", {})
    task = asyncio.create_task(CrewOutboxWorker(crew, {"k": blocked}).run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    other_db = CrewDatabase(crew.db.db_path)
    await other_db.connect()

    async def recovered(item: OutboxItem) -> str:
        return "recovered"

    try:
        assert await CrewOutboxWorker(CrewStore(other_db), {"k": recovered}).run_once() == {"done": 1, "retry": 0, "failed": 0}
        assert (await crew.get_outbox(outbox_id))["result_id"] == "recovered"
    finally:
        await other_db.close()


async def test_lock_failure_never_runs_a_handler_unlocked(crew: CrewStore, monkeypatch) -> None:
    effects: list[str] = []

    async def handler(item: OutboxItem) -> None:
        effects.append(item.id)

    def refused(*args, **kwargs):
        raise PermissionError("lock file unavailable")

    outbox_id = await crew.enqueue_outbox(crew.crew_id, "k", {})
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", refused)
        with pytest.raises(PermissionError, match="lock file unavailable"):
            await CrewOutboxWorker(crew, {"k": handler}).run_once()
    assert effects == []
    assert (await crew.get_outbox(outbox_id))["state"] == "pending"
    assert await CrewOutboxWorker(crew, {"k": handler}).run_once() == {"done": 1, "retry": 0, "failed": 0}


async def test_in_memory_database_serializes_worker_instances(tmp_path: Path) -> None:
    db = CrewDatabase(":memory:")
    await db.init_schema()
    store = CrewStore(db)
    row, _ = await store.ensure_crew("u1", "p")
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def handler(item: OutboxItem) -> None:
        calls.append(item.id)
        entered.set()
        await finish.wait()

    item_id = await store.enqueue_outbox(row["id"], "k", {})
    first = asyncio.create_task(CrewOutboxWorker(store, {"k": handler}).run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await CrewOutboxWorker(CrewStore(db), {"k": handler}).run_once() == {"done": 0, "retry": 0, "failed": 0}
    finally:
        finish.set()
        await first
        await db.close()
    assert calls == [item_id]


async def test_distinct_databases_process_independently(crew: CrewStore, tmp_path: Path) -> None:
    other_db = CrewDatabase(str(tmp_path / "other-crew.db"))
    await other_db.init_schema()
    other_store = CrewStore(other_db)
    row, _ = await other_store.ensure_crew("u2", "other-project")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def blocked(item: OutboxItem) -> None:
        entered.set()
        await finish.wait()

    async def immediate(item: OutboxItem) -> str:
        return "other-effect"

    await crew.enqueue_outbox(crew.crew_id, "k", {})
    other_id = await other_store.enqueue_outbox(row["id"], "k", {})
    first = asyncio.create_task(CrewOutboxWorker(crew, {"k": blocked}).run_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await CrewOutboxWorker(other_store, {"k": immediate}).run_once() == {"done": 1, "retry": 0, "failed": 0}
        assert (await other_store.get_outbox(other_id))["result_id"] == "other-effect"
    finally:
        finish.set()
        await first
        await other_db.close()


async def test_worker_applies_retries_and_dead_letters(crew: CrewStore) -> None:
    calls: list[OutboxItem] = []
    failures = {"flaky": 1, "broken": 99}

    async def handler(item: OutboxItem) -> str | None:
        calls.append(item)
        name = item.payload["name"]
        if failures.get(name, 0) > 0:
            failures[name] -= 1
            raise ConnectionError(f"{name} unavailable")
        return f"res_{name}"

    worker = CrewOutboxWorker(crew, {"k": handler}, max_attempts=3, base_backoff_s=30)
    ok = await crew.enqueue_outbox(crew.crew_id, "k", {"name": "ok"})
    flaky = await crew.enqueue_outbox(crew.crew_id, "k", {"name": "flaky"})
    broken = await crew.enqueue_outbox(crew.crew_id, "k", {"name": "broken"})

    assert await worker.run_once() == {"done": 1, "retry": 2, "failed": 0}
    assert (await crew.get_outbox(ok))["result_id"] == "res_ok"
    row = await crew.get_outbox(flaky)
    assert row["attempts"] == 1 and row["last_error"] == "ConnectionError: flaky unavailable"
    wait = (parse_iso(row["next_attempt_at"]) - datetime.now(UTC)).total_seconds()
    assert 25 < wait <= 30  # backoff persisted on the row, survives a restart
    assert await worker.run_once() == {"done": 0, "retry": 0, "failed": 0}  # nothing due yet

    await force_due(crew, flaky)
    await force_due(crew, broken)
    assert await worker.run_once() == {"done": 1, "retry": 1, "failed": 0}
    await force_due(crew, broken)
    assert await worker.run_once() == {"done": 0, "retry": 0, "failed": 1}
    dead = await crew.get_outbox(broken)
    assert (dead["state"], dead["attempts"]) == ("failed", 3)
    assert [c.payload["name"] for c in calls] == ["ok", "flaky", "broken", "flaky", "broken", "broken"]
    assert calls[0].crew_id == crew.crew_id and calls[0].attempts == 0 and calls[3].attempts == 1


async def test_permanent_errors_unknown_kinds_and_bad_payloads_fail_at_once(crew: CrewStore) -> None:
    async def reject(item: OutboxItem) -> str | None:
        raise OutboxPermanentError("payload.user_id must be a non-empty string")

    worker = CrewOutboxWorker(crew, {"k": reject})
    a = await crew.enqueue_outbox(crew.crew_id, "k", {})
    b = await crew.enqueue_outbox(crew.crew_id, "nobody_handles_this", {})
    c = await crew.enqueue_outbox(crew.crew_id, "k", {})
    await crew.db.conn.execute("UPDATE crew_outbox SET payload = 'not json' WHERE id = ?", (c,))
    await crew.db.conn.commit()
    assert await worker.run_once() == {"done": 0, "retry": 0, "failed": 3}
    for oid in (a, b, c):
        row = await crew.get_outbox(oid)
        assert row["state"] == "failed" and row["attempts"] == 1 and row["last_error"].startswith("permanent:")


def test_backoff_is_exponential_and_capped(crew: CrewStore) -> None:
    worker = CrewOutboxWorker(crew, {}, base_backoff_s=5, max_backoff_s=60)
    assert [worker.backoff(n) for n in (1, 2, 3, 4, 5)] == [5, 10, 20, 40, 60]
    with pytest.raises(ValueError):
        CrewOutboxWorker(crew, {}, poll_interval_s=0)


async def test_loop_processes_on_wake_survives_errors_and_stops(crew: CrewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    applied = asyncio.Event()

    async def handler(item: OutboxItem) -> str | None:
        applied.set()
        return "r"

    worker = CrewOutboxWorker(crew, {"k": handler}, poll_interval_s=30)
    real_due = crew.due_outbox
    boom = {"n": 1}

    async def flaky_due(**kwargs):
        if boom["n"]:
            boom["n"] -= 1
            raise RuntimeError("disk hiccup")
        return await real_due(**kwargs)

    monkeypatch.setattr(crew, "due_outbox", flaky_due)
    worker.start()
    worker.start()  # idempotent
    try:
        assert worker.running
        await asyncio.sleep(0.05)  # first pass hits the store error; the loop must keep going
        oid = await crew.enqueue_outbox(crew.crew_id, "k", {})
        worker.wake()  # far sooner than the 30 s poll
        await asyncio.wait_for(applied.wait(), timeout=2)
        for _ in range(50):
            if (await crew.get_outbox(oid))["state"] == "done":
                break
            await asyncio.sleep(0.01)
        assert (await crew.get_outbox(oid))["state"] == "done"
    finally:
        await worker.stop()
    assert not worker.running


async def test_stop_mid_handler_leaves_the_item_pending(crew: CrewStore) -> None:
    started = asyncio.Event()

    async def slow(item: OutboxItem) -> str | None:
        started.set()
        await asyncio.sleep(60)
        return "never"

    worker = CrewOutboxWorker(crew, {"k": slow}, poll_interval_s=0.01)
    oid = await crew.enqueue_outbox(crew.crew_id, "k", {})
    worker.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await worker.stop()
    row = await crew.get_outbox(oid)
    assert (row["state"], row["attempts"]) == ("pending", 0)


# ---------------------------------------------------------------------------
# memory promotions (real MemoryService)
# ---------------------------------------------------------------------------


async def _memories(stack, sql_where: str = "1", params: tuple = ()) -> list[dict]:
    cursor = await stack.db.conn.execute(f"SELECT * FROM memories WHERE {sql_where}", params)
    return [dict(r) for r in await cursor.fetchall()]


async def test_memory_promotions_store_one_atomic_memory_and_replay_is_a_no_op(crew: CrewStore, stack) -> None:
    relay = RelayService(db=stack.db, memory_service=stack.service)
    handlers = default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay)
    worker = CrewOutboxWorker(crew, handlers)

    checkpoint_text = "T-12 checkpoint: 2 commits, pdf.spec failing. Next: fix margin calc, then push."
    decision_text = "D-7 GCT rounding is half-up per line (confirmed by Mani)."
    async with crew.db.transaction():
        ckp = await crew.enqueue_outbox(
            crew.crew_id,
            KIND_MEMORY_PROMOTION,
            {
                "user_id": "u1",
                "project_id": "yaadbooks",
                "memory_type": "checkpoint",
                "content": checkpoint_text,
                "metadata": {"task": "T-12", "checkpoint_id": "ckp_1"},
            },
            dedupe_key="ckp_1",
        )
        dec = await crew.enqueue_outbox(
            crew.crew_id,
            KIND_MEMORY_PROMOTION,
            {"user_id": "u1", "project_id": "yaadbooks", "memory_type": "decision", "content": decision_text},
            dedupe_key="dec_7",
        )
    assert await worker.run_once() == {"done": 2, "retry": 0, "failed": 0}

    ckp_id = (await crew.get_outbox(ckp))["result_id"]
    dec_id = (await crew.get_outbox(dec))["result_id"]
    rows = {r["id"]: r for r in await _memories(stack, "user_id = 'u1'")}
    assert set(rows) == {ckp_id, dec_id}  # one memory each: never fact-split
    ckp_row, dec_row = rows[ckp_id], rows[dec_id]
    assert (ckp_row["memory_type"], ckp_row["content"], ckp_row["project_id"]) == ("checkpoint", checkpoint_text, "yaadbooks")
    assert (dec_row["memory_type"], dec_row["content"]) == ("decision", decision_text)
    ckp_meta, dec_meta = json.loads(ckp_row["metadata"]), json.loads(dec_row["metadata"])
    assert ckp_meta["crew_outbox_id"] == ckp and ckp_meta["crew_id"] == crew.crew_id and ckp_meta["source"] == "crew"
    assert ckp_meta["task"] == "T-12" and dec_meta["crew_outbox_id"] == dec
    assert ckp_row["expires_at"] is not None  # checkpoint policy: default TTL
    assert dec_row["expires_at"] is None  # decisions do not expire

    # Crash replay: the item is pending again although the memory exists.
    await reset_pending(crew, ckp)
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    assert (await crew.get_outbox(ckp))["result_id"] == ckp_id
    assert len(await _memories(stack, "user_id = 'u1'")) == 2

    # A second enqueue of the same effect is deduplicated before it reaches the worker.
    again = await crew.enqueue_outbox(crew.crew_id, KIND_MEMORY_PROMOTION, {"user_id": "u1"}, dedupe_key="ckp_1")
    assert again == ckp and (await crew.get_outbox(ckp))["state"] == "done"


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "u1", "project_id": "p", "memory_type": "fact", "content": "x"},  # not a crew promotion type
        {"user_id": "u1", "project_id": "p", "memory_type": "status", "content": "x"},
        {"project_id": "p", "memory_type": "decision", "content": "x"},
        {"user_id": "u1", "project_id": "p", "memory_type": "decision", "content": "   "},
        {"user_id": "u1", "project_id": "p", "memory_type": "decision", "content": "x", "metadata": ["no"]},
        {"user_id": "u1", "project_id": "p", "memory_type": "checkpoint", "content": "x" * 20000},
    ],
)
async def test_invalid_promotions_are_dead_lettered_without_touching_memory(crew: CrewStore, stack, payload: dict) -> None:
    relay = RelayService(db=stack.db, memory_service=stack.service)
    worker = CrewOutboxWorker(crew, default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay))
    oid = await crew.enqueue_outbox(crew.crew_id, KIND_MEMORY_PROMOTION, payload)
    assert await worker.run_once() == {"done": 0, "retry": 0, "failed": 1}
    assert (await crew.get_outbox(oid))["state"] == "failed"
    assert await _memories(stack) == []


async def test_memory_service_failure_is_retried(crew: CrewStore, stack) -> None:
    relay = RelayService(db=stack.db, memory_service=stack.service)
    worker = CrewOutboxWorker(crew, default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay))
    real_store = stack.service.store
    state = {"fail": True}

    async def store(*args, **kwargs):
        if state["fail"]:
            raise TimeoutError("qdrant timeout")
        return await real_store(*args, **kwargs)

    stack.service.store = store
    oid = await crew.enqueue_outbox(
        crew.crew_id,
        KIND_MEMORY_PROMOTION,
        {"user_id": "u1", "project_id": "p", "memory_type": "decision", "content": "Use Supabase RLS for tenants."},
    )
    assert await worker.run_once() == {"done": 0, "retry": 1, "failed": 0}
    state["fail"] = False
    await force_due(crew, oid)
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    assert [r["memory_type"] for r in await _memories(stack)] == ["decision"]


# ---------------------------------------------------------------------------
# relay handoffs (real RelayService)
# ---------------------------------------------------------------------------

FACTS = {
    "facts_source": "server-inferred",
    "branch": "feat/pos",
    "head_commit": "abc1234def",
    "commits": [{"sha": "abc1234", "subject": "pos: split tender"}],
    "uncommitted_files": ["src/app/pos/tender.ts"],
    "next_step": "finish tender.ts and run npm test -- pos",
}


def _handoff_payload(**over) -> dict:
    return {
        "user_id": "u1",
        "project_id": "yaadbooks",
        "agent_id": "claude-code",
        "session_id": "sess-A",
        "facts": FACTS,
        "end_reason": "stalled:billing_error",
        **over,
    }


async def _handoffs(stack, key: str) -> list[dict]:
    return await _memories(
        stack,
        "memory_type = 'handoff' AND json_extract(metadata, '$.relay_key') = ? ORDER BY created_at",
        (key,),
    )


async def test_relay_handoff_is_written_once_even_when_replayed(crew: CrewStore, stack) -> None:
    relay = RelayService(db=stack.db, memory_service=stack.service)
    worker = CrewOutboxWorker(crew, default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay))
    oid = await crew.enqueue_outbox(crew.crew_id, KIND_RELAY_HANDOFF, _handoff_payload(), dedupe_key="stall:cs_A")
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    handoff_id = (await crew.get_outbox(oid))["result_id"]
    key = relay_key("claude-code", "sess-A")
    rows = await _handoffs(stack, key)
    assert [r["id"] for r in rows] == [handoff_id]
    meta = json.loads(rows[0]["metadata"])
    # services/relay.py (WP-8) does not know crew's "server-inferred" label yet and
    # stores unknown labels as "agent-declared"; the worker passes the facts through unchanged.
    assert meta["relay"]["facts_source"] in ("server-inferred", "agent-declared")
    assert meta["agent_id"] == "claude-code" and meta["session_id"] == "sess-A"
    assert meta["relay"]["end_reason"] == "stalled:billing_error"
    assert "tender.ts" in rows[0]["content"] or meta["relay"]["uncommitted_count"] == 1

    await reset_pending(crew, oid)
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    assert (await crew.get_outbox(oid))["result_id"] == handoff_id
    assert [r["id"] for r in await _handoffs(stack, key)] == [handoff_id]  # no duplicate, nothing superseded


async def test_relay_handoff_never_overwrites_a_newer_close_by_the_agent(crew: CrewStore, stack) -> None:
    relay = RelayService(db=stack.db, memory_service=stack.service)
    worker = CrewOutboxWorker(crew, default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay))
    oid = await crew.enqueue_outbox(crew.crew_id, KIND_RELAY_HANDOFF, _handoff_payload())
    # Before the worker runs, the agent closes its own session with fresher facts.
    own = await relay.close_session(
        user_id="u1",
        project_id="yaadbooks",
        agent_id="claude-code",
        session_id="sess-A",
        facts={**FACTS, "facts_source": "relay-cli", "next_step": "push and deploy"},
    )
    assert await worker.run_once() == {"done": 1, "retry": 0, "failed": 0}
    assert (await crew.get_outbox(oid))["result_id"] == own["handoff_id"]
    rows = await _handoffs(stack, relay_key("claude-code", "sess-A"))
    assert [r["id"] for r in rows] == [own["handoff_id"]]
    assert rows[0]["superseded_by"] is None


@pytest.mark.parametrize(
    "over",
    [{"facts": "not a dict"}, {"agent_id": ""}, {"summary": 7}, {"agent_verified": "yes"}, {"session_id": None}],
)
async def test_invalid_relay_payloads_are_dead_lettered(crew: CrewStore, stack, over: dict) -> None:
    relay = RelayService(db=stack.db, memory_service=stack.service)
    worker = CrewOutboxWorker(crew, default_handlers(main_db=stack.db, memory_service=stack.service, relay_service=relay))
    oid = await crew.enqueue_outbox(crew.crew_id, KIND_RELAY_HANDOFF, _handoff_payload(**over))
    assert await worker.run_once() == {"done": 0, "retry": 0, "failed": 1}
    assert (await crew.get_outbox(oid))["last_error"].startswith("permanent:")
    assert await _memories(stack, "memory_type = 'handoff'") == []
