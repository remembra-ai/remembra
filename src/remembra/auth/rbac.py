"""
Role-Based Access Control (RBAC) for Remembra.

Defines roles, permissions, and enforcement helpers.

Roles:
  admin   – Full access: manage keys, manage users, read/write memories, export audit logs.
  editor  – Read/write memories, manage own keys.
  viewer  – Read-only: recall memories, list entities.

Permissions are stored alongside API keys (via a join table) and enforced
via FastAPI dependencies that compose with the existing ``CurrentUser`` flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Roles & Permissions
# ---------------------------------------------------------------------------


class Role(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Permission(StrEnum):
    """Granular permission tokens."""

    # Memory operations
    MEMORY_STORE = "memory:store"
    MEMORY_RECALL = "memory:recall"
    MEMORY_DELETE = "memory:delete"

    # Key management
    KEY_CREATE = "key:create"
    KEY_LIST = "key:list"
    KEY_REVOKE = "key:revoke"

    # Webhook management
    WEBHOOK_MANAGE = "webhook:manage"

    # Conflict management
    CONFLICT_MANAGE = "conflict:manage"

    # Entity operations
    ENTITY_READ = "entity:read"

    # Admin-only
    ADMIN_AUDIT = "admin:audit"
    ADMIN_EXPORT = "admin:export"
    ADMIN_USERS = "admin:users"


# Default permission sets per role
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # all permissions
    Role.EDITOR: {
        Permission.MEMORY_STORE,
        Permission.MEMORY_RECALL,
        Permission.MEMORY_DELETE,
        Permission.KEY_LIST,
        Permission.ENTITY_READ,
        Permission.WEBHOOK_MANAGE,
        Permission.CONFLICT_MANAGE,
    },
    Role.VIEWER: {
        Permission.MEMORY_RECALL,
        Permission.KEY_LIST,
        Permission.ENTITY_READ,
    },
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class KeyRole:
    """A role assignment on an API key."""

    api_key_id: str
    role: Role
    scopes: list[str] = field(default_factory=list)
    project_ids: list[str] = field(default_factory=list)

    @property
    def permissions(self) -> set[Permission]:
        """Effective permissions from role + any explicit scope overrides."""
        perms = set(ROLE_PERMISSIONS.get(self.role, set()))
        # Scopes can further restrict permissions (whitelist model)
        if self.scopes:
            allowed = {Permission(s) for s in self.scopes if s in {p.value for p in Permission}}
            perms = perms & allowed
        return perms

    def has_permission(self, perm: Permission) -> bool:
        return perm in self.permissions

    def has_project_access(self, project_id: str) -> bool:
        """Check if key is allowed to access the given project."""
        if not self.project_ids:
            return True  # No restriction → all projects
        return project_id in self.project_ids


# ---------------------------------------------------------------------------
# Role Manager (SQLite-backed)
# ---------------------------------------------------------------------------


class RoleManager:
    """Manages role assignments stored in SQLite.

    The ``api_key_roles`` table associates each API key with a role,
    optional scopes (comma-separated), and optional project restrictions.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def init_schema(self) -> None:
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_key_roles (
                api_key_id TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'editor',
                scopes TEXT DEFAULT '',
                project_ids TEXT DEFAULT '',
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
            );
        """)
        await self._db.conn.commit()

    async def assign_role(
        self,
        api_key_id: str,
        role: Role,
        scopes: list[str] | None = None,
        project_ids: list[str] | None = None,
    ) -> KeyRole:
        """Assign or update a role on an API key."""
        scopes_str = ",".join(scopes) if scopes else ""
        projects_str = ",".join(project_ids) if project_ids else ""

        await self._db.conn.execute(
            """
            INSERT INTO api_key_roles (api_key_id, role, scopes, project_ids)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(api_key_id)
            DO UPDATE SET role = excluded.role,
                          scopes = excluded.scopes,
                          project_ids = excluded.project_ids
            """,
            (api_key_id, role.value, scopes_str, projects_str),
        )
        await self._db.conn.commit()

        logger.info("Role assigned: key=%s role=%s", api_key_id, role.value)
        return KeyRole(
            api_key_id=api_key_id,
            role=role,
            scopes=scopes or [],
            project_ids=project_ids or [],
        )

    async def get_role(self, api_key_id: str) -> KeyRole:
        """Get role info for a key. Defaults to editor if not set."""
        cursor = await self._db.conn.execute(
            "SELECT role, scopes, project_ids FROM api_key_roles WHERE api_key_id = ?",
            (api_key_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return KeyRole(api_key_id=api_key_id, role=Role.EDITOR)

        return KeyRole(
            api_key_id=api_key_id,
            role=Role(row[0]) if row[0] else Role.EDITOR,
            scopes=[s for s in row[1].split(",") if s] if row[1] else [],
            project_ids=[p for p in row[2].split(",") if p] if row[2] else [],
        )

    async def remove_role(self, api_key_id: str) -> bool:
        """Remove role assignment (key reverts to default editor)."""
        cursor = await self._db.conn.execute(
            "DELETE FROM api_key_roles WHERE api_key_id = ?",
            (api_key_id,),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def list_roles(self, user_id: str) -> list[dict[str, Any]]:
        """List role assignments for all keys owned by a user."""
        cursor = await self._db.conn.execute(
            """
            SELECT r.api_key_id, r.role, r.scopes, r.project_ids, k.name
            FROM api_key_roles r
            JOIN api_keys k ON r.api_key_id = k.id
            WHERE k.user_id = ? AND k.active = TRUE
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "api_key_id": row[0],
                "role": row[1],
                "scopes": [s for s in row[2].split(",") if s] if row[2] else [],
                "project_ids": [p for p in row[3].split(",") if p] if row[3] else [],
                "key_name": row[4],
            }
            for row in rows
        ]
