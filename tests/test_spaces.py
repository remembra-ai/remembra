"""Tests for the SpaceManager — CRUD, access control, and memory membership."""

import pytest

from remembra.spaces.manager import SpaceManager, VALID_PERMISSIONS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def space_manager(in_memory_db):
    """SpaceManager backed by a real in-memory SQLite database."""
    mgr = SpaceManager(in_memory_db)
    await mgr.init_schema()
    # Memories referenced by the membership tests, with real owners.
    await in_memory_db.conn.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, user_id TEXT)")
    rows = [(f"mem-{i}", "user-1") for i in range(6)] + [("mem-u2", "user-2")]
    await in_memory_db.conn.executemany("INSERT INTO memories (id, user_id) VALUES (?, ?)", rows)
    await in_memory_db.conn.commit()
    return mgr


async def grant(mgr, space_id, agent_id, permission, granted_by="user-1"):
    """Grant access the consented way: invite, then the grantee accepts."""
    result = await mgr.grant_access(space_id, agent_id, permission, granted_by=granted_by)
    if result["status"] == "pending":
        result = await mgr.accept_invite(space_id, agent_id)
    return result


# ---------------------------------------------------------------------------
# Space CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSpaceCRUD:
    async def test_create_space(self, space_manager):
        space = await space_manager.create_space(
            name="research",
            owner_id="user-1",
            description="Research notes",
        )
        assert space["name"] == "research"
        assert space["owner_id"] == "user-1"
        assert space["description"] == "Research notes"
        assert space["id"].startswith("space_")
        assert space["members"] == 1  # owner auto-added

    async def test_create_space_duplicate_name_raises(self, space_manager):
        await space_manager.create_space(name="research", owner_id="user-1")
        with pytest.raises(ValueError, match="already exists"):
            await space_manager.create_space(name="research", owner_id="user-1")

    async def test_create_space_same_name_different_owner(self, space_manager):
        s1 = await space_manager.create_space(name="research", owner_id="user-1")
        s2 = await space_manager.create_space(name="research", owner_id="user-2")
        assert s1["id"] != s2["id"]

    async def test_get_space(self, space_manager):
        created = await space_manager.create_space(name="research", owner_id="user-1")
        fetched = await space_manager.get_space(created["id"])
        assert fetched is not None
        assert fetched["name"] == "research"
        assert fetched["members"] == 1
        assert fetched["memory_count"] == 0

    async def test_get_nonexistent_space(self, space_manager):
        result = await space_manager.get_space("space_doesnotexist")
        assert result is None

    async def test_list_spaces(self, space_manager):
        await space_manager.create_space(name="space-a", owner_id="user-1")
        await space_manager.create_space(name="space-b", owner_id="user-1")

        spaces = await space_manager.list_spaces("user-1")
        assert len(spaces) == 2
        names = {s["name"] for s in spaces}
        assert names == {"space-a", "space-b"}

    async def test_list_spaces_only_accessible(self, space_manager):
        await space_manager.create_space(name="mine", owner_id="user-1")
        await space_manager.create_space(name="theirs", owner_id="user-2")

        spaces = await space_manager.list_spaces("user-1")
        assert len(spaces) == 1
        assert spaces[0]["name"] == "mine"

    async def test_delete_space(self, space_manager):
        space = await space_manager.create_space(name="temp", owner_id="user-1")
        deleted = await space_manager.delete_space(space["id"], "user-1")
        assert deleted is True

        fetched = await space_manager.get_space(space["id"])
        assert fetched is None

    async def test_delete_space_non_admin(self, space_manager):
        space = await space_manager.create_space(name="temp", owner_id="user-1")
        # user-2 has no access — delete should fail
        deleted = await space_manager.delete_space(space["id"], "user-2")
        assert deleted is False

    async def test_delete_nonexistent_space(self, space_manager):
        # user-1 doesn't have admin on a non-existent space
        deleted = await space_manager.delete_space("space_nope", "user-1")
        assert deleted is False


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAccessControl:
    async def test_owner_has_admin(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        assert await space_manager.check_access(space["id"], "user-1", "admin") is True
        assert await space_manager.check_access(space["id"], "user-1", "write") is True
        assert await space_manager.check_access(space["id"], "user-1", "read") is True

    async def test_no_access_by_default(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        assert await space_manager.check_access(space["id"], "user-2", "read") is False

    async def test_grant_read_access(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        pending = await space_manager.grant_access(space["id"], "user-2", "read", granted_by="user-1")
        assert pending["status"] == "pending"
        # No access until the invitee accepts (consent).
        assert await space_manager.check_access(space["id"], "user-2", "read") is False
        result = await space_manager.accept_invite(space["id"], "user-2")
        assert result["permission"] == "read"
        assert await space_manager.check_access(space["id"], "user-2", "read") is True
        assert await space_manager.check_access(space["id"], "user-2", "write") is False

    async def test_grant_write_includes_read(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "write")
        assert await space_manager.check_access(space["id"], "user-2", "read") is True
        assert await space_manager.check_access(space["id"], "user-2", "write") is True
        assert await space_manager.check_access(space["id"], "user-2", "admin") is False

    async def test_grant_invalid_permission_raises(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        with pytest.raises(ValueError, match="Invalid permission"):
            await space_manager.grant_access(space["id"], "user-2", "superadmin", granted_by="user-1")

    async def test_grant_by_non_admin_raises(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        # user-2 has no access — can't grant
        with pytest.raises(PermissionError, match="Admin access required"):
            await space_manager.grant_access(space["id"], "user-3", "read", granted_by="user-2")

    async def test_update_permission(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "read")
        assert await space_manager.check_access(space["id"], "user-2", "write") is False

        # Upgrade an existing member directly (they already consented)
        upgraded = await space_manager.grant_access(space["id"], "user-2", "write", granted_by="user-1")
        assert upgraded["status"] == "active"
        assert await space_manager.check_access(space["id"], "user-2", "write") is True

    async def test_revoke_access(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "read")
        assert await space_manager.check_access(space["id"], "user-2", "read") is True

        revoked = await space_manager.revoke_access(space["id"], "user-2", revoked_by="user-1")
        assert revoked is True
        assert await space_manager.check_access(space["id"], "user-2", "read") is False

    async def test_revoke_by_non_admin_raises(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "read")

        with pytest.raises(PermissionError):
            await space_manager.revoke_access(space["id"], "user-2", revoked_by="user-3")

    async def test_list_members(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "read")
        await grant(space_manager, space["id"], "user-3", "write")

        members = await space_manager.list_members(space["id"])
        assert len(members) == 3  # owner + 2 grantees
        agent_ids = {m["agent_id"] for m in members}
        assert agent_ids == {"user-1", "user-2", "user-3"}


# ---------------------------------------------------------------------------
# Memory membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMemoryMembership:
    async def test_add_memory_to_space(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        # owner has admin which implies write
        added = await space_manager.add_memory_to_space("mem-1", space["id"], "user-1")
        assert added is True

        mem_ids = await space_manager.get_space_memory_ids(space["id"])
        assert "mem-1" in mem_ids

    async def test_add_memory_no_write_access_raises(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        # user-2 has no access
        with pytest.raises(PermissionError, match="Write access required"):
            await space_manager.add_memory_to_space("mem-1", space["id"], "user-2")

    async def test_add_memory_with_read_only_raises(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "read")
        with pytest.raises(PermissionError, match="Write access required"):
            await space_manager.add_memory_to_space("mem-1", space["id"], "user-2")

    async def test_add_memory_with_write_access(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await grant(space_manager, space["id"], "user-2", "write")
        added = await space_manager.add_memory_to_space("mem-u2", space["id"], "user-2")
        assert added is True
        # ...but never someone else's memory.
        with pytest.raises(LookupError):
            await space_manager.add_memory_to_space("mem-1", space["id"], "user-2")

    async def test_add_duplicate_memory_idempotent(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await space_manager.add_memory_to_space("mem-1", space["id"], "user-1")
        await space_manager.add_memory_to_space("mem-1", space["id"], "user-1")

        mem_ids = await space_manager.get_space_memory_ids(space["id"])
        assert mem_ids.count("mem-1") == 1

    async def test_remove_memory_from_space(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await space_manager.add_memory_to_space("mem-1", space["id"], "user-1")
        removed = await space_manager.remove_memory_from_space("mem-1", space["id"], "user-1")
        assert removed is True

        mem_ids = await space_manager.get_space_memory_ids(space["id"])
        assert "mem-1" not in mem_ids

    async def test_remove_memory_no_write_access_raises(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await space_manager.add_memory_to_space("mem-1", space["id"], "user-1")
        with pytest.raises(PermissionError, match="Write access required"):
            await space_manager.remove_memory_from_space("mem-1", space["id"], "user-2")

    async def test_get_space_memory_ids(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        await space_manager.add_memory_to_space("mem-1", space["id"], "user-1")
        await space_manager.add_memory_to_space("mem-2", space["id"], "user-1")
        await space_manager.add_memory_to_space("mem-3", space["id"], "user-1")

        mem_ids = await space_manager.get_space_memory_ids(space["id"])
        assert set(mem_ids) == {"mem-1", "mem-2", "mem-3"}

    async def test_get_space_memory_ids_limit(self, space_manager):
        space = await space_manager.create_space(name="s1", owner_id="user-1")
        for i in range(5):
            await space_manager.add_memory_to_space(f"mem-{i}", space["id"], "user-1")

        mem_ids = await space_manager.get_space_memory_ids(space["id"], limit=3)
        assert len(mem_ids) == 3

    async def test_get_accessible_space_ids(self, space_manager):
        s1 = await space_manager.create_space(name="s1", owner_id="user-1")
        s2 = await space_manager.create_space(name="s2", owner_id="user-1")
        await space_manager.create_space(name="s3", owner_id="user-2")

        ids = await space_manager.get_accessible_space_ids("user-1")
        assert set(ids) == {s1["id"], s2["id"]}

    async def test_get_memory_spaces(self, space_manager):
        s1 = await space_manager.create_space(name="space-a", owner_id="user-1")
        s2 = await space_manager.create_space(name="space-b", owner_id="user-1")
        await space_manager.add_memory_to_space("mem-1", s1["id"], "user-1")
        await space_manager.add_memory_to_space("mem-1", s2["id"], "user-1")

        spaces = await space_manager.get_memory_spaces("mem-1")
        assert len(spaces) == 2
        names = {s["space_name"] for s in spaces}
        assert names == {"space-a", "space-b"}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_valid_permissions(self):
        assert {"read", "write", "admin"} == VALID_PERMISSIONS
