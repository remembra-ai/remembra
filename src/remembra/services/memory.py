"""Memory service - core business logic for store, recall, update, forget."""

from datetime import datetime, timedelta
from typing import Any

import structlog

from remembra.config import Settings
from remembra.models.memory import (
    EntityRef,
    ForgetResponse,
    Memory,
    RecallRequest,
    RecallResponse,
    RecallResult,
    StoreRequest,
    StoreResponse,
)
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingService
from remembra.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


def parse_ttl(ttl: str | None) -> timedelta | None:
    """
    Parse TTL string like '30d', '1y', '2w' into timedelta.
    
    Supported formats:
    - Xd = X days
    - Xw = X weeks
    - Xm = X months (30 days)
    - Xy = X years (365 days)
    """
    if not ttl:
        return None

    ttl = ttl.strip().lower()
    if not ttl:
        return None

    try:
        value = int(ttl[:-1])
        unit = ttl[-1]

        if unit == "d":
            return timedelta(days=value)
        elif unit == "w":
            return timedelta(weeks=value)
        elif unit == "m":
            return timedelta(days=value * 30)
        elif unit == "y":
            return timedelta(days=value * 365)
        else:
            log.warning("invalid_ttl_unit", ttl=ttl, unit=unit)
            return None
    except (ValueError, IndexError):
        log.warning("invalid_ttl_format", ttl=ttl)
        return None


