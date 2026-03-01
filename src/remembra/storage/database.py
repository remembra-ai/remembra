"""SQLite metadata database for entities, relationships, and memory metadata."""

import json
from datetime import datetime
from typing import Any

import aiosqlite
import structlog

from remembra.config import Settings
from remembra.models.memory import Entity, EntityRef, Relationship

log = structlog.get_logger(__name__)

# SQL schemas
SCHEMA_SQL = """
-- Memories metadata (vector lives in Qdrant, metadata here)
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    content TEXT NOT NULL,
    extracted_facts TEXT,  -- JSON array
    metadata TEXT,  -- JSON object
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(user_id, project_id);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

-- Entities (people, places, things)
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    canonical_name TEXT NOT NULL,
    aliases TEXT,  -- JSON array
    type TEXT NOT NULL,  -- person, company, place, concept
    attributes TEXT,  -- JSON object
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_user ON entities(user_id);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

-- Relationships between entities
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT NOT NULL,
    to_entity_id TEXT NOT NULL,
    type TEXT NOT NULL,  -- works_at, knows, married_to, etc.
    properties TEXT,  -- JSON object
    confidence REAL DEFAULT 1.0,
    source_memory_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (from_entity_id) REFERENCES entities(id),
    FOREIGN KEY (to_entity_id) REFERENCES entities(id),
    FOREIGN KEY (source_memory_id) REFERENCES memories(id)
);

CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(type);

-- Memory-Entity associations
CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
"""


