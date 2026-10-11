"""Synthetic durable adapter for approval contract tests, NEVER production DDL.

Uses existing CrewDatabase transactions and outbox with isolated test tables.
No migrations, event kinds or handlers are registered with the application.
"""

import json
from contextlib import asynccontextmanager

from remembra.crew.approval_contracts import ApprovalRecord
from remembra.crew.approvals import ApprovalError
from remembra.crew.store import CrewStore


def request_key(request):
    return json.dumps(
        [
            request.boundary.model_dump(),
            request.requester.model_dump(),
            request.idempotency_key,
        ],
        sort_keys=True,
    )


class SQLiteContractStore:
    def __init__(self, db):
        self.db = db
        self.fail_at = None

    @property
    def conn(self):
        return self.db.conn

    async def setup(self):
        await self.conn.executescript("""
            CREATE TABLE test_approvals (
                id TEXT PRIMARY KEY, boundary TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL,
                version INTEGER NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE test_approval_audit (
                delivery_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL
            );
            CREATE TRIGGER test_audit_no_update BEFORE UPDATE ON test_approval_audit
                BEGIN SELECT RAISE(ABORT, 'immutable audit'); END;
            CREATE TRIGGER test_audit_no_delete BEFORE DELETE ON test_approval_audit
                BEGIN SELECT RAISE(ABORT, 'immutable audit'); END;
        """)
        await self.conn.commit()

    def fail(self, point):
        if self.fail_at == point:
            self.fail_at = None
            raise ConnectionError(point)

    @asynccontextmanager
    async def transaction(self):
        async with self.db.transaction():
            yield self
            self.fail("before_commit")
        self.fail("after_commit")

    async def get(self, boundary, approval_id):
        row = await self.db.fetchone(
            "SELECT body FROM test_approvals WHERE id = ? AND boundary = ?",
            (approval_id, boundary.model_dump_json()),
        )
        return ApprovalRecord.model_validate_json(row["body"]) if row else None

    async def find_request(self, request):
        row = await self.db.fetchone("SELECT body FROM test_approvals WHERE request_key = ?", (request_key(request),))
        return ApprovalRecord.model_validate_json(row["body"]) if row else None

    async def save(self, record, *, expected_version):
        if expected_version is None:
            await self.conn.execute(
                "INSERT INTO test_approvals VALUES (?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.request.boundary.model_dump_json(),
                    request_key(record.request),
                    record.version,
                    record.model_dump_json(),
                ),
            )
        else:
            old = await self.get(record.request.boundary, record.id)
            if old is None or old.request != record.request or (old.decision and old.decision != record.decision):
                raise ApprovalError("immutable_request_or_decision")
            if record.version != expected_version + 1:
                raise ApprovalError("version_conflict")
            cursor = await self.conn.execute(
                "UPDATE test_approvals SET version = ?, body = ? WHERE id = ? AND version = ?",
                (record.version, record.model_dump_json(), record.id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ApprovalError("version_conflict")
        self.fail("after_save")

    async def append(self, event, boundary):
        await self.conn.execute(
            "INSERT INTO test_approval_audit VALUES (?, ?, ?, ?)",
            (
                event.delivery_id,
                event.approval_id,
                event.version,
                event.model_dump_json(),
            ),
        )
        self.fail("after_audit")
        # Existing durable outbox reused in the fixture; this test-only kind has
        # NO production handler and is never passed to a running outbox worker.
        await CrewStore(self.db).enqueue_outbox(
            boundary.crew_id,
            "test_approval_notification",
            event.model_dump(mode="json"),
            dedupe_key=event.delivery_id,
        )
        self.fail("after_outbox")

    async def history(self, approval_id):
        rows = await self.db.fetchall(
            "SELECT body FROM test_approval_audit WHERE approval_id = ? ORDER BY version",
            (approval_id,),
        )
        return [json.loads(row["body"]) for row in rows]
