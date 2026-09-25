"""Rebuild a :class:`Memory` (for Qdrant upserts) from its SQLite row.

SQLite is the source of truth; Qdrant payloads are derived. The pending
embedding worker, the reindex rebuild, and reconcile repairs all need the
full Memory (content, facts, metadata, timestamps, entity refs) to write a
complete payload via ``QdrantStore.upsert``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, get_args

from remembra.core.time import utcnow
from remembra.models.memory import MEMORY_TYPES, EntityRef, Memory


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, list | dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


_VALID_TYPES = set(get_args(MEMORY_TYPES))


def is_source_row(row: dict[str, Any]) -> bool:
    """Verbatim source records live in SQLite only (no vector, no FTS)."""
    if row.get("memory_type") == "source":
        return True
    metadata = _parse_json(row.get("metadata"), {})
    return bool(metadata.get("record_kind") == "source")


def memory_from_row(row: dict[str, Any], entities: list[EntityRef] | None = None) -> Memory:
    """Build a Memory from a ``memories`` row dict (``Database.get_memory``)."""
    created_at = _parse_dt(row.get("created_at")) or utcnow()
    memory_type = row.get("memory_type")
    return Memory(
        id=row["id"],
        user_id=row["user_id"],
        project_id=row.get("project_id") or "default",
        content=row["content"],
        memory_type=memory_type if memory_type in _VALID_TYPES else None,
        scope=row.get("scope"),
        supersedes=row.get("supersedes"),
        contradicts=row.get("contradicts"),
        extracted_facts=[f for f in _parse_json(row.get("extracted_facts"), []) if isinstance(f, str)],
        entities=list(entities or []),
        metadata=_parse_json(row.get("metadata"), {}),
        created_at=created_at,
        updated_at=_parse_dt(row.get("updated_at")) or created_at,
        expires_at=_parse_dt(row.get("expires_at")),
        access_count=int(row.get("access_count") or 0),
        last_accessed=_parse_dt(row.get("last_accessed")),
        valid_from=_parse_dt(row.get("valid_from")) or created_at,
        valid_to=_parse_dt(row.get("valid_to")),
    )
