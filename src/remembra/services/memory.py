"""Memory service - core business logic for store, recall, update, forget."""

import math
from datetime import datetime, timedelta
from typing import Any

import structlog

from remembra.config import Settings
from remembra.extraction.conflicts import (
    ConflictManager,
    ConflictStatus,
    ConflictStrategy,
    MemoryConflict,
)
from remembra.extraction.consolidator import (
    ConsolidationAction,
    ExistingMemory,
    MemoryConsolidator,
)
from remembra.extraction.entities import EntityExtractor, create_entity_extractor
from remembra.extraction.extractor import ExtractionConfig, FactExtractor
from remembra.extraction.matcher import EntityMatcher, ExistingEntity
from remembra.models.memory import (
    Entity,
    EntityRef,
    ForgetResponse,
    Memory,
    RecallRequest,
    RecallResponse,
    RecallResult,
    Relationship,
    StoreRequest,
    StoreResponse,
    UpdateResponse,
)
from remembra.retrieval.context import ContextOptimizer
from remembra.retrieval.graph import GraphRetriever

# Advanced retrieval (Week 6)
from remembra.retrieval.hybrid import HybridSearchConfig, HybridSearcher
from remembra.retrieval.ranking import RankingConfig, RelevanceRanker
from remembra.retrieval.reranker import CrossEncoderReranker
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
    - Cross-agent memory sharing via spaces
    """

    def __init__(
        self,
        settings: Settings,
        qdrant: QdrantStore,
        db: Database,
        embeddings: EmbeddingService,
        conflict_manager: ConflictManager | None = None,
        space_manager: Any | None = None,
    ):
        self.settings = settings
        self.qdrant = qdrant
        self.db = db
        self.embeddings = embeddings
        self.conflict_manager = conflict_manager
        self.space_manager = space_manager  # Injected after init
        
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
        self.entity_extractor = create_entity_extractor(settings)
        self.entity_matcher = EntityMatcher(
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
            min_confidence=0.6,
        )
        
        # Initialize advanced retrieval (Week 6)
        # Hybrid search with FTS5 BM25 + vector fusion
        self.hybrid_searcher = HybridSearcher(HybridSearchConfig(
            alpha=settings.hybrid_alpha,  # Research default: 0.4
        ))
        
        # Graph-aware retrieval using entity relationships
        self.graph_retriever = GraphRetriever(
            db=db,
            max_depth=settings.graph_max_depth,
        )
        
        # Context window optimization for LLM output
        self.context_optimizer = ContextOptimizer(
            max_tokens=settings.context_max_tokens,
            include_metadata=settings.context_include_metadata,
        )
        
        # Multi-signal relevance ranking
        self.relevance_ranker = RelevanceRanker(RankingConfig(
            semantic_weight=settings.ranking_semantic_weight,
            recency_weight=settings.ranking_recency_weight,
            entity_weight=settings.ranking_entity_weight,
            keyword_weight=settings.ranking_keyword_weight,
            recency_decay_days=settings.ranking_recency_decay_days,
        ))
        
        # CrossEncoder reranking (optional, gracefully degrades)
        self.reranker = CrossEncoderReranker(
            model_name=settings.rerank_model,
            enabled=settings.enable_reranking,
        )

    # -----------------------------------------------------------------------
    # Store
    # -----------------------------------------------------------------------

    async def store(
        self,
        request: StoreRequest,
        source: str = "user_input",
        trust_score: float = 1.0,
        checksum: str | None = None,
    ) -> StoreResponse:
        """
        Store a new memory with intelligent extraction and consolidation.
        
        Steps:
        1. Extract atomic facts from content (LLM-powered)
        2. For each fact, check for similar existing memories
        3. Consolidate: ADD new, UPDATE existing, or skip duplicates
        4. Store in Qdrant (vector) + SQLite (metadata)
        
        Args:
            request: StoreRequest with content, user_id, etc.
            source: Content provenance (user_input, agent_generated, external_api)
            trust_score: Security trust score (0.0-1.0)
            checksum: SHA-256 hash for integrity verification
        """
        log.info(
            "storing_memory",
            user_id=request.user_id,
            project_id=request.project_id,
            content_length=len(request.content),
            trust_score=trust_score,
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
                source=source,
                trust_score=trust_score,
                checksum=checksum,
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
        source: str = "user_input",
        trust_score: float = 1.0,
        checksum: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Store a single fact with consolidation logic.
        
        Returns dict with id and content if stored, None if skipped.
        
        Args:
            fact: The extracted fact to store
            user_id: User ID
            project_id: Project namespace
            metadata: Additional metadata
            expires_at: Optional expiration time
            now: Current timestamp
            source: Content provenance (user_input, agent_generated, external_api)
            trust_score: Security trust score (0.0-1.0)
            checksum: SHA-256 hash for integrity verification
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

        # ── Conflict detection ───────────────────────────────────────────
        # When the consolidator detects a contradiction (DELETE) or update,
        # record the conflict and apply the configured strategy.
        is_conflict = result.action in (
            ConsolidationAction.DELETE,
            ConsolidationAction.UPDATE,
        ) and result.target_id is not None

        conflict_target = None  # The existing memory that was contradicted
        if is_conflict:
            conflict_target = next(
                (m for m in existing_memories if m.id == result.target_id), None
            )

        strategy = ConflictStrategy.UPDATE  # default: overwrite
        if is_conflict and self.conflict_manager is not None:
            strategy = self.conflict_manager.default_strategy
            try:
                conflict = MemoryConflict(
                    user_id=user_id,
                    project_id=project_id,
                    new_fact=fact,
                    existing_memory_id=result.target_id or "",
                    existing_content=conflict_target.content if conflict_target else "",
                    similarity_score=conflict_target.score if conflict_target else 0.0,
                    reason=result.reason,
                    strategy_applied=strategy,
                    status=(
                        ConflictStatus.RESOLVED
                        if strategy == ConflictStrategy.UPDATE
                        else ConflictStatus.OPEN
                    ),
                )
                await self.conflict_manager.record(conflict)
            except Exception as exc:
                log.warning("conflict_recording_failed", error=str(exc))

        # ── Apply conflict strategy ──────────────────────────────────────
        old_memory_id = None

        if strategy == ConflictStrategy.VERSION and is_conflict:
            # Keep both memories — don't delete the old one.
            # Store the new fact as-is alongside the existing one.
            content = result.content or fact
            log.debug(
                "conflict_versioned",
                old_id=result.target_id,
                fact=fact[:50],
            )
        elif strategy == ConflictStrategy.FLAG and is_conflict:
            # Store the new fact but don't delete the old one.
            # Both are kept; the conflict record stays open for review.
            content = result.content or fact
            log.debug(
                "conflict_flagged",
                old_id=result.target_id,
                fact=fact[:50],
            )
        else:
            # strategy == UPDATE (or no conflict): original behaviour
            if result.action == ConsolidationAction.DELETE and result.target_id:
                old_memory_id = result.target_id
                log.debug("old_memory_will_be_deleted", memory_id=result.target_id)

            if result.action == ConsolidationAction.UPDATE and result.target_id:
                content = result.content or fact
                old_memory_id = result.target_id
                log.debug("memory_will_be_updated", old_id=result.target_id)
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
            source=source,
            trust_score=trust_score,
            checksum=checksum,
        )
        
        # Index in FTS5 for hybrid search (Week 6)
        if self.settings.enable_hybrid_search:
            try:
                await self.db.index_memory_fts(
                    memory_id=memory.id,
                    user_id=memory.user_id,
                    project_id=memory.project_id,
                    content=memory.content,
                )
            except Exception as e:
                log.warning("fts_indexing_failed", error=str(e), memory_id=memory.id)
        
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
        
        # Clean up old memory AFTER new one is fully created (fixes FK constraint bug)
        if old_memory_id:
            try:
                # Migrate entity links and relationships to the new memory
                await self.db.migrate_memory_relationships(old_memory_id, memory.id)
                
                # Now safe to delete the old memory (relationships migrated)
                await self.qdrant.delete(old_memory_id)
                await self.db.delete_memory_fts(old_memory_id)
                await self.db.delete_memory(old_memory_id)
                
                log.debug(
                    "old_memory_cleaned_up",
                    old_id=old_memory_id,
                    new_id=memory.id,
                    action=result.action.value,
                )
            except Exception as e:
                log.warning(
                    "old_memory_cleanup_failed",
                    error=str(e),
                    old_id=old_memory_id,
                    new_id=memory.id,
                )
        
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
    # Recall (v0.4.0 - Advanced Retrieval)
    # -----------------------------------------------------------------------

    async def recall(self, request: RecallRequest) -> RecallResponse:
        """
        Recall memories relevant to a query using advanced retrieval.
        
        v0.4.0 Features:
        1. Hybrid search (semantic + keyword via FTS5/BM25)
        2. Graph-aware retrieval (entity relationships)
        3. CrossEncoder reranking (optional, reduces hallucinations)
        4. Advanced relevance ranking (recency, entity, keyword boosts)
        5. Context window optimization (smart truncation with tiktoken)
        
        Args:
            request: RecallRequest with query, user_id, project_id, etc.
            
        Returns:
            RecallResponse with context, memories, and entities
        """
        # Resolve feature flags
        use_hybrid = self.settings.enable_hybrid_search
        use_rerank = self.settings.enable_reranking
        max_tokens = request.max_tokens or self.settings.context_max_tokens
        
        log.info(
            "recalling_memories_v2",
            user_id=request.user_id,
            project_id=request.project_id,
            query_length=len(request.query),
            hybrid_enabled=use_hybrid,
            graph_enabled=self.settings.enable_graph_retrieval,
            rerank_enabled=use_rerank,
            max_tokens=max_tokens,
        )

        # Step 1: Embed query for semantic search
        query_vector = await self.embeddings.embed(request.query)

        # Step 2: Semantic search in Qdrant
        semantic_results = await self.qdrant.search(
            query_vector=query_vector,
            user_id=request.user_id,
            project_id=request.project_id,
            limit=request.limit * 2,  # Get more for hybrid fusion
            score_threshold=request.threshold,
        )
        
        log.debug("semantic_search_done", count=len(semantic_results))
        
        # Step 3: Graph-aware retrieval (entity relationships)
        graph_memory_ids: set[str] = set()
        matched_entities: list[EntityRef] = []
        related_entities: list[EntityRef] = []
        
        if self.settings.enable_graph_retrieval:
            try:
                graph_result = await self.graph_retriever.search(
                    query=request.query,
                    user_id=request.user_id,
                    project_id=request.project_id,
                )
                graph_memory_ids = graph_result.memory_ids
                matched_entities = graph_result.matched_entities
                related_entities = graph_result.related_entities
                
                log.debug(
                    "graph_retrieval_done",
                    matched_entities=len(matched_entities),
                    related_entities=len(related_entities),
                    memory_ids=len(graph_memory_ids),
                )
            except Exception as e:
                log.warning("graph_retrieval_failed", error=str(e))
        
        # Step 4: Hybrid search (combine semantic + keyword via FTS5)
        hybrid_results: list[dict[str, Any]] = []
        
        if use_hybrid and semantic_results:
            try:
                # Build payload map from semantic results
                payload_map: dict[str, dict[str, Any]] = {}
                for memory_id, score, payload in semantic_results:
                    mid = str(memory_id)
                    payload_map[mid] = {**payload, "semantic_score": score}
                
                # Add graph memories
                for mem_id in graph_memory_ids:
                    if mem_id not in payload_map:
                        mem_data = await self.db.get_memory(mem_id)
                        if mem_data:
                            payload_map[mem_id] = {
                                **mem_data, 
                                "semantic_score": 0.4,  # Default for graph-only
                            }
                
                # Try FTS5 search first (persistent, accurate BM25)
                fts_results: list[tuple[str, float]] = []
                try:
                    fts_results = await self.db.search_fts(
                        query=request.query,
                        user_id=request.user_id,
                        project_id=request.project_id,
                        limit=request.limit * 2,
                    )
                    log.debug("fts5_search_done", count=len(fts_results))
                except Exception as e:
                    log.debug("fts5_search_failed_fallback_bm25", error=str(e))
                
                # Fall back to in-memory BM25 if FTS5 fails or returns nothing
                if not fts_results:
                    all_docs = [(mid, p.get("content", "")) for mid, p in payload_map.items()]
                    self.hybrid_searcher.index_documents(all_docs)
                    # Get BM25 keyword results from in-memory index
                    kw_raw = self.hybrid_searcher.keyword_search(request.query, limit=request.limit * 2)
                    kw_for_fusion = [(doc_id, score) for doc_id, score, _ in kw_raw]
                    fused = await self.hybrid_searcher.search(
                        semantic_results=semantic_results,
                        keyword_results=kw_for_fusion,
                        limit=request.limit * 2,
                    )
                    for result in fused:
                        hybrid_results.append({
                            "id": result.id,
                            "content": result.content,
                            "semantic_score": result.semantic_score,
                            "keyword_score": result.keyword_score,
                            "relevance": result.combined_score,
                            "created_at": payload_map.get(result.id, {}).get("created_at"),
                            "matched_keywords": [],  # BM25 fallback doesn't track individual terms
                            "payload": result.payload or payload_map.get(result.id, {}),
                        })
                else:
                    # Fuse FTS5 results with semantic results
                    # Normalize scores with min-max scaling
                    semantic_scores = {str(mid): score for mid, score, _ in semantic_results}
                    max_semantic = max(semantic_scores.values()) if semantic_scores else 1.0
                    max_keyword = max(s for _, s in fts_results) if fts_results else 1.0
                    
                    keyword_scores = {mid: score for mid, score in fts_results}
                    all_ids = set(semantic_scores.keys()) | set(keyword_scores.keys()) | set(payload_map.keys())
                    
                    alpha = self.settings.hybrid_alpha  # Keyword weight
                    
                    for mid in all_ids:
                        sem = semantic_scores.get(mid, 0.0) / max_semantic if max_semantic > 0 else 0
                        kw = keyword_scores.get(mid, 0.0) / max_keyword if max_keyword > 0 else 0
                        combined = alpha * kw + (1 - alpha) * sem
                        
                        payload = payload_map.get(mid, {})
                        hybrid_results.append({
                            "id": mid,
                            "content": payload.get("content", ""),
                            "semantic_score": sem,
                            "keyword_score": kw,
                            "relevance": combined,
                            "created_at": payload.get("created_at"),
                            "payload": payload,
                        })
                    
                    # Sort by combined score
                    hybrid_results.sort(key=lambda x: x["relevance"], reverse=True)
                
                log.debug("hybrid_search_done", count=len(hybrid_results))
                
            except Exception as e:
                log.warning("hybrid_search_failed", error=str(e))
                # Fall back to semantic results
                hybrid_results = [
                    {
                        "id": str(mid),
                        "content": payload.get("content", ""),
                        "relevance": score,
                        "semantic_score": score,
                        "keyword_score": 0.0,
                        "created_at": payload.get("created_at"),
                        "payload": payload,
                    }
                    for mid, score, payload in semantic_results
                ]
        else:
            # No hybrid search - use semantic results directly
            hybrid_results = [
                {
                    "id": str(mid),
                    "content": payload.get("content", ""),
                    "relevance": score,
                    "semantic_score": score,
                    "keyword_score": 0.0,
                    "created_at": payload.get("created_at"),
                    "payload": payload,
                }
                for mid, score, payload in semantic_results
            ]
        
        # Add graph-only memories that weren't in hybrid results
        seen_ids = {r["id"] for r in hybrid_results}
        for mem_id in graph_memory_ids:
            if mem_id not in seen_ids:
                mem_data = await self.db.get_memory(mem_id)
                if mem_data:
                    hybrid_results.append({
                        "id": mem_id,
                        "content": mem_data.get("content", ""),
                        "relevance": 0.5,  # Default for graph-only
                        "semantic_score": 0.4,
                        "keyword_score": 0.0,
                        "created_at": mem_data.get("created_at"),
                        "payload": mem_data,
                    })
        
        if not hybrid_results:
            log.info("recall_no_results", user_id=request.user_id)
            return RecallResponse(context="", memories=[], entities=[])
        
        # Step 5: CrossEncoder reranking (optional, reduces hallucinations)
        if use_rerank and hybrid_results:
            try:
                reranked = self.reranker.rerank(
                    query=request.query,
                    documents=hybrid_results,
                    top_k=request.limit * 2,
                    content_key="content",
                    score_key="relevance",
                )
                # Update hybrid_results with rerank scores
                hybrid_results = [
                    {
                        **r.payload,
                        "id": r.id,
                        "content": r.content,
                        "relevance": r.final_score,
                        "rerank_score": r.rerank_score,
                    }
                    for r in reranked
                ]
                log.debug("reranking_done", count=len(hybrid_results))
            except Exception as e:
                log.warning("reranking_failed", error=str(e))
        
        # Step 6: Advanced relevance ranking (recency, entity, keyword boosts)
        ranked = self.relevance_ranker.rank(
            memories=hybrid_results,
            query=request.query,
            query_entities=matched_entities,
        )
        
        log.debug(
            "ranking_done",
            count=len(ranked),
            top_score=ranked[0].final_score if ranked else 0,
        )
        
        # Step 7: Context window optimization with token budgeting
        # Create optimizer with request-specific token limit
        context_optimizer = ContextOptimizer(max_tokens=max_tokens)
        
        # Prepare memories for context optimizer
        memories_for_context = [
            {
                "id": r.id,
                "content": r.content,
                "relevance": r.final_score,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in ranked
        ]
        
        optimized = context_optimizer.optimize(
            memories=memories_for_context,
            sort_by_relevance=False,  # Already sorted by ranker
        )
        
        log.debug(
            "context_optimized",
            total_tokens=optimized.total_tokens,
            chunks=len(optimized.chunks),
            truncated=optimized.truncated_count,
            dropped=optimized.dropped_count,
        )
        
        # Step 8: Build final response
        # Use top N from ranked results (respecting limit)
        final_ranked = ranked[:request.limit]
        
        memories: list[RecallResult] = []
        for r in final_ranked:
            memories.append(RecallResult(
                id=r.id,
                relevance=r.final_score,
                content=r.content,
                created_at=r.created_at or datetime.utcnow(),
            ))
            # Update access tracking
            await self.db.update_access(r.id)
        
        # Combine all entities (matched + related)
        all_entities = list(matched_entities)
        seen_entity_ids = {e.id for e in all_entities}
        for entity in related_entities:
            if entity.id not in seen_entity_ids:
                all_entities.append(entity)
                seen_entity_ids.add(entity.id)
        
        log.info(
            "recall_complete_v2",
            user_id=request.user_id,
            results_count=len(memories),
            entities_count=len(all_entities),
            context_tokens=optimized.total_tokens,
        )

        return RecallResponse(
            context=optimized.context,
            memories=memories,
            entities=all_entities,
        )

    # -----------------------------------------------------------------------
    # Cross-Space Recall
    # -----------------------------------------------------------------------

    async def recall_across_spaces(
        self,
        query: str,
        agent_id: str,
        project_id: str = "default",
        limit: int = 10,
        threshold: float = 0.4,
        max_tokens: int | None = None,
    ) -> RecallResponse:
        """Recall memories from all spaces the agent has access to.

        This enables cross-agent knowledge sharing: if agent A stores
        memories in a space that agent B can read, agent B will surface
        those memories alongside its own.

        Steps:
        1. Get all space IDs the agent has read access to
        2. Collect all memory IDs across those spaces
        3. Run standard recall scoped to those memory IDs + the agent's
           own memories
        4. De-duplicate and rank
        """
        if self.space_manager is None:
            # Spaces not enabled — fall back to standard recall
            return await self.recall(RecallRequest(
                query=query,
                user_id=agent_id,
                project_id=project_id,
                limit=limit,
                threshold=threshold,
                max_tokens=max_tokens,
            ))

        log.info(
            "recall_across_spaces",
            agent_id=agent_id,
            query_length=len(query),
        )

        # 1. Get accessible spaces
        space_ids = await self.space_manager.get_accessible_space_ids(agent_id)

        # 2. Collect memory IDs from all accessible spaces
        space_memory_ids: set[str] = set()
        for sid in space_ids:
            mids = await self.space_manager.get_space_memory_ids(sid, limit=500)
            space_memory_ids.update(mids)

        # 3. Run the agent's own recall first
        own_result = await self.recall(RecallRequest(
            query=query,
            user_id=agent_id,
            project_id=project_id,
            limit=limit,
            threshold=threshold,
            max_tokens=max_tokens,
        ))

        if not space_memory_ids:
            return own_result

        # 4. Embed the query and search space memories
        query_vector = await self.embeddings.embed(query)

        # Fetch each space memory from Qdrant and compute similarity
        space_results: list[RecallResult] = []
        seen_ids = {m.id for m in (own_result.memories or [])}

        for mem_id in space_memory_ids:
            if mem_id in seen_ids:
                continue
            try:
                mem_data = await self.qdrant.get_by_id(mem_id)
                if mem_data is None:
                    continue
                # Compute cosine similarity using the stored embedding
                stored_vec = mem_data.get("embedding")
                if stored_vec:
                    from numpy import dot
                    from numpy.linalg import norm
                    sim = float(dot(query_vector, stored_vec) / (norm(query_vector) * norm(stored_vec) + 1e-9))
                else:
                    sim = 0.5  # Default if no embedding stored
                if sim < threshold:
                    continue
                space_results.append(RecallResult(
                    id=mem_id,
                    content=mem_data.get("content", ""),
                    relevance=sim,
                    created_at=datetime.fromisoformat(mem_data["created_at"]) if mem_data.get("created_at") else datetime.utcnow(),
                ))
                seen_ids.add(mem_id)
            except Exception as e:
                log.debug("space_memory_fetch_failed", mem_id=mem_id, error=str(e))
                continue

        # 5. Merge and re-sort by relevance
        all_memories = list(own_result.memories or []) + space_results
        all_memories.sort(key=lambda m: m.relevance, reverse=True)
        final_memories = all_memories[:limit]

        # Rebuild context from merged results
        context_parts = []
        for m in final_memories:
            context_parts.append(m.content)
        merged_context = "\n\n".join(context_parts)

        log.info(
            "recall_across_spaces_done",
            agent_id=agent_id,
            own_count=len(own_result.memories or []),
            space_count=len(space_results),
            total=len(final_memories),
        )

        return RecallResponse(
            context=merged_context,
            memories=final_memories,
            entities=own_result.entities or [],
        )

    # -----------------------------------------------------------------------
    # Update
    # -----------------------------------------------------------------------

    async def update(
        self,
        memory_id: str,
        user_id: str,
        new_content: str,
        new_metadata: dict[str, Any] | None = None,
    ) -> UpdateResponse:
        """
        Update memory content, re-extract facts/entities, re-embed.
        
        Steps:
        1. Fetch existing memory and verify ownership
        2. Re-extract facts from new content
        3. Re-extract entities from new content  
        4. Generate new embedding
        5. Update vector in Qdrant
        6. Update metadata in SQLite
        7. Update entities (delete old links, create new ones)
        
        Args:
            memory_id: ID of memory to update
            user_id: User ID (must match memory owner)
            new_content: New content text
            new_metadata: Optional metadata to merge
            
        Returns:
            UpdateResponse with updated entity refs
        """
        log.info("updating_memory", memory_id=memory_id, user_id=user_id)
        
        # 1. Fetch existing memory from database
        existing = await self.db.get_memory(memory_id)
        if not existing or existing.get("user_id") != user_id:
            raise ValueError(f"Memory {memory_id} not found")
        
        project_id = existing.get("project_id", "default")
        
        # 2. Re-extract facts from new content
        extracted_facts = await self.extractor.extract(new_content)
        if not extracted_facts:
            extracted_facts = [new_content.strip()]
        
        # 3. Generate new embedding
        embedding = await self.embeddings.embed(new_content)
        
        # 4. Update vector in Qdrant
        from remembra.models.memory import Memory
        memory = Memory(
            id=memory_id,
            user_id=user_id,
            project_id=project_id,
            content=new_content,
            extracted_facts=extracted_facts,
            entities=[],
            embedding=embedding,
            metadata=existing.get("metadata") or {},
        )
        await self.qdrant.upsert(memory)
        
        # 5. Update metadata in SQLite
        existing_meta = {}
        if existing.get("metadata"):
            import json
            existing_meta = json.loads(existing["metadata"]) if isinstance(existing["metadata"], str) else existing["metadata"]
        merged_metadata = {**existing_meta, **(new_metadata or {})}
        
        await self.db.update_memory(
            memory_id=memory_id,
            content=new_content,
            extracted_facts=extracted_facts,
            metadata=merged_metadata,
        )
        
        # 6. Update entities (delete old links, create new ones)
        await self.db.delete_memory_entities(memory_id)
        
        entity_refs: list[EntityRef] = []
        if self.settings.enable_entity_resolution:
            try:
                entity_refs = await self._process_entities_for_memory(
                    memory_id=memory_id,
                    content=new_content,
                    user_id=user_id,
                    project_id=project_id,
                )
            except Exception as e:
                log.warning("entity_update_failed", error=str(e), memory_id=memory_id)
        
        # 7. Handle conflict detection if enabled
        if self.conflict_manager:
            try:
                from remembra.extraction.conflicts import MemoryConflict, ConflictStatus
                conflict = MemoryConflict(
                    user_id=user_id,
                    project_id=project_id,
                    new_fact=new_content,
                    existing_memory_id=memory_id,
                    existing_content=existing.get("content", ""),
                    similarity_score=1.0,
                    reason="Memory updated via PATCH",
                    strategy_applied=self.conflict_manager.default_strategy,
                    status=ConflictStatus.RESOLVED,
                )
                await self.conflict_manager.record(conflict)
            except Exception as e:
                log.warning("conflict_recording_failed", error=str(e))
        
        log.info(
            "memory_updated",
            memory_id=memory_id,
            facts_count=len(extracted_facts),
            entities_count=len(entity_refs),
        )
        
        return UpdateResponse(id=memory_id, updated_entities=entity_refs)

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
            # SECURITY: Verify ownership before deleting (prevent IDOR)
            if user_id:
                memory = await self.db.get_memory(memory_id)
                if not memory:
                    log.warning("forget_memory_not_found", memory_id=memory_id)
                    return ForgetResponse(deleted_memories=0, deleted_entities=0, deleted_relationships=0)
                if memory.get("user_id") != user_id:
                    log.warning("forget_memory_unauthorized", memory_id=memory_id, user_id=user_id)
                    return ForgetResponse(deleted_memories=0, deleted_entities=0, deleted_relationships=0)
            
            # Delete specific memory (ownership verified)
            await self.qdrant.delete(memory_id)
            await self.db.delete_memory_fts(memory_id)  # Clean FTS5 index
            if await self.db.delete_memory(memory_id):
                deleted_memories = 1
            log.info("forgot_memory", memory_id=memory_id, user_id=user_id)

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

    # -----------------------------------------------------------------------
    # Temporal Features (Week 8)
    # -----------------------------------------------------------------------

    def calculate_decay_score(
        self,
        created_at: datetime,
        last_accessed: datetime | None,
        access_count: int,
        half_life_days: float = 30.0,
        access_boost: float = 0.1,
    ) -> float:
        """
        Calculate memory decay score based on age and access patterns.
        
        The decay model combines:
        1. Exponential time decay (older = lower score)
        2. Access frequency boost (more accesses = higher score)
        3. Recency of access (recently accessed = higher score)
        
        Args:
            created_at: When the memory was created
            last_accessed: When the memory was last accessed (can be None)
            access_count: Number of times the memory was accessed
            half_life_days: Days until memory decays to 50% (default: 30)
            access_boost: Boost per access (default: 0.1)
            
        Returns:
            Decay score between 0.0 (fully decayed) and 1.0+ (fresh/boosted)
        """
        now = datetime.utcnow()
        
        # Calculate age-based decay (exponential)
        age_days = (now - created_at).total_seconds() / 86400.0
        decay_factor = math.exp(-math.log(2) * age_days / half_life_days)
        
        # Access count boost (log scale to prevent runaway)
        if access_count > 0:
            access_factor = 1.0 + access_boost * math.log(1 + access_count)
        else:
            access_factor = 1.0
        
        # Recency of access boost
        recency_boost = 0.0
        if last_accessed:
            recency_days = (now - last_accessed).total_seconds() / 86400.0
            # Boost fades with same half-life
            recency_boost = 0.2 * math.exp(-math.log(2) * recency_days / half_life_days)
        
        return decay_factor * access_factor + recency_boost

    async def recall_as_of(
        self,
        user_id: str,
        query: str,
        as_of: datetime,
        project_id: str = "default",
        limit: int = 5,
    ) -> RecallResponse:
        """
        Recall memories as they existed at a specific point in time.
        
        This enables "time travel" queries - seeing what the memory
        state was in the past. Useful for:
        - Auditing what an AI knew at a specific time
        - Debugging memory changes
        - Historical analysis
        
        Args:
            user_id: User ID
            query: Natural language query
            as_of: The point in time to query from
            project_id: Project namespace
            limit: Maximum results
            
        Returns:
            RecallResponse with memories that existed at as_of time
        """
        log.info(
            "recall_as_of",
            user_id=user_id,
            as_of=as_of.isoformat(),
            query=query[:50],
        )
        
        # Get memories that existed at that time
        historical_memories = await self.db.get_memories_as_of(
            user_id=user_id,
            project_id=project_id,
            as_of=as_of,
            limit=limit * 2,  # Get more for filtering
        )
        
        if not historical_memories:
            return RecallResponse(context="", memories=[], entities=[])
        
        # Embed query and find most relevant among historical
        await self.embeddings.embed(query)
        
        # Re-embed historical memories for comparison
        # (In production, we'd store embeddings - this is for correctness)
        results: list[RecallResult] = []
        for mem in historical_memories[:limit]:
            results.append(RecallResult(
                id=mem["id"],
                content=mem["content"],
                relevance=0.8,  # Simplified - historical queries don't rank
                created_at=datetime.fromisoformat(mem["created_at"]),
            ))
        
        # Build context string
        context_parts = []
        for r in results:
            context_parts.append(f"[{r.created_at.strftime('%Y-%m-%d')}] {r.content}")
        context = "\n".join(context_parts)
        
        return RecallResponse(
            context=context,
            memories=results,
            entities=[],
        )

    async def cleanup_expired(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """
        Clean up expired memories (TTL-based expiration).
        
        This should be called periodically (e.g., via cron or heartbeat)
        to remove memories that have exceeded their TTL.
        
        Args:
            user_id: Optional user filter
            project_id: Optional project filter
            
        Returns:
            Number of memories deleted
        """
        # Get expired memory IDs
        expired_ids = await self.db.get_expired_memories(
            user_id=user_id,
            project_id=project_id or "default",
        )
        
        if not expired_ids:
            return 0
        
        log.info("cleaning_expired_memories", count=len(expired_ids))
        
        # Delete from Qdrant and SQLite
        deleted = 0
        for memory_id in expired_ids:
            try:
                await self.qdrant.delete(memory_id)
                await self.db.delete_memory_fts(memory_id)
                if await self.db.delete_memory(memory_id):
                    deleted += 1
            except Exception as e:
                log.warning("expired_memory_cleanup_failed", memory_id=memory_id, error=str(e))
        
        log.info("expired_memories_cleaned", deleted=deleted)
        return deleted

    async def get_memories_with_decay(
        self,
        user_id: str,
        project_id: str = "default",
        min_decay_score: float = 0.1,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get memories with their decay scores for ranking/filtering.
        
        This is useful for:
        - Prioritizing recent/active memories in recall
        - Finding "stale" memories that might be archived
        - Analytics on memory usage patterns
        
        Args:
            user_id: User ID
            project_id: Project namespace
            min_decay_score: Filter out memories below this decay score
            limit: Maximum memories to return
            
        Returns:
            List of memories with decay_score added
        """
        memories = await self.db.get_memories_with_decay_info(
            user_id=user_id,
            project_id=project_id,
            limit=limit,
        )
        
        results = []
        half_life = self.settings.ranking_recency_decay_days
        
        for mem in memories:
            created_at = datetime.fromisoformat(mem["created_at"])
            last_accessed = (
                datetime.fromisoformat(mem["last_accessed"])
                if mem.get("last_accessed")
                else None
            )
            access_count = mem.get("access_count", 0)
            
            decay_score = self.calculate_decay_score(
                created_at=created_at,
                last_accessed=last_accessed,
                access_count=access_count,
                half_life_days=half_life,
            )
            
            if decay_score >= min_decay_score:
                results.append({
                    **mem,
                    "decay_score": decay_score,
                })
        
        # Sort by decay score (highest first)
        results.sort(key=lambda x: x["decay_score"], reverse=True)
        return results
