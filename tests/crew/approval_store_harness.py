"""Temporary schema/event registration for isolated approval integration tests only."""

from contextlib import asynccontextmanager

import pytest
from tests.crew.test_approvals import Harness
from tests.crew.test_approvals import h as _base_h

from remembra.crew import schemas
from remembra.crew.approval_events import EVENT_TYPE, event_spec
from remembra.crew.approval_schema import APPROVAL_DDL
from remembra.crew.approval_store import SQLiteApprovalStore, SQLiteApprovalTransaction
from remembra.crew.bus import CrewBus, db_loader
from remembra.crew.events import CrewEventLog, EventTx
from remembra.crew.store import CrewStore

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
