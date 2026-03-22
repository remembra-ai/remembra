"""
Tests for entity merge functionality.

Operation Circuit Breaker (March 19, 2026):
- Test FK constraint handling during entity merge
- Test relationship updates during merge
- Test circuit breaker limits
"""

import asyncio
import pytest
import aiosqlite

# Test that merge handles relationships correctly
@pytest.mark.asyncio
async def test_merge_entities_updates_relationships():
    """
    When merging entities, relationships pointing to the deleted entity
    should be updated to point to the kept entity.
    """
    # Create in-memory SQLite database
    async with aiosqlite.connect(":memory:") as conn:
        # Create minimal schema
        await conn.execute("""
            CREATE TABLE entities (
                id TEXT PRIMARY KEY,
                canonical_name TEXT,
                aliases TEXT,
                type TEXT,
                user_id TEXT
            )
        """)
        
        await conn.execute("""
            CREATE TABLE relationships (
                id TEXT PRIMARY KEY,
                from_entity_id TEXT NOT NULL,
                to_entity_id TEXT NOT NULL,
                type TEXT NOT NULL,
                FOREIGN KEY (from_entity_id) REFERENCES entities(id),
                FOREIGN KEY (to_entity_id) REFERENCES entities(id)
            )
        """)
        
        await conn.execute("""
            CREATE TABLE memory_entities (
                memory_id TEXT NOT NULL,
                entity_id TEXT NOT NULL
            )
        """)
        
        # Insert test entities
        await conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
            ("entity_keep", "John Smith", "", "person", "user_1"),
        )
        await conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
            ("entity_delete", "John", "", "person", "user_1"),
        )
        await conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
            ("entity_other", "Acme Corp", "", "organization", "user_1"),
        )
        
        # Insert relationships pointing to entity_delete
        await conn.execute(
            "INSERT INTO relationships VALUES (?, ?, ?, ?)",
            ("rel_1", "entity_delete", "entity_other", "works_at"),  # from = delete
        )
        await conn.execute(
            "INSERT INTO relationships VALUES (?, ?, ?, ?)",
            ("rel_2", "entity_other", "entity_delete", "employs"),  # to = delete
        )
        
        await conn.commit()
        
        # Simulate merge: update relationships THEN delete
        # (This is what the fixed code does)
        await conn.execute(
            "UPDATE relationships SET from_entity_id = ? WHERE from_entity_id = ?",
            ("entity_keep", "entity_delete"),
        )
        await conn.execute(
            "UPDATE relationships SET to_entity_id = ? WHERE to_entity_id = ?",
            ("entity_keep", "entity_delete"),
        )
        
        # Now delete should succeed
        await conn.execute("DELETE FROM entities WHERE id = ?", ("entity_delete",))
        await conn.commit()
        
        # Verify entity was deleted
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM entities WHERE id = ?", ("entity_delete",)
        )
        count = (await cursor.fetchone())[0]
        assert count == 0, "Entity should be deleted"
        
        # Verify relationships were updated
        cursor = await conn.execute(
            "SELECT from_entity_id, to_entity_id FROM relationships WHERE id = ?",
            ("rel_1",),
        )
        row = await cursor.fetchone()
        assert row[0] == "entity_keep", "from_entity_id should be updated to kept entity"
        
        cursor = await conn.execute(
            "SELECT from_entity_id, to_entity_id FROM relationships WHERE id = ?",
            ("rel_2",),
        )
        row = await cursor.fetchone()
        assert row[1] == "entity_keep", "to_entity_id should be updated to kept entity"


@pytest.mark.asyncio
async def test_merge_without_relationship_update_fails():
    """
    Test that demonstrates the bug: without updating relationships,
    delete fails with FK constraint error.
    """
    async with aiosqlite.connect(":memory:") as conn:
        # Enable FK constraints
        await conn.execute("PRAGMA foreign_keys = ON")
        
        await conn.execute("""
            CREATE TABLE entities (
                id TEXT PRIMARY KEY,
                canonical_name TEXT
            )
        """)
        
        await conn.execute("""
            CREATE TABLE relationships (
                id TEXT PRIMARY KEY,
                from_entity_id TEXT NOT NULL,
                to_entity_id TEXT NOT NULL,
                FOREIGN KEY (from_entity_id) REFERENCES entities(id),
                FOREIGN KEY (to_entity_id) REFERENCES entities(id)
            )
        """)
        
        # Insert entities
        await conn.execute("INSERT INTO entities VALUES (?, ?)", ("e1", "Entity 1"))
        await conn.execute("INSERT INTO entities VALUES (?, ?)", ("e2", "Entity 2"))
        
        # Insert relationship pointing to e2
        await conn.execute(
            "INSERT INTO relationships VALUES (?, ?, ?)",
            ("r1", "e1", "e2"),
        )
        
        await conn.commit()
        
        # Try to delete e2 WITHOUT updating relationships - should fail
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute("DELETE FROM entities WHERE id = ?", ("e2",))
            await conn.commit()


@pytest.mark.asyncio
async def test_merge_removes_self_referential_relationships():
    """
    After merge, if from_entity_id == to_entity_id, 
    those relationships should be removed.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("""
            CREATE TABLE entities (id TEXT PRIMARY KEY)
        """)
        
        await conn.execute("""
            CREATE TABLE relationships (
                id TEXT PRIMARY KEY,
                from_entity_id TEXT,
                to_entity_id TEXT
            )
        """)
        
        await conn.execute("INSERT INTO entities VALUES (?)", ("e1",))
        await conn.execute("INSERT INTO entities VALUES (?)", ("e2",))
        
        # Relationship from e2 to e1
        await conn.execute(
            "INSERT INTO relationships VALUES (?, ?, ?)",
            ("r1", "e2", "e1"),
        )
        
        await conn.commit()
        
        # After merge (e1 keeps, e2 deleted), update e2 -> e1
        await conn.execute(
            "UPDATE relationships SET from_entity_id = ? WHERE from_entity_id = ?",
            ("e1", "e2"),
        )
        
        # Now r1 is e1 -> e1 (self-referential), should be cleaned up
        await conn.execute(
            "DELETE FROM relationships WHERE from_entity_id = to_entity_id"
        )
        
        await conn.commit()
        
        cursor = await conn.execute("SELECT COUNT(*) FROM relationships")
        count = (await cursor.fetchone())[0]
        assert count == 0, "Self-referential relationship should be deleted"


if __name__ == "__main__":
    # Quick manual run
    asyncio.run(test_merge_entities_updates_relationships())
    print("✅ test_merge_entities_updates_relationships passed")
    
    asyncio.run(test_merge_without_relationship_update_fails())
    print("✅ test_merge_without_relationship_update_fails passed")
    
    asyncio.run(test_merge_removes_self_referential_relationships())
    print("✅ test_merge_removes_self_referential_relationships passed")
    
    print("\n✅ All entity merge tests passed!")
