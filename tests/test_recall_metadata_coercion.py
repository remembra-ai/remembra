"""Regression test for the recall-500 caused by string metadata.

Graph-retrieval fetch paths surface a memory's metadata as the raw JSON
string from SQLite. Passing that into RecallResult(metadata=...) raised a
pydantic ValidationError and 500'd the entire recall whenever a graph-matched
memory had non-empty metadata. `_coerce_metadata` normalizes it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from remembra.core.time import utcnow
from remembra.models.memory import RecallResult
from remembra.services.memory import _coerce_metadata


def test_coerce_dict_passthrough() -> None:
    assert _coerce_metadata({"a": 1}) == {"a": 1}


def test_coerce_json_string() -> None:
    assert _coerce_metadata('{"bench_item": "pref-001", "verified": true}') == {
        "bench_item": "pref-001",
        "verified": True,
    }


def test_coerce_none_and_empty() -> None:
    assert _coerce_metadata(None) == {}
    assert _coerce_metadata("") == {}
    assert _coerce_metadata("   ") == {}


def test_coerce_malformed_string() -> None:
    assert _coerce_metadata("{not valid json") == {}


def test_coerce_non_dict_json() -> None:
    # A JSON array is valid JSON but not a metadata dict
    assert _coerce_metadata("[1, 2, 3]") == {}


def test_recallresult_accepts_coerced_string_metadata() -> None:
    """The exact failure: string metadata must not break RecallResult."""
    raw = '{"bench_item": "pref-001", "verified": true}'
    base = dict(id="m1", relevance=1.0, content="x", created_at=utcnow())

    # Direct construction with the raw string metadata raises ValidationError...
    with pytest.raises(ValidationError):
        RecallResult(**base, metadata=raw)

    # ...but coercion makes it safe.
    result = RecallResult(**base, metadata=_coerce_metadata(raw))
    assert result.metadata == {"bench_item": "pref-001", "verified": True}
