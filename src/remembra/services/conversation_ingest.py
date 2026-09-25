"""
Conversation Ingestion Service.

Orchestrates the extraction pipeline for conversations:
1. Message selection (system role excluded by default)
2. Fact extraction with a conversation-aware prompt (speaker attribution,
   importance, relative dates resolved against message timestamps)
3. Entity extraction for the response
4. Per-fact grounding + consolidation decision + storage through
   ``MemoryService.store_fact`` — the SAME path as ``store()``. Nothing is
   extracted or consolidated twice, and existing memories are never rewritten
   or deleted: an update stores the new fact and marks the old one superseded.
"""

import json
import time
from typing import Any

import structlog
from openai import AsyncOpenAI

from remembra.config import Settings
from remembra.core.ai_spend import SpendBudgetExceeded, metered_chat
from remembra.core.time import utcnow
from remembra.extraction.prompting import reference_date_line, wrap_untrusted
from remembra.extraction.prompts.conversation import (
    CONVERSATION_EXTRACTION_SYSTEM_PROMPT,
    CONVERSATION_EXTRACTION_USER_PROMPT,
    format_messages_as_json,
    format_messages_for_extraction,
    select_messages,
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


class ConversationExtractionError(Exception):
    """The extraction model call failed or returned something unusable."""


def _clamp01(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(0.0, min(1.0, float(value)))


class ConversationIngestService:
    """Service for ingesting conversations and extracting memories."""

    def __init__(
        self,
        settings: Settings,
        memory_service: Any,  # MemoryService - avoid circular import
    ) -> None:
        self.settings = settings
        self.memory_service = memory_service
        self.entity_extractor = memory_service.entity_extractor

        # LLM client for conversation-specific extraction
        self._client: AsyncOpenAI | None = None

        log.info("conversation_ingest_service_initialized")

    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.settings.openai_api_key, max_retries=1)
        return self._client

    async def ingest(
        self,
        request: ConversationIngestRequest,
    ) -> ConversationIngestResponse:
        """
        Main ingestion pipeline.

        Returns status "ok" when every fact was processed, "partial" when some
        facts failed, and "error" when extraction itself failed (nothing is
        reported as ok when nothing could be extracted).
        """
        start_time = time.time()

        # The API layer overrides request.user_id with the authenticated user_id.
        assert request.user_id is not None, "user_id must be set by the API layer before ingest"

        log.info(
            "conversation_ingest_started",
            user_id=request.user_id,
            message_count=len(request.messages),
            session_id=request.session_id,
            options=request.options.model_dump(),
        )

        extracted_facts: list[ExtractedFact] = []
        extracted_entities: list[ExtractedEntityResult] = []
        deduped_results: list[DedupeResult] = []
        errors: list[str] = []
        stats = IngestStats(messages_processed=len(request.messages))

        def response(status: str) -> ConversationIngestResponse:
            stats.processing_time_ms = int((time.time() - start_time) * 1000)
            return ConversationIngestResponse(
                status=status,
                session_id=request.session_id,
                facts=extracted_facts,
                entities=extracted_entities,
                deduped=deduped_results,
                stats=stats,
                errors=errors,
            )

        if not request.options.infer:
            return await self._store_raw_messages(request, start_time)

        message_dicts = [m.model_dump() for m in request.messages]
        try:
            extracted_facts = await self._extract_facts(
                messages=request.messages,
                options=request.options,
                context=request.context,
            )
        except ConversationExtractionError as e:
            if isinstance(e.__cause__, SpendBudgetExceeded):
                # The write's reserved AI budget is used up: keep the messages verbatim.
                log.info("conversation_extraction_skipped_budget")
                return await self._store_raw_messages(request, start_time)
            log.error("conversation_extraction_failed", error=str(e))
            errors.append(f"Extraction failed: {e}")
            return response("error")
        stats.facts_extracted = len(extracted_facts)

        transcript = format_messages_for_extraction(
            message_dicts,
            extract_from=request.options.extract_from,
            include_system=request.options.include_system,
        )

        try:
            entity_result = await self.entity_extractor.extract(transcript)
            for entity in entity_result.entities:
                extracted_entities.append(ExtractedEntityResult(name=entity.name, type=entity.type))
            for rel in entity_result.relationships:
                extracted_entities.append(
                    ExtractedEntityResult(
                        name=rel.subject,
                        type="relationship",
                        relationship=f"{rel.predicate} {rel.object}",
                        subtype=rel.predicate,
                    )
                )
            stats.entities_found = len(entity_result.entities)
        except Exception as e:  # entity listing is informational; never fails the ingest
            log.warning("conversation_entity_extraction_failed", error=str(e))
            errors.append("Entity extraction failed")

        if not request.options.store:
            for fact in extracted_facts:
                fact.stored = False
                fact.action = "add"
                fact.action_reason = "Dry run - not stored"
            return response("ok")

        sibling_ids: set[str] = set()
        for fact in extracted_facts:
            try:
                result = await self.memory_service.store_fact(
                    fact=fact.content,
                    user_id=request.user_id,
                    project_id=request.project_id,
                    metadata={
                        "source": "conversation_ingest",
                        "session_id": request.session_id,
                        "source_message_index": fact.source_message_index,
                        "speaker": fact.speaker,
                        "importance": fact.importance,
                        "channel": request.context.get("channel") if request.context else None,
                    },
                    source="conversation_ingest",
                    trust_score=fact.confidence,
                    grounding_source=transcript,
                    consolidate=request.options.dedupe,
                    exclude_ids=sibling_ids,
                )
            except Exception as e:
                log.error("fact_processing_error", fact=fact.content[:50], error=str(e))
                fact.action = "skipped"
                fact.action_reason = "Store failed"
                errors.append(f"Failed to process fact {fact.content[:40]!r}: {type(e).__name__}")
                continue

            fact.action = result.action
            fact.action_reason = result.reason
            fact.memory_id = result.memory_id
            fact.stored = result.memory_id is not None
            if result.memory_id:
                sibling_ids.add(result.memory_id)

            if result.action == "add":
                stats.facts_stored += 1
            elif result.action == "supersede":
                stats.facts_stored += 1
                stats.facts_updated += 1
                deduped_results.append(
                    DedupeResult(content=fact.content, existing_memory_id=result.target_id or "", action="superseded")
                )
            elif result.action == "noop":
                stats.facts_skipped += 1
                stats.facts_deduped += 1
                if result.target_id:
                    deduped_results.append(
                        DedupeResult(content=fact.content, existing_memory_id=result.target_id, action="skipped")
                    )
            elif result.action == "dropped":
                stats.facts_dropped += 1

        status = "ok" if not errors else "partial"
        log.info(
            "conversation_ingest_completed",
            user_id=request.user_id,
            status=status,
            facts_extracted=stats.facts_extracted,
            facts_stored=stats.facts_stored,
            facts_deduped=stats.facts_deduped,
            facts_dropped=stats.facts_dropped,
        )
        return response(status)

    async def _store_raw_messages(
        self,
        request: ConversationIngestRequest,
        start_time: float,
    ) -> ConversationIngestResponse:
        """
        Store messages verbatim (infer=False): one atomic memory per message.

        ``skip_extraction=True`` means no fact extraction and no consolidation —
        raw really is raw (ING-11). System messages are skipped unless
        ``options.include_system``.
        """
        log.info("storing_raw_messages", message_count=len(request.messages))

        facts: list[ExtractedFact] = []
        errors: list[str] = []
        stored_count = 0

        selected = select_messages(
            [m.model_dump() for m in request.messages],
            extract_from="both",
            include_system=request.options.include_system,
        )
        for m in selected:
            i, speaker, role = m["index"], m["speaker"], m["role"]
            content = f"{speaker}: {m['text']}"
            fact = ExtractedFact(content=content, confidence=1.0, importance=0.5, source_message_index=i, speaker=speaker)

            if not request.options.store:
                fact.action = "add"
                fact.action_reason = "Dry run"
                facts.append(fact)
                continue
            try:
                result = await self.memory_service.store(
                    StoreRequest(
                        user_id=request.user_id,
                        content=content,
                        project_id=request.project_id,
                        metadata={
                            "source": "conversation_ingest_raw",
                            "session_id": request.session_id,
                            "message_index": i,
                            "speaker": speaker,
                            "role": role,
                            "timestamp": m["timestamp"],
                        },
                    ),
                    source="conversation_ingest",
                    trust_score=1.0,
                    skip_extraction=True,
                )
                fact.stored = True
                fact.memory_id = result.id
                fact.action = "add"
                fact.action_reason = "Raw message stored"
                stored_count += 1
            except Exception as e:
                log.error("raw_message_store_error", index=i, error=str(e))
                fact.action = "skipped"
                fact.action_reason = "Store failed"
                errors.append(f"Failed to store message {i}: {type(e).__name__}")
            facts.append(fact)

        return ConversationIngestResponse(
            status="ok" if not errors else ("error" if stored_count == 0 else "partial"),
            session_id=request.session_id,
            facts=facts,
            entities=[],
            deduped=[],
            errors=errors,
            stats=IngestStats(
                messages_processed=len(request.messages),
                facts_extracted=len(facts),
                facts_stored=stored_count,
                processing_time_ms=int((time.time() - start_time) * 1000),
            ),
        )

    async def _extract_facts(
        self,
        messages: list[ConversationMessage],
        options: IngestOptions,
        context: dict[str, Any] | None,
    ) -> list[ExtractedFact]:
        """
        Extract facts with the conversation-aware prompt.

        Each returned fact is validated (ING-15): non-empty string content,
        importance clamped to [0, 1], source_message must be one of the
        messages actually offered, and the speaker is taken from that message
        (never from free text). Raises ConversationExtractionError when the
        model call fails or returns an unusable payload.
        """
        message_dicts = [m.model_dump() for m in messages]
        offered = select_messages(message_dicts, options.extract_from, options.include_system)
        if not offered:
            return []
        speaker_by_index = {m["index"]: m["speaker"] for m in offered}

        context_section = f"CONTEXT:\n{wrap_untrusted(json.dumps(context, ensure_ascii=False))}" if context else ""
        user_prompt = CONVERSATION_EXTRACTION_USER_PROMPT.format(
            formatted_messages=wrap_untrusted(
                format_messages_as_json(message_dicts, options.extract_from, options.include_system)
            ),
            context_section=context_section,
            extract_from=options.extract_from,
            min_importance=options.min_importance,
            reference_line=reference_date_line(utcnow()),
        )

        try:
            response = await metered_chat(
                self._get_client(),
                model=self.settings.extraction_model,
                messages=[
                    {"role": "system", "content": CONVERSATION_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=60.0,
            )
        except Exception as e:
            raise ConversationExtractionError(type(e).__name__) from e

        result_text = response.choices[0].message.content
        if not result_text:
            raise ConversationExtractionError("empty response")
        try:
            result = json.loads(result_text)
        except json.JSONDecodeError as e:
            raise ConversationExtractionError("invalid JSON") from e
        if not isinstance(result, dict) or not isinstance(result.get("facts", []), list):
            raise ConversationExtractionError("response has no facts list")

        extracted: list[ExtractedFact] = []
        for fact_data in result.get("facts", []):
            if not isinstance(fact_data, dict):
                continue
            content = fact_data.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            importance = _clamp01(fact_data.get("importance"), 0.5)
            if importance < options.min_importance:
                continue
            index = fact_data.get("source_message")
            if isinstance(index, bool) or not isinstance(index, int) or index not in speaker_by_index:
                log.warning("conversation_fact_bad_source_message", source_message=str(index)[:20])
                index = -1
            extracted.append(
                ExtractedFact(
                    content=content.strip(),
                    confidence=1.0,
                    importance=importance,
                    source_message_index=index,
                    speaker=speaker_by_index.get(index),
                    stored=False,
                    action="add",
                )
            )
        return extracted
