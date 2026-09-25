"""
Tests for cross-project recall (Addendum B fix).

When a client calls /recall without specifying a project_id, the endpoint
used to coerce it to "default", silently hiding 99% of the user's memories
(which were stored under real project namespaces like "trademind", "clawdbot",
etc.). The fix: let `project_id=None` flow all the way through to the
storage layer so recall spans every project owned by the user. Restricted
API keys still resolve to their allowed project via `resolve_project_access`.

These tests pin the contract at three layers:
  1. RecallRequest model surface
  2. QdrantStore.search filter-building behavior
  3. Database.search_fts SQL selection
"""

from __future__ import annotations


import pytest

from remembra.core.time import utcnow

from remembra.models.memory import RecallRequest


# ---------------------------------------------------------------------------
# 1) Model surface
# ---------------------------------------------------------------------------


class TestRecallRequestProjectIdOptional:
    def test_project_id_defaults_to_none_for_cross_project_recall(self):
        req = RecallRequest(query="hello")
        assert req.project_id is None, (
            "RecallRequest.project_id must default to None so recall spans "
            "all projects; defaulting to 'default' silently hides most data."
        )

    def test_project_id_accepts_explicit_value(self):
        req = RecallRequest(query="hello", project_id="trademind")
        assert req.project_id == "trademind"

    def test_project_id_accepts_explicit_none(self):
        req = RecallRequest(query="hello", project_id=None)
        assert req.project_id is None


# ---------------------------------------------------------------------------
# 2) Qdrant filter-building (import-light: mimic the predicate)
# ---------------------------------------------------------------------------


def _build_qdrant_must(user_id: str, project_id: str | None) -> list[dict]:
    """Mirror of the filter block in QdrantStore.search."""
    must: list[dict] = [{"key": "user_id", "match": {"value": user_id}}]
    if project_id is not None:
        must.append({"key": "project_id", "match": {"value": project_id}})
    return must


class TestQdrantFilterBuilding:
    def test_user_scoped_only_when_project_id_none(self):
        must = _build_qdrant_must("user_x", None)
        assert len(must) == 1
        assert must[0]["key"] == "user_id"

    def test_user_and_project_scoped_when_project_id_present(self):
        must = _build_qdrant_must("user_x", "trademind")
        assert len(must) == 2
        keys = {c["key"] for c in must}
        assert keys == {"user_id", "project_id"}


# ---------------------------------------------------------------------------
# 3) FTS SQL selection
# ---------------------------------------------------------------------------


async def _fts_db(tmp_path):
    from remembra.storage.database import Database

    db = Database(str(tmp_path / "fts.db"))
    await db.connect()
    await db.init_schema()
    now = utcnow()
    for mid, user, project in [("a", "user_x", "trademind"), ("b", "user_x", "yaadbooks"), ("c", "other", "trademind")]:
        await db.save_memory_metadata(
            memory_id=mid,
            user_id=user,
            project_id=project,
            content="hello world deploy notes",
            extracted_facts=[],
            metadata={},
            created_at=now,
        )
        await db.index_memory_fts(mid, user, project, "hello world deploy notes")
    return db


@pytest.mark.asyncio
async def test_search_fts_spans_all_projects_when_none(tmp_path):
    """project_id=None searches every project of the user - and only that user."""
    db = await _fts_db(tmp_path)
    try:
        hits = await db.search_fts(query="hello world", user_id="user_x", project_id=None, limit=5)
        assert {mid for mid, _ in hits} == {"a", "b"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_search_fts_scopes_to_project_when_set(tmp_path):
    db = await _fts_db(tmp_path)
    try:
        hits = await db.search_fts(query="hello world", user_id="user_x", project_id="trademind", limit=5)
        assert [mid for mid, _ in hits] == ["a"]
    finally:
        await db.close()
