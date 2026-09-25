"""WP-1: MEMORY_TYPES gains ``decision`` (and keeps ``checkpoint``) with an atomic policy (spec §3.1).

Exercised through the production ``POST /api/v1/memories`` route over a real
SQLite database and a real MemoryService (the fixture from the AGT-5 tests,
whose extractor fails loudly if a hygiene type is ever fact-split).
"""

from __future__ import annotations

from typing import get_args

import pytest

from remembra.models.memory import MEMORY_TYPES, StoreRequest
from remembra.services.agent_session import MemoryTypePolicyError, apply_memory_type_policy
from remembra.storage.memory_rows import memory_from_row
from tests.test_agent_session_api import env  # noqa: F401  (pytest fixture)


def test_memory_types_include_decision_and_checkpoint() -> None:
    assert {"decision", "checkpoint"} <= set(get_args(MEMORY_TYPES))


def test_decision_policy_is_atomic_and_never_expires_by_default() -> None:
    request = StoreRequest(content="D-7 GCT rounding half-up per line", memory_type="decision")
    apply_memory_type_policy(request, "7d")
    assert request.skip_extraction is True
    assert request.ttl is None and request.expires_at is None

    checkpoint = StoreRequest(content="halfway", memory_type="checkpoint")
    apply_memory_type_policy(checkpoint, "7d")
    assert checkpoint.skip_extraction is True and checkpoint.ttl == "7d"

    with pytest.raises(MemoryTypePolicyError):
        apply_memory_type_policy(StoreRequest(content="x", memory_type="status"), "7d")


async def test_decision_is_stored_verbatim_as_one_memory(env) -> None:  # noqa: F811
    text = "D-7: GCT rounding is half-up per line item. Rationale: matches TAJ guidance. Confirmed by Mani."
    r = await env["client"].post("/api/v1/memories", json={"content": text, "project_id": "alpha", "memory_type": "decision"})
    assert r.status_code == 201, r.text
    assert r.json()["expires_at"] is None
    row = await env["db"].get_memory(r.json()["id"])
    assert row["memory_type"] == "decision"
    assert row["content"] == text  # one unit; the NoLLMExtractor would have raised on a split
    assert memory_from_row(row).memory_type == "decision"
    timeline = (await env["client"].get("/api/v1/timeline", params={"project_id": "alpha", "memory_type": "decision"})).json()
    assert timeline["total"] == 1


async def test_unknown_memory_type_is_still_rejected(env) -> None:  # noqa: F811
    r = await env["client"].post("/api/v1/memories", json={"content": "x", "project_id": "alpha", "memory_type": "verdict"})
    assert r.status_code == 422
