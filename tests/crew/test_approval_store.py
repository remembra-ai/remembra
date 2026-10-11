"""Real Crew chain/outbox tests behind an unregistered synthetic HTTP/DDL harness."""

import asyncio
import importlib
import json
import sqlite3
from dataclasses import replace

import pytest
from tests.crew.approval_store_harness import base_h as base_h
from tests.crew.approval_store_harness import h as h
from tests.crew.approval_store_harness import inject_failure
from tests.crew.test_approval_http import http as http
from tests.crew.test_approvals import AGENT, NOW, OWNER, Harness, operation

from remembra.crew import schemas
from remembra.crew.approval_contracts import ApprovalRecord, AuditEvent, Block
from remembra.crew.approval_events import EVENT_TYPE, NOTIFICATION_KIND, event_spec
from remembra.crew.approval_schema import APPROVAL_DDL, SCHEMA_OBJECTS
from remembra.crew.approval_store import SQLiteApprovalStore
from remembra.crew.approvals import ApprovalError
from remembra.crew.db import CrewDatabase
from remembra.crew.events import Actor, CrewEventLog, EventValidationError, fetch_events, verify_crew_chain
from remembra.crew.outbox import CrewOutboxWorker
from remembra.crew.store import CrewStore
from remembra.storage.database import MigrationRunner


async def rows(h):
    return (
        await h.db.fetchall("SELECT * FROM crew_approval_records"),
        await h.db.fetchall("SELECT * FROM crew_approval_versions ORDER BY version"),
        await h.db.fetchall("SELECT * FROM crew_events WHERE type=? ORDER BY seq", (EVENT_TYPE,)),
        await h.db.fetchall("SELECT * FROM crew_outbox WHERE kind=? ORDER BY created_at, id", (NOTIFICATION_KIND,)),
    )


async def test_import_and_constructor_never_install_schema_or_register_events(base_h):
    before = dict(schemas.EVENT_SPECS)
    assert EVENT_TYPE not in before
    from remembra.crew import approval_events, approval_schema, approval_store

    for module in (approval_schema, approval_events, approval_store):
        assert importlib.import_module(module.__name__) is module
    assert before == schemas.EVENT_SPECS
    store = SQLiteApprovalStore(base_h.db, event_log=CrewEventLog(base_h.db))
    assert await base_h.db.fetchall("SELECT name FROM sqlite_master WHERE name LIKE 'crew_approval_%'") == []
    with pytest.raises(ApprovalError, match="approval_schema_unavailable"):
        async with store.transaction():
            pytest.fail("missing schema yielded a usable transaction")
    assert await base_h.db.fetchall("SELECT name FROM sqlite_master WHERE name LIKE 'crew_approval_%'") == []
    assert not base_h.db.in_transaction


