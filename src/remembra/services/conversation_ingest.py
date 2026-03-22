"""
Conversation Ingestion Service.

Orchestrates the extraction pipeline for conversations:
1. Message parsing and filtering
2. Fact extraction (using FactExtractor with conversation-aware prompts)
3. Entity extraction (using EntityExtractor)
4. Deduplication (using MemoryConsolidator)
5. Storage (using MemoryService)

This is the #1 feature gap vs Mem0 - automatic conversation ingestion.
"""

import json
import time
from datetime import datetime
from typing import Any

import structlog
from openai import AsyncOpenAI

from remembra.config import Settings
from remembra.extraction.prompts.conversation import (
    CONVERSATION_EXTRACTION_SYSTEM_PROMPT,
    CONVERSATION_EXTRACTION_USER_PROMPT,
    DEDUP_DECISION_FUNCTIONS,
    DEDUP_DECISION_PROMPT,
    format_existing_memories,
    format_messages_for_extraction,
)
from remembra.models.memory import (
    ConversationIngestRequest,
    ConversationIngestResponse,
    ConversationMessage,
    DedupeResult,
    ExtractedEntityResult,
    ExtractedFact,
    IngestOptions,
    IngestStats,
    StoreRequest,
)

log = structlog.get_logger(__name__)


class ConversationIngestService:
    """
    Service for ingesting conversations and extracting memories.
    
    Orchestrates existing extractors into a unified pipeline:
    - FactExtractor for atomic fact extraction
    - EntityExtractor for entity/relationship extraction
    - MemoryConsolidator for deduplication decisions
    - ConflictManager for contradiction handling
    """
    
    def __init__(
        self,
        settings: Settings,
        memory_service: Any,  # MemoryService - avoid circular import
    ) -> None:
        self.settings = settings
        self.memory_service = memory_service
        
        # Access extractors through memory_service
        self.extractor = memory_service.extractor
        self.entity_extractor = memory_service.entity_extractor
        self.consolidator = memory_service.consolidator
        self.conflict_manager = memory_service.conflict_manager
        self.embeddings = memory_service.embeddings
        self.qdrant = memory_service.qdrant
        
        # LLM client for conversation-specific extraction
        self._client: AsyncOpenAI | None = None
        
        log.info("conversation_ingest_service_initialized")
    
    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        return self._client
    
    async def ingest(
        self,
        request: ConversationIngestRequest,
    ) -> ConversationIngestResponse:
        """
        Main ingestion pipeline.
        
        Phases:
        1. Message parsing - filter and format messages
        2. Fact extraction - extract atomic facts with importance scores
        3. Entity extraction - extract entities and relationships
        4. Deduplication - check each fact against existing memories
        5. Storage - store new/updated facts
        
        Args:
            request: ConversationIngestRequest with messages and options
            
        Returns:
            ConversationIngestResponse with extracted facts, entities, and stats
        """
        start_time = time.time()
        
        log.info(
            "conversation_ingest_started",
            user_id=request.user_id,
            message_count=len(request.messages),
            session_id=request.session_id,
            options=request.options.model_dump(),
        )
        
        # Initialize response components
        extracted_facts: list[ExtractedFact] = []
        extracted_entities: list[ExtractedEntityResult] = []
        deduped_results: list[DedupeResult] = []
        errors: list[str] = []
        
        stats = IngestStats(messages_processed=len(request.messages))
        
        try:
            # Phase 1: Handle raw mode (infer=False)
            if not request.options.infer:
                return await self._store_raw_messages(request, start_time)
            
            # Phase 2: Extract facts from conversation
            extracted_facts = await self._extract_facts(
                messages=request.messages,
                options=request.options,
                context=request.context,
            )
            stats.facts_extracted = len(extracted_facts)
            
            log.debug("facts_extracted", count=len(extracted_facts))
            
            # Phase 3: Extract entities
            transcript = format_messages_for_extraction(
                [m.model_dump() for m in request.messages],
                extract_from=request.options.extract_from,
            )
            entity_result = await self.entity_extractor.extract(transcript)
            
            for entity in entity_result.entities:
                extracted_entities.append(ExtractedEntityResult(
                    name=entity.name,
                    type=entity.type,
                    relationship=None,
                    subtype=None,
                ))
            
            for rel in entity_result.relationships:
                extracted_entities.append(ExtractedEntityResult(
                    name=rel.subject,
                    type="relationship",
                    relationship=f"{rel.predicate} {rel.object}",
                    subtype=rel.predicate,
                ))
            
            stats.entities_found = len(entity_result.entities)
            
            log.debug(
                "entities_extracted",
                entity_count=len(entity_result.entities),
                relationship_count=len(entity_result.relationships),
            )
            
            # Phase 4 & 5: Deduplication and Storage
            if request.options.store:
                for fact in extracted_facts:
                    try:
                        result = await self._process_fact(
                            fact=fact,
                            user_id=request.user_id,
                            project_id=request.project_id,
                            session_id=request.session_id,
                            options=request.options,
                            context=request.context,
                        )
                        
                        # Update fact with result
                        fact.action = result["action"].lower()
                        fact.action_reason = result.get("reason")
                        fact.stored = result.get("stored", False)
                        fact.memory_id = result.get("memory_id")
                        
                        # Track deduplication
                        if result["action"] in ["UPDATE", "NOOP"]:
                            if result.get("target_memory_id"):
                                deduped_results.append(DedupeResult(
                                    content=fact.content,
                                    existing_memory_id=result["target_memory_id"],
                                    action="merged" if result["action"] == "UPDATE" else "skipped",
                                ))
                        
                        # Update stats
                        if result["action"] == "ADD":
                            stats.facts_stored += 1
                        elif result["action"] == "UPDATE":
                            stats.facts_updated += 1
                            stats.facts_deduped += 1
                        elif result["action"] == "NOOP":
                            stats.facts_skipped += 1
                            stats.facts_deduped += 1
                        elif result["action"] == "DELETE":
                            stats.facts_stored += 1  # We store the new version
                            
                    except Exception as e:
                        log.error("fact_processing_error", fact=fact.content[:50], error=str(e))
                        fact.action = "skipped"
                        fact.action_reason = f"Error: {str(e)}"
                        errors.append(f"Failed to process fact: {str(e)}")
            else:
                # Dry run - mark all as not stored
                for fact in extracted_facts:
                    fact.stored = False
                    fact.action = "add"  # Would be added
                    fact.action_reason = "Dry run - not stored"
            
            processing_time_ms = int((time.time() - start_time) * 1000)
            stats.processing_time_ms = processing_time_ms
            
            status = "ok" if not errors else "partial"
            
            log.info(
                "conversation_ingest_completed",
                user_id=request.user_id,
                status=status,
                facts_extracted=stats.facts_extracted,
                facts_stored=stats.facts_stored,
                facts_deduped=stats.facts_deduped,
                processing_time_ms=processing_time_ms,
            )
            
            return ConversationIngestResponse(
                status=status,
                session_id=request.session_id,
                facts=extracted_facts,
                entities=extracted_entities,
                deduped=deduped_results,
                stats=stats,
            )
            
        except Exception as e:
            log.error("conversation_ingest_failed", error=str(e))
            processing_time_ms = int((time.time() - start_time) * 1000)
            stats.processing_time_ms = processing_time_ms
            
            return ConversationIngestResponse(
                status="error",
                session_id=request.session_id,
                facts=extracted_facts,
                entities=extracted_entities,
                deduped=deduped_results,
                stats=stats,
            )
    
    async def _store_raw_messages(
        self,
        request: ConversationIngestRequest,
        start_time: float,
    ) -> ConversationIngestResponse:
        """
        Store messages as raw memories without extraction (infer=False mode).
        """
        log.info("storing_raw_messages", message_count=len(request.messages))
        
        facts: list[ExtractedFact] = []
        stored_count = 0
        
        for i, msg in enumerate(request.messages):
            if msg.role == "system":
                continue  # Skip system messages
            
            # Build content with speaker attribution
            speaker = msg.name or msg.role.capitalize()
            content = f"{speaker}: {msg.content}"
            
            # Store via memory service
            if request.options.store:
                try:
                    store_request = StoreRequest(
                        user_id=request.user_id,
                        content=content,
                        project_id=request.project_id,
                        metadata={
                            "source": "conversation_ingest_raw",
                            "session_id": request.session_id,
                            "message_index": i,
                            "speaker": speaker,
                            "role": msg.role,
                            "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                        },
                    )
                    result = await self.memory_service.store(
                        store_request,
                        source="conversation_ingest",
                        trust_score=1.0,
                    )
                    
                    facts.append(ExtractedFact(
                        content=content,
                        confidence=1.0,
                        importance=0.5,
                        source_message_index=i,
                        speaker=speaker,
                        stored=True,
                        memory_id=result.id,
                        action="add",
                        action_reason="Raw message stored",
                    ))
                    stored_count += 1
                    
                except Exception as e:
                    log.error("raw_message_store_error", index=i, error=str(e))
                    facts.append(ExtractedFact(
                        content=content,
                        confidence=1.0,
                        importance=0.5,
                        source_message_index=i,
                        speaker=speaker,
                        stored=False,
                        action="skipped",
                        action_reason=f"Error: {str(e)}",
                    ))
            else:
                facts.append(ExtractedFact(
                    content=content,
                    confidence=1.0,
                    importance=0.5,
                    source_message_index=i,
                    speaker=speaker,
                    stored=False,
                    action="add",
                    action_reason="Dry run",
                ))
        
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        return ConversationIngestResponse(
            status="ok",
            session_id=request.session_id,
            facts=facts,
            entities=[],
            deduped=[],
            stats=IngestStats(
                messages_processed=len(request.messages),
                facts_extracted=len(facts),
                facts_stored=stored_count,
                processing_time_ms=processing_time_ms,
            ),
        )
    
    async def _extract_facts(
        self,
        messages: list[ConversationMessage],
        options: IngestOptions,
        context: dict[str, Any] | None,
    ) -> list[ExtractedFact]:
        """
        Extract facts from conversation using LLM with conversation-aware prompt.
        """
        # Format messages for extraction
        formatted = format_messages_for_extraction(
            [m.model_dump() for m in messages],
            extract_from=options.extract_from,
        )
        
        # Build context section
        context_section = ""
        if context:
            context_section = f"CONTEXT: {json.dumps(context)}"
        
        # Build user prompt
        user_prompt = CONVERSATION_EXTRACTION_USER_PROMPT.format(
            formatted_messages=formatted,
            context_section=context_section,
            extract_from=options.extract_from,
            min_importance=options.min_importance,
        )
        
        try:
            client = self._get_client()
            
            response = await client.chat.completions.create(
                model=self.settings.extraction_model,
                messages=[
                    {"role": "system", "content": CONVERSATION_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=60.0,
            )
            
            result_text = response.choices[0].message.content
            if not result_text:
                log.warning("empty_extraction_response")
                return []
            
            result = json.loads(result_text)
            raw_facts = result.get("facts", [])
            
            # Convert to ExtractedFact objects and filter by importance
            extracted = []
            for fact_data in raw_facts:
                importance = fact_data.get("importance", 0.5)
                
                # Filter by minimum importance
                if importance < options.min_importance:
                    continue
                
                extracted.append(ExtractedFact(
                    content=fact_data.get("content", ""),
                    confidence=1.0,  # Extraction confidence
                    importance=importance,
                    source_message_index=fact_data.get("source_message", 0),
                    speaker=fact_data.get("speaker"),
                    stored=False,
                    action="add",
                ))
            
            return extracted
            
        except json.JSONDecodeError as e:
            log.error("extraction_json_error", error=str(e))
            return []
        except Exception as e:
            log.error("extraction_error", error=str(e))
            return []
    
    async def _process_fact(
        self,
        fact: ExtractedFact,
        user_id: str,
        project_id: str,
        session_id: str | None,
        options: IngestOptions,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        Process a single fact through deduplication and storage.
        
        Returns dict with: action, reason, stored, memory_id, target_memory_id
        """
        # Step 1: Generate embedding for this fact
        embedding = await self.embeddings.embed(fact.content)
        
        # Step 2: Search for similar existing memories
        similar_memories = await self.qdrant.search(
            query_vector=embedding,
            user_id=user_id,
            project_id=project_id,
            limit=5,
            score_threshold=0.5,  # Lower threshold for dedup checking
        )
        
        # Step 3: If no similar memories, just ADD
        if not similar_memories and options.dedupe:
            return await self._store_new_fact(
                fact=fact,
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                context=context,
            )
        
        # Step 4: Use LLM to decide action (if dedup enabled)
        if options.dedupe and similar_memories:
            decision = await self._get_dedup_decision(
                new_fact=fact.content,
                existing_memories=[
                    {"id": m.get("id"), "content": m.get("content"), "score": m.get("score", 0.0)}
                    for m in similar_memories
                ],
            )
            
            # Execute decision
            if decision["action"] == "ADD":
                return await self._store_new_fact(
                    fact=fact,
                    user_id=user_id,
                    project_id=project_id,
                    session_id=session_id,
                    context=context,
                    reason=decision.get("reason"),
                )
            
            elif decision["action"] == "UPDATE":
                return await self._update_existing(
                    fact=fact,
                    target_id=decision.get("target_memory_id"),
                    merged_content=decision.get("merged_content"),
                    user_id=user_id,
                    project_id=project_id,
                    session_id=session_id,
                    context=context,
                    reason=decision.get("reason"),
                )
            
            elif decision["action"] == "DELETE":
                # Delete old, store new
                if decision.get("target_memory_id"):
                    await self.memory_service.forget_by_id(
                        memory_id=decision["target_memory_id"],
                        user_id=user_id,
                    )
                return await self._store_new_fact(
                    fact=fact,
                    user_id=user_id,
                    project_id=project_id,
                    session_id=session_id,
                    context=context,
                    reason=f"Replaced old memory: {decision.get('reason')}",
                )
            
            elif decision["action"] == "NOOP":
                return {
                    "action": "NOOP",
                    "reason": decision.get("reason", "Already exists"),
                    "stored": False,
                    "memory_id": None,
                    "target_memory_id": decision.get("target_memory_id"),
                }
        
        # Default: store as new
        return await self._store_new_fact(
            fact=fact,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            context=context,
        )
    
    async def _get_dedup_decision(
        self,
        new_fact: str,
        existing_memories: list[dict],
    ) -> dict[str, Any]:
        """
        Use LLM with function calling to decide dedup action.
        """
        try:
            client = self._get_client()
            
            formatted_existing = format_existing_memories(existing_memories)
            
            prompt = DEDUP_DECISION_PROMPT.format(
                new_fact=new_fact,
                existing_memories=formatted_existing,
            )
            
            response = await client.chat.completions.create(
                model=self.settings.extraction_model,
                messages=[
                    {"role": "user", "content": prompt},
                ],
                tools=DEDUP_DECISION_FUNCTIONS,
                tool_choice={"type": "function", "function": {"name": "decide_action"}},
                temperature=0.1,
                timeout=30.0,
            )
            
            # Extract function call result
            tool_call = response.choices[0].message.tool_calls
            if tool_call and len(tool_call) > 0:
                args = json.loads(tool_call[0].function.arguments)
                return {
                    "action": args.get("action", "ADD"),
                    "reason": args.get("reason", ""),
                    "target_memory_id": args.get("target_memory_id"),
                    "merged_content": args.get("merged_content"),
                }
            
            # Fallback to ADD
            return {"action": "ADD", "reason": "No decision returned"}
            
        except Exception as e:
            log.error("dedup_decision_error", error=str(e))
            return {"action": "ADD", "reason": f"Decision error: {str(e)}"}
    
    async def _store_new_fact(
        self,
        fact: ExtractedFact,
        user_id: str,
        project_id: str,
        session_id: str | None,
        context: dict[str, Any] | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Store a new fact as a memory."""
        try:
            store_request = StoreRequest(
                user_id=user_id,
                content=fact.content,
                project_id=project_id,
                metadata={
                    "source": "conversation_ingest",
                    "session_id": session_id,
                    "source_message_index": fact.source_message_index,
                    "speaker": fact.speaker,
                    "importance": fact.importance,
                    "channel": context.get("channel") if context else None,
                },
            )
            
            result = await self.memory_service.store(
                store_request,
                source="conversation_ingest",
                trust_score=fact.confidence,
            )
            
            return {
                "action": "ADD",
                "reason": reason or "New information",
                "stored": True,
                "memory_id": result.id,
                "target_memory_id": None,
            }
            
        except Exception as e:
            log.error("store_fact_error", error=str(e))
            return {
                "action": "ADD",
                "reason": f"Store failed: {str(e)}",
                "stored": False,
                "memory_id": None,
                "target_memory_id": None,
            }
    
    async def _update_existing(
        self,
        fact: ExtractedFact,
        target_id: str | None,
        merged_content: str | None,
        user_id: str,
        project_id: str,
        session_id: str | None,
        context: dict[str, Any] | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing memory with merged content."""
        if not target_id:
            # Fallback to storing as new
            return await self._store_new_fact(
                fact=fact,
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                context=context,
                reason="No target ID for update, stored as new",
            )
        
        try:
            # Use merged content if provided, otherwise use fact content
            content = merged_content or fact.content
            
            # Update the memory
            # Note: MemoryService.update() method varies by implementation
            # This is a simplified version - adjust based on actual method signature
            await self.memory_service.db.conn.execute(
                """
                UPDATE memories 
                SET content = ?, updated_at = ? 
                WHERE id = ? AND user_id = ?
                """,
                (content, datetime.utcnow().isoformat(), target_id, user_id),
            )
            await self.memory_service.db.conn.commit()
            
            return {
                "action": "UPDATE",
                "reason": reason or "Merged with existing",
                "stored": True,
                "memory_id": target_id,
                "target_memory_id": target_id,
            }
            
        except Exception as e:
            log.error("update_memory_error", error=str(e))
            # Fallback to storing as new
            return await self._store_new_fact(
                fact=fact,
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                context=context,
                reason=f"Update failed: {str(e)}, stored as new",
            )
