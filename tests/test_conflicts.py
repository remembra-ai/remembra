"""Tests for the ConflictManager — CRUD, resolution, and statistics."""

import pytest

from remembra.extraction.conflicts import (
    ConflictManager,
    ConflictStatus,
    ConflictStrategy,
    MemoryConflict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def conflict_manager(in_memory_db):
    """ConflictManager backed by a real in-memory SQLite database."""
    mgr = ConflictManager(in_memory_db)
    await mgr.init_schema()
    return mgr


def _make_conflict(**overrides) -> MemoryConflict:
    """Helper to create a MemoryConflict with sensible defaults."""
    defaults = dict(
        user_id="user-1",
        project_id="default",
        new_fact="Alice is CTO of Acme",
        existing_memory_id="mem-old-1",
        existing_content="Alice is CEO of Acme",
        similarity_score=0.92,
        reason="Role contradiction",
        strategy_applied=ConflictStrategy.UPDATE,
    )
    defaults.update(overrides)
    return MemoryConflict(**defaults)


# ---------------------------------------------------------------------------
# MemoryConflict dataclass
# ---------------------------------------------------------------------------


class TestMemoryConflict:
    def test_defaults(self):
        c = MemoryConflict()
        assert c.user_id == ""
        assert c.status == ConflictStatus.OPEN
        assert c.strategy_applied == ConflictStrategy.UPDATE
        assert c.resolved_memory_id is None
        assert c.id  # auto-generated UUID

    def test_to_dict(self):
        c = _make_conflict()
        d = c.to_dict()
        assert d["user_id"] == "user-1"
        assert d["strategy_applied"] == "update"
        assert d["status"] == "open"
        assert d["new_fact"] == "Alice is CTO of Acme"
        assert d["existing_content"] == "Alice is CEO of Acme"


# ---------------------------------------------------------------------------
# ConflictManager — record + query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConflictManagerCRUD:
    async def test_record_and_get(self, conflict_manager):
        conflict = _make_conflict()
        recorded = await conflict_manager.record(conflict)
        assert recorded.id == conflict.id

        fetched = await conflict_manager.get_conflict(conflict.id, "user-1")
        assert fetched is not None
        assert fetched["new_fact"] == "Alice is CTO of Acme"
        assert fetched["status"] == "open"

    async def test_get_conflict_wrong_user(self, conflict_manager):
        conflict = _make_conflict()
        await conflict_manager.record(conflict)

        fetched = await conflict_manager.get_conflict(conflict.id, "wrong-user")
        assert fetched is None

    async def test_get_nonexistent_conflict(self, conflict_manager):
        result = await conflict_manager.get_conflict("nope", "user-1")
        assert result is None

    async def test_list_conflicts_returns_all(self, conflict_manager):
        await conflict_manager.record(_make_conflict())
        await conflict_manager.record(_make_conflict(new_fact="Bob is VP"))

        conflicts = await conflict_manager.list_conflicts("user-1")
        assert len(conflicts) == 2

    async def test_list_conflicts_filter_by_status(self, conflict_manager):
        c1 = _make_conflict()
        await conflict_manager.record(c1)
        await conflict_manager.record(_make_conflict(new_fact="Bob is VP"))

        # Resolve the first one
        await conflict_manager.resolve(c1.id, "user-1")

        open_conflicts = await conflict_manager.list_conflicts(
            "user-1", status=ConflictStatus.OPEN
        )
        assert len(open_conflicts) == 1

        resolved = await conflict_manager.list_conflicts(
            "user-1", status=ConflictStatus.RESOLVED
        )
        assert len(resolved) == 1

    async def test_list_conflicts_filter_by_project(self, conflict_manager):
        await conflict_manager.record(_make_conflict(project_id="proj-a"))
        await conflict_manager.record(_make_conflict(project_id="proj-b"))

        results = await conflict_manager.list_conflicts("user-1", project_id="proj-a")
        assert len(results) == 1

    async def test_list_conflicts_respects_limit(self, conflict_manager):
        for i in range(5):
            await conflict_manager.record(_make_conflict(new_fact=f"fact-{i}"))

        results = await conflict_manager.list_conflicts("user-1", limit=3)
        assert len(results) == 3

    async def test_list_conflicts_different_user(self, conflict_manager):
        await conflict_manager.record(_make_conflict(user_id="user-1"))
        await conflict_manager.record(_make_conflict(user_id="user-2"))

        results = await conflict_manager.list_conflicts("user-1")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# ConflictManager — resolve + dismiss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConflictManagerResolution:
    async def test_resolve(self, conflict_manager):
        conflict = _make_conflict()
        await conflict_manager.record(conflict)

        resolved = await conflict_manager.resolve(
            conflict.id, "user-1", resolved_memory_id="mem-new-1"
        )
        assert resolved is not None
        assert resolved["status"] == "resolved"
        assert resolved["resolved_memory_id"] == "mem-new-1"
        assert resolved["resolved_at"] is not None

    async def test_resolve_nonexistent(self, conflict_manager):
        result = await conflict_manager.resolve("nope", "user-1")
        assert result is None

    async def test_resolve_wrong_user(self, conflict_manager):
        conflict = _make_conflict()
        await conflict_manager.record(conflict)

        result = await conflict_manager.resolve(conflict.id, "wrong-user")
        assert result is None

    async def test_dismiss(self, conflict_manager):
        conflict = _make_conflict()
        await conflict_manager.record(conflict)

        dismissed = await conflict_manager.dismiss(conflict.id, "user-1")
        assert dismissed is not None
        assert dismissed["status"] == "dismissed"
        assert dismissed["resolved_at"] is not None

    async def test_dismiss_nonexistent(self, conflict_manager):
        result = await conflict_manager.dismiss("nope", "user-1")
        assert result is None


# ---------------------------------------------------------------------------
# ConflictManager — statistics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConflictManagerStats:
    async def test_stats_empty(self, conflict_manager):
        stats = await conflict_manager.get_stats("user-1")
        assert stats["total"] == 0
        assert stats["open"] == 0
        assert stats["resolved"] == 0
        assert stats["dismissed"] == 0

    async def test_stats_counts(self, conflict_manager):
        c1 = _make_conflict(strategy_applied=ConflictStrategy.UPDATE)
        c2 = _make_conflict(strategy_applied=ConflictStrategy.VERSION)
        c3 = _make_conflict(strategy_applied=ConflictStrategy.FLAG)

        await conflict_manager.record(c1)
        await conflict_manager.record(c2)
        await conflict_manager.record(c3)

        # Resolve one, dismiss another
        await conflict_manager.resolve(c1.id, "user-1")
        await conflict_manager.dismiss(c2.id, "user-1")

        stats = await conflict_manager.get_stats("user-1")
        assert stats["total"] == 3
        assert stats["open"] == 1
        assert stats["resolved"] == 1
        assert stats["dismissed"] == 1
        assert stats["by_strategy"]["update"] == 1
        assert stats["by_strategy"]["version"] == 1
        assert stats["by_strategy"]["flag"] == 1

    async def test_stats_different_user_isolated(self, conflict_manager):
        await conflict_manager.record(_make_conflict(user_id="user-1"))
        await conflict_manager.record(_make_conflict(user_id="user-2"))

        stats = await conflict_manager.get_stats("user-1")
        assert stats["total"] == 1


# ---------------------------------------------------------------------------
# ConflictStrategy / ConflictStatus enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_strategy_values(self):
        assert ConflictStrategy.UPDATE.value == "update"
        assert ConflictStrategy.VERSION.value == "version"
        assert ConflictStrategy.FLAG.value == "flag"

    def test_status_values(self):
        assert ConflictStatus.OPEN.value == "open"
        assert ConflictStatus.RESOLVED.value == "resolved"
        assert ConflictStatus.DISMISSED.value == "dismissed"
