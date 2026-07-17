"""LangGraph integration for Remembra.

Provides RemembraStore, an implementation of LangGraph's ``BaseStore`` — the
long-term, cross-thread memory store — backed by Remembra. Drop it into a
graph so agents can put/get/search durable memories across threads and runs,
with semantic search and namespace scoping.

Usage:
    from langgraph.graph import StateGraph
    from remembra.integrations.langgraph import RemembraStore

    store = RemembraStore(
        base_url="https://api.remembra.dev",
        api_key="rem_...",
        user_id="user_42",
    )

    store.put(("users", "alice", "memories"), "food", {"note": "loves pizza"})
    hits = store.search(("users", "alice"), query="what food does she like?")

    graph = builder.compile(store=store)   # tools get `store` injected

Requires: pip install remembra langgraph
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from remembra.client.memory import Memory, MemoryError

try:  # langgraph is optional at import time; required to actually run a graph
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        PutOp,
        SearchItem,
        SearchOp,
    )

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    BaseStore = object  # type: ignore[assignment, misc]
    _SDK_AVAILABLE = False


# A marker on every store item so we can enumerate all of them (recall requires
# a query or filter; this gives us a "match everything in this store" filter).
_STORE_MARKER = {"lg_store": "1"}


def _value_text(value: dict[str, Any]) -> str:
    """Flatten a value dict into searchable text (for semantic recall)."""
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    text = " ".join(p for p in parts if p.strip())
    return text or json.dumps(value)


class RemembraStore(BaseStore):
    """LangGraph BaseStore backed by Remembra (long-term cross-thread memory).

    Each (namespace, key) pair is one Remembra memory, stored atomically. The
    namespace tuple is recorded both whole (for exact get) and per-level (for
    prefix search). Values are JSON-preserved for faithful reconstruction and
    flattened into searchable text for semantic recall.

    Args:
        base_url: Remembra server URL.
        api_key: API key (omit for a local, auth-disabled server).
        user_id: User ID for memory isolation.
        project: Project namespace.
        ttl: Optional default TTL for stored items (e.g. "30d").
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        api_key: str | None = None,
        user_id: str = "default",
        project: str = "default",
        ttl: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = Memory(
            base_url=base_url,
            api_key=api_key,
            user_id=user_id,
            project=project,
            timeout=timeout,
        )
        self._ttl = ttl

    # -- storage helpers -------------------------------------------------

    @staticmethod
    def _ns_meta(namespace: tuple[str, ...]) -> dict[str, Any]:
        meta: dict[str, Any] = {"lg_ns": json.dumps(list(namespace)), "lg_ns_len": len(namespace)}
        for i, part in enumerate(namespace):
            meta[f"lg_ns_{i}"] = part
        return meta

    @staticmethod
    def _prefix_filter(prefix: tuple[str, ...]) -> dict[str, Any]:
        # Match any namespace that STARTS WITH prefix, via exact-match on each
        # prefix position. (Remembra filters are exact-match; positional keys
        # turn a prefix match into a set of exact matches.)
        return {f"lg_ns_{i}": part for i, part in enumerate(prefix)}

    def _find(self, namespace: tuple[str, ...], key: str) -> Any | None:
        try:
            res = self._client.recall(filters={"lg_ns": json.dumps(list(namespace)), "lg_key": key}, limit=1)
        except MemoryError:
            return None
        return res.memories[0] if res.memories else None

    def _to_item(self, memory: Any, *, search: bool = False) -> Any:
        md = memory.metadata or {}
        namespace = tuple(json.loads(md.get("lg_ns", "[]")))
        value = json.loads(md.get("lg_value", "{}"))
        common = {
            "namespace": namespace,
            "key": md.get("lg_key", ""),
            "value": value,
            "created_at": memory.created_at,
            "updated_at": memory.created_at,
        }
        if search:
            return SearchItem(**common, score=getattr(memory, "relevance", None))
        return Item(**common)

    # -- op handlers -----------------------------------------------------

    def _do_put(self, op: Any) -> None:
        namespace, key = tuple(op.namespace), str(op.key)
        # Upsert semantics: remove any existing (namespace, key) first.
        existing = self._find(namespace, key)
        if existing is not None:
            with contextlib.suppress(MemoryError):
                self._client.forget(memory_id=existing.id)
        if op.value is None:
            return  # value=None is a delete; nothing more to store
        metadata = {**_STORE_MARKER, **self._ns_meta(namespace), "lg_key": key, "lg_value": json.dumps(op.value)}
        with contextlib.suppress(MemoryError):
            self._client.store(
                content=_value_text(op.value),
                metadata=metadata,
                ttl=op.ttl if getattr(op, "ttl", None) else self._ttl,
                skip_extraction=True,
            )

    def _do_get(self, op: Any) -> Any | None:
        memory = self._find(tuple(op.namespace), str(op.key))
        return self._to_item(memory) if memory is not None else None

    def _do_search(self, op: Any) -> list[Any]:
        prefix = tuple(op.namespace_prefix)
        # Always include the store marker so a query never leaks the user's
        # non-store Remembra memories; add per-level prefix matching on top.
        filters = {**_STORE_MARKER, **self._prefix_filter(prefix)}
        want = (op.offset or 0) + (op.limit or 10)
        try:
            if op.query:
                res = self._client.recall(query=op.query, filters=filters, limit=min(want, 50), threshold=0.0)
            else:
                res = self._client.recall(filters=filters, limit=min(want, 50))
        except MemoryError:
            return []
        items = [self._to_item(m, search=True) for m in res.memories]
        # Value-field filter (op.filter matches keys in the stored value dict).
        if op.filter:
            items = [it for it in items if all(it.value.get(k) == v for k, v in op.filter.items())]
        return items[op.offset or 0 : (op.offset or 0) + (op.limit or 10)]

    def _do_list_namespaces(self, op: Any) -> list[tuple[str, ...]]:
        try:
            res = self._client.recall(filters=_STORE_MARKER, limit=50)
        except MemoryError:
            return []
        seen: list[tuple[str, ...]] = []
        for m in res.memories:
            ns = tuple(json.loads((m.metadata or {}).get("lg_ns", "[]")))
            if op.max_depth is not None:
                ns = ns[: op.max_depth]
            if not self._ns_matches(ns, op.match_conditions):
                continue
            if ns not in seen:
                seen.append(ns)
        return seen[op.offset or 0 : (op.offset or 0) + (op.limit or 100)]

    @staticmethod
    def _ns_matches(ns: tuple[str, ...], conditions: Any) -> bool:
        for cond in conditions or []:
            path = tuple(cond.path)
            if cond.match_type == "prefix" and ns[: len(path)] != path:
                return False
            if cond.match_type == "suffix" and ns[-len(path) :] != path:
                return False
        return True

    # -- BaseStore interface ---------------------------------------------

    def batch(self, ops: Any) -> list[Any]:
        results: list[Any] = []
        for op in ops:
            if isinstance(op, PutOp):
                self._do_put(op)
                results.append(None)
            elif isinstance(op, GetOp):
                results.append(self._do_get(op))
            elif isinstance(op, SearchOp):
                results.append(self._do_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._do_list_namespaces(op))
            else:  # pragma: no cover - defensive
                results.append(None)
        return results

    async def abatch(self, ops: Any) -> list[Any]:
        return await asyncio.to_thread(self.batch, list(ops))
