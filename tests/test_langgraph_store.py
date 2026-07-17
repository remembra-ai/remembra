"""Tests for the LangGraph BaseStore adapter (RemembraStore).

Pure namespace/value helpers run everywhere; the op-handler tests run only
when langgraph is installed (they need its Op/Item types), using a fake client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from remembra.integrations.langgraph import RemembraStore, _value_text


# ---------------------------------------------------------------------------
# Pure helpers (no langgraph needed)
# ---------------------------------------------------------------------------


def test_value_text_flattens_strings() -> None:
    assert "loves pizza" in _value_text({"note": "loves pizza", "n": 3})
    assert "a b" in _value_text({"x": "a", "y": {"z": "b"}}) or "b a" in _value_text({"x": "a", "y": {"z": "b"}})


def test_value_text_falls_back_to_json() -> None:
    # No string content -> non-empty JSON fallback (content must never be empty)
    out = _value_text({"count": 5})
    assert out and out.strip()


def test_ns_meta_records_whole_and_per_level() -> None:
    meta = RemembraStore._ns_meta(("users", "alice", "memories"))
    assert meta["lg_ns"] == json.dumps(["users", "alice", "memories"])
    assert meta["lg_ns_0"] == "users" and meta["lg_ns_1"] == "alice" and meta["lg_ns_len"] == 3


def test_prefix_filter_is_positional() -> None:
    assert RemembraStore._prefix_filter(("users", "alice")) == {"lg_ns_0": "users", "lg_ns_1": "alice"}


# ---------------------------------------------------------------------------
# Op handlers (need langgraph)
# ---------------------------------------------------------------------------

lg = pytest.importorskip("langgraph.store.base")


@dataclass
class _Row:
    id: str
    content: str
    relevance: float = 0.9
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def created_at(self) -> str:
        return "2026-07-16T00:00:00"


class _FakeClient:
    def __init__(self) -> None:
        self.user_id = "u1"
        self.rows: list[_Row] = []
        self._n = 0

    def store(self, content: str, metadata: dict | None = None, ttl: str | None = None, skip_extraction: bool = False) -> Any:
        assert skip_extraction is True
        self._n += 1
        self.rows.append(_Row(id=f"m{self._n}", content=content, metadata=dict(metadata or {})))
        return _Row(id=f"m{self._n}", content=content)

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


def _store(fake: _FakeClient) -> RemembraStore:
    s = RemembraStore.__new__(RemembraStore)
    s._client = fake
    s._ttl = None
    return s


def test_put_get_roundtrip_and_upsert() -> None:
    fake = _FakeClient()
    s = _store(fake)
    s.put(("users", "alice"), "food", {"note": "loves pizza"})
    got = s.get(("users", "alice"), "food")
    assert got is not None and got.value == {"note": "loves pizza"}
    assert got.namespace == ("users", "alice") and got.key == "food"

    # upsert: same (namespace, key) replaces, never duplicates
    s.put(("users", "alice"), "food", {"note": "loves sushi now"})
    assert len([r for r in fake.rows if (r.metadata or {}).get("lg_key") == "food"]) == 1
    assert s.get(("users", "alice"), "food").value == {"note": "loves sushi now"}


def test_search_is_prefix_scoped() -> None:
    fake = _FakeClient()
    s = _store(fake)
    s.put(("users", "alice"), "a", {"note": "alice memory"})
    s.put(("users", "bob"), "b", {"note": "bob memory"})

    hits = s.search(("users", "alice"))
    assert {h.key for h in hits} == {"a"}  # bob excluded by prefix
    assert all(h.namespace == ("users", "alice") for h in hits)


def test_search_value_filter() -> None:
    fake = _FakeClient()
    s = _store(fake)
    s.put(("n",), "x", {"kind": "task", "v": 1})
    s.put(("n",), "y", {"kind": "note", "v": 2})
    hits = s.search(("n",), filter={"kind": "task"})
    assert [h.key for h in hits] == ["x"]


def test_delete_removes_only_that_key() -> None:
    fake = _FakeClient()
    s = _store(fake)
    s.put(("n",), "x", {"a": 1})
    s.put(("n",), "y", {"a": 2})
    s.delete(("n",), "x")
    assert s.get(("n",), "x") is None
    assert s.get(("n",), "y") is not None


def test_list_namespaces() -> None:
    fake = _FakeClient()
    s = _store(fake)
    s.put(("users", "alice"), "k", {"a": 1})
    s.put(("users", "bob"), "k", {"a": 2})
    s.put(("teams", "x"), "k", {"a": 3})
    namespaces = set(s.list_namespaces())
    assert namespaces == {("users", "alice"), ("users", "bob"), ("teams", "x")}
