"""
Webhook event type definitions.

Each event has a type string and a payload schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

MEMORY_STORED = "memory.stored"
MEMORY_RECALLED = "memory.recalled"
MEMORY_DELETED = "memory.deleted"
ENTITY_CREATED = "entity.created"
ENTITY_MERGED = "entity.merged"

ALL_EVENT_TYPES = [
    MEMORY_STORED,
    MEMORY_RECALLED,
    MEMORY_DELETED,
    ENTITY_CREATED,
    ENTITY_MERGED,
]


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass
class WebhookEvent:
    """A webhook event ready for delivery."""

    type: str
    payload: dict[str, Any]
    user_id: str
    project_id: str = "default"
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "data": self.payload,
        }


# ---------------------------------------------------------------------------
# Event factory helpers
# ---------------------------------------------------------------------------


def memory_stored_event(
    user_id: str,
    memory_id: str,
    extracted_facts: list[str] | None = None,
    entities: list[str] | None = None,
    project_id: str = "default",
) -> WebhookEvent:
    return WebhookEvent(
        type=MEMORY_STORED,
        user_id=user_id,
        project_id=project_id,
        payload={
            "memory_id": memory_id,
            "extracted_facts": extracted_facts or [],
            "entities": entities or [],
        },
    )


def memory_recalled_event(
    user_id: str,
    query: str,
    result_count: int,
    project_id: str = "default",
) -> WebhookEvent:
    return WebhookEvent(
        type=MEMORY_RECALLED,
        user_id=user_id,
        project_id=project_id,
        payload={
            "query": query,
            "result_count": result_count,
        },
    )


def memory_deleted_event(
    user_id: str,
    memory_id: str | None = None,
    deleted_count: int = 0,
    project_id: str = "default",
) -> WebhookEvent:
    return WebhookEvent(
        type=MEMORY_DELETED,
        user_id=user_id,
        project_id=project_id,
        payload={
            "memory_id": memory_id,
            "deleted_count": deleted_count,
        },
    )


def entity_created_event(
    user_id: str,
    entity_id: str,
    canonical_name: str,
    entity_type: str,
    project_id: str = "default",
) -> WebhookEvent:
    return WebhookEvent(
        type=ENTITY_CREATED,
        user_id=user_id,
        project_id=project_id,
        payload={
            "entity_id": entity_id,
            "canonical_name": canonical_name,
            "entity_type": entity_type,
        },
    )


def entity_merged_event(
    user_id: str,
    surviving_id: str,
    merged_id: str,
    canonical_name: str,
    project_id: str = "default",
) -> WebhookEvent:
    return WebhookEvent(
        type=ENTITY_MERGED,
        user_id=user_id,
        project_id=project_id,
        payload={
            "surviving_entity_id": surviving_id,
            "merged_entity_id": merged_id,
            "canonical_name": canonical_name,
        },
    )