class Database:
    """
    Async SQLite database for metadata storage.
    
    Stores:
    - Memory metadata (content lives in Qdrant)
    - Entities and their aliases
    - Relationships between entities
    - Memory-entity associations
    """

    def __init__(self, db_path: str = "remembra.db"):
        # Extract path from connection string if needed
        if db_path.startswith("sqlite"):
            db_path = db_path.split("///")[-1]
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open database connection."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA foreign_keys = ON")
        log.info("database_connected", path=self.db_path)

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            log.info("database_closed")

    async def init_schema(self) -> None:
        """Create tables if they don't exist."""
        if not self._connection:
            await self.connect()
        
        await self._connection.executescript(SCHEMA_SQL)
        await self._connection.commit()
        log.info("database_schema_initialized")

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._connection:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._connection

    # -----------------------------------------------------------------------
    # Memory operations
    # -----------------------------------------------------------------------

    async def save_memory_metadata(
        self,
        memory_id: str,
        user_id: str,
        project_id: str,
        content: str,
        extracted_facts: list[str],
        metadata: dict[str, Any],
        created_at: datetime,
        expires_at: datetime | None = None,
    ) -> None:
        """Save memory metadata to SQLite."""
        await self.conn.execute(
            """
            INSERT INTO memories (id, user_id, project_id, content, extracted_facts, 
                                  metadata, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                extracted_facts = excluded.extracted_facts,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (
                memory_id,
                user_id,
                project_id,
                content,
                json.dumps(extracted_facts),
                json.dumps(metadata),
                created_at.isoformat(),
                datetime.utcnow().isoformat(),
                expires_at.isoformat() if expires_at else None,
            ),
        )
        await self.conn.commit()

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Get memory metadata by ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory and its associations."""
        cursor = await self.conn.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memories for a user."""
        cursor = await self.conn.execute(
            "DELETE FROM memories WHERE user_id = ?", (user_id,)
        )
        await self.conn.commit()
        return cursor.rowcount

    async def update_access(self, memory_id: str) -> None:
        """Update access count and timestamp."""
        await self.conn.execute(
            """
            UPDATE memories 
            SET access_count = access_count + 1, last_accessed = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), memory_id),
        )
        await self.conn.commit()

    # -----------------------------------------------------------------------
    # Entity operations
    # -----------------------------------------------------------------------

    async def save_entity(self, entity: Entity, user_id: str, project_id: str = "default") -> None:
        """Save or update an entity."""
        await self.conn.execute(
            """
            INSERT INTO entities (id, user_id, project_id, canonical_name, aliases, 
                                  type, attributes, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                aliases = excluded.aliases,
                attributes = excluded.attributes,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (
                entity.id,
                user_id,
                project_id,
                entity.canonical_name,
                json.dumps(entity.aliases),
                entity.type,
                json.dumps(entity.attributes),
                entity.confidence,
                entity.created_at.isoformat(),
                datetime.utcnow().isoformat(),
            ),
        )
        await self.conn.commit()

    async def find_entity_by_name(
        self, name: str, user_id: str, project_id: str = "default"
    ) -> Entity | None:
        """Find entity by canonical name or alias."""
        # First try canonical name
        cursor = await self.conn.execute(
            """
            SELECT * FROM entities 
            WHERE user_id = ? AND project_id = ? AND canonical_name = ?
            """,
            (user_id, project_id, name),
        )
        row = await cursor.fetchone()

        if not row:
            # Search in aliases (JSON array)
            cursor = await self.conn.execute(
                """
                SELECT * FROM entities 
                WHERE user_id = ? AND project_id = ? 
                AND aliases LIKE ?
                """,
                (user_id, project_id, f'%"{name}"%'),
            )
            row = await cursor.fetchone()

        if not row:
            return None

        return Entity(
            id=row["id"],
            canonical_name=row["canonical_name"],
            aliases=json.loads(row["aliases"]) if row["aliases"] else [],
            type=row["type"],
            attributes=json.loads(row["attributes"]) if row["attributes"] else {},
            confidence=row["confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def get_user_entities(
        self, user_id: str, project_id: str = "default"
    ) -> list[Entity]:
        """Get all entities for a user with full details including aliases."""
        cursor = await self.conn.execute(
            """
            SELECT id, canonical_name, type, aliases, attributes, confidence 
            FROM entities 
            WHERE user_id = ? AND project_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id, project_id),
        )
        rows = await cursor.fetchall()

        return [
            Entity(
                id=row["id"],
                canonical_name=row["canonical_name"],
                type=row["type"],
                aliases=json.loads(row["aliases"] or "[]"),
                attributes=json.loads(row["attributes"] or "{}"),
                confidence=row["confidence"],
            )
            for row in rows
        ]

    async def delete_user_entities(self, user_id: str) -> int:
        """Delete all entities for a user."""
        cursor = await self.conn.execute(
            "DELETE FROM entities WHERE user_id = ?", (user_id,)
        )
        await self.conn.commit()
        return cursor.rowcount
    
    async def get_entity(self, entity_id: str) -> Entity | None:
        """Get entity by ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return Entity(
            id=row["id"],
            canonical_name=row["canonical_name"],
            type=row["type"],
            aliases=json.loads(row["aliases"] or "[]"),
            attributes=json.loads(row["attributes"] or "{}"),
            confidence=row["confidence"],
        )
    
    async def get_entities_by_type(
        self, 
        user_id: str, 
        project_id: str, 
        entity_type: str
    ) -> list[Entity]:
        """Get all entities of a specific type for a user/project."""
        cursor = await self.conn.execute(
            """
            SELECT * FROM entities 
            WHERE user_id = ? AND project_id = ? AND LOWER(type) = LOWER(?)
            """,
            (user_id, project_id, entity_type),
        )
        rows = await cursor.fetchall()
        return [
            Entity(
                id=row["id"],
                canonical_name=row["canonical_name"],
                type=row["type"],
                aliases=json.loads(row["aliases"] or "[]"),
                attributes=json.loads(row["attributes"] or "{}"),
                confidence=row["confidence"],
            )
            for row in rows
        ]
    
    async def update_entity_aliases(self, entity_id: str, aliases: list[str]) -> None:
        """Update aliases for an entity."""
        await self.conn.execute(
            "UPDATE entities SET aliases = ?, updated_at = ? WHERE id = ?",
            (json.dumps(aliases), datetime.utcnow().isoformat(), entity_id),
        )
        await self.conn.commit()
    
    async def link_memory_to_entity(self, memory_id: str, entity_id: str) -> None:
        """Link a memory to an entity."""
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_entities (memory_id, entity_id, confidence)
            VALUES (?, ?, 1.0)
            """,
            (memory_id, entity_id),
        )
        await self.conn.commit()
    
    async def get_memories_by_entity(self, entity_id: str) -> list[str]:
        """Get all memory IDs linked to an entity."""
        cursor = await self.conn.execute(
            "SELECT memory_id FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        rows = await cursor.fetchall()
        return [row["memory_id"] for row in rows]

    # -----------------------------------------------------------------------
    # Relationship operations
    # -----------------------------------------------------------------------

    async def save_relationship(self, rel: Relationship) -> None:
        """Save a relationship between entities."""
        await self.conn.execute(
            """
            INSERT INTO relationships (id, from_entity_id, to_entity_id, type, 
                                        properties, confidence, source_memory_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                properties = excluded.properties,
                confidence = excluded.confidence
            """,
            (
                rel.id,
                rel.from_entity_id,
                rel.to_entity_id,
                rel.type,
                json.dumps(rel.properties),
                rel.confidence,
                rel.source_memory_id,
                datetime.utcnow().isoformat(),
            ),
        )
        await self.conn.commit()

    async def get_entity_relationships(self, entity_id: str) -> list[Relationship]:
        """Get all relationships for an entity."""
        cursor = await self.conn.execute(
            """
            SELECT * FROM relationships 
            WHERE from_entity_id = ? OR to_entity_id = ?
            """,
            (entity_id, entity_id),
        )
        rows = await cursor.fetchall()

        return [
            Relationship(
                id=row["id"],
                from_entity_id=row["from_entity_id"],
                to_entity_id=row["to_entity_id"],
                type=row["type"],
                properties=json.loads(row["properties"]) if row["properties"] else {},
                confidence=row["confidence"],
                source_memory_id=row["source_memory_id"],
            )
            for row in rows
        ]

    async def delete_user_relationships(self, user_id: str) -> int:
        """Delete all relationships for a user's entities."""
        cursor = await self.conn.execute(
            """
            DELETE FROM relationships 
            WHERE from_entity_id IN (SELECT id FROM entities WHERE user_id = ?)
            OR to_entity_id IN (SELECT id FROM entities WHERE user_id = ?)
            """,
            (user_id, user_id),
        )
        await self.conn.commit()
        return cursor.rowcount

    # -----------------------------------------------------------------------
    # Memory-Entity associations
    # -----------------------------------------------------------------------

    async def link_memory_entity(
        self, memory_id: str, entity_id: str, confidence: float = 1.0
    ) -> None:
        """Link a memory to an entity."""
        await self.conn.execute(
            """
            INSERT INTO memory_entities (memory_id, entity_id, confidence)
            VALUES (?, ?, ?)
            ON CONFLICT DO UPDATE SET confidence = excluded.confidence
            """,
            (memory_id, entity_id, confidence),
        )
        await self.conn.commit()

    async def get_memory_entities(self, memory_id: str) -> list[EntityRef]:
        """Get entities associated with a memory."""
        cursor = await self.conn.execute(
            """
            SELECT e.id, e.canonical_name, e.type, me.confidence
            FROM entities e
            JOIN memory_entities me ON e.id = me.entity_id
            WHERE me.memory_id = ?
            """,
            (memory_id,),
        )
        rows = await cursor.fetchall()

        return [
            EntityRef(
                id=row["id"],
                canonical_name=row["canonical_name"],
                type=row["type"],
                confidence=row["confidence"],
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Initialization helper
# ---------------------------------------------------------------------------

async def init_db(settings: Settings) -> Database:
    """Initialize and return a connected database."""
    db = Database(settings.database_url)
    await db.connect()
    await db.init_schema()
    return db
