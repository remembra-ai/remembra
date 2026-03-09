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
    last_accessed TEXT,
    -- Memory provenance columns (Week 7 - Security)
    source TEXT DEFAULT 'user_input',  -- user_input, agent_generated, external_api
    trust_score REAL DEFAULT 1.0,  -- 0.0-1.0 confidence rating
    checksum TEXT  -- SHA-256 hash for integrity verification
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(user_id, project_id);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

-- API Keys table (Week 7 - Authentication)
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,  -- bcrypt hash of the API key
    user_id TEXT NOT NULL,
    name TEXT,  -- "Production Key", "Dev Key"
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    active BOOLEAN DEFAULT TRUE,
    rate_limit_tier TEXT DEFAULT 'standard'  -- 'standard', 'premium'
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- Audit log table (Week 7 - Security)
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT NOT NULL,
    api_key_id TEXT,
    action TEXT NOT NULL,  -- 'store', 'recall', 'forget', 'key_created', 'key_revoked', 'auth_failed'
    resource_id TEXT,  -- memory_id or key_id
    ip_address TEXT,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, timestamp);

-- Users table (User Authentication)
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    email_verified BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    totp_secret TEXT,
    totp_enabled BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Password reset tokens
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    user_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Token blacklist (for logout/invalidation)
CREATE TABLE IF NOT EXISTS token_blacklist (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_token_blacklist_expires ON token_blacklist(expires_at);

-- FTS5 full-text search index for BM25 keyword matching
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED,
    user_id UNINDEXED, 
    project_id UNINDEXED,
    content,
    tokenize='porter unicode61'
);

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

-- Relationships between entities (with temporal validity for point-in-time queries)
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT NOT NULL,
    to_entity_id TEXT NOT NULL,
    type TEXT NOT NULL,  -- works_at, knows, married_to, etc.
    properties TEXT,  -- JSON object
    confidence REAL DEFAULT 1.0,
    source_memory_id TEXT,
    created_at TEXT NOT NULL,
    -- Temporal validity (bi-temporal model)
    valid_from TEXT NOT NULL DEFAULT (datetime('now')),  -- When relationship became true
    valid_to TEXT,  -- When relationship stopped being true (NULL = still valid)
    superseded_by TEXT,  -- ID of relationship that supersedes this one
    FOREIGN KEY (from_entity_id) REFERENCES entities(id),
    FOREIGN KEY (to_entity_id) REFERENCES entities(id),
    FOREIGN KEY (source_memory_id) REFERENCES memories(id),
    FOREIGN KEY (superseded_by) REFERENCES relationships(id)
);

CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(type);
CREATE INDEX IF NOT EXISTS idx_rel_valid_from ON relationships(valid_from);
CREATE INDEX IF NOT EXISTS idx_rel_valid_to ON relationships(valid_to);
CREATE INDEX IF NOT EXISTS idx_rel_current ON relationships(valid_to) WHERE valid_to IS NULL;

-- Memory-Entity associations
CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

-- Teams (collaborative workspaces)
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    owner_id TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    max_seats INTEGER NOT NULL DEFAULT 5,
    used_seats INTEGER NOT NULL DEFAULT 1,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_teams_slug ON teams(slug);
CREATE INDEX IF NOT EXISTS idx_teams_owner ON teams(owner_id);

-- Team members
CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    invited_by TEXT,
    joined_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id),
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);

