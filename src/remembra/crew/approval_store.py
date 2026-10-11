"""Unregistered approval store over the existing Crew transaction/event/outbox.

Construction performs no I/O. Missing or different proposal schema fails closed;
this adapter never creates tables, registers events or starts delivery workers.
Only synthetic fixtures install the proposal in this increment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite
from pydantic import ValidationError

from remembra.crew.approval_contracts import ApprovalRecord, ApprovalRequest, AuditEvent, Boundary, DecisionState
from remembra.crew.approval_events import append_notification
from remembra.crew.approval_schema import SCHEMA_FINGERPRINTS, schema_fingerprint
from remembra.crew.approvals import ApprovalError
from remembra.crew.db import CrewDatabase
from remembra.crew.events import CrewEventLog, EventTx


def request_key(request: ApprovalRequest) -> str:
    """Tenant/requester/credential/session-scoped identity, separate from body digest."""
    raw = json.dumps(
        [request.boundary.model_dump(), request.requester.model_dump(), request.idempotency_key],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _transition(old: ApprovalRecord | None, new: ApprovalRecord) -> tuple[str, str]:
    if old is None:
        if new.version != 1 or any((new.decision, new.invalidation, new.block, new.consumption)):
            raise ApprovalError("approval_invalid_initial_state")
        return "requested", new.request.requester.user_id
    if new.id != old.id or new.request != old.request or new.version != old.version + 1:
        raise ApprovalError("approval_immutable_request")
    if old.invalidation or old.consumption:
        raise ApprovalError("approval_terminal_state")
    changes: dict[str, Any] = {"version": new.version}
    kind, actor = "", ""
    if new.decision != old.decision:
        if old.decision or new.decision is None or new.decision.request_digest != new.request.digest:
            raise ApprovalError("approval_immutable_decision")
        if not 0 < new.decision.at_ms < new.request.expires_at_ms:
            raise ApprovalError("approval_invalid_decision_time")
        kind, actor = new.decision.state.value, new.decision.user_id
        changes["decision"] = new.decision
    elif new.invalidation != old.invalidation and new.invalidation:
        kind, actor = new.invalidation.reason, new.invalidation.actor_id
        if kind != "revoked" and actor != "server":
            raise ApprovalError("approval_invalid_system_actor")
        changes["invalidation"] = new.invalidation
    elif new.consumption != old.consumption and new.consumption:
        claim = new.consumption
        if (
            not old.decision
            or old.decision.state != DecisionState.APPROVED
            or claim.request_digest != new.request.digest
            or claim.executor != new.request.executor
            or not old.decision.at_ms <= claim.at_ms < new.request.expires_at_ms
        ):
            raise ApprovalError("approval_invalid_consumption")
        kind, actor = "consumed", new.request.requester.user_id
        changes.update(consumption=claim, block=None)
    elif new.block != old.block and new.block:
        if not old.decision or old.decision.state != DecisionState.APPROVED:
            raise ApprovalError("approval_invalid_block")
        kind, actor = "blocked", new.request.requester.user_id
        changes["block"] = new.block
    if not kind or new != old.model_copy(update=changes):
        raise ApprovalError("approval_invalid_transition")
    return kind, actor


class SQLiteApprovalStore:
    """Own each approval commit; use exactly the supplied Crew database/event log."""

    def __init__(self, db: CrewDatabase, *, event_log: CrewEventLog) -> None:
        self.db: CrewDatabase = db
        if event_log.db is not self.db:
            raise ValueError("Approval store and event log must share one Crew database.")
        self.event_log = event_log

    @property
    def in_transaction(self) -> bool:
        # The raw flag also catches legacy/external BEGIN, which Crew's generic
        # coordinator otherwise commits on entry. Approval must never adopt it.
        return self.db.in_transaction or self.db.conn.in_transaction

    async def _check_schema(self) -> None:
        rows = await self.db.fetchall("SELECT name, sql FROM sqlite_master WHERE name LIKE 'crew_approval_%'")
        actual = {r["name"]: schema_fingerprint(r["sql"] or "") for r in rows}
        if any(actual.get(name) != digest for name, digest in SCHEMA_FINGERPRINTS.items()):
            raise ApprovalError("approval_schema_unavailable")
        foreign_keys = await self.db.fetchone("PRAGMA foreign_keys")
        if foreign_keys is None or next(iter(foreign_keys.values())) != 1:
            raise ApprovalError("approval_foreign_keys_required")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SQLiteApprovalTransaction]:
        """Reject external transactions; publish events only after this owned commit."""
        if self.in_transaction:
            raise RuntimeError("approval_transaction_must_own_commit")
        try:
            async with self.db.transaction():
                await self._check_schema()
                # A child may inherit CrewEventLog's ContextVar after that log's
                # transaction has ended. Use a fresh public log scope on the same
                # database/bus so this owned commit always gets its own callback.
                log = CrewEventLog(self.db, self.event_log.bus)
                async with log.transaction() as events:
                    tx = SQLiteApprovalTransaction(self.db, events)
                    try:
                        yield tx
                        if tx.pending:
                            raise ApprovalError("approval_audit_required")
                    finally:
                        tx.active = False
        except sqlite3.Error:
            # SQLite errors may contain caller-derived values or schema details.
            raise ApprovalError("approval_storage_unavailable") from None


class SQLiteApprovalTransaction:
    """Scoped port; it cannot be reused after exit or from a child task."""

    def __init__(self, db: CrewDatabase, events: EventTx) -> None:
        self.db, self.events = db, events
        self.owner = asyncio.current_task()
        self.active = True
        self.pending: dict[str, tuple[ApprovalRecord, str, str]] = {}

    def _check(self) -> None:
        if not self.active or self.owner is not asyncio.current_task() or not self.db.in_transaction:
            raise ApprovalError("approval_transaction_inactive")

    @property
    def conn(self) -> aiosqlite.Connection:
        self._check()
        return self.db.conn

    async def _decode(self, row: dict[str, Any] | None) -> ApprovalRecord | None:
        if row is None:
            return None
        try:
            record = ApprovalRecord.model_validate_json(row["body"])
        except ValidationError:
            raise ApprovalError("approval_storage_invalid") from None
        expected = {
            "id": record.id,
            "crew_id": record.request.boundary.crew_id,
            "tenant_id": record.request.boundary.tenant_id,
            "project_id": record.request.boundary.project_id,
            "request_key": request_key(record.request),
            "request_digest": record.request.digest,
            "version": record.version,
        }
        history = await self.db.fetchone(
            "SELECT body FROM crew_approval_versions WHERE approval_id=? AND version=?", (record.id, record.version)
        )
        if any(row[key] != value for key, value in expected.items()) or history is None or history["body"] != row["body"]:
            raise ApprovalError("approval_storage_invalid")
        return record

    async def get(self, boundary: Boundary, approval_id: str) -> ApprovalRecord | None:
        """Lookup using every authority boundary field, never just a guessed id."""
        self._check()
        return await self._decode(
            await self.db.fetchone(
                "SELECT * FROM crew_approval_records WHERE id=? AND crew_id=? AND tenant_id=? AND project_id=?",
                (approval_id, boundary.crew_id, boundary.tenant_id, boundary.project_id),
            )
        )

    async def find_request(self, request: ApprovalRequest) -> ApprovalRecord | None:
        """Resolve an idempotency identity without equating it with the request body."""
        self._check()
        boundary = request.boundary
        return await self._decode(
            await self.db.fetchone(
                "SELECT * FROM crew_approval_records WHERE request_key=? AND crew_id=? AND tenant_id=? AND project_id=?",
                (request_key(request), boundary.crew_id, boundary.tenant_id, boundary.project_id),
            )
        )

    async def save(self, record: ApprovalRecord, *, expected_version: int | None) -> None:
        """Compare version and preserve immutable authority; append must follow before commit."""
        self._check()
        try:
            # model_copy is intentionally unchecked by Pydantic; close that gap
            # at the persistence boundary as well as at the HTTP parser.
            record = ApprovalRecord.model_validate_json(record.model_dump_json())
        except ValidationError:
            raise ApprovalError("approval_invalid_record") from None
        if record.id in self.pending:
            raise ApprovalError("approval_audit_required")
        old = await self.get(record.request.boundary, record.id)
        if (old is None) != (expected_version is None) or (old is not None and old.version != expected_version):
            raise ApprovalError("version_conflict")
        kind, actor = _transition(old, record)
        body = record.model_dump_json()
        if old is None:
            await self.conn.execute(
                "INSERT INTO crew_approval_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.request.boundary.crew_id,
                    record.request.boundary.tenant_id,
                    record.request.boundary.project_id,
                    request_key(record.request),
                    record.request.digest,
                    record.version,
                    body,
                ),
            )
        else:
            cur = await self.conn.execute(
                "UPDATE crew_approval_records SET version=?, body=? WHERE id=? AND version=?",
                (record.version, body, record.id, expected_version),
            )
            if cur.rowcount != 1:
                raise ApprovalError("version_conflict")
        self.pending[record.id] = record, kind, actor

    async def append(self, event: AuditEvent, boundary: Boundary) -> None:
        """Atomically bind the saved version, real Crew event and durable notification."""
        self._check()
        pending = self.pending.get(event.approval_id)
        if pending is None:
            raise ApprovalError("approval_saved_transition_required")
        record, kind, actor = pending
        if (
            boundary != record.request.boundary
            or event.version != record.version
            or event.request_digest != record.request.digest
            or (event.kind, event.actor_id) != (kind, actor)
            or event.at_ms <= 0
        ):
            raise ApprovalError("approval_audit_mismatch")
        seq, outbox_id = await append_notification(self.db, self.events, record, event)
        await self.conn.execute(
            "INSERT INTO crew_approval_versions VALUES (?, ?, ?, ?, ?, ?)",
            (record.id, record.version, boundary.crew_id, seq, outbox_id, record.model_dump_json()),
        )
        del self.pending[record.id]
