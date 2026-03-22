"""
Memory conflict detection and resolution.

When new facts contradict existing memories, this module:
1. Detects the conflict and captures both sides
2. Applies a resolution strategy (update, version, flag)
3. Records the conflict for audit / human review

Strategies:
  update  – Overwrite the old memory (current default behaviour).
  version – Keep both memories; tag them as conflicting versions.
  flag    – Store the new memory and mark both for human review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ConflictStrategy(StrEnum):
    """How to resolve a detected conflict."""

    UPDATE = "update"
    VERSION = "version"
    FLAG = "flag"


class ConflictStatus(StrEnum):
    """Lifecycle status of a recorded conflict."""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


@dataclass
class MemoryConflict:
    """A detected contradiction between a new fact and an existing memory."""

    id: str = field(default_factory=lambda: str(uuid4()))
    user_id: str = ""
    project_id: str = "default"
    new_fact: str = ""
    existing_memory_id: str = ""
    existing_content: str = ""
    similarity_score: float = 0.0
    reason: str = ""
    strategy_applied: ConflictStrategy = ConflictStrategy.UPDATE
    status: ConflictStatus = ConflictStatus.OPEN
    resolved_memory_id: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "new_fact": self.new_fact,
            "existing_memory_id": self.existing_memory_id,
            "existing_content": self.existing_content,
            "similarity_score": self.similarity_score,
            "reason": self.reason,
            "strategy_applied": self.strategy_applied.value,
            "status": self.status.value,
            "resolved_memory_id": self.resolved_memory_id,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


# ---------------------------------------------------------------------------
# Conflict Manager (SQLite-backed)
# ---------------------------------------------------------------------------


class ConflictManager:
    """Tracks and manages memory conflicts in SQLite.

    Args:
        db: The application's Database instance.
        default_strategy: Default resolution strategy for detected conflicts.
    """

    def __init__(
        self,
        db: Any,
        default_strategy: ConflictStrategy = ConflictStrategy.UPDATE,
    ) -> None:
        self._db = db
        self.default_strategy = default_strategy

    async def init_schema(self) -> None:
        """Create the conflicts table if it doesn't exist."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'default',
                new_fact TEXT NOT NULL,
                existing_memory_id TEXT NOT NULL,
                existing_content TEXT NOT NULL,
                similarity_score REAL DEFAULT 0.0,
                reason TEXT,
                strategy_applied TEXT NOT NULL DEFAULT 'update',
                status TEXT NOT NULL DEFAULT 'open',
                resolved_memory_id TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_conflicts_user
                ON memory_conflicts(user_id);

            CREATE INDEX IF NOT EXISTS idx_conflicts_status
                ON memory_conflicts(status);

            CREATE INDEX IF NOT EXISTS idx_conflicts_project
                ON memory_conflicts(user_id, project_id);
        """)
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Record conflicts
    # -----------------------------------------------------------------------

    async def record(self, conflict: MemoryConflict) -> MemoryConflict:
        """Persist a new conflict record."""
        await self._db.conn.execute(
            """
            INSERT INTO memory_conflicts
                (id, user_id, project_id, new_fact, existing_memory_id,
                 existing_content, similarity_score, reason, strategy_applied,
                 status, resolved_memory_id, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict.id,
                conflict.user_id,
                conflict.project_id,
                conflict.new_fact,
                conflict.existing_memory_id,
                conflict.existing_content,
                conflict.similarity_score,
                conflict.reason,
                conflict.strategy_applied.value,
                conflict.status.value,
                conflict.resolved_memory_id,
                conflict.created_at,
                conflict.resolved_at,
            ),
        )
        await self._db.conn.commit()
        logger.info(
            "Conflict recorded: id=%s strategy=%s user=%s",
            conflict.id,
            conflict.strategy_applied.value,
            conflict.user_id,
        )
        return conflict

    # -----------------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------------

    async def list_conflicts(
        self,
        user_id: str,
        project_id: str | None = None,
        status: ConflictStatus | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return conflicts for a user, optionally filtered."""
        query = "SELECT * FROM memory_conflicts WHERE user_id = ?"
        params: list[Any] = [user_id]

        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)

        if status:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self._db.conn.execute(query, params)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row, strict=False)) for row in rows]

    async def get_conflict(
        self, conflict_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Get a single conflict by ID with ownership check."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM memory_conflicts WHERE id = ? AND user_id = ?",
            (conflict_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row, strict=False))

    # -----------------------------------------------------------------------
    # Resolve / dismiss
    # -----------------------------------------------------------------------

    async def resolve(
        self,
        conflict_id: str,
        user_id: str,
        resolved_memory_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a conflict as resolved."""
        now = datetime.now(UTC).isoformat()
        cursor = await self._db.conn.execute(
            """
            UPDATE memory_conflicts
            SET status = ?, resolved_at = ?, resolved_memory_id = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                ConflictStatus.RESOLVED.value,
                now,
                resolved_memory_id,
                conflict_id,
                user_id,
            ),
        )
        await self._db.conn.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get_conflict(conflict_id, user_id)

    async def dismiss(
        self, conflict_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Dismiss a conflict (mark as not needing resolution)."""
        now = datetime.now(UTC).isoformat()
        cursor = await self._db.conn.execute(
            """
            UPDATE memory_conflicts
            SET status = ?, resolved_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                ConflictStatus.DISMISSED.value,
                now,
                conflict_id,
                user_id,
            ),
        )
        await self._db.conn.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get_conflict(conflict_id, user_id)

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """Summary statistics for a user's conflicts."""
        cursor = await self._db.conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open,
                SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) as resolved,
                SUM(CASE WHEN status = 'dismissed' THEN 1 ELSE 0 END) as dismissed,
                SUM(CASE WHEN strategy_applied = 'update' THEN 1 ELSE 0 END) as strategy_update,
                SUM(CASE WHEN strategy_applied = 'version' THEN 1 ELSE 0 END) as strategy_version,
                SUM(CASE WHEN strategy_applied = 'flag' THEN 1 ELSE 0 END) as strategy_flag
            FROM memory_conflicts
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"total": 0, "open": 0, "resolved": 0, "dismissed": 0}
        return {
            "total": row[0] or 0,
            "open": row[1] or 0,
            "resolved": row[2] or 0,
            "dismissed": row[3] or 0,
            "by_strategy": {
                "update": row[4] or 0,
                "version": row[5] or 0,
                "flag": row[6] or 0,
            },
        }