class MemoryService:
    """
    Core memory operations: store, recall, update, forget.
    
    This is the main business logic layer that coordinates:
    - Embedding generation
    - Vector storage (Qdrant)
    - Metadata storage (SQLite)
    - Entity extraction (future: LLM-powered)
    """

    def __init__(
        self,
        settings: Settings,
        qdrant: QdrantStore,
        db: Database,
        embeddings: EmbeddingService,
    ):
        self.settings = settings
        self.qdrant = qdrant
        self.db = db
        self.embeddings = embeddings

    # -----------------------------------------------------------------------
    # Store
    # -----------------------------------------------------------------------

    async def store(self, request: StoreRequest) -> StoreResponse:
        """
        Store a new memory.
        
        Steps:
        1. Extract facts from content (TODO: LLM-powered in Week 4)
        2. Generate embedding
        3. Resolve entities (TODO: Week 5)
        4. Store in Qdrant (vector) + SQLite (metadata)
        """
        log.info(
            "storing_memory",
            user_id=request.user_id,
            project_id=request.project_id,
            content_length=len(request.content),
        )

        # Create memory object
        now = datetime.utcnow()
        
        # Calculate expiration if TTL provided
        expires_at = None
        if request.ttl:
            ttl_delta = parse_ttl(request.ttl)
            if ttl_delta:
                expires_at = now + ttl_delta
        elif self.settings.default_ttl_days:
            expires_at = now + timedelta(days=self.settings.default_ttl_days)

        # Generate embedding
        embedding = await self.embeddings.embed(request.content)

        # For now, simple fact extraction (TODO: LLM-powered in Week 4)
        extracted_facts = self._simple_fact_extraction(request.content)

        # Create memory
        memory = Memory(
            user_id=request.user_id,
            project_id=request.project_id,
            content=request.content,
            extracted_facts=extracted_facts,
            entities=[],  # TODO: Entity extraction in Week 5
            embedding=embedding,
            metadata=request.metadata,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )

        # Store in Qdrant (vectors)
        await self.qdrant.upsert(memory)

        # Store metadata in SQLite
        await self.db.save_memory_metadata(
            memory_id=memory.id,
            user_id=memory.user_id,
            project_id=memory.project_id,
            content=memory.content,
            extracted_facts=memory.extracted_facts,
            metadata=memory.metadata,
            created_at=memory.created_at,
            expires_at=memory.expires_at,
        )

        log.info("memory_stored", memory_id=memory.id, facts_count=len(extracted_facts))

        return StoreResponse(
            id=memory.id,
            extracted_facts=extracted_facts,
            entities=memory.entities,
        )

    def _simple_fact_extraction(self, content: str) -> list[str]:
        """
        Simple rule-based fact extraction.
        Splits content into sentences as basic facts.
        
        TODO(Week 4): Replace with LLM-powered extraction.
        """
        # Split by sentence-ending punctuation
        sentences = []
        current = []

        for char in content:
            current.append(char)
            if char in ".!?":
                sentence = "".join(current).strip()
                if len(sentence) > 10:  # Skip very short fragments
                    sentences.append(sentence)
                current = []

        # Don't forget the last sentence without punctuation
        if current:
            sentence = "".join(current).strip()
            if len(sentence) > 10:
                sentences.append(sentence)

        return sentences[:10]  # Limit to 10 facts per memory

    # -----------------------------------------------------------------------
    # Recall
    # -----------------------------------------------------------------------

    async def recall(self, request: RecallRequest) -> RecallResponse:
        """
        Recall memories relevant to a query.
        
        Steps:
        1. Embed the query
        2. Semantic search in Qdrant
        3. Synthesize context from results
        4. Track access (for temporal decay)
        """
        log.info(
            "recalling_memories",
            user_id=request.user_id,
            project_id=request.project_id,
            query_length=len(request.query),
        )

        # Embed query
        query_vector = await self.embeddings.embed(request.query)

        # Search Qdrant
        results = await self.qdrant.search(
            query_vector=query_vector,
            user_id=request.user_id,
            project_id=request.project_id,
            limit=request.limit,
            score_threshold=request.threshold,
        )

        if not results:
            log.info("recall_no_results", user_id=request.user_id)
            return RecallResponse(context="", memories=[], entities=[])

        # Build recall results
        memories: list[RecallResult] = []
        all_entities: list[EntityRef] = []
        context_parts: list[str] = []

        for memory_id, score, payload in results:
            content = payload.get("content", "")
            created_at_str = payload.get("created_at", datetime.utcnow().isoformat())

            memories.append(
                RecallResult(
                    id=str(memory_id),
                    relevance=score,
                    content=content,
                    created_at=datetime.fromisoformat(created_at_str),
                )
            )

            # Add to context
            context_parts.append(content)

            # Collect entities
            entity_dicts = payload.get("entities", [])
            for ed in entity_dicts:
                if isinstance(ed, dict):
                    all_entities.append(EntityRef(**ed))

            # Update access tracking
            await self.db.update_access(str(memory_id))

        # Synthesize context (simple join for now, TODO: LLM synthesis in Week 6)
        context = " ".join(context_parts)

        # Deduplicate entities by ID
        seen_ids = set()
        unique_entities = []
        for entity in all_entities:
            if entity.id not in seen_ids:
                seen_ids.add(entity.id)
                unique_entities.append(entity)

        log.info(
            "recall_complete",
            user_id=request.user_id,
            results_count=len(memories),
            entities_count=len(unique_entities),
        )

        return RecallResponse(
            context=context,
            memories=memories,
            entities=unique_entities,
        )

    # -----------------------------------------------------------------------
    # Forget
    # -----------------------------------------------------------------------

    async def forget(
        self,
        memory_id: str | None = None,
        user_id: str | None = None,
        entity: str | None = None,
    ) -> ForgetResponse:
        """
        GDPR-compliant deletion of memories.
        
        Can delete by:
        - Specific memory ID
        - All memories for a user
        - All memories mentioning an entity (TODO: Week 5)
        """
        deleted_memories = 0
        deleted_entities = 0
        deleted_relationships = 0

        if memory_id:
            # Delete specific memory
            await self.qdrant.delete(memory_id)
            if await self.db.delete_memory(memory_id):
                deleted_memories = 1
            log.info("forgot_memory", memory_id=memory_id)

        elif user_id:
            # Delete all user data
            deleted_memories = await self.qdrant.delete_by_user(user_id)
            await self.db.delete_user_memories(user_id)
            deleted_relationships = await self.db.delete_user_relationships(user_id)
            deleted_entities = await self.db.delete_user_entities(user_id)
            log.info(
                "forgot_user",
                user_id=user_id,
                memories=deleted_memories,
                entities=deleted_entities,
            )

        elif entity:
            # TODO(Week 5): Entity-based deletion
            log.warning("entity_deletion_not_implemented", entity=entity)

        return ForgetResponse(
            deleted_memories=deleted_memories,
            deleted_entities=deleted_entities,
            deleted_relationships=deleted_relationships,
        )

    # -----------------------------------------------------------------------
    # Get by ID
    # -----------------------------------------------------------------------

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """Get a memory by ID."""
        return await self.qdrant.get_by_id(memory_id)