-- Team invites
CREATE TABLE IF NOT EXISTS team_invites (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    invited_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_team_invites_token ON team_invites(token_hash);
CREATE INDEX IF NOT EXISTS idx_team_invites_email ON team_invites(email);
CREATE INDEX IF NOT EXISTS idx_team_invites_status ON team_invites(team_id, status);

-- Team spaces junction (link spaces to teams)
CREATE TABLE IF NOT EXISTS team_spaces (
    team_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (team_id, space_id),
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_team_spaces_space ON team_spaces(space_id);
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
        
        # Run migrations for existing tables
        await self._run_migrations()
        
        log.info("database_schema_initialized")

    async def _run_migrations(self) -> None:
        """Apply migrations for existing databases (adds missing columns)."""
        # Memory provenance columns (Week 7)
        # TOTP/2FA columns (security hardening)
        migrations = [
            "ALTER TABLE memories ADD COLUMN source TEXT DEFAULT 'user_input'",
            "ALTER TABLE memories ADD COLUMN trust_score REAL DEFAULT 1.0",
            "ALTER TABLE memories ADD COLUMN checksum TEXT",
            "ALTER TABLE users ADD COLUMN totp_secret TEXT",
            "ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT FALSE",
            # Temporal edges for relationships (v0.8.4)
            "ALTER TABLE relationships ADD COLUMN valid_from TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE relationships ADD COLUMN valid_to TEXT",
            "ALTER TABLE relationships ADD COLUMN superseded_by TEXT",
        ]
        
        for migration in migrations:
            try:
                await self._connection.execute(migration)
            except Exception:
                # Column likely already exists, ignore
                pass
        
        await self._connection.commit()

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
        source: str = "user_input",
        trust_score: float = 1.0,
        checksum: str | None = None,
    ) -> None:
        """Save memory metadata to SQLite."""
        await self.conn.execute(
            """
            INSERT INTO memories (id, user_id, project_id, content, extracted_facts, 
                                  metadata, created_at, updated_at, expires_at,
                                  source, trust_score, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                extracted_facts = excluded.extracted_facts,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                source = excluded.source,
                trust_score = excluded.trust_score,
                checksum = excluded.checksum
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
                source,
                trust_score,
                checksum,
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

    async def update_memory(
        self,
        memory_id: str,
        content: str,
        extracted_facts: list[str],
        metadata: dict[str, Any],
    ) -> None:
        """Update memory content, facts, and metadata."""
        await self.conn.execute(
            """
            UPDATE memories 
            SET content = ?, extracted_facts = ?, metadata = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                content,
                json.dumps(extracted_facts),
                json.dumps(metadata),
                datetime.utcnow().isoformat(),
                memory_id,
            ),
        )
        # Update FTS index
        await self.conn.execute(
            "DELETE FROM memories_fts WHERE id = ?",
            (memory_id,),
        )
        # Re-fetch to get user_id and project_id for FTS
        mem = await self.get_memory(memory_id)
        if mem:
            await self.conn.execute(
                """
                INSERT INTO memories_fts (id, user_id, project_id, content)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, mem["user_id"], mem["project_id"], content),
            )
        await self.conn.commit()

    async def delete_memory_entities(self, memory_id: str) -> None:
        """Delete all entity links for a memory."""
        await self.conn.execute(
            "DELETE FROM memory_entities WHERE memory_id = ?",
            (memory_id,),
        )
        await self.conn.commit()

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory and its associations.
        
        Properly handles FK constraints by deleting relationships first.
        """
        # Delete relationships that reference this memory as source
        # (source_memory_id FK doesn't have CASCADE)
        await self.conn.execute(
            "DELETE FROM relationships WHERE source_memory_id = ?",
            (memory_id,),
        )
        
        # memory_entities has ON DELETE CASCADE, but explicit delete is cleaner
        await self.conn.execute(
            "DELETE FROM memory_entities WHERE memory_id = ?",
            (memory_id,),
        )
        
        # Now safe to delete the memory
        cursor = await self.conn.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memories for a user.
        
        Properly handles FK constraints by deleting relationships first.
        """
        # First, get all memory IDs for this user
        cursor = await self.conn.execute(
            "SELECT id FROM memories WHERE user_id = ?", (user_id,)
        )
        memory_ids = [row[0] for row in await cursor.fetchall()]
        
        if not memory_ids:
            return 0
        
        # Delete relationships that reference these memories as source
        # (source_memory_id FK doesn't have CASCADE)
        placeholders = ",".join("?" * len(memory_ids))
        await self.conn.execute(
            f"DELETE FROM relationships WHERE source_memory_id IN ({placeholders})",
            memory_ids,
        )
        
        # Delete memory_entities (has CASCADE but explicit is cleaner)
        await self.conn.execute(
            f"DELETE FROM memory_entities WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        
        # Now safe to delete the memories
        cursor = await self.conn.execute(
            "DELETE FROM memories WHERE user_id = ?", (user_id,)
        )
        await self.conn.commit()
        return cursor.rowcount

    async def migrate_memory_relationships(
        self,
        old_memory_id: str,
        new_memory_id: str,
    ) -> int:
        """
        Migrate relationships from old memory to new memory.
        
        Used during UPDATE consolidation to preserve entity links.
        """
        # Update relationships that reference the old memory as source
        cursor = await self.conn.execute(
            "UPDATE relationships SET source_memory_id = ? WHERE source_memory_id = ?",
            (new_memory_id, old_memory_id),
        )
        rel_count = cursor.rowcount
        
        # Migrate memory_entity links
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_entities (memory_id, entity_id, confidence)
            SELECT ?, entity_id, confidence FROM memory_entities WHERE memory_id = ?
            """,
            (new_memory_id, old_memory_id),
        )
        
        await self.conn.commit()
        return rel_count

    # -----------------------------------------------------------------------
    # Temporal queries (Week 8)
    # -----------------------------------------------------------------------

    async def get_expired_memories(
        self,
        user_id: str | None = None,
        project_id: str = "default",
        before: datetime | None = None,
    ) -> list[str]:
        """
        Get IDs of expired memories (expires_at < now).
        
        Args:
            user_id: Filter by user (optional)
            project_id: Project namespace
            before: Check expiry before this time (default: now)
        """
        check_time = (before or datetime.utcnow()).isoformat()
        
        query = """
            SELECT id FROM memories 
            WHERE expires_at IS NOT NULL AND expires_at < ?
        """
        params: list[Any] = [check_time]
        
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        
        query += " AND project_id = ?"
        params.append(project_id)
        
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def get_memories_as_of(
        self,
        user_id: str,
        project_id: str,
        as_of: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get memories that existed at a specific point in time.
        
        Returns memories where:
        - created_at <= as_of
        - (expires_at IS NULL OR expires_at > as_of)
        
        This enables "time travel" queries to see historical state.
        """
        cursor = await self.conn.execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND project_id = ?
              AND created_at <= ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, project_id, as_of.isoformat(), as_of.isoformat(), limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_memory_with_decay(
        self,
        memory_id: str,
    ) -> dict[str, Any] | None:
        """
        Get memory with decay metadata (access_count, last_accessed).
        
        Used for calculating decay scores.
        """
        cursor = await self.conn.execute(
            """
            SELECT id, content, created_at, updated_at, expires_at, 
                   access_count, last_accessed
            FROM memories WHERE id = ?
            """,
            (memory_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_memories_with_decay_info(
        self,
        user_id: str,
        project_id: str = "default",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get all memories with decay/access metadata.
        
        Returns memories with access_count and last_accessed for decay calculation.
        """
        cursor = await self.conn.execute(
            """
            SELECT id, content, created_at, updated_at, expires_at,
                   access_count, last_accessed
            FROM memories
            WHERE user_id = ? AND project_id = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, project_id, datetime.utcnow().isoformat(), limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def cleanup_expired_memories(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """
        Delete all expired memories.
        
        Returns count of deleted memories.
        """
        now = datetime.utcnow().isoformat()
        
        query = "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?"
        params: list[Any] = [now]
        
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        
        cursor = await self.conn.execute(query, params)
        expired_ids = [row["id"] for row in await cursor.fetchall()]
        
        # Delete each memory properly (handles FK constraints)
        count = 0
        for memory_id in expired_ids:
            if await self.delete_memory(memory_id):
                count += 1
        
        return count

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
    # FTS5 Full-Text Search (BM25)
    # -----------------------------------------------------------------------

    async def index_memory_fts(
        self,
        memory_id: str,
        user_id: str,
        project_id: str,
        content: str,
    ) -> None:
        """Index a memory in FTS5 for keyword search."""
        # Delete existing entry first (upsert)
        await self.conn.execute(
            "DELETE FROM memories_fts WHERE id = ?",
            (memory_id,),
        )
        await self.conn.execute(
            """
            INSERT INTO memories_fts (id, user_id, project_id, content)
            VALUES (?, ?, ?, ?)
            """,
            (memory_id, user_id, project_id, content),
        )
        await self.conn.commit()

    async def delete_memory_fts(self, memory_id: str) -> None:
        """Remove a memory from FTS5 index."""
        await self.conn.execute(
            "DELETE FROM memories_fts WHERE id = ?",
            (memory_id,),
        )
        await self.conn.commit()

    async def search_fts(
        self,
        query: str,
        user_id: str,
        project_id: str = "default",
        limit: int = 20,
    ) -> list[tuple[str, float]]:
        """
        Perform FTS5 BM25 search.
        
        Returns list of (memory_id, bm25_score) tuples, sorted by relevance.
        BM25 scores are negative (closer to 0 = more relevant).
        """
        # Escape special FTS5 characters
        safe_query = query.replace('"', '""')
        
        cursor = await self.conn.execute(
            """
            SELECT id, bm25(memories_fts) as score
            FROM memories_fts
            WHERE user_id = ? AND project_id = ? 
              AND memories_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (user_id, project_id, safe_query, limit),
        )
        rows = await cursor.fetchall()
        
        # Convert negative BM25 scores to positive (negate them)
        return [(row["id"], -row["score"]) for row in rows]

    async def get_all_memory_content_for_user(
        self,
        user_id: str,
        project_id: str = "default",
    ) -> list[tuple[str, str]]:
        """Get all (id, content) pairs for a user's memories."""
        cursor = await self.conn.execute(
            """
            SELECT id, content FROM memories 
            WHERE user_id = ? AND project_id = ?
            """,
            (user_id, project_id),
        )
        rows = await cursor.fetchall()
        return [(row["id"], row["content"]) for row in rows]

    async def rebuild_fts_index(self, user_id: str, project_id: str = "default") -> int:
        """Rebuild FTS5 index for a user's memories. Returns count indexed."""
        memories = await self.get_all_memory_content_for_user(user_id, project_id)
        
        for memory_id, content in memories:
            await self.index_memory_fts(memory_id, user_id, project_id, content)
        
        return len(memories)

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
        self, user_id: str, project_id: str | None = None
    ) -> list[Entity]:
        """Get all entities for a user with full details including aliases.
        
        If project_id is None, returns entities from ALL projects.
        """
        if project_id:
            cursor = await self.conn.execute(
                """
                SELECT id, canonical_name, type, aliases, attributes, confidence 
                FROM entities 
                WHERE user_id = ? AND project_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id, project_id),
            )
        else:
            # No project filter - get ALL entities for user
            cursor = await self.conn.execute(
                """
                SELECT id, canonical_name, type, aliases, attributes, confidence 
                FROM entities 
                WHERE user_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id,),
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
        project_id: str | None, 
        entity_type: str
    ) -> list[Entity]:
        """Get all entities of a specific type for a user/project.
        
        If project_id is None, returns entities from ALL projects.
        """
        if project_id:
            cursor = await self.conn.execute(
                """
                SELECT * FROM entities 
                WHERE user_id = ? AND project_id = ? AND LOWER(type) = LOWER(?)
                """,
                (user_id, project_id, entity_type),
            )
        else:
            cursor = await self.conn.execute(
                """
                SELECT * FROM entities 
                WHERE user_id = ? AND LOWER(type) = LOWER(?)
                """,
                (user_id, entity_type),
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
        """Save a relationship between entities with temporal validity."""
        await self.conn.execute(
            """
            INSERT INTO relationships (id, from_entity_id, to_entity_id, type, 
                                        properties, confidence, source_memory_id, created_at,
                                        valid_from, valid_to, superseded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                properties = excluded.properties,
                confidence = excluded.confidence,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                superseded_by = excluded.superseded_by
            """,
            (
                rel.id,
                rel.from_entity_id,
                rel.to_entity_id,
                rel.type,
                json.dumps(rel.properties),
                rel.confidence,
                rel.source_memory_id,
                rel.created_at.isoformat(),
                rel.valid_from.isoformat(),
                rel.valid_to.isoformat() if rel.valid_to else None,
                rel.superseded_by,
            ),
        )
        await self.conn.commit()

    async def get_entity_relationships(
        self,
        entity_id: str,
        as_of: datetime | None = None,
        include_superseded: bool = False,
    ) -> list[Relationship]:
        """Get relationships for an entity with temporal filtering.
        
        Args:
            entity_id: Entity to get relationships for
            as_of: Optional point-in-time filter. Returns relationships valid at this time.
            include_superseded: If True, include superseded relationships (default: False)
        """
        cursor = await self.conn.execute(
            """
            SELECT * FROM relationships 
            WHERE from_entity_id = ? OR to_entity_id = ?
            """,
            (entity_id, entity_id),
        )
        rows = await cursor.fetchall()

        relationships = []
        for row in rows:
            # Parse temporal fields
            valid_from = datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else datetime.utcnow()
            valid_to = datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None
            created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow()
            
            rel = Relationship(
                id=row["id"],
                from_entity_id=row["from_entity_id"],
                to_entity_id=row["to_entity_id"],
                type=row["type"],
                properties=json.loads(row["properties"]) if row["properties"] else {},
                confidence=row["confidence"],
                source_memory_id=row["source_memory_id"],
                valid_from=valid_from,
                valid_to=valid_to,
                created_at=created_at,
                superseded_by=row["superseded_by"],
            )
            
            # Apply temporal filtering
            if as_of is not None:
                if not rel.is_valid_at(as_of):
                    continue
            elif not include_superseded:
                # For current queries, exclude superseded relationships
                if not rel.is_current:
                    continue
            
            relationships.append(rel)
        
        return relationships

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

    async def supersede_relationship(
        self,
        old_rel_id: str,
        new_rel_id: str,
        end_time: datetime | None = None,
    ) -> None:
        """Mark a relationship as superseded by a newer one.
        
        This is used for contradiction detection - when we learn that
        "Alice works at Google" but she used to work at Meta, we supersede
        the Meta relationship.
        
        Args:
            old_rel_id: ID of the relationship being superseded
            new_rel_id: ID of the new relationship that supersedes it
            end_time: When the old relationship ended (default: now)
        """
        end = end_time or datetime.utcnow()
        await self.conn.execute(
            """
            UPDATE relationships 
            SET valid_to = ?, superseded_by = ?
            WHERE id = ?
            """,
            (end.isoformat(), new_rel_id, old_rel_id),
        )
        await self.conn.commit()

    async def find_contradicting_relationships(
        self,
        from_entity_id: str,
        to_entity_id: str,
        rel_type: str,
    ) -> list[Relationship]:
        """Find existing current relationships that might be contradicted.
        
        Used to detect when a new relationship contradicts an existing one.
        For exclusive relationship types (WORKS_AT, SPOUSE_OF), finding an
        existing relationship to a DIFFERENT entity suggests the old one
        should be superseded.
        
        Args:
            from_entity_id: Subject entity
            to_entity_id: Object entity (the one we're adding)
            rel_type: Relationship type
            
        Returns:
            List of current relationships of the same type from the same
            subject but to DIFFERENT objects.
        """
        # Exclusive relationship types - only one can be current at a time
        exclusive_types = {"WORKS_AT", "SPOUSE_OF", "MARRIED_TO", "LIVES_IN", "ROLE"}
        
        if rel_type.upper() not in exclusive_types:
            return []
        
        cursor = await self.conn.execute(
            """
            SELECT * FROM relationships 
            WHERE from_entity_id = ? 
              AND type = ?
              AND to_entity_id != ?
              AND valid_to IS NULL
            """,
            (from_entity_id, rel_type, to_entity_id),
        )
        rows = await cursor.fetchall()
        
        relationships = []
        for row in rows:
            valid_from = datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else datetime.utcnow()
            valid_to = datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None
            created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow()
            
            relationships.append(Relationship(
                id=row["id"],
                from_entity_id=row["from_entity_id"],
                to_entity_id=row["to_entity_id"],
                type=row["type"],
                properties=json.loads(row["properties"]) if row["properties"] else {},
                confidence=row["confidence"],
                source_memory_id=row["source_memory_id"],
                valid_from=valid_from,
                valid_to=valid_to,
                created_at=created_at,
                superseded_by=row["superseded_by"],
            ))
        
        return relationships

    async def get_relationship_history(
        self,
        from_entity_id: str,
        to_entity_id: str | None = None,
        rel_type: str | None = None,
    ) -> list[Relationship]:
        """Get full relationship history including superseded relationships.
        
        Useful for timeline queries like "Where has Alice worked?"
        
        Args:
            from_entity_id: Subject entity
            to_entity_id: Optional filter by object entity
            rel_type: Optional filter by relationship type
            
        Returns:
            List of all relationships (current and superseded), ordered by valid_from.
        """
        query = "SELECT * FROM relationships WHERE from_entity_id = ?"
        params: list[Any] = [from_entity_id]
        
        if to_entity_id:
            query += " AND to_entity_id = ?"
            params.append(to_entity_id)
        
        if rel_type:
            query += " AND type = ?"
            params.append(rel_type)
        
        query += " ORDER BY valid_from DESC"
        
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        
        relationships = []
        for row in rows:
            valid_from = datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else datetime.utcnow()
            valid_to = datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None
            created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow()
            
            relationships.append(Relationship(
                id=row["id"],
                from_entity_id=row["from_entity_id"],
                to_entity_id=row["to_entity_id"],
                type=row["type"],
                properties=json.loads(row["properties"]) if row["properties"] else {},
                confidence=row["confidence"],
                source_memory_id=row["source_memory_id"],
                valid_from=valid_from,
                valid_to=valid_to,
                created_at=created_at,
                superseded_by=row["superseded_by"],
            ))
        
        return relationships

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


    # -----------------------------------------------------------------------
    # API Key operations (Week 7 - Authentication)
    # -----------------------------------------------------------------------

    async def save_api_key(
        self,
        key_id: str,
        key_hash: str,
        user_id: str,
        name: str | None = None,
        rate_limit_tier: str = "standard",
    ) -> None:
        """Save a new API key (hashed)."""
        await self.conn.execute(
            """
            INSERT INTO api_keys (id, key_hash, user_id, name, created_at, active, rate_limit_tier)
            VALUES (?, ?, ?, ?, ?, TRUE, ?)
            """,
            (key_id, key_hash, user_id, name, datetime.utcnow().isoformat(), rate_limit_tier),
        )
        await self.conn.commit()

    async def get_api_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Get API key record by hash."""
        cursor = await self.conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND active = TRUE",
            (key_hash,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_user_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        """Get all API keys for a user (without hashes)."""
        cursor = await self.conn.execute(
            """
            SELECT id, user_id, name, created_at, last_used_at, active, rate_limit_tier
            FROM api_keys WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_api_key_last_used(self, key_id: str) -> None:
        """Update last_used_at timestamp for an API key."""
        await self.conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), key_id),
        )
        await self.conn.commit()

    async def revoke_api_key(self, key_id: str, user_id: str) -> bool:
        """Revoke (deactivate) an API key. Returns True if found and revoked."""
        cursor = await self.conn.execute(
            "UPDATE api_keys SET active = FALSE WHERE id = ? AND user_id = ?",
            (key_id, user_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_api_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        """Get API key by ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM api_keys WHERE id = ?",
            (key_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_api_key_name(self, key_id: str, name: str) -> bool:
        """Update the name of an API key. Returns True if updated."""
        cursor = await self.conn.execute(
            "UPDATE api_keys SET name = ? WHERE id = ?",
            (name, key_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # -----------------------------------------------------------------------
    # Audit Log operations (Week 7 - Security)
    # -----------------------------------------------------------------------

    async def log_audit_event(
        self,
        audit_id: str,
        user_id: str,
        action: str,
        api_key_id: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        """Log an audit event."""
        await self.conn.execute(
            """
            INSERT INTO audit_log (id, timestamp, user_id, api_key_id, action, 
                                   resource_id, ip_address, success, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                datetime.utcnow().isoformat(),
                user_id,
                api_key_id,
                action,
                resource_id,
                ip_address,
                success,
                error_message,
            ),
        )
        await self.conn.commit()

    async def get_audit_logs(
        self,
        user_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get audit log entries with optional filters."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        
        if action:
            query += " AND action = ?"
            params.append(action)
        
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # -----------------------------------------------------------------------
    # User operations (User Authentication)
    # -----------------------------------------------------------------------

    async def create_user(
        self,
        user_id: str,
        email: str,
        password_hash: str,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """Create a new user."""
        now = created_at or datetime.utcnow()
        await self.conn.execute(
            """
            INSERT INTO users (id, email, password_hash, name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, email, password_hash, name, now.isoformat(), now.isoformat()),
        )
        await self.conn.commit()

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Get user by email."""
        cursor = await self.conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email.lower(),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        """Get user by ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_user_last_login(self, user_id: str) -> None:
        """Update user's last login timestamp."""
        await self.conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), user_id),
        )
        await self.conn.commit()

    async def update_user_password(self, user_id: str, password_hash: str) -> None:
        """Update user's password hash."""
        await self.conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, datetime.utcnow().isoformat(), user_id),
        )
        await self.conn.commit()

    async def update_user_email_verified(self, user_id: str, verified: bool = True) -> None:
        """Update user's email verification status."""
        await self.conn.execute(
            "UPDATE users SET email_verified = ?, updated_at = ? WHERE id = ?",
            (verified, datetime.utcnow().isoformat(), user_id),
        )
        await self.conn.commit()

    async def update_user_profile(self, user_id: str, name: str | None = None) -> bool:
        """Update user's profile information (name)."""
        cursor = await self.conn.execute(
            "UPDATE users SET name = ?, updated_at = ? WHERE id = ?",
            (name, datetime.utcnow().isoformat(), user_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def deactivate_user(self, user_id: str) -> bool:
        """Deactivate a user account."""
        cursor = await self.conn.execute(
            "UPDATE users SET is_active = FALSE, updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), user_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # -----------------------------------------------------------------------
    # Password reset token operations
    # -----------------------------------------------------------------------

    async def save_password_reset_token(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Save a password reset token (replaces existing)."""
        await self.conn.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                token_hash = excluded.token_hash,
                expires_at = excluded.expires_at,
                created_at = CURRENT_TIMESTAMP
            """,
            (user_id, token_hash, expires_at.isoformat()),
        )
        await self.conn.commit()

    async def get_password_reset_token(self, user_id: str) -> dict[str, Any] | None:
        """Get password reset token for a user."""
        cursor = await self.conn.execute(
            "SELECT * FROM password_reset_tokens WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_password_reset_token(self, user_id: str) -> None:
        """Delete password reset token for a user."""
        await self.conn.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = ?",
            (user_id,),
        )
        await self.conn.commit()

    # -----------------------------------------------------------------------
    # Two-Factor Authentication (TOTP) operations
    # -----------------------------------------------------------------------

    async def save_totp_secret(self, user_id: str, secret: str) -> None:
        """Save TOTP secret for a user (but don't enable yet)."""
        await self.conn.execute(
            "UPDATE users SET totp_secret = ? WHERE id = ?",
            (secret, user_id),
        )
        await self.conn.commit()

    async def enable_totp(self, user_id: str) -> None:
        """Enable TOTP for a user (after verification)."""
        await self.conn.execute(
            "UPDATE users SET totp_enabled = TRUE WHERE id = ?",
            (user_id,),
        )
        await self.conn.commit()

    async def disable_totp(self, user_id: str) -> None:
        """Disable TOTP and clear secret for a user."""
        await self.conn.execute(
            "UPDATE users SET totp_enabled = FALSE, totp_secret = NULL WHERE id = ?",
            (user_id,),
        )
        await self.conn.commit()

    # -----------------------------------------------------------------------
    # Token blacklist operations (for logout)
    # -----------------------------------------------------------------------

    async def add_token_to_blacklist(
        self,
        token_hash: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Add a token to the blacklist."""
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO token_blacklist (token_hash, user_id, expires_at)
            VALUES (?, ?, ?)
            """,
            (token_hash, user_id, expires_at.isoformat()),
        )
        await self.conn.commit()

    async def is_token_blacklisted(self, token_hash: str) -> bool:
        """Check if a token is blacklisted."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM token_blacklist WHERE token_hash = ?",
            (token_hash,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def cleanup_expired_blacklist_tokens(self) -> int:
        """Remove expired tokens from the blacklist."""
        cursor = await self.conn.execute(
            "DELETE FROM token_blacklist WHERE expires_at < ?",
            (datetime.utcnow().isoformat(),),
        )
        await self.conn.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Initialization helper
# ---------------------------------------------------------------------------

async def init_db(settings: Settings) -> Database:
    """Initialize and return a connected database."""
    db = Database(settings.database_url)
    await db.connect()
    await db.init_schema()
    return db
