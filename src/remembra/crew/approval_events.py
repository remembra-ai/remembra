"""Unregistered closed approval events and atomic durable notification mapping.

No event type or outbox handler is installed by importing this module. The
synthetic harness explicitly supplies the proposed event spec. Production
registration, recipient delivery and host resumption require separate review.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Final

from remembra.crew import schemas
from remembra.crew.approval_contracts import ApprovalRecord, AuditEvent
from remembra.crew.approvals import ApprovalError
from remembra.crew.db import CrewDatabase
from remembra.crew.events import Actor, EventTx, format_ts, load_session_actor
from remembra.crew.store import CrewStore

EVENT_TYPE: Final = "approval.transition"
NOTIFICATION_KIND: Final = "approval_notification"
TRANSITIONS: Final = ("requested", "approved", "denied", "revoked", "expired", "stale_candidate", "blocked", "consumed")


def event_spec() -> schemas.EventSpec:
    """Return the proposal for explicit review/fixture registration, never register it."""
    return schemas.EventSpec(
        type=EVENT_TYPE,
        payload=schemas.Shape(
            "ApprovalTransition",
            {
                "approval_id": schemas.Field("str", max_len=200, pattern=re.compile(r"apr_[a-f0-9]{32}")),
                "version": schemas.Field("int", min_value=1),
                "request_digest": schemas.Field("str", pattern=re.compile(r"[a-f0-9]{64}")),
                "kind": schemas.Field("str", enum=TRANSITIONS),
                "actor_id": schemas.Field("str", max_len=200),
                "at_ms": schemas.Field("int", min_value=1),
            },
        ),
        client=False,
        release="L0",
    )


async def append_notification(db: CrewDatabase, tx: EventTx, record: ApprovalRecord, audit: AuditEvent) -> tuple[int, str]:
    """Append the real Crew chain and a reference-only outbox row in the caller's transaction."""
    if tx.conn is not db.conn or not db.in_transaction or not db.conn.in_transaction:
        raise ApprovalError("approval_event_transaction_required")
    registered = schemas.EVENT_SPECS.get(EVENT_TYPE)
    proposed = event_spec()
    if (
        registered is None
        or registered.client
        or registered.release != proposed.release
        or EVENT_TYPE not in schemas.ALWAYS_MOMENT_TYPES
        or schemas.to_json_schema(registered.payload) != schemas.to_json_schema(proposed.payload)
    ):
        raise ApprovalError("approval_event_schema_unavailable")
    boundary = record.request.boundary
    crew = await db.fetchone("SELECT owner_user_id, project_id FROM crews WHERE id = ?", (boundary.crew_id,))
    if crew is None or (crew["owner_user_id"], crew["project_id"]) != (boundary.tenant_id, boundary.project_id):
        raise ApprovalError("approval_boundary_changed")

    if audit.kind in ("expired", "stale_candidate"):
        actor = Actor.system()
    elif audit.kind in ("approved", "denied", "revoked"):
        actor = Actor.human(audit.actor_id)
        member = await db.fetchone(
            "SELECT 1 FROM crews c LEFT JOIN crew_members m ON m.crew_id=c.id AND m.user_id=? "
            "WHERE c.id=? AND (c.owner_user_id=? OR m.role IN ('owner','admin'))",
            (actor.id, boundary.crew_id, actor.id),
        )
        if member is None:
            raise ApprovalError("approval_actor_not_authorized")
    else:
        session = await load_session_actor(db.conn, boundary.crew_id, record.request.requester.session_id)
        if session is None or (session.user_id, session.agent_id) != (
            record.request.requester.user_id,
            record.request.requester.agent_id,
        ):
            raise ApprovalError("approval_actor_not_authorized")
        actor = session

    payload = audit.model_dump(mode="json")
    summary = f"Approval {audit.kind}"
    stamp = datetime.fromtimestamp(audit.at_ms / 1000, UTC)
    emitted = await tx.emit(
        crew_id=boundary.crew_id,
        type=EVENT_TYPE,
        actor=actor,
        payload=payload,
        summary=summary,
        idem_key=f"approval:{audit.delivery_id}",
        now=stamp,
    )
    envelope = emitted.envelope
    # CrewEventLog returns a matching key before validating its payload. Never
    # accept an unrelated or altered envelope merely because its key exists.
    expected = {
        "crew_id": boundary.crew_id,
        "project_id": boundary.project_id,
        "type": EVENT_TYPE,
        "v": 1,
        "ts": format_ts(stamp),
        "origin": "server",
        "severity": "info",
        "moment": True,
        "actor": actor.to_dict(),
        "payload": payload,
        "summary": summary,
        "refs": {},
    }
    if any(envelope.get(key) != value for key, value in expected.items()):
        raise ApprovalError("approval_event_conflict")
    notification: dict[str, Any] = {
        "crew_id": boundary.crew_id,
        "tenant_id": boundary.tenant_id,
        "project_id": boundary.project_id,
        "approval_id": record.id,
        "version": record.version,
        "request_digest": record.request.digest,
        "event_id": envelope["id"],
        "event_seq": envelope["seq"],
    }
    store = CrewStore(db)
    outbox_id = await store.enqueue_outbox(
        boundary.crew_id, NOTIFICATION_KIND, notification, dedupe_key=f"approval:{audit.delivery_id}"
    )
    queued = await store.get_outbox(outbox_id)
    try:
        equivalent = (
            queued is not None
            and queued["crew_id"] == boundary.crew_id
            and queued["kind"] == NOTIFICATION_KIND
            and json.loads(queued["payload"]) == notification
        )
    except (ValueError, TypeError):
        equivalent = False
    if not equivalent:
        raise ApprovalError("approval_notification_conflict")
    return int(envelope["seq"]), outbox_id
