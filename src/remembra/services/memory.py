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
from remembra.extraction.extractor import FactExtractor, ExtractionConfig
from remembra.extraction.consolidator import (
    MemoryConsolidator,
    ConsolidationAction,
    ExistingMemory,
)

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
        
        # Initialize intelligent extraction (Week 4)
        extraction_config = ExtractionConfig(
            enabled=settings.smart_extraction_enabled,
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
        )
        self.extractor = FactExtractor(extraction_config)
        self.consolidator = MemoryConsolidator(
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
            similarity_threshold=settings.consolidation_threshold,
        )

    # -----------------------------------------------------------------------
    # Store
    # -----------------------------------------------------------------------

    async def store(self, request: StoreRequest) -> StoreResponse:
        """
        Store a new memory with intelligent extraction and consolidation.
        
        Steps:
        1. Extract atomic facts from content (LLM-powered)
        2. For each fact, check for similar existing memories
        3. Consolidate: ADD new, UPDATE existing, or skip duplicates
        4. Store in Qdrant (vector) + SQLite (metadata)
        """
        log.info(
            "storing_memory",
            user_id=request.user_id,
            project_id=request.project_id,
            content_length=len(request.content),
        )

        now = datetime.utcnow()
        
        # Calculate expiration if TTL provided
        expires_at = None
        if request.ttl:
            ttl_delta = parse_ttl(request.ttl)
            if ttl_delta:
                expires_at = now + ttl_delta
        elif self.settings.default_ttl_days:
            expires_at = now + timedelta(days=self.settings.default_ttl_days)

        # Step 1: Extract atomic facts using LLM
        extracted_facts = await self.extractor.extract(request.content)
        
        if not extracted_facts:
            # If no facts extracted, store raw content
            extracted_facts = [request.content.strip()]
        
        log.debug("facts_extracted", count=len(extracted_facts))
        
        # Step 2 & 3: Process each fact with consolidation
        stored_facts: list[str] = []
        memory_id = None  # Track primary memory ID
        
        for fact in extracted_facts:
            fact_result = await self._store_single_fact(
                fact=fact,
                user_id=request.user_id,
                project_id=request.project_id,
                metadata=request.metadata,
                expires_at=expires_at,
                now=now,
            )
            if fact_result:
                stored_facts.append(fact_result["content"])
                if memory_id is None:
                    memory_id = fact_result["id"]
        
        # If nothing stored (all NOOPs), return minimal response
        if not memory_id:
            log.info("all_facts_skipped", user_id=request.user_id)
            return StoreResponse(
                id="",
                extracted_facts=extracted_facts,
                entities=[],
            )

        log.info(
            "memory_stored",
            memory_id=memory_id,
            facts_extracted=len(extracted_facts),
            facts_stored=len(stored_facts),
        )

        return StoreResponse(
            id=memory_id,
            extracted_facts=stored_facts,
            entities=[],  # TODO: Entity extraction in Week 5
        )
    
    async def _store_single_fact(
        self,
        fact: str,
        user_id: str,
        project_id: str,
        metadata: dict[str, Any],
        expires_at: datetime | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        """
        Store a single fact with consolidation logic.
        
        Returns dict with id and content if stored, None if skipped.
        """
        # Generate embedding for this fact
        embedding = await self.embeddings.embed(fact)
        
        # Search for similar existing memories
        similar = await self.qdrant.search(
            query_vector=embedding,
            user_id=user_id,
            project_id=project_id,
            limit=5,
            score_threshold=0.4,  # Lower threshold to find candidates
        )
        
        # Convert to ExistingMemory objects
        existing_memories = [
            ExistingMemory(id=str(mid), content=payload.get("content", ""), score=score)
            for mid, score, payload in similar
        ]
        
        # Consolidate: decide ADD/UPDATE/DELETE/NOOP
        result = await self.consolidator.consolidate(fact, existing_memories)
        
        if result.action == ConsolidationAction.NOOP:
            log.debug("fact_skipped_noop", fact=fact[:50])
            return None
        
        if result.action == ConsolidationAction.DELETE and result.target_id:
            # Delete old memory, then add new
            await self.qdrant.delete(result.target_id)
            await self.db.delete_memory(result.target_id)
            log.debug("old_memory_deleted", memory_id=result.target_id)
        
        if result.action == ConsolidationAction.UPDATE and result.target_id:
            # Update existing memory with merged content
            content = result.content or fact
            # Delete old and insert new (simpler than true update)
            await self.qdrant.delete(result.target_id)
            await self.db.delete_memory(result.target_id)
            log.debug("memory_updated", old_id=result.target_id)
        else:
            content = result.content or fact
        
        # Create and store the memory
        memory = Memory(
            user_id=user_id,
            project_id=project_id,
            content=content,
            extracted_facts=[content],
            entities=[],
            embedding=embedding,
            metadata=metadata,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        
        await self.qdrant.upsert(memory)
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
        
        return {"id": memory.id, "content": content}

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
