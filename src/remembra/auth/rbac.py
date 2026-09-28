"""
Role-Based Access Control (RBAC) for Remembra.

Defines roles, permissions, and enforcement helpers.

Roles:
  admin   – every permission. Created only with the server's master key (an admin
            key can then give another of the account's keys the admin role).
  editor  – every permission except the ``admin:*`` ones.
  viewer  – read-only: recall memories, read entities, list keys.

Any API key, whatever its role, may revoke itself (not permanently delete
itself): see ``require_revoke_permission`` in ``remembra.api.v1.keys``.

``ROLE_PERMISSIONS`` below is the only role table: ``has_permission`` in
``remembra.auth.middleware``, the ``remembra.auth.scopes`` dependencies and
``GET /admin/permissions`` all read it, and ``permission_table()`` renders it
for docs/guides/rbac.md and SECURITY.md (tests keep those equal to it).

A key's role is stored in the ``api_key_roles`` table. Explicit scopes on a
key only ever narrow its role; they never add a permission the role lacks.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
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

    # Account changes made with an API key (accounts created by API signup)
    ACCOUNT_MANAGE = "account:manage"


# Credential ids that are not real API keys (shared by every JWT / dev session).
SYNTHETIC_KEY_IDS = frozenset({"jwt_auth", "dev_key"})

ROLE_LEVEL: dict[Role, int] = {Role.VIEWER: 1, Role.EDITOR: 2, Role.ADMIN: 3}


# Default permission sets per role
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # all permissions
    Role.EDITOR: {
        Permission.MEMORY_STORE,
        Permission.MEMORY_RECALL,
        Permission.MEMORY_DELETE,
        Permission.KEY_CREATE,
        Permission.KEY_LIST,
        Permission.KEY_REVOKE,
        Permission.ENTITY_READ,
        Permission.WEBHOOK_MANAGE,
        Permission.CONFLICT_MANAGE,
        Permission.ACCOUNT_MANAGE,
    },
    Role.VIEWER: {
        Permission.MEMORY_RECALL,
        Permission.KEY_LIST,
        Permission.ENTITY_READ,
    },
}

# What each permission lets a key do, in the words the docs use.
PERMISSION_SUMMARIES: dict[Permission, str] = {
    Permission.MEMORY_STORE: (
        "Store, change, pin, import and ingest memories; send recall feedback; write inbox messages, "
        "relay handoffs and session status; change spaces, teams and project links; recompute the brain "
        "layer; start audio capture (self-hosted servers only)"
    ),
    Permission.MEMORY_RECALL: (
        "Recall, list, read and export memories; read spaces, inbox messages, relay briefs and trails, "
        "conflicts, timelines and brain insights"
    ),
    Permission.MEMORY_DELETE: "Delete memories and clean up expired or decayed ones",
    Permission.KEY_CREATE: "Create API keys (never above the caller's own role, never admin) and rename them",
    Permission.KEY_LIST: "List the account's API keys",
    Permission.KEY_REVOKE: (
        "Revoke or delete API keys (an API key only ones with no more access than itself); any key may revoke itself without it"
    ),
    Permission.WEBHOOK_MANAGE: "Create, read, change and delete webhooks and read their deliveries",
    Permission.CONFLICT_MANAGE: "Resolve or dismiss memory conflicts",
    Permission.ENTITY_READ: "Read entities, their relationships and the memories that mention them",
    Permission.ADMIN_AUDIT: "Read the account's audit log",
    Permission.ADMIN_EXPORT: "Export the account's audit log as JSON or CSV",
    Permission.ADMIN_USERS: "Nothing yet: no route checks it",
    Permission.ACCOUNT_MANAGE: "Redeem a promo code; email the verification link of an account created by API signup",
}

# Attribute a permission-checking FastAPI dependency carries, so a route's
# required permissions can be read from its dependency tree.
PERMISSION_ATTR = "__remembra_permission__"


def effective_permissions(role: str, scopes: Iterable[str] | None = None) -> frozenset[Permission]:
    """What a credential may do: its role's permissions, narrowed to ``scopes`` when it has any.

    Scopes only ever remove permissions: a viewer key scoped to ``memory:store``
    still cannot store. An unknown role holds no permission.
    """
    try:
        granted = ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()
    if scopes:
        allowed = set(scopes)
        return frozenset(p for p in granted if p.value in allowed)
    return frozenset(granted)


def permission_table() -> str:
    """The role/permission table as Markdown, as docs/guides/rbac.md and SECURITY.md show it."""
    roles = (Role.ADMIN, Role.EDITOR, Role.VIEWER)
    lines = [
        "| Permission | " + " | ".join(role.value for role in roles) + " | What it allows |",
        "|---|" + ":---:|" * len(roles) + "---|",
    ]
    for perm in Permission:
        marks = " | ".join("yes" if perm in ROLE_PERMISSIONS[role] else "no" for role in roles)
        lines.append(f"| `{perm.value}` | {marks} | {PERMISSION_SUMMARIES[perm]} |")
    return "\n".join(lines)


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
        """Effective permissions: the role's, narrowed by any explicit scopes."""
        return set(effective_permissions(self.role, self.scopes))

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
        if api_key_id in SYNTHETIC_KEY_IDS:
            raise ValueError(f"'{api_key_id}' is not an API key and cannot carry a role")
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
        deleted: bool = cursor.rowcount > 0
        return deleted

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
