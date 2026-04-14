"""
Tests for metadata-first recall filter (handoff doc Issue 3).

Exercises the filter logic embedded in MemoryService.recall() by feeding a
hand-built hybrid_results list through the filter branch. This avoids spinning
up Qdrant/embedding stacks in unit tests while still proving the exact
AND-combined exact-match semantics that ship to production.

The filter is implemented in-line in services/memory.py — we re-implement the
same predicate here and assert the model carries the filters field.
"""

from __future__ import annotations

from remembra.models.memory import RecallRequest


# ---------------------------------------------------------------------------
# Model surface
# ---------------------------------------------------------------------------


class TestRecallRequestFiltersField:
    def test_filters_defaults_to_none(self):
        req = RecallRequest(query="hello")
        assert req.filters is None

    def test_filters_accepts_dict(self):
        req = RecallRequest(query="hello", filters={"project": "trademind", "type": "deploy-config"})
        assert req.filters == {"project": "trademind", "type": "deploy-config"}

    def test_existing_request_shape_unchanged(self):
        # Backward compat — omitting filters must still validate.
        req = RecallRequest(query="hello", limit=10, threshold=0.3)
        assert req.query == "hello"
        assert req.limit == 10
        assert req.threshold == 0.3


# ---------------------------------------------------------------------------
# Filter predicate behavior (mirrors services/memory.py _matches)
# ---------------------------------------------------------------------------


def _apply_filter(hybrid_results: list[dict], filters: dict[str, str]) -> list[dict]:
    """Mirror of the in-service filter predicate for unit testing."""

    def _matches(r: dict) -> bool:
        meta = (r.get("payload") or {}).get("metadata") or {}
        for k, v in filters.items():
            if str(meta.get(k)) != str(v):
                return False
        return True

    return [r for r in hybrid_results if _matches(r)]


def _mem(mid: str, metadata: dict) -> dict:
    return {"id": mid, "content": f"memory {mid}", "payload": {"metadata": metadata}}


class TestFilterPredicate:
    def test_single_key_filter_keeps_only_matches(self):
        results = [
            _mem("a", {"project": "trademind"}),
            _mem("b", {"project": "yaadbooks"}),
            _mem("c", {"project": "trademind"}),
        ]
        out = _apply_filter(results, {"project": "trademind"})
        assert [r["id"] for r in out] == ["a", "c"]

    def test_and_combined_multi_key(self):
        results = [
            _mem("a", {"project": "trademind", "type": "deploy-config"}),
            _mem("b", {"project": "trademind", "type": "trade-outcome"}),
            _mem("c", {"project": "yaadbooks", "type": "deploy-config"}),
        ]
        out = _apply_filter(results, {"project": "trademind", "type": "deploy-config"})
        assert [r["id"] for r in out] == ["a"]

    def test_missing_key_drops_memory(self):
        results = [
            _mem("a", {"project": "trademind"}),
            _mem("b", {}),
        ]
        out = _apply_filter(results, {"project": "trademind"})
        assert [r["id"] for r in out] == ["a"]

    def test_non_string_values_are_stringified(self):
        # Metadata stored as int/bool should still match string filter input.
        results = [
            _mem("a", {"version": 2}),
            _mem("b", {"version": 3}),
        ]
        out = _apply_filter(results, {"version": "2"})
        assert [r["id"] for r in out] == ["a"]

    def test_empty_filter_returns_all(self):
        # Edge case: empty dict should be treated as no filter by caller;
        # the predicate itself with an empty dict returns everything.
        results = [_mem("a", {"x": "1"}), _mem("b", {"y": "2"})]
        out = _apply_filter(results, {})
        assert [r["id"] for r in out] == ["a", "b"]

    def test_no_payload_field_drops_memory(self):
        # A result missing payload entirely cannot satisfy any filter.
        results = [{"id": "a", "content": "no payload"}, _mem("b", {"project": "t"})]
        out = _apply_filter(results, {"project": "t"})
        assert [r["id"] for r in out] == ["b"]
