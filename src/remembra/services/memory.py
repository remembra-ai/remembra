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
from remembra.extraction.entities import EntityExtractor, ExtractedEntity
from remembra.extraction.matcher import EntityMatcher, ExistingEntity, MatchResult
from remembra.models.memory import Entity, Relationship

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
        
        # Initialize entity resolution (Week 5)
        self.entity_extractor = EntityExtractor(
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
        )
        self.entity_matcher = EntityMatcher(
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
            min_confidence=0.6,
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
        
        # Extract and link entities (Week 5)
        if self.settings.enable_entity_resolution:
            try:
                await self._process_entities_for_memory(
                    memory_id=memory.id,
                    content=content,
                    user_id=user_id,
                    project_id=project_id,
                )
            except Exception as e:
                # Don't fail the whole store if entity extraction fails
                log.warning("entity_extraction_failed", error=str(e), memory_id=memory.id)
        
        return {"id": memory.id, "content": content}
    
    async def _process_entities_for_memory(
        self,
        memory_id: str,
        content: str,
        user_id: str,
        project_id: str,
    ) -> list[EntityRef]:
        """
        Extract entities from content and link to memory.
        
        Steps:
        1. Extract entities and relationships
        2. Match each entity against existing ones
        3. Create new entities or add aliases
        4. Store relationships
        5. Link memory to entities
        """
        try:
            # Step 1: Extract entities
            extraction = await self.entity_extractor.extract(content)
            
            if not extraction.entities:
                return []
            
            log.debug(
                "processing_entities",
                memory_id=memory_id,
                entity_count=len(extraction.entities),
            )
            
            entity_refs: list[EntityRef] = []
            entity_id_map: dict[str, str] = {}  # name -> entity_id
            
            # Step 2 & 3: Match or create entities
            for extracted in extraction.entities:
                # Get existing entities for matching
                existing = await self._get_existing_entities(user_id, project_id, extracted.type)
                
                # Try to match
                match_result = await self.entity_matcher.match(extracted, existing)
                
                if match_result.match and match_result.matched_entity_id:
                    # Matched existing entity - add alias if suggested
                    entity_id = match_result.matched_entity_id
                    if match_result.suggested_aliases:
                        await self._add_entity_aliases(
                            entity_id, 
                            match_result.suggested_aliases
                        )
                    log.debug(
                        "entity_matched",
                        name=extracted.name,
                        matched_id=entity_id,
                    )
                else:
                    # Create new entity
                    entity = Entity(
                        canonical_name=extracted.name,
                        type=extracted.type.lower(),
                        aliases=extracted.aliases,
                        attributes={"description": extracted.description},
                        confidence=1.0,
                    )
                    await self.db.save_entity(entity, user_id, project_id)
                    entity_id = entity.id
                    log.debug("entity_created", name=extracted.name, id=entity_id)
                
                entity_id_map[extracted.name] = entity_id
                entity_refs.append(EntityRef(
                    id=entity_id, 
                    canonical_name=extracted.name,
                    type=extracted.type.lower(),
                    confidence=1.0,
                ))
            
            # Step 4: Store relationships (only between valid entities)
            for rel in extraction.relationships:
                subject_id = entity_id_map.get(rel.subject)
                object_id = entity_id_map.get(rel.object)
                
                # Only save relationships where both ends are entities
                # Skip value relationships like ROLE -> "CEO"
                if subject_id and object_id:
                    relationship = Relationship(
                        from_entity_id=subject_id,
                        to_entity_id=object_id,
                        type=rel.predicate.lower(),
                        properties={},
                        confidence=1.0,
                        source_memory_id=memory_id,
                    )
                    try:
                        await self.db.save_relationship(relationship)
                    except Exception as e:
                        log.warning("relationship_save_failed", error=str(e))
            
            # Step 5: Link memory to entities
            for entity_id in entity_id_map.values():
                await self.db.link_memory_to_entity(memory_id, entity_id)
            
            log.info(
                "entities_processed",
                memory_id=memory_id,
                entities_linked=len(entity_id_map),
            )
            
            return entity_refs
            
        except Exception as e:
            log.error("entity_processing_error", error=str(e), memory_id=memory_id)
            return []
    
    async def _get_existing_entities(
        self,
        user_id: str,
        project_id: str,
        entity_type: str,
    ) -> list[ExistingEntity]:
        """Get existing entities for matching."""
        entities = await self.db.get_entities_by_type(user_id, project_id, entity_type)
        return [
            ExistingEntity(
                id=e.id,
                name=e.canonical_name,
                type=e.type,
                description=e.attributes.get("description", ""),
                aliases=e.aliases,
            )
            for e in entities
        ]
    
    async def _add_entity_aliases(self, entity_id: str, aliases: list[str]) -> None:
        """Add aliases to an existing entity."""
        entity = await self.db.get_entity(entity_id)
        if entity:
            new_aliases = list(set(entity.aliases + aliases))
            await self.db.update_entity_aliases(entity_id, new_aliases)

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
        3. Entity-aware retrieval (find memories via entity graph)
        4. Combine and deduplicate results
        5. Synthesize context from results
        """
        log.info(
            "recalling_memories",
            user_id=request.user_id,
            project_id=request.project_id,
            query_length=len(request.query),
        )

        # Embed query
        query_vector = await self.embeddings.embed(request.query)

        # Search Qdrant (semantic search)
        results = await self.qdrant.search(
            query_vector=query_vector,
            user_id=request.user_id,
            project_id=request.project_id,
            limit=request.limit,
            score_threshold=request.threshold,
        )
        
        # Entity-aware retrieval (Week 5)
        entity_memory_ids: set[str] = set()
        matched_entities: list[EntityRef] = []
        
        if self.settings.enable_entity_resolution:
            try:
                entity_results = await self._find_memories_by_entity(
                    query=request.query,
                    user_id=request.user_id,
                    project_id=request.project_id,
                )
                entity_memory_ids = entity_results["memory_ids"]
                matched_entities = entity_results["entities"]
                
                log.debug(
                    "entity_recall",
                    matched_entities=len(matched_entities),
                    memory_ids=len(entity_memory_ids),
                )
            except Exception as e:
                log.warning("entity_recall_failed", error=str(e))

        if not results and not entity_memory_ids:
            log.info("recall_no_results", user_id=request.user_id)
            return RecallResponse(context="", memories=[], entities=[])

        # Track seen memory IDs to avoid duplicates
        seen_memory_ids: set[str] = set()
        
        # Build recall results from semantic search
        memories: list[RecallResult] = []
        all_entities: list[EntityRef] = list(matched_entities)  # Start with matched entities
        context_parts: list[str] = []

        for memory_id, score, payload in results:
            memory_id_str = str(memory_id)
            if memory_id_str in seen_memory_ids:
                continue
            seen_memory_ids.add(memory_id_str)
            
            content = payload.get("content", "")
            created_at_str = payload.get("created_at", datetime.utcnow().isoformat())

            memories.append(
                RecallResult(
                    id=memory_id_str,
                    relevance=score,
                    content=content,
                    created_at=datetime.fromisoformat(created_at_str),
                )
            )
            context_parts.append(content)
            await self.db.update_access(memory_id_str)
        
        # Add entity-linked memories that weren't in semantic results
        for entity_mem_id in entity_memory_ids:
            if entity_mem_id in seen_memory_ids:
                continue
            seen_memory_ids.add(entity_mem_id)
            
            # Fetch memory from database
            mem_data = await self.db.get_memory(entity_mem_id)
            if mem_data:
                memories.append(
                    RecallResult(
                        id=entity_mem_id,
                        relevance=0.5,  # Default relevance for entity matches
                        content=mem_data.get("content", ""),
                        created_at=datetime.fromisoformat(
                            mem_data.get("created_at", datetime.utcnow().isoformat())
                        ),
                    )
                )
                context_parts.append(mem_data.get("content", ""))
                await self.db.update_access(entity_mem_id)

        # Synthesize context
        context = " ".join(context_parts)

        # Deduplicate entities by ID
        seen_entity_ids: set[str] = set()
        unique_entities: list[EntityRef] = []
        for entity in all_entities:
            if entity.id not in seen_entity_ids:
                seen_entity_ids.add(entity.id)
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
    
    async def _find_memories_by_entity(
        self,
        query: str,
        user_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        """
        Find memories linked to entities mentioned in the query.
        
        Steps:
        1. Extract entity mentions from query
        2. Match to existing entities (including aliases)
        3. Get all memories linked to matched entities
        
        Returns:
            Dict with "memory_ids" (set) and "entities" (list of EntityRef)
        """
        # Simple entity matching: check if any entity name/alias appears in query
        all_entities = await self.db.get_user_entities(user_id, project_id)
        
        matched_entities: list[EntityRef] = []
        memory_ids: set[str] = set()
        query_lower = query.lower()
        
        for entity in all_entities:
            # Check canonical name
            if entity.canonical_name.lower() in query_lower:
                matched_entities.append(EntityRef(
                    id=entity.id,
                    canonical_name=entity.canonical_name,
                    type=entity.type,
                    confidence=1.0,
                ))
                # Get linked memories
                linked = await self.db.get_memories_by_entity(entity.id)
                memory_ids.update(linked)
                continue
            
            # Check aliases
            for alias in entity.aliases:
                if alias.lower() in query_lower:
                    matched_entities.append(EntityRef(
                        id=entity.id,
                        canonical_name=entity.canonical_name,
                        type=entity.type,
                        confidence=0.9,
                    ))
                    linked = await self.db.get_memories_by_entity(entity.id)
                    memory_ids.update(linked)
                    break
        
        return {
            "memory_ids": memory_ids,
            "entities": matched_entities,
        }

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
