"""Regression test: memory_type and scope must surface from their table columns.

Source records (and any typed/scoped memory) are stored with memory_type and
scope as first-class SQLite columns. The serializer previously read them from
the metadata dict, so they surfaced as null over the API — making a verbatim
source record indistinguishable from a derived fact. This locks the behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from remembra.config import Settings
from remembra.services.memory import MemoryService


class _StubDB:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        return self._row if memory_id == self._row["id"] else None

    async def get_memory_entities(self, memory_id: str) -> list[Any]:
        return []


def _service(row: dict[str, Any]) -> MemoryService:
    svc = MemoryService.__new__(MemoryService)  # skip heavy __init__
    svc.settings = Settings(openai_api_key="test")
    svc.db = _StubDB(row)
    return svc


@pytest.mark.asyncio
async def test_memory_type_column_surfaces() -> None:
    row = {
        "id": "s1",
        "user_id": "u1",
        "project_id": "p",
        "content": "The exact original text.",
        "memory_type": "source",  # stored as a column
        "scope": "work:acme",
        "metadata": '{"record_kind": "source"}',  # no memory_type inside
    }
    out = await _service(row)._serialize_memory_record(row)
    assert out["memory_type"] == "source"
    assert out["scope"] == "work:acme"


@pytest.mark.asyncio
async def test_metadata_fallback_for_legacy_records() -> None:
    # Older records that stored the type inside metadata still resolve.
    row = {
        "id": "s2",
        "user_id": "u1",
        "content": "x",
        "memory_type": None,
        "metadata": {"memory_type": "fact", "scope": "personal"},
    }
    out = await _service(row)._serialize_memory_record(row)
    assert out["memory_type"] == "fact"
    assert out["scope"] == "personal"


@pytest.mark.asyncio
async def test_plain_memory_has_null_type() -> None:
    row = {"id": "s3", "user_id": "u1", "content": "x", "memory_type": None, "metadata": {}}
    out = await _service(row)._serialize_memory_record(row)
    assert out["memory_type"] is None
    assert out["scope"] is None
