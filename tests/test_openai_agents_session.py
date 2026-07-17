"""Tests for the OpenAI Agents SDK Session adapter (RemembraSession).

Verifies the Session-protocol behavior against a fake client (no SDK or server
needed): items are stored atomically and in order, get_items honors limit,
pop_item removes the most recent, and clear_session is session-scoped (never
wipes the user's other memory).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from remembra.integrations.openai_agents import RemembraSession


@dataclass
class _Item:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class _FakeClient:
    def __init__(self) -> None:
        self.user_id = "u1"
        self.rows: list[_Item] = []
        self.store_calls: list[dict[str, Any]] = []
        self._n = 0

    def store(self, content: str, metadata: dict | None = None, ttl: str | None = None, skip_extraction: bool = False) -> Any:
        self._n += 1
        self.store_calls.append({"skip_extraction": skip_extraction, "metadata": metadata})
        self.rows.append(_Item(id=f"m{self._n}", content=content, metadata=dict(metadata or {})))
        return _Item(id=f"m{self._n}", content=content)

    def recall(self, query: str | None = None, limit: int = 50, threshold: float = 0.7, filters: dict | None = None) -> Any:
        items = self.rows
        if filters:
            items = [r for r in items if all((r.metadata or {}).get(k) == v for k, v in filters.items())]

        class _R:
            memories = items[:limit]

        return _R()

    def forget(self, memory_id: str | None = None, **_: Any) -> Any:
        if memory_id:
            self.rows = [r for r in self.rows if r.id != memory_id]

        class _F:
            deleted_memories = 1

        return _F()


def _session(fake: _FakeClient, sid: str) -> RemembraSession:
    s = RemembraSession.__new__(RemembraSession)
    s.session_id = sid
    s.session_settings = None
    s._client = fake
    s._ttl = None
    s._seq = None
    return s


@pytest.mark.asyncio
async def test_add_and_get_items_preserves_order_and_fidelity() -> None:
    fake = _FakeClient()
    s = _session(fake, "s1")
    items = [
        {"role": "user", "content": "My name is Ivy"},
        {"role": "assistant", "content": "Hello Ivy!"},
        {"role": "user", "content": "I live in Oslo"},
    ]
    await s.add_items(items)

    # all stored atomically
    assert all(c["skip_extraction"] is True for c in fake.store_calls)
    # full item preserved as JSON, sequence assigned in order
    seqs = [c["metadata"]["sequence"] for c in fake.store_calls]
    assert seqs == [1, 2, 3]

    got = await s.get_items()
    assert got == items  # exact round trip, in order


@pytest.mark.asyncio
async def test_get_items_limit_returns_latest_n() -> None:
    fake = _FakeClient()
    s = _session(fake, "s1")
    await s.add_items([{"role": "user", "content": str(i)} for i in range(5)])
    got = await s.get_items(limit=2)
    assert [i["content"] for i in got] == ["3", "4"]  # latest 2, chronological


@pytest.mark.asyncio
async def test_pop_item_removes_most_recent() -> None:
    fake = _FakeClient()
    s = _session(fake, "s1")
    await s.add_items([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    popped = await s.pop_item()
    assert popped == {"role": "user", "content": "b"}
    remaining = await s.get_items()
    assert [i["content"] for i in remaining] == ["a"]


@pytest.mark.asyncio
async def test_clear_session_is_scoped() -> None:
    fake = _FakeClient()
    s1 = _session(fake, "s1")
    s2 = _session(fake, "s2")
    await s1.add_items([{"role": "user", "content": "one"}])
    await s2.add_items([{"role": "user", "content": "two"}])

    await s1.clear_session()

    assert await s1.get_items() == []
    assert [i["content"] for i in await s2.get_items()] == ["two"]  # other session survives


@pytest.mark.asyncio
async def test_sequence_seeds_from_existing_for_restart_safety() -> None:
    # A fresh session instance over pre-existing items must continue the
    # sequence, not restart at 1 (which would corrupt ordering).
    fake = _FakeClient()
    fake.rows.append(
        _Item(
            id="m0",
            content="[user] old",
            metadata={
                "session_id": "s1",
                "sequence": 7,
                "agent_item": json.dumps({"role": "user", "content": "old"}),
            },
        )
    )
    s = _session(fake, "s1")
    await s.add_items([{"role": "user", "content": "new"}])
    new_seq = fake.store_calls[-1]["metadata"]["sequence"]
    assert new_seq == 8
