"""
Team manager — CRUD, membership, and invite handling.

Teams enable multi-user collaboration with shared memory spaces.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ROLES = {"owner", "admin", "member", "viewer"}
INVITE_EXPIRY_DAYS = 7


def slugify(name: str) -> str:
    """Convert name to URL-friendly slug."""
    slug = name.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug[:50]


def hash_token(token: str) -> str:
    """Hash a token for secure storage."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Team Manager
# ---------------------------------------------------------------------------


class TeamManager:
    """Manages teams, membership, and invites.

    Args:
        db: The application's Database instance.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def init_schema(self) -> None:
        """Create team tables if they don't exist (handled by main schema)."""
        # Tables are created in database.py SCHEMA_SQL
        # This is here for consistency with SpaceManager pattern
        pass

    # -----------------------------------------------------------------------
    # Team CRUD
    # -----------------------------------------------------------------------

    async def create_team(
        self,
        name: str,
        owner_id: str,
        description: str = "",
        slug: str | None = None,
        plan: str = "pro",
        max_seats: int = 5,
    ) -> dict[str, Any]:
        """Create a new team.

        The creator becomes the owner with full admin access.
        """
        team_id = f"team_{uuid4().hex[:16]}"
        slug = slug or slugify(name)
        now = datetime.now(UTC).isoformat()

        # Check for duplicate slug
        cursor = await self._db.conn.execute(
            "SELECT id FROM teams WHERE slug = ?",
            (slug,),
        )
        if await cursor.fetchone():
            # Add random suffix if slug exists
            slug = f"{slug}-{uuid4().hex[:6]}"

        await self._db.conn.execute(
            """
            INSERT INTO teams (id, name, slug, description, owner_id, plan, max_seats, used_seats, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (team_id, name, slug, description, owner_id, plan, max_seats, now, now),
        )

        # Add owner as team member
        await self._db.conn.execute(
            """
            INSERT INTO team_members (team_id, user_id, role, invited_by, joined_at, updated_at)
            VALUES (?, ?, 'owner', ?, ?, ?)
            """,
            (team_id, owner_id, owner_id, now, now),
        )
        await self._db.conn.commit()

        logger.info("Team created: id=%s name=%s owner=%s", team_id, name, owner_id)

        return {
            "id": team_id,
            "name": name,
            "slug": slug,
            "description": description,
            "owner_id": owner_id,
            "plan": plan,
            "max_seats": max_seats,
            "used_seats": 1,
            "created_at": now,
            "role": "owner",
        }

    async def get_team(self, team_id: str) -> dict[str, Any] | None:
        """Get team details by ID."""
        cursor = await self._db.conn.execute(
            """
            SELECT id, name, slug, description, owner_id, plan, max_seats, used_seats,
                   stripe_customer_id, stripe_subscription_id, created_at, updated_at
            FROM teams WHERE id = ?
            """,
            (team_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return {
            "id": row[0],
            "name": row[1],
            "slug": row[2],
            "description": row[3],
            "owner_id": row[4],
            "plan": row[5],
            "max_seats": row[6],
            "used_seats": row[7],
            "stripe_customer_id": row[8],
            "stripe_subscription_id": row[9],
            "created_at": row[10],
            "updated_at": row[11],
        }

    async def get_team_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Get team by slug."""
        cursor = await self._db.conn.execute(
            "SELECT id FROM teams WHERE slug = ?",
            (slug,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return await self.get_team(row[0])

    async def list_user_teams(self, user_id: str) -> list[dict[str, Any]]:
        """List all teams a user is a member of."""
        cursor = await self._db.conn.execute(
            """
            SELECT t.id, t.name, t.slug, t.description, t.owner_id, t.plan,
                   t.max_seats, t.used_seats, t.created_at, m.role
            FROM teams t
            JOIN team_members m ON t.id = m.team_id
            WHERE m.user_id = ?
            ORDER BY t.created_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "slug": row[2],
                "description": row[3],
                "owner_id": row[4],
                "plan": row[5],
                "max_seats": row[6],
                "used_seats": row[7],
                "created_at": row[8],
                "role": row[9],
            }
            for row in rows
        ]

    async def update_team(
        self,
        team_id: str,
        user_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any] | None:
        """Update team settings (admin/owner only)."""
        if not await self._has_permission(team_id, user_id, "admin"):
            raise PermissionError("Admin access required to update team")

        team = await self.get_team(team_id)
        if not team:
            return None

        updates = []
        values = []
        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if description is not None:
            updates.append("description = ?")
            values.append(description)

        if updates:
            updates.append("updated_at = ?")
            values.append(datetime.now(UTC).isoformat())
            values.append(team_id)

            await self._db.conn.execute(
                f"UPDATE teams SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            await self._db.conn.commit()

        return await self.get_team(team_id)

    async def delete_team(self, team_id: str, user_id: str) -> bool:
        """Delete a team (owner only)."""
        team = await self.get_team(team_id)
        if not team or team["owner_id"] != user_id:
            raise PermissionError("Only the team owner can delete the team")

        # Delete team members, invites, team_spaces, then team
        await self._db.conn.execute(
            "DELETE FROM team_members WHERE team_id = ?", (team_id,)
        )
        await self._db.conn.execute(
            "DELETE FROM team_invites WHERE team_id = ?", (team_id,)
        )
        await self._db.conn.execute(
            "DELETE FROM team_spaces WHERE team_id = ?", (team_id,)
        )
        cursor = await self._db.conn.execute(
            "DELETE FROM teams WHERE id = ?", (team_id,)
        )
        await self._db.conn.commit()

        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Team deleted: id=%s by=%s", team_id, user_id)
        return deleted

    # -----------------------------------------------------------------------
    # Membership
    # -----------------------------------------------------------------------

    async def get_membership(
        self, team_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Get a user's membership in a team."""
        cursor = await self._db.conn.execute(
            """
            SELECT team_id, user_id, role, invited_by, joined_at, updated_at
            FROM team_members WHERE team_id = ? AND user_id = ?
            """,
            (team_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return {
            "team_id": row[0],
            "user_id": row[1],
            "role": row[2],
            "invited_by": row[3],
            "joined_at": row[4],
            "updated_at": row[5],
        }

    async def list_members(self, team_id: str) -> list[dict[str, Any]]:
        """List all members of a team."""
        cursor = await self._db.conn.execute(
            """
            SELECT m.user_id, m.role, m.invited_by, m.joined_at, u.email, u.name
            FROM team_members m
            LEFT JOIN users u ON m.user_id = u.id
            WHERE m.team_id = ?
            ORDER BY m.joined_at
            """,
            (team_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "user_id": row[0],
                "role": row[1],
                "invited_by": row[2],
                "joined_at": row[3],
                "email": row[4],
                "name": row[5],
            }
            for row in rows
        ]

    async def add_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        invited_by: str | None = None,
    ) -> dict[str, Any]:
        """Add a member to the team."""
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role}. Use: {', '.join(VALID_ROLES)}")

        # Check seat limit
        team = await self.get_team(team_id)
        if not team:
            raise ValueError("Team not found")

        if team["used_seats"] >= team["max_seats"]:
            raise ValueError(
                f"Team has reached maximum seats ({team['max_seats']}). "
                "Upgrade your plan to add more members."
            )

        now = datetime.now(UTC).isoformat()
        await self._db.conn.execute(
            """
            INSERT INTO team_members (team_id, user_id, role, invited_by, joined_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id, user_id) DO UPDATE SET role = excluded.role, updated_at = excluded.updated_at
            """,
            (team_id, user_id, role, invited_by, now, now),
        )

        # Increment seat count
        await self._db.conn.execute(
            "UPDATE teams SET used_seats = used_seats + 1, updated_at = ? WHERE id = ?",
            (now, team_id),
        )
        await self._db.conn.commit()

        logger.info(
            "Member added: team=%s user=%s role=%s by=%s",
            team_id, user_id, role, invited_by,
        )

        return {
            "team_id": team_id,
            "user_id": user_id,
            "role": role,
            "invited_by": invited_by,
            "joined_at": now,
        }

    async def update_member_role(
        self,
        team_id: str,
        user_id: str,
        new_role: str,
        updated_by: str,
    ) -> dict[str, Any] | None:
        """Update a member's role (admin/owner only)."""
        if new_role not in VALID_ROLES or new_role == "owner":
            raise ValueError(f"Invalid role: {new_role}")

        if not await self._has_permission(team_id, updated_by, "admin"):
            raise PermissionError("Admin access required to update roles")

        # Can't change owner role this way
        membership = await self.get_membership(team_id, user_id)
        if not membership:
            return None
        if membership["role"] == "owner":
            raise PermissionError("Cannot change owner role. Use transfer ownership.")

        now = datetime.now(UTC).isoformat()
        await self._db.conn.execute(
            "UPDATE team_members SET role = ?, updated_at = ? WHERE team_id = ? AND user_id = ?",
            (new_role, now, team_id, user_id),
        )
        await self._db.conn.commit()

        logger.info(
            "Member role updated: team=%s user=%s role=%s by=%s",
            team_id, user_id, new_role, updated_by,
        )

        return await self.get_membership(team_id, user_id)

    async def remove_member(
        self,
        team_id: str,
        user_id: str,
        removed_by: str,
    ) -> bool:
        """Remove a member from the team (admin/owner only)."""
        if not await self._has_permission(team_id, removed_by, "admin"):
            raise PermissionError("Admin access required to remove members")

        # Can't remove the owner
        membership = await self.get_membership(team_id, user_id)
        if not membership:
            return False
        if membership["role"] == "owner":
            raise PermissionError("Cannot remove the team owner")

        cursor = await self._db.conn.execute(
            "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user_id),
        )

        if cursor.rowcount > 0:
            # Decrement seat count
            now = datetime.now(UTC).isoformat()
            await self._db.conn.execute(
                "UPDATE teams SET used_seats = used_seats - 1, updated_at = ? WHERE id = ?",
                (now, team_id),
            )
            await self._db.conn.commit()
            logger.info(
                "Member removed: team=%s user=%s by=%s",
                team_id, user_id, removed_by,
            )
            return True

        return False

    async def leave_team(self, team_id: str, user_id: str) -> bool:
        """Leave a team (self-service)."""
        membership = await self.get_membership(team_id, user_id)
        if not membership:
            return False
        if membership["role"] == "owner":
            raise PermissionError("Owner cannot leave. Transfer ownership or delete the team.")

        return await self.remove_member(team_id, user_id, user_id)

    # -----------------------------------------------------------------------
    # Invites
    # -----------------------------------------------------------------------

    async def create_invite(
        self,
        team_id: str,
        email: str,
        role: str,
        invited_by: str,
    ) -> dict[str, Any]:
        """Create a team invite and return the token."""
        if role not in VALID_ROLES or role == "owner":
            raise ValueError(f"Invalid invite role: {role}")

        if not await self._has_permission(team_id, invited_by, "admin"):
            raise PermissionError("Admin access required to invite members")

        # Check seat availability
        team = await self.get_team(team_id)
        if not team:
            raise ValueError("Team not found")

        if team["used_seats"] >= team["max_seats"]:
            raise ValueError(
                f"Team has reached maximum seats ({team['max_seats']}). "
                "Upgrade your plan to invite more members."
            )

        # Check if already a member
        cursor = await self._db.conn.execute(
            """
            SELECT u.id FROM users u
            JOIN team_members m ON u.id = m.user_id
            WHERE u.email = ? AND m.team_id = ?
            """,
            (email.lower(), team_id),
        )
        if await cursor.fetchone():
            raise ValueError(f"{email} is already a team member")

        # Check for pending invite
        cursor = await self._db.conn.execute(
            """
            SELECT id FROM team_invites
            WHERE team_id = ? AND email = ? AND status = 'pending'
            """,
            (team_id, email.lower()),
        )
        existing = await cursor.fetchone()
        if existing:
            # Return existing invite ID (they can resend)
            invite = await self.get_invite(existing[0])
            if invite:
                return invite

        # Generate secure token
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        invite_id = f"invite_{uuid4().hex[:16]}"
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=INVITE_EXPIRY_DAYS)

        await self._db.conn.execute(
            """
            INSERT INTO team_invites (id, team_id, email, role, invited_by, status, token_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                invite_id,
                team_id,
                email.lower(),
                role,
                invited_by,
                token_hash,
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )
        await self._db.conn.commit()

        logger.info(
            "Invite created: team=%s email=%s role=%s by=%s",
            team_id, email, role, invited_by,
        )

        return {
            "id": invite_id,
            "team_id": team_id,
            "team_name": team["name"],
            "email": email.lower(),
            "role": role,
            "token": token,  # Only returned on creation
            "expires_at": expires_at.isoformat(),
            "created_at": now.isoformat(),
        }

    async def get_invite(self, invite_id: str) -> dict[str, Any] | None:
        """Get invite details by ID."""
        cursor = await self._db.conn.execute(
            """
            SELECT i.id, i.team_id, i.email, i.role, i.invited_by, i.status,
                   i.expires_at, i.accepted_at, i.created_at, t.name
            FROM team_invites i
            JOIN teams t ON i.team_id = t.id
            WHERE i.id = ?
            """,
            (invite_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return {
            "id": row[0],
            "team_id": row[1],
            "email": row[2],
            "role": row[3],
            "invited_by": row[4],
            "status": row[5],
            "expires_at": row[6],
            "accepted_at": row[7],
            "created_at": row[8],
            "team_name": row[9],
        }

    async def get_invite_by_token(self, token: str) -> dict[str, Any] | None:
        """Get invite by token (for accepting)."""
        token_hash = hash_token(token)
        cursor = await self._db.conn.execute(
            """
            SELECT i.id, i.team_id, i.email, i.role, i.invited_by, i.status,
                   i.expires_at, i.accepted_at, i.created_at, t.name
            FROM team_invites i
            JOIN teams t ON i.team_id = t.id
            WHERE i.token_hash = ?
            """,
            (token_hash,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return {
            "id": row[0],
            "team_id": row[1],
            "email": row[2],
            "role": row[3],
            "invited_by": row[4],
            "status": row[5],
            "expires_at": row[6],
            "accepted_at": row[7],
            "created_at": row[8],
            "team_name": row[9],
        }

    async def list_pending_invites(self, team_id: str) -> list[dict[str, Any]]:
        """List pending invites for a team."""
        cursor = await self._db.conn.execute(
            """
            SELECT id, email, role, invited_by, status, expires_at, created_at
            FROM team_invites
            WHERE team_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            """,
            (team_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "email": row[1],
                "role": row[2],
                "invited_by": row[3],
                "status": row[4],
                "expires_at": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    async def accept_invite(
        self,
        token: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Accept a team invite."""
        invite = await self.get_invite_by_token(token)
        if not invite:
            raise ValueError("Invalid or expired invite token")

        if invite["status"] != "pending":
            raise ValueError(f"Invite already {invite['status']}")

        # Check expiry
        expires_at = datetime.fromisoformat(invite["expires_at"].replace("Z", "+00:00"))
        if datetime.now(UTC) > expires_at:
            await self._db.conn.execute(
                "UPDATE team_invites SET status = 'expired' WHERE id = ?",
                (invite["id"],),
            )
            await self._db.conn.commit()
            raise ValueError("Invite has expired")

        # Add member
        now = datetime.now(UTC).isoformat()
        await self.add_member(
            team_id=invite["team_id"],
            user_id=user_id,
            role=invite["role"],
            invited_by=invite["invited_by"],
        )

        # Mark invite as accepted
        await self._db.conn.execute(
            "UPDATE team_invites SET status = 'accepted', accepted_at = ? WHERE id = ?",
            (now, invite["id"]),
        )
        await self._db.conn.commit()

        logger.info(
            "Invite accepted: team=%s user=%s role=%s",
            invite["team_id"], user_id, invite["role"],
        )

        return {
            "team_id": invite["team_id"],
            "team_name": invite["team_name"],
            "role": invite["role"],
            "joined_at": now,
        }

    async def revoke_invite(
        self,
        invite_id: str,
        revoked_by: str,
    ) -> bool:
        """Revoke a pending invite."""
        invite = await self.get_invite(invite_id)
        if not invite:
            return False

        if not await self._has_permission(invite["team_id"], revoked_by, "admin"):
            raise PermissionError("Admin access required to revoke invites")

        if invite["status"] != "pending":
            return False

        await self._db.conn.execute(
            "UPDATE team_invites SET status = 'revoked' WHERE id = ?",
            (invite_id,),
        )
        await self._db.conn.commit()

        logger.info(
            "Invite revoked: id=%s by=%s",
            invite_id, revoked_by,
        )
        return True

    # -----------------------------------------------------------------------
    # Team Spaces
    # -----------------------------------------------------------------------

    async def link_space(
        self,
        team_id: str,
        space_id: str,
        linked_by: str,
    ) -> dict[str, Any]:
        """Link a space to a team (admin/owner only)."""
        if not await self._has_permission(team_id, linked_by, "admin"):
            raise PermissionError("Admin access required to link spaces")

        now = datetime.now(UTC).isoformat()
        await self._db.conn.execute(
            """
            INSERT INTO team_spaces (team_id, space_id, created_at, created_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id, space_id) DO NOTHING
            """,
            (team_id, space_id, now, linked_by),
        )
        await self._db.conn.commit()

        return {
            "team_id": team_id,
            "space_id": space_id,
            "linked_by": linked_by,
            "linked_at": now,
        }

    async def unlink_space(
        self,
        team_id: str,
        space_id: str,
        unlinked_by: str,
    ) -> bool:
        """Unlink a space from a team."""
        if not await self._has_permission(team_id, unlinked_by, "admin"):
            raise PermissionError("Admin access required to unlink spaces")

        cursor = await self._db.conn.execute(
            "DELETE FROM team_spaces WHERE team_id = ? AND space_id = ?",
            (team_id, space_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def list_team_spaces(self, team_id: str) -> list[str]:
        """List all space IDs linked to a team."""
        cursor = await self._db.conn.execute(
            "SELECT space_id FROM team_spaces WHERE team_id = ?",
            (team_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_user_team_spaces(self, user_id: str) -> list[str]:
        """Get all space IDs from teams the user belongs to."""
        cursor = await self._db.conn.execute(
            """
            SELECT DISTINCT ts.space_id
            FROM team_spaces ts
            JOIN team_members tm ON ts.team_id = tm.team_id
            WHERE tm.user_id = ?
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _has_permission(
        self, team_id: str, user_id: str, required: str
    ) -> bool:
        """Check if user has at least the required permission level."""
        hierarchy = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}
        required_level = hierarchy.get(required, 0)

        membership = await self.get_membership(team_id, user_id)
        if membership is None:
            return False

        actual_level = hierarchy.get(membership["role"], 0)
        return actual_level >= required_level
