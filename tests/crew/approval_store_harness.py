"""Temporary schema/event registration for isolated approval integration tests only."""

from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Final

import pytest
from tests.crew.test_approvals import Harness
from tests.crew.test_approvals import h as _base_h

from remembra.crew import schemas
from remembra.crew.approval_events import EVENT_TYPE, event_spec
from remembra.crew.approval_store import SQLiteApprovalStore, SQLiteApprovalTransaction
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.events import CrewEventLog, EventTx
from remembra.crew.store import CrewStore

# Temporary proposal only; production modules contain no installable DDL.
SCHEMA_OBJECTS: Final = MappingProxyType(
    {
        "crew_approval_records": """CREATE TABLE crew_approval_records (
            id TEXT PRIMARY KEY,
            crew_id TEXT NOT NULL REFERENCES crews(id),
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
            body TEXT NOT NULL CHECK(json_valid(body)),
            CHECK(json_extract(body, '$.id') IS id),
            CHECK(json_extract(body, '$.version') IS version),
            CHECK(json_extract(body, '$.request.boundary.crew_id') IS crew_id),
            CHECK(json_extract(body, '$.request.boundary.tenant_id') IS tenant_id),
            CHECK(json_extract(body, '$.request.boundary.project_id') IS project_id)
        )""",
        "crew_approval_versions": """CREATE TABLE crew_approval_versions (
            approval_id TEXT NOT NULL REFERENCES crew_approval_records(id),
            version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0),
            crew_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            outbox_id TEXT NOT NULL UNIQUE REFERENCES crew_outbox(id),
            body TEXT NOT NULL CHECK(json_valid(body)),
            PRIMARY KEY(approval_id, version),
            UNIQUE(crew_id, event_seq),
            FOREIGN KEY(crew_id, event_seq) REFERENCES crew_events(crew_id, seq),
            CHECK(json_extract(body, '$.id') IS approval_id),
            CHECK(json_extract(body, '$.version') IS version)
        )""",
        "crew_approval_records_immutable": """CREATE TRIGGER crew_approval_records_immutable
            BEFORE UPDATE ON crew_approval_records
            WHEN NEW.id IS NOT OLD.id OR NEW.crew_id IS NOT OLD.crew_id
              OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.project_id IS NOT OLD.project_id
              OR NEW.request_key IS NOT OLD.request_key OR NEW.request_digest IS NOT OLD.request_digest
              OR NEW.version != OLD.version + 1
              OR json_extract(NEW.body, '$.request') IS NOT json_extract(OLD.body, '$.request')
              OR (json_extract(OLD.body, '$.decision') IS NOT NULL AND
                  json_extract(NEW.body, '$.decision') IS NOT json_extract(OLD.body, '$.decision'))
              OR json_extract(OLD.body, '$.invalidation') IS NOT NULL
              OR json_extract(OLD.body, '$.consumption') IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'immutable approval'); END""",
        "crew_approval_versions_no_update": """CREATE TRIGGER crew_approval_versions_no_update
            BEFORE UPDATE ON crew_approval_versions
            BEGIN SELECT RAISE(ABORT, 'immutable approval history'); END""",
        "crew_approval_versions_no_delete": """CREATE TRIGGER crew_approval_versions_no_delete
            BEFORE DELETE ON crew_approval_versions
            BEGIN SELECT RAISE(ABORT, 'immutable approval history'); END""",
    }
)

APPROVAL_DDL: Final = tuple(SCHEMA_OBJECTS.values())


base_h = _base_h


@pytest.fixture
async def h(base_h, monkeypatch):
    # Test database only. No production migration or event registry is changed.
    async with base_h.db.transaction():
        for sql in APPROVAL_DDL:
            await base_h.db.conn.execute(sql)
        # The contract-only fixture predates real event validation. Use a
        # schema-valid synthetic callsign; production actor rules stay intact.
        await base_h.db.conn.execute("UPDATE crew_sessions SET callsign='worker-1' WHERE crew_id=?", (base_h.crew_id,))
    monkeypatch.setattr(schemas, "EVENT_SPECS", {**schemas.EVENT_SPECS, EVENT_TYPE: event_spec()})
    monkeypatch.setattr(schemas, "ALWAYS_MOMENT_TYPES", schemas.ALWAYS_MOMENT_TYPES | {EVENT_TYPE})
    bus = CrewBus(loader=db_loader(base_h.db))
    seen = []

    def observe(event):
        assert not base_h.db.in_transaction
        seen.append(event)

    bus.subscribe(observe)
    log = CrewEventLog(base_h.db, bus)
    store = SQLiteApprovalStore(base_h.db, event_log=log)
    harness = Harness(base_h.db, base_h.crew_id, store, base_h.path)
    harness.bus, harness.seen, harness.log = bus, seen, log
    yield harness


def inject_failure(monkeypatch, h, phase, exception):
    """Inject transport loss/cancellation without adding production fault hooks."""
    if phase in ("before_commit", "after_commit"):
        original = h.db.transaction

        @asynccontextmanager
        async def transaction():
            outer = not h.db.in_transaction
            async with original():
                yield
                if outer and phase == "before_commit":
                    raise exception
            if outer and phase == "after_commit":
                raise exception

        monkeypatch.setattr(h.db, "transaction", transaction)
        return
    cls, method = {
        "after_save": (SQLiteApprovalTransaction, "save"),
        "after_event": (EventTx, "emit"),
        "after_outbox": (CrewStore, "enqueue_outbox"),
        "after_version": (SQLiteApprovalTransaction, "append"),
    }[phase]
    original = getattr(cls, method)

    async def broken(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise exception

    monkeypatch.setattr(cls, method, broken)
