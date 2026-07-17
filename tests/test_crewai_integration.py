"""Regression tests for the CrewAI storage adapter.

Guards the fixes for two data/behavior bugs:
- reset() used to call forget(user_id=...), wiping the user's ENTIRE memory
  (all crew memory types + anything else) when one store was reset.
- search() didn't filter by memory type, so short-term/long-term/entity
  stores (which share a namespace) cross-contaminated each other's results.
Plus: each item is stored atomically (skip_extraction) so distinct items are
never fact-split or consolidated away.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from remembra.integrations.crewai import RemembraStorage


@dataclass
class _Item:
    id: str
    content: str
    relevance: float = 0.9
    metadata: dict[str, Any] | None = None


class _FakeClient:
    """Records store calls and serves filter-aware recall/forget."""

    def __init__(self) -> None:
        self.user_id = "u1"
        self.rows: list[_Item] = []
        self.store_calls: list[dict[str, Any]] = []
        self.forgot: list[str] = []
        self._n = 0

    def store(self, content: str, metadata: dict | None = None, ttl: str | None = None, skip_extraction: bool = False) -> Any:
        self._n += 1
        mid = f"m{self._n}"
        self.store_calls.append({"content": content, "metadata": metadata, "ttl": ttl, "skip_extraction": skip_extraction})
        self.rows.append(_Item(id=mid, content=content, metadata=dict(metadata or {})))
        return _Item(id=mid, content=content)

    def recall(self, query: str | None = None, limit: int = 5, threshold: float = 0.7, filters: dict | None = None) -> Any:
        items = self.rows
        if filters:
            items = [r for r in items if all((r.metadata or {}).get(k) == v for k, v in filters.items())]

        class _R:
            memories = items[:limit]

        return _R()

    def forget(self, memory_id: str | None = None, **_: Any) -> Any:
        if memory_id:
            self.forgot.append(memory_id)
            self.rows = [r for r in self.rows if r.id != memory_id]

        class _F:
            deleted_memories = 1

        return _F()


def _storage(fake: _FakeClient, mem_type: str) -> RemembraStorage:
    s = RemembraStorage.__new__(RemembraStorage)
    s._client = fake
    s._type = mem_type
    return s


def test_save_is_atomic_and_tagged() -> None:
    fake = _FakeClient()
    st = _storage(fake, "short_term")
    st.save("The agent scheduled a meeting")
    call = fake.store_calls[0]
    assert call["skip_extraction"] is True, "CrewAI items must be stored atomically"
    assert call["metadata"]["memory_type"] == "short_term"
    assert call["ttl"] == "24h", "short-term memories expire after 24h"


def test_search_is_type_isolated() -> None:
    fake = _FakeClient()
    st, lt = _storage(fake, "short_term"), _storage(fake, "long_term")
    st.save("meeting on Tuesday")
    lt.save("task result: 5 competitors")

    st_hits = st.search("anything")
    lt_hits = lt.search("anything")
    assert {h["metadata"]["memory_type"] for h in st_hits} == {"short_term"}
    assert {h["metadata"]["memory_type"] for h in lt_hits} == {"long_term"}
    assert len(st_hits) == 1 and len(lt_hits) == 1


def test_reset_is_scoped_not_user_wide() -> None:
    fake = _FakeClient()
    st, lt = _storage(fake, "short_term"), _storage(fake, "long_term")
    st.save("meeting on Tuesday")
    lt.save("task result: 5 competitors")

    st.reset()

    # Only the short-term memory was forgotten; long-term survives.
    assert len(st.search("x")) == 0
    assert len(lt.search("x")) == 1
    # And it deleted by id, never by user_id (the data-loss path).
    assert fake.forgot, "reset must delete the session's memories by id"