async def test_real_chain_outbox_and_immutable_history_commit_together(h):
    requested = await h.request()
    approved = await h.decide(requested)
    claimed = await h.consume(approved)
    current, history, events, outbox = await rows(h)
    assert len(current) == 1
    assert len(history) == len(events) == len(outbox) == len(h.seen) == 3
    assert [e["type"] for e in events] == [EVENT_TYPE] * 3
    assert all(e["moment"] for e in events)
    assert [json.loads(e["payload"])["kind"] for e in events] == ["requested", "approved", "consumed"]
    assert [e["actor_kind"] for e in events] == ["session", "human", "session"]
    assert [e["actor_id"] for e in events] == [
        requested.request.requester.session_id,
        OWNER.user_id,
        requested.request.requester.session_id,
    ]
    assert all(json.loads(q["payload"])["tenant_id"] == "owner" for q in outbox)
    assert all("parameters_json" not in q["payload"] and "synthetic-session-token" not in q["payload"] for q in outbox)
    assert (await verify_crew_chain(h.db.conn, h.crew_id)).ok
    assert claimed.acquired and (await h.consume(approved)).acquired is False
    assert len((await rows(h))[1]) == 3
    for sql in ("UPDATE crew_approval_versions SET version=99", "DELETE FROM crew_approval_versions"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            async with h.db.transaction():
                await h.db.conn.execute(sql)
    assert len((await rows(h))[1]) == 3


@pytest.mark.parametrize("phase", ["after_save", "after_event", "after_outbox", "after_version", "before_commit", "after_commit"])
@pytest.mark.parametrize("action", ["request", "decide", "consume"])
async def test_disconnect_boundaries_atomic_reopen_and_lost_response(h, monkeypatch, phase, action):
    record = None
    if action != "request":
        record = await h.request()
    if action == "consume":
        record = await h.decide(record)
    before = await rows(h)
    seen = len(h.seen)
    call = h.request if action == "request" else lambda: getattr(h, action)(record)
    with monkeypatch.context() as m:
        inject_failure(m, h, phase, ConnectionError("synthetic disconnect"))
        with pytest.raises(ConnectionError):
            await call()
    after = await rows(h)
    committed = phase == "after_commit"
    if committed:
        assert [len(x) for x in after[1:]] == [len(x) + 1 for x in before[1:]]
        assert len(h.seen) == seen + 1
    else:
        assert after == before
        assert len(h.seen) == seen
    # A separate reopened connection sees exactly the committed result.
    db = CrewDatabase(h.path)
    await db.connect()
    try:
        retry = Harness(db, h.crew_id, SQLiteApprovalStore(db, event_log=CrewEventLog(db)), h.path)
        if action == "request":
            recovered = await retry.request()
            assert recovered.version == 1
        elif action == "decide":
            if committed:
                with pytest.raises(ApprovalError, match="decision_conflict"):
                    await retry.decide(record)
            else:
                await retry.decide(record)
            assert (await retry.get(record)).decision.state.value == "approved"
        else:
            acquired = await retry.consume(record)
            assert acquired.acquired is not committed
            replay = await retry.consume(record)
            assert not replay.acquired and replay.record.consumption == acquired.record.consumption
        assert (await verify_crew_chain(db.conn, h.crew_id)).ok
    finally:
        await db.close()


@pytest.mark.parametrize("phase", ["after_save", "after_event", "after_outbox", "after_version", "before_commit"])
async def test_cancellation_never_publishes_or_leaves_partial_authority(h, monkeypatch, phase):
    before = await rows(h)
    with monkeypatch.context() as m:
        inject_failure(m, h, phase, asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await h.request()
    assert await rows(h) == before
    assert h.seen == []
    assert (await h.request()).version == 1


@pytest.mark.parametrize("outer", ["database", "eventlog", "raw"])
async def test_external_transactions_never_return_commit_dependent_result(h, outer):
    approved = await h.decide(await h.request())
    if outer == "raw":
        await h.db.conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(RuntimeError, match="must_own_commit"):
                await h.consume(approved)
            assert h.db.conn.in_transaction
        finally:
            await h.db.conn.rollback()
    else:
        context = h.db.transaction() if outer == "database" else h.log.transaction()
        with pytest.raises(ConnectionError):
            async with context:
                with pytest.raises(RuntimeError, match="must_own_commit"):
                    await h.consume(approved)
                raise ConnectionError("outer rollback")
    assert (await h.consume(approved)).acquired
    assert not (await h.consume(approved)).acquired


async def test_transaction_port_cannot_escape_or_cross_tasks(h):
    async with h.store.transaction() as tx:
        boundary = (await h.db.fetchone("SELECT id FROM crews WHERE id=?", (h.crew_id,)))["id"]
        assert boundary == h.crew_id

        async def child():
            with pytest.raises(ApprovalError, match="transaction_inactive"):
                _ = tx.conn

        await asyncio.create_task(child())
    with pytest.raises(ApprovalError, match="transaction_inactive"):
        _ = tx.conn


@pytest.mark.parametrize("parent_rollback", [False, True])
@pytest.mark.parametrize("child_rollback", [False, True])
async def test_completed_inherited_event_context_cannot_steal_new_commit(h, monkeypatch, parent_rollback, child_rollback):
    ready = asyncio.Event()

    async def child():
        await ready.wait()
        if child_rollback:
            with monkeypatch.context() as m:
                inject_failure(m, h, "after_version", ConnectionError("child rollback"))
                with pytest.raises(ConnectionError):
                    await h.request()
            assert all(not items for items in await rows(h))
            assert h.seen == []
        return await h.request()

    try:
        async with h.log.transaction():
            task = asyncio.create_task(child())
            if parent_rollback:
                raise ConnectionError("parent rollback")
    except ConnectionError:
        assert parent_rollback
    ready.set()
    record = await task
    assert record.version == 1
    assert [len(items) for items in await rows(h)] == [1, 1, 1, 1]
    assert len(h.seen) == 1
    assert (await verify_crew_chain(h.db.conn, h.crew_id)).ok


@pytest.mark.parametrize("action", ["request", "decide", "consume"])
async def test_two_connections_have_one_committed_winner(h, action):
    record = None if action == "request" else await h.request()
    if action == "consume":
        record = await h.decide(record)
    db = CrewDatabase(h.path)
    await db.connect()
    try:
        other = Harness(db, h.crew_id, SQLiteApprovalStore(db, event_log=CrewEventLog(db)), h.path)
        if action == "request":
            results = await asyncio.gather(h.request(), other.request())
            assert results[0] == results[1]
            assert len((await rows(h))[1]) == 1
        elif action == "decide":
            results = await asyncio.gather(h.decide(record), other.decide(record, approve=False), return_exceptions=True)
            assert sum(isinstance(x, ApprovalRecord) for x in results) == 1
            assert sum(isinstance(x, ApprovalError) for x in results) == 1
            assert len((await rows(h))[1]) == 2
        else:
            results = await asyncio.gather(h.consume(record), other.consume(record))
            assert sum(x.acquired for x in results) == 1
            assert results[0].record.consumption == results[1].record.consumption
    finally:
        await db.close()


async def test_scope_and_idempotency_are_credential_tenant_and_project_bound(h):
    record = await h.request()
    assert (await h.request()).id == record.id
    other_key = await h.request(user=replace(AGENT, api_key_id="other_key"))
    assert other_key.id != record.id
    async with h.store.transaction() as tx:
        for field in ("tenant_id", "project_id", "crew_id"):
            wrong = record.request.boundary.model_copy(update={field: "other"})
            assert await tx.get(wrong, record.id) is None
    with pytest.raises(ApprovalError, match="idempotency_conflict"):
        await h.request(operation=operation(scope=("other.py",)))


@pytest.mark.parametrize("kind", ["expired", "stale_candidate", "revoked", "blocked"])
async def test_invalidation_and_block_history_preserve_human_decision(h, kind):
    approved = await h.decide(await h.request())
    if kind == "expired":
        h.now = approved.request.expires_at_ms
        result = await h.get(approved)
    elif kind == "stale_candidate":
        h.policy.revision = ("c" * 40, "d" * 40)
        result = await h.get(approved)
    elif kind == "revoked":
        result = await h.service.revoke(
            crew_id=h.crew_id,
            approval_id=approved.id,
            user=OWNER,
            auth_time_ms=NOW,
            expected_version=2,
            request_digest=approved.request.digest,
        )
    else:
        h.policy.block = Block(reason="human_handoff_required", resolver="human_owner", next_action="confirm_in_host")
        result = (await h.consume(approved)).record
    assert result.decision == approved.decision
    assert not (await h.consume(approved)).acquired
    assert len((await rows(h))[1]) == 3
    assert json.loads((await rows(h))[2][-1]["payload"])["kind"] == kind


async def test_unregistered_or_widened_event_type_fails_closed(h, monkeypatch):
    for spec in (None, replace(event_spec(), client=True)):
        registry = dict(schemas.EVENT_SPECS)
        if spec is None:
            registry.pop(EVENT_TYPE)
        else:
            registry[EVENT_TYPE] = spec
        with monkeypatch.context() as m:
            m.setattr(schemas, "EVENT_SPECS", registry)
            with pytest.raises((EventValidationError, ApprovalError)):
                await h.request()
        assert all(not items for items in await rows(h))
    assert schemas.validate_event_payload("unrelated.approval", {})


async def test_changed_schema_and_disabled_constraints_fail_closed(h):
    async with h.db.transaction():
        await h.db.conn.execute("DROP TRIGGER crew_approval_versions_no_update")
    with pytest.raises(ApprovalError, match="schema_unavailable"):
        await h.request()
    async with h.db.transaction():
        await h.db.conn.execute(SCHEMA_OBJECTS["crew_approval_versions_no_update"])
    await h.db.conn.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(ApprovalError, match="foreign_keys_required"):
        await h.request()
    await h.db.conn.execute("PRAGMA foreign_keys=ON")
    assert (await h.request()).version == 1


async def test_missing_retention_registration_rejects_writes(h, monkeypatch):
    monkeypatch.setattr(schemas, "ALWAYS_MOMENT_TYPES", schemas.ALWAYS_MOMENT_TYPES - {EVENT_TYPE})
    with pytest.raises(ApprovalError, match="event_schema_unavailable"):
        await h.request()
    assert all(not items for items in await rows(h))


async def test_proposed_migration_uses_existing_runner_and_is_restart_idempotent(base_h):
    from remembra.crew.db import CREW_MIGRATIONS

    before = await base_h.db.fetchone("SELECT * FROM crews WHERE id=?", (base_h.crew_id,))
    proposed = MigrationRunner([*CREW_MIGRATIONS, (2, "crew_owner_approvals", list(APPROVAL_DDL))], label="crew")
    assert await proposed.apply(base_h.db.conn, base_h.db.transaction) == [2]
    assert await proposed.apply(base_h.db.conn, base_h.db.transaction) == []
    assert await base_h.db.fetchone("SELECT * FROM crews WHERE id=?", (base_h.crew_id,)) == before
    db = CrewDatabase(base_h.path)
    await db.connect()
    try:
        assert await proposed.apply(db.conn, db.transaction) == []
        async with SQLiteApprovalStore(db, event_log=CrewEventLog(db)).transaction():
            pass
    finally:
        await db.close()
    assert [v for v, _, _ in CREW_MIGRATIONS] == [1]  # global production registry is untouched


async def test_proposed_migration_failure_rolls_back_all_new_objects(base_h):
    from remembra.crew.db import CREW_MIGRATIONS

    proposed = MigrationRunner(
        [*CREW_MIGRATIONS, (2, "crew_owner_approvals", [*APPROVAL_DDL, "SELECT missing_column FROM missing_table"])],
        label="crew",
    )
    with pytest.raises(sqlite3.OperationalError):
        await proposed.apply(base_h.db.conn, base_h.db.transaction)
    assert await base_h.db.fetchall("SELECT name FROM sqlite_master WHERE name LIKE 'crew_approval_%'") == []
    assert await base_h.db.get_schema_version() == 1


async def test_expiry_crossing_revision_lookup_commits_invalidation_in_real_chain(h):
    record = await h.request()

    async def revision(tx, request):
        h.now = request.expires_at_ms
        return h.policy.revision

    h.policy.current_revision = revision
    result = await h.decide(record)
    assert result.decision is None and result.invalidation.reason == "expired"
    assert (await h.get(record)).invalidation == result.invalidation
    assert json.loads((await rows(h))[2][-1]["payload"])["kind"] == "expired"


async def test_missing_audit_mismatched_transition_and_modified_decision_rollback(h):
    approved = await h.decide(await h.request())
    bad = approved.model_copy(update={"version": 3, "decision": approved.decision.model_copy(update={"user_id": "someone"})})
    with pytest.raises(ApprovalError, match="immutable_decision"):
        async with h.store.transaction() as tx:
            await tx.save(bad, expected_version=2)
    requested = await h.request(idempotency_key="another")
    denied = requested.model_copy(
        update={"version": 2, "decision": approved.decision.model_copy(update={"request_digest": requested.request.digest})}
    )
    with pytest.raises(ApprovalError, match="audit_required"):
        async with h.store.transaction() as tx:
            await tx.save(denied, expected_version=1)
    with pytest.raises(ApprovalError, match="audit_mismatch"):
        async with h.store.transaction() as tx:
            await tx.save(denied, expected_version=1)
            await tx.append(
                AuditEvent(
                    approval_id=denied.id,
                    version=2,
                    request_digest=denied.request.digest,
                    kind="approved",
                    actor_id="intruder",
                    at_ms=NOW,
                ),
                denied.request.boundary,
            )
    assert (await h.get(requested)).decision is None


async def test_outbox_duplicate_key_with_changed_payload_rolls_back_real_event(h):
    requested = await h.request()
    await CrewStore(h.db).enqueue_outbox(
        h.crew_id, NOTIFICATION_KIND, {"approval_id": "different"}, dedupe_key=f"approval:{requested.id}:2"
    )
    before = await rows(h)
    with pytest.raises(ApprovalError, match="notification_conflict"):
        await h.decide(requested)
    assert await rows(h) == before
    assert (await h.get(requested)).decision is None
    assert len(h.seen) == 1


@pytest.mark.parametrize("changed", ["payload", "severity"])
async def test_event_duplicate_key_must_match_transition_before_commit(h, changed):
    from datetime import UTC, datetime

    requested = await h.request()
    audit = AuditEvent(
        approval_id=requested.id,
        version=2,
        request_digest=requested.request.digest,
        kind="approved",
        actor_id=OWNER.user_id,
        at_ms=NOW,
    ).model_dump(mode="json")
    if changed == "payload":
        audit["kind"] = "denied"
    async with h.log.transaction() as tx:
        await tx.emit(
            crew_id=h.crew_id,
            type=EVENT_TYPE,
            actor=Actor.human(OWNER.user_id),
            payload=audit,
            summary="Approval approved",
            severity="notice" if changed == "severity" else "info",
            idem_key=f"approval:{requested.id}:2",
            now=datetime.fromtimestamp(NOW / 1000, UTC),
        )
    before = await rows(h)
    seen = len(h.seen)
    with pytest.raises(ApprovalError, match="event_conflict"):
        await h.decide(requested)
    assert await rows(h) == before and len(h.seen) == seen
    assert (await h.get(requested)).decision is None


async def test_constructor_rejects_event_log_from_another_connection(h):
    other = CrewDatabase(h.path)
    await other.connect()
    try:
        with pytest.raises(ValueError, match="share one Crew database"):
            SQLiteApprovalStore(h.db, event_log=CrewEventLog(other))
    finally:
        await other.close()


async def test_direct_sql_cannot_rewrite_decision_or_delete_authority(h):
    approved = await h.decide(await h.request())
    changed = approved.model_copy(
        update={"version": 3, "decision": approved.decision.model_copy(update={"user_id": "different"})}
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        async with h.db.transaction():
            await h.db.conn.execute(
                "UPDATE crew_approval_records SET version=3, body=? WHERE id=?", (changed.model_dump_json(), approved.id)
            )
    with pytest.raises(sqlite3.IntegrityError):
        async with h.db.transaction():
            await h.db.conn.execute("DELETE FROM crew_approval_records WHERE id=?", (approved.id,))
    assert (await h.get(approved)).decision == approved.decision


async def test_after_commit_publication_loss_recovers_from_real_event_replay(h, monkeypatch):
    async def disconnected(_):
        raise ConnectionError("synthetic subscriber disconnect")

    with monkeypatch.context() as m:
        m.setattr(h.bus, "publish", disconnected)
        record = await h.request()
    assert record.version == 1
    assert h.seen == []
    committed = await fetch_events(h.db.conn, h.crew_id, after_seq=0)
    assert await h.bus.publish(committed) == 1
    assert await h.bus.publish(committed) == 0
    assert len(h.seen) == 1
    assert len((await rows(h))[3]) == 1


async def test_existing_outbox_worker_replays_synthetic_notification_without_resuming(h):
    await h.request()
    delivered = set()
    attempts = []

    async def synthetic_recipient(item):
        attempts.append(item.id)
        delivered.add(item.id)  # recipient uses the stable delivery identity
        if len(attempts) == 1:
            raise ConnectionError("lost synthetic ACK")
        return "synthetic-ack"

    worker = CrewOutboxWorker(CrewStore(h.db), {NOTIFICATION_KIND: synthetic_recipient})
    first = await worker.run_once()
    assert first["retry"] == 1
    await h.db.conn.execute("UPDATE crew_outbox SET next_attempt_at=NULL WHERE kind=?", (NOTIFICATION_KIND,))
    await h.db.conn.commit()
    second = await worker.run_once()
    assert second["done"] == 1
    assert len(attempts) == 2 and len(delivered) == 1
    assert (await rows(h))[3][0]["state"] == "done"
    assert (await rows(h))[0][0]["body"].find('"consumption":null') >= 0


async def test_http_owner_flow_uses_real_chain_and_scaffold_store_without_execution(http):
    created = await http.client.post(http.url, headers=http.worker_headers, json=http.body)
    assert created.status_code == 200, created.text
    body = created.json()
    url = http.url + "/" + body["approval"]["id"]
    decision = {"request_digest": body["request_digest"], "expected_version": 1, "approve": True}
    agent = await http.client.post(url + "/decide", headers=http.admin_headers, json=decision)
    assert agent.status_code == 403
    owner = await http.client.post(url + "/decide", headers=http.owner_headers, json=decision)
    assert owner.status_code == 200, owner.text
    assert owner.json()["execution"] == {"supported": False, "reason": "unsupported_adapter"}
    denied = await http.client.get(url, headers=http.other_headers)
    assert denied.status_code == 404
    assert len((await rows(http.h))[1]) == 2
    assert len(http.h.seen) == 2
    assert (await http.client.post(url + "/consume", headers=http.worker_headers, json={})).status_code == 404
