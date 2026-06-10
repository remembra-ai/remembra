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

from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.mark.asyncio
async def test_search_fts_omits_project_clause_when_none():
    """When project_id is None, the SQL must NOT include 'AND project_id = ?'."""
    from remembra.storage.database import Database

    # Build a bare Database with a mocked aiosqlite connection
    db = Database.__new__(Database)
    conn = MagicMock()
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=cursor)
    db._connection = conn

    await db.search_fts(query="hello world", user_id="user_x", project_id=None, limit=5)

    sql = conn.execute.call_args.args[0]
    params = conn.execute.call_args.args[1]

    assert "project_id" not in sql, (
        "FTS query must omit project_id clause when project_id is None so recall spans all of the user's projects."
    )
    # The query is sanitized into a safe FTS5 MATCH expression before binding.
    from remembra.storage.database import _build_fts_match_query

    assert params == ("user_x", _build_fts_match_query("hello world"), 5)


@pytest.mark.asyncio
async def test_search_fts_includes_project_clause_when_set():
    from remembra.storage.database import Database

    db = Database.__new__(Database)
    conn = MagicMock()
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=cursor)
    db._connection = conn

    await db.search_fts(
        query="hello world",
        user_id="user_x",
        project_id="trademind",
        limit=5,
    )

    sql = conn.execute.call_args.args[0]
    params = conn.execute.call_args.args[1]

    assert "project_id = ?" in sql
    from remembra.storage.database import _build_fts_match_query

    assert params == ("user_x", "trademind", _build_fts_match_query("hello world"), 5)
