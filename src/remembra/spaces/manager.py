"""
Memory space manager — CRUD, access control, and membership.

Stores space definitions and access grants in SQLite.
Memories are linked to spaces via the memory_space_membership table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_PERMISSIONS = {"read", "write", "admin"}


# ---------------------------------------------------------------------------
# Space Manager
# ---------------------------------------------------------------------------


class SpaceManager:
    """Manages memory spaces and cross-agent access.

    Args:
        db: The application's Database instance.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def init_schema(self) -> None:
        """Create space tables if they don't exist."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_spaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                owner_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_spaces_name_owner
                ON memory_spaces(owner_id, name);

            CREATE TABLE IF NOT EXISTS space_access (
                space_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                permission TEXT NOT NULL DEFAULT 'read',
                granted_by TEXT NOT NULL,
                granted_at TEXT NOT NULL,
                PRIMARY KEY (space_id, agent_id),
                FOREIGN KEY (space_id) REFERENCES memory_spaces(id)
            );

            CREATE INDEX IF NOT EXISTS idx_space_access_agent
                ON space_access(agent_id);

            CREATE TABLE IF NOT EXISTS memory_space_membership (
                memory_id TEXT NOT NULL,
                space_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                added_by TEXT NOT NULL,
                PRIMARY KEY (memory_id, space_id),
                FOREIGN KEY (space_id) REFERENCES memory_spaces(id)
            );

            CREATE INDEX IF NOT EXISTS idx_space_membership_space
                ON memory_space_membership(space_id);
        """)
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Space CRUD
    # -----------------------------------------------------------------------

    async def create_space(
        self,
        name: str,
        owner_id: str,
        description: str = "",
        project_id: str = "default",
    ) -> dict[str, Any]:
        """Create a new memory space.

        The creator automatically gets admin access.
        """
        space_id = f"space_{uuid4().hex[:16]}"
        now = datetime.now(UTC).isoformat()

        # Check for duplicate name
        cursor = await self._db.conn.execute(
            "SELECT id FROM memory_spaces WHERE owner_id = ? AND name = ?",
            (owner_id, name),
        )
        if await cursor.fetchone():
            raise ValueError(f"Space '{name}' already exists for this user")

        await self._db.conn.execute(
            """
            INSERT INTO memory_spaces (id, name, description, owner_id, project_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (space_id, name, description, owner_id, project_id, now, now),
        )

        # Grant admin access to owner
        await self._db.conn.execute(
            """
            INSERT INTO space_access (space_id, agent_id, permission, granted_by, granted_at)
            VALUES (?, ?, 'admin', ?, ?)
            """,
            (space_id, owner_id, owner_id, now),
        )
        await self._db.conn.commit()

        logger.info("Space created: id=%s name=%s owner=%s", space_id, name, owner_id)

        return {
            "id": space_id,
            "name": name,
            "description": description,
            "owner_id": owner_id,
            "project_id": project_id,
            "created_at": now,
            "members": 1,
        }

    async def get_space(self, space_id: str) -> dict[str, Any] | None:
        """Get space details by ID."""
        cursor = await self._db.conn.execute(
            "SELECT id, name, description, owner_id, project_id, created_at, updated_at FROM memory_spaces WHERE id = ?",
            (space_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        # Count members
        count_cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM space_access WHERE space_id = ?",
            (space_id,),
        )
        count_row = await count_cursor.fetchone()

        # Count memories
        mem_cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM memory_space_membership WHERE space_id = ?",
            (space_id,),
        )
        mem_row = await mem_cursor.fetchone()

        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "owner_id": row[3],
            "project_id": row[4],
            "created_at": row[5],
            "updated_at": row[6],
            "members": count_row[0] if count_row else 0,
            "memory_count": mem_row[0] if mem_row else 0,
        }

    async def list_spaces(self, agent_id: str) -> list[dict[str, Any]]:
        """List all spaces accessible to an agent/user."""
        cursor = await self._db.conn.execute(
            """
            SELECT s.id, s.name, s.description, s.owner_id, s.project_id,
                   s.created_at, a.permission
            FROM memory_spaces s
            JOIN space_access a ON s.id = a.space_id
            WHERE a.agent_id = ?
            ORDER BY s.created_at DESC
            """,
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "owner_id": row[3],
                "project_id": row[4],
                "created_at": row[5],
                "permission": row[6],
            }
            for row in rows
        ]

    async def delete_space(self, space_id: str, user_id: str) -> bool:
        """Delete a space (only owner or admin can delete)."""
        if not await self._has_permission(space_id, user_id, "admin"):
            return False

        await self._db.conn.execute(
            "DELETE FROM memory_space_membership WHERE space_id = ?", (space_id,)
        )
        await self._db.conn.execute(
            "DELETE FROM space_access WHERE space_id = ?", (space_id,)
        )
        cursor = await self._db.conn.execute(
            "DELETE FROM memory_spaces WHERE id = ?", (space_id,)
        )
        await self._db.conn.commit()

        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Space deleted: id=%s by=%s", space_id, user_id)
        return deleted

    # -----------------------------------------------------------------------
    # Access control
    # -----------------------------------------------------------------------

    async def grant_access(
        self,
        space_id: str,
        agent_id: str,
        permission: str,
        granted_by: str,
    ) -> dict[str, Any]:
        """Grant or update access to a space for an agent."""
        if permission not in VALID_PERMISSIONS:
            raise ValueError(f"Invalid permission: {permission}. Use: {', '.join(VALID_PERMISSIONS)}")

        # Granter must have admin permission
        if not await self._has_permission(space_id, granted_by, "admin"):
            raise PermissionError("Admin access required to grant permissions")

        now = datetime.now(UTC).isoformat()
        await self._db.conn.execute(
            """
            INSERT INTO space_access (space_id, agent_id, permission, granted_by, granted_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(space_id, agent_id)
            DO UPDATE SET permission = excluded.permission,
                          granted_by = excluded.granted_by,
                          granted_at = excluded.granted_at
            """,
            (space_id, agent_id, permission, granted_by, now),
        )
        await self._db.conn.commit()

        logger.info(
            "Access granted: space=%s agent=%s perm=%s by=%s",
            space_id, agent_id, permission, granted_by,
        )
        return {
            "space_id": space_id,
            "agent_id": agent_id,
            "permission": permission,
            "granted_by": granted_by,
            "granted_at": now,
        }

    async def revoke_access(
        self, space_id: str, agent_id: str, revoked_by: str
    ) -> bool:
        """Revoke an agent's access to a space."""
        if not await self._has_permission(space_id, revoked_by, "admin"):
            raise PermissionError("Admin access required to revoke permissions")

        cursor = await self._db.conn.execute(
            "DELETE FROM space_access WHERE space_id = ? AND agent_id = ?",
            (space_id, agent_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def list_members(self, space_id: str) -> list[dict[str, Any]]:
        """List all agents with access to a space."""
        cursor = await self._db.conn.execute(
            "SELECT agent_id, permission, granted_by, granted_at FROM space_access WHERE space_id = ?",
            (space_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "agent_id": row[0],
                "permission": row[1],
                "granted_by": row[2],
                "granted_at": row[3],
            }
            for row in rows
        ]

    async def check_access(
        self, space_id: str, agent_id: str, required: str = "read"
    ) -> bool:
        """Check if an agent has the required permission level."""
        return await self._has_permission(space_id, agent_id, required)

    # -----------------------------------------------------------------------
    # Memory membership
    # -----------------------------------------------------------------------

    async def add_memory_to_space(
        self, memory_id: str, space_id: str, added_by: str
    ) -> bool:
        """Add a memory to a space (requires write access)."""
        if not await self._has_permission(space_id, added_by, "write"):
            raise PermissionError("Write access required to add memories to a space")

        now = datetime.now(UTC).isoformat()
        try:
            await self._db.conn.execute(
                """
                INSERT OR IGNORE INTO memory_space_membership (memory_id, space_id, added_at, added_by)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, space_id, now, added_by),
            )
            await self._db.conn.commit()
            return True
        except Exception as e:
            logger.warning("Failed to add memory to space: %s", e)
            return False

    async def remove_memory_from_space(
        self, memory_id: str, space_id: str, removed_by: str
    ) -> bool:
        """Remove a memory from a space (requires write access)."""
        if not await self._has_permission(space_id, removed_by, "write"):
            raise PermissionError("Write access required to remove memories")

        cursor = await self._db.conn.execute(
            "DELETE FROM memory_space_membership WHERE memory_id = ? AND space_id = ?",
            (memory_id, space_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def get_space_memory_ids(
        self, space_id: str, limit: int = 1000
    ) -> list[str]:
        """Get all memory IDs in a space."""
        cursor = await self._db.conn.execute(
            "SELECT memory_id FROM memory_space_membership WHERE space_id = ? LIMIT ?",
            (space_id, limit),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_accessible_space_ids(self, agent_id: str) -> list[str]:
        """Get all space IDs the agent has read (or higher) access to."""
        cursor = await self._db.conn.execute(
            "SELECT space_id FROM space_access WHERE agent_id = ?",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_memory_spaces(self, memory_id: str) -> list[dict[str, Any]]:
        """Get all spaces a memory belongs to."""
        cursor = await self._db.conn.execute(
            """
            SELECT s.id, s.name, m.added_at
            FROM memory_space_membership m
            JOIN memory_spaces s ON m.space_id = s.id
            WHERE m.memory_id = ?
            """,
            (memory_id,),
        )
        rows = await cursor.fetchall()
        return [
            {"space_id": row[0], "space_name": row[1], "added_at": row[2]}
            for row in rows
        ]

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _has_permission(
        self, space_id: str, agent_id: str, required: str
    ) -> bool:
        """Check if agent has at least the required permission level."""
        hierarchy = {"read": 1, "write": 2, "admin": 3}
        required_level = hierarchy.get(required, 0)

        cursor = await self._db.conn.execute(
            "SELECT permission FROM space_access WHERE space_id = ? AND agent_id = ?",
            (space_id, agent_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return False

        actual_level = hierarchy.get(row[0], 0)
        return actual_level >= required_level
