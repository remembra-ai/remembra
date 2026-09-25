"""Tests for filter-only recall reliability.

Filter-only recall is used when callers omit ``query`` but provide ``filters``.
This should reliably find matches even when they are not among the most recent
N memories.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from remembra.config import Settings
from remembra.core.time import utcnow
from remembra.models.memory import RecallRequest
from remembra.retrieval.ranking import RelevanceRanker
from remembra.services.memory import MemoryService


class _FakeDB:
    def __init__(self) -> None:
        now = utcnow()
        # Two "pages" of results returned by list_memories. The first page has
        # no matches; the second page contains the matches.
        self._pages: dict[int, list[dict]] = {
            0: [
                {
                    "id": "m0",
                    "content": "newest non-match",
                    "metadata": '{"project":"other"}',
                    "created_at": (now - timedelta(minutes=1)).isoformat(),
                },
            ],
            200: [
                {
                    "id": "m1",
                    "content": "older match 1",
                    "metadata": '{"project":"trademind"}',
                    "created_at": (now - timedelta(days=7)).isoformat(),
                },
                {
                    "id": "m2",
                    "content": "older match 2",
                    "metadata": '{"project":"trademind"}',
                    "created_at": (now - timedelta(days=8)).isoformat(),
                },
            ],
        }

    async def list_memories(
        self,
        user_id: str,
        project_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        return list(self._pages.get(offset, []))


@pytest.mark.asyncio
async def test_filter_only_recall_scans_past_first_page():
    svc = MemoryService.__new__(MemoryService)
    svc.db = _FakeDB()
    svc.settings = Settings(openai_api_key="t")
    svc.relevance_ranker = RelevanceRanker()

    req = RecallRequest(
        user_id="u1",
        project_id=None,
        query=None,
        filters={"project": "trademind"},
        limit=2,
    )

    resp = await MemoryService._recall_by_filters(svc, req, max_tokens=2000, slim_mode=False)
    assert [m.id for m in resp.memories] == ["m1", "m2"]
    assert "older match 1" in resp.context
    assert "older match 2" in resp.context
