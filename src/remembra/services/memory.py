"""Memory service - core business logic for store, recall, update, forget."""

import asyncio
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from remembra.config import Settings
from remembra.core.time import utcnow
from remembra.extraction import metrics
from remembra.extraction.background import spawn
from remembra.extraction.conflicts import (
    ConflictManager,
    ConflictStatus,
    ConflictStrategy,
    MemoryConflict,
)
from remembra.extraction.consolidator import (
    ConsolidationAction,
    ConsolidationResult,
    ExistingMemory,
    MemoryConsolidator,
    validate_decision,
)
from remembra.extraction.entities import create_entity_extractor
from remembra.extraction.extractor import ExtractionConfig, FactExtractor
from remembra.extraction.matcher import EntityMatcher, ExistingEntity, rank_candidates
from remembra.extraction.typesafe import JevDecider
from remembra.extraction.typesafe import decide as jev_decide
from remembra.models.memory import (
    ConsolidationEntry,
    DivergenceDetail,
    DroppedFact,
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
    SupersedeResponse,
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


_USER_MEMORY_TYPES = {"observation", "fact", "inference", "task"}
_ENTITY_CANDIDATE_LIMIT = 500


def _decision_label(action: str, target_id: str | None) -> str:
    """Comparable label for decision_log: 'ADD', 'NOOP:<id>', 'SUPERSEDE:<id>'."""
    return action if action == "ADD" or not target_id else f"{action}:{target_id}"


def _parse_date(value: Any) -> datetime | None:
    """Parse an ISO date/datetime (or pass a datetime through); None if absent or invalid."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


@dataclass
class _FactResult:
    """Outcome of the per-fact pipeline."""

    fact: str
    action: str  # "add" | "noop" | "supersede" | "dropped"
    decided_by: str
    memory_id: str | None = None
    target_id: str | None = None
    confidence: float | None = None
    reason: str | None = None
    grounding_score: float | None = None


@dataclass
class _StoreOutcome:
    """Everything one store() call did, in order."""

    extracted: list[str]
    source_id: str | None
    extraction: str
    results: list[_FactResult] = field(default_factory=list)

    def add(self, result: _FactResult) -> None:
        self.results.append(result)

    @property
    def stored(self) -> list[_FactResult]:
        return [r for r in self.results if r.memory_id]

    @property
    def decided(self) -> list[_FactResult]:
        return [r for r in self.results if r.action == "noop"]

    @property
    def dropped(self) -> list[_FactResult]:
        return [r for r in self.results if r.action == "dropped"]

    def to_response(self, expires_at: datetime | None, entities_status: str) -> StoreResponse:
        stored = self.stored
        noops = self.decided
        if stored:
            status, rid, dup = "stored", stored[0].memory_id or "", None
        elif noops:
            # ING-24: nothing new was stored; say so instead of passing the
            # pre-existing id off as a fresh store.
            status, rid, dup = "duplicate", noops[0].target_id or "", noops[0].target_id
        else:
            status, rid, dup = "not_stored", "", None
        return StoreResponse(
            id=rid,
            extracted_facts=[r.fact for r in stored],
            entities=[],
            status=status,
            duplicate_of=dup,
            consolidation=[
                ConsolidationEntry(
                    fact=r.fact,
                    action=r.action,
                    target_id=r.target_id,
                    memory_id=r.memory_id,
                    confidence=r.confidence,
                    decided_by=r.decided_by,
                    reason=r.reason,
                )
                for r in self.results
                if r.action != "dropped"
            ],
            dropped_facts=[
                DroppedFact(fact=r.fact, reason=r.reason or "", grounding_score=r.grounding_score, decided_by=r.decided_by)
                for r in self.dropped
            ],
            entities_status=entities_status,
            extraction=self.extraction,
            expires_at=expires_at,
            source_id=self.source_id,
        )


def _coerce_metadata(value: Any) -> dict[str, Any]:
    """Return metadata as a dict regardless of how it was stored.

    SQLite stores metadata as a JSON TEXT column. Some fetch paths (notably
    graph retrieval) surface the raw string; passing that straight into
    RecallResult(metadata=...) raises a pydantic ValidationError and 500s the
    whole recall. This normalizes string/None/dict inputs to a dict so a
    single memory with metadata can never break a recall.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _is_qdrant_point_id(value: str) -> bool:
    """Qdrant point IDs must be a UUID or an unsigned integer; anything else is invalid.

    Used to avoid issuing a Qdrant lookup for malformed ids, which would raise
    and surface to API callers as a 500 instead of a clean "not found".
    """
    if not isinstance(value, str):
        return False
    if value.isdigit():
        return True
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _get_nested_metadata_value(metadata: dict[str, Any], key: str) -> Any:
    """Fetch a potentially nested metadata value.

    Supports dotted keys like ``"regime.phase"`` for nested dict metadata.
    """
    if not key:
        return None

    value: Any = metadata
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        if part not in value:
            return None
        value = value.get(part)
    return value


def metadata_filters_match(metadata: dict[str, Any], filters: dict[str, str]) -> bool:
    """Return True if metadata satisfies all AND-combined exact-match filters.

    Matching rules (backward compatible for scalar values):
    - Scalars: compare ``str(value) == str(filter_value)``
    - Lists: match if ANY element stringifies equal to the filter value
    - Nested keys: dotted paths (e.g. ``"regime.phase"``) are supported
    """
    for k, v in filters.items():
        meta_value = _get_nested_metadata_value(metadata, k)

        if isinstance(meta_value, list):
            if not any(str(item) == str(v) for item in meta_value):
                return False
            continue

        if str(meta_value) != str(v):
            return False

    return True


def parse_ttl(ttl: str | None) -> timedelta | None:
    """
    Parse TTL string like '30d', '1y', '2w' into timedelta.

    Supported formats:
    - Xh = X hours
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

        if unit == "h":
            return timedelta(hours=value)
        elif unit == "d":
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
    ) -> None:
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
            max_facts_per_input=settings.extraction_max_facts,
            chunk_chars=settings.extraction_chunk_chars,
        )
        self.extractor = FactExtractor(extraction_config)
        self.consolidator = MemoryConsolidator(
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
            similarity_threshold=settings.consolidation_threshold,
        )
        # TypeSafe/Jev decisions (UPG-5): off | shadow | enforce
        self.jev = JevDecider(settings)

        # Initialize entity resolution (Week 5)
        self.entity_extractor = create_entity_extractor(settings)
        self.entity_matcher = EntityMatcher(
            model=settings.extraction_model,
            api_key=settings.openai_api_key,
            min_confidence=settings.entity_matching_threshold,
        )
        # ING-12: entity resolution for one user is serialized so concurrent
        # stores cannot create duplicate entities or lose alias updates.
        self._entity_locks: dict[str, asyncio.Lock] = {}

        # Initialize advanced retrieval (Week 6)
        # Hybrid search with FTS5 BM25 + vector fusion
        self.hybrid_searcher = HybridSearcher(
            HybridSearchConfig(
                alpha=settings.hybrid_alpha,  # Research default: 0.4
            )
        )

        # Graph-aware retrieval using entity relationships
        self.graph_retriever = GraphRetriever(
            db=db,
            max_depth=settings.graph_max_depth,
            max_entities=settings.graph_max_entities,
            max_memories=settings.graph_max_memories,
        )

        # Context window optimization for LLM output
        self.context_optimizer = ContextOptimizer(
            max_tokens=settings.context_max_tokens,
            include_metadata=settings.context_include_metadata,
        )

        # Multi-signal relevance ranking
        self.relevance_ranker = RelevanceRanker(
            RankingConfig(
                semantic_weight=settings.ranking_semantic_weight,
                recency_weight=settings.ranking_recency_weight,
                entity_weight=settings.ranking_entity_weight,
                keyword_weight=settings.ranking_keyword_weight,
                recency_decay_days=settings.ranking_recency_decay_days,
            )
        )

        # CrossEncoder reranking (optional, gracefully degrades)
        self.reranker = CrossEncoderReranker(
            model_name=settings.rerank_model,
            enabled=settings.enable_reranking,
        )

    # -----------------------------------------------------------------------
    # Store
    # -----------------------------------------------------------------------

    # Unicode-aware word matcher: \w covers Latin, CJK, Cyrillic, Arabic, etc.
    # (NOT [a-z0-9], which silently matches nothing for non-Latin scripts and
    # would make verification a no-op there).
    _WORD_RE = re.compile(r"\w+", re.UNICODE)

    @staticmethod
    def _fact_source_overlap(fact: str, source_text: str) -> float:
        """Content-word overlap between a derived fact and its source text.

        Cheap, advisory lexical check used to flag likely extraction
        drift/hallucination: a fact whose content words mostly don't appear in
        the source probably wasn't derived from it. Numbers always count —
        invented figures are the most dangerous hallucination.

        This is a heuristic, not ground truth: aggressive extraction that
        renames pronouns or heavily paraphrases can score low on a faithful
        fact. It only sets an advisory `verified` flag; the fact is stored
        regardless. Numbers are comma-normalized so "3,000" and "3000" match.
        """

        def norm(text: str) -> str:
            # Casefold (stronger than lower for non-ASCII) and strip digit
            # group separators so 3,000 == 3000.
            return re.sub(r"(?<=\d),(?=\d)", "", text.casefold())

        def tokens(text: str) -> list[str]:
            words = [w for w in MemoryService._WORD_RE.findall(norm(text)) if len(w) >= 3 or w.isdigit()]
            # Non-space-segmented scripts (CJK, etc.) collapse to one giant
            # token that never matches by equality, making word overlap useless.
            # Fall back to character bigrams so verification still discriminates.
            if len(words) <= 1 and len(text.strip()) >= 4:
                s = re.sub(r"\s+", "", norm(text))
                return [s[i : i + 2] for i in range(len(s) - 1)]
            return words

        fact_tokens = tokens(fact)
        if not fact_tokens:
            return 1.0
        source_tokens = set(tokens(source_text))
        hits = sum(1 for w in fact_tokens if w in source_tokens)
        return hits / len(fact_tokens)

    @staticmethod
    def _content_checksum(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def _store_source_record(
        self,
        request: StoreRequest,
        now: datetime,
        expires_at: datetime | None,
        source: str,
        trust_score: float,
        checksum: str | None,
    ) -> str:
        """Persist the verbatim original content as an immutable source record.

        Source records are evidence, not derived knowledge: they are stored in
        SQLite only (no vector, no FTS), so they never pollute recall with
        near-duplicates of their own facts, and they are never consolidation
        candidates — the original text is never LLM-merged or rewritten.
        Derived facts point back via metadata.source_id (the receipt).

        A retried store of the same content (same user, project and checksum)
        reuses the existing source row instead of duplicating it (ING-18).
        """
        assert request.user_id is not None
        checksum = checksum or self._content_checksum(request.content)
        existing_id = await self.db.find_source_record(request.user_id, request.project_id, checksum)
        if existing_id:
            log.info("source_record_reused", memory_id=existing_id)
            return existing_id

        record = Memory(
            user_id=request.user_id,
            project_id=request.project_id,
            content=request.content,
            memory_type="source",
            extracted_facts=[],
            entities=[],
            metadata={**(request.metadata or {}), "record_kind": "source"},
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        await self.db.save_memory_metadata(
            memory_id=record.id,
            user_id=record.user_id,
            project_id=record.project_id,
            content=record.content,
            extracted_facts=record.extracted_facts,
            metadata=record.metadata,
            created_at=record.created_at,
            expires_at=record.expires_at,
            source=source,
            trust_score=trust_score,
            checksum=checksum,
            visibility=request.visibility,
            space_id=request.space_id,
            team_id=request.team_id,
            memory_type="source",
            scope=request.scope,
        )
        log.info("source_record_stored", memory_id=record.id, chars=len(record.content))
        return record.id

    @staticmethod
    def _resolve_expiry(
        expires_at: datetime | None,
        ttl: str | None,
        now: datetime,
        default_ttl_days: int | None,
    ) -> datetime | None:
        """Explicit expires_at > ttl > server default."""
        if expires_at:
            return expires_at
        if ttl:
            delta = parse_ttl(ttl)
            if delta:
                return now + delta
        elif default_ttl_days:
            return now + timedelta(days=default_ttl_days)
        return None

    @staticmethod
    def _apply_metadata_type(request: StoreRequest) -> None:
        """Honour ``metadata.type`` as the memory type when none was given (ING-25)."""
        if request.memory_type is not None:
            return
        meta_type = (request.metadata or {}).get("type")
        if isinstance(meta_type, str) and meta_type.strip().lower() in _USER_MEMORY_TYPES:
            request.memory_type = meta_type.strip().lower()  # type: ignore[assignment]

    def _entities_status(self, stored_any: bool) -> str:
        if not stored_any:
            return "none"
        return "pending" if self.settings.enable_entity_resolution else "disabled"

    async def store(
        self,
        request: StoreRequest,
        source: str = "user_input",
        trust_score: float = 1.0,
        checksum: str | None = None,
        skip_extraction: bool = False,
    ) -> StoreResponse:
        """
        Store a new memory: extract facts, ground them, decide consolidation, persist.

        Steps:
        1. Extract atomic facts from content (LLM) — skipped if skip_extraction=True
        2. Grounding: every derived fact is checked against the source text;
           unsupported facts are dropped (or flagged, per ``grounding_action``)
        3. Consolidation *decision* per fact against filtered candidates:
           ADD / NOOP (duplicate) / SUPERSEDE(target). Existing memories are
           never rewritten or deleted — superseded ones are marked, not removed.
        4. Store the fact exactly as extracted in Qdrant (vector) + SQLite + FTS

        Args:
            request: StoreRequest with content, user_id, etc.
            source: Content provenance (user_input, agent_generated, external_api)
            trust_score: Security trust score (0.0-1.0)
            checksum: SHA-256 hash for integrity verification
            skip_extraction: If True, store content as one atomic memory with no
                extraction and no consolidation. Embeddings are still generated.
        """
        log.info(
            "storing_memory",
            user_id=request.user_id,
            project_id=request.project_id,
            content_length=len(request.content),
            trust_score=trust_score,
        )

        now = utcnow()
        expires_at = self._resolve_expiry(request.expires_at, request.ttl, now, self.settings.default_ttl_days)
        self._apply_metadata_type(request)

        # ── Lossless memory: async fast path ─────────────────────────────
        # Persist the verbatim source immediately and enrich in background.
        if self.settings.async_enrichment and self.settings.enable_source_records and not skip_extraction:
            source_id = await self._store_source_record(
                request=request,
                now=now,
                expires_at=expires_at,
                source=source,
                trust_score=trust_score,
                checksum=checksum,
            )

            async def _bg_enrichment() -> None:
                try:
                    await self._extract_and_store_facts(
                        request=request,
                        now=now,
                        expires_at=expires_at,
                        source=source,
                        trust_score=trust_score,
                        checksum=checksum,
                        skip_extraction=False,
                        source_id=source_id,
                    )
                    log.info("bg_enrichment_done", source_id=source_id)
                except Exception as e:
                    metrics.incr("enrichment_failures_total")
                    log.error("bg_enrichment_failed", source_id=source_id, error=str(e))

            spawn(_bg_enrichment(), "store_enrichment")
            return StoreResponse(
                id=source_id,
                extracted_facts=[],
                entities=[],
                status="pending",
                entities_status="pending" if self.settings.enable_entity_resolution else "disabled",
                expires_at=expires_at,
                source_id=source_id,
                enrichment="pending",
            )

        # ── Synchronous path (default) ────────────────────────────────────
        outcome = await self._extract_and_store_facts(
            request=request,
            now=now,
            expires_at=expires_at,
            source=source,
            trust_score=trust_score,
            checksum=checksum,
            skip_extraction=skip_extraction,
        )
        response = outcome.to_response(expires_at=expires_at, entities_status=self._entities_status(bool(outcome.stored)))
        log.info(
            "memory_stored",
            memory_id=response.id,
            status=response.status,
            facts_extracted=len(outcome.extracted),
            facts_stored=len(outcome.stored),
            facts_dropped=len(outcome.dropped),
            source_id=outcome.source_id,
        )
        return response

    async def _extract_and_store_facts(
        self,
        request: StoreRequest,
        now: datetime,
        expires_at: datetime | None,
        source: str,
        trust_score: float,
        checksum: str | None,
        skip_extraction: bool,
        source_id: str | None = None,
    ) -> "_StoreOutcome":
        """Extract facts from content and run each through the fact pipeline.

        Lossless-memory behaviour: when extraction derives facts (rather than
        passing raw content through), the verbatim original is preserved as an
        immutable source record (unless the async fast path already made one)
        and every derived fact carries a metadata.source_id receipt.

        Grounding: each derived fact must be supported by the source text or it
        is dropped/flagged. If *every* derived fact is dropped the verbatim
        content is stored as the single fact instead, so nothing the caller
        sent is silently lost.
        """
        # The API layer overrides request.user_id with the authenticated user_id.
        assert request.user_id is not None, "user_id must be set by the API layer before store"
        content = request.content.strip()

        extraction_method = "skipped"
        if skip_extraction:
            extracted_facts = [content]
        else:
            extraction = await self.extractor.extract_detailed(request.content, reference_date=now)
            extraction_method = extraction.method
            extracted_facts = extraction.facts or [content]
            if extraction.method == "fallback":
                metrics.incr("extraction_fallbacks_total")
            log.debug("facts_extracted", count=len(extracted_facts), method=extraction_method)

        derived = extracted_facts != [content]
        if source_id is None and derived and self.settings.enable_source_records and not skip_extraction:
            source_id = await self._store_source_record(
                request=request,
                now=now,
                expires_at=expires_at,
                source=source,
                trust_score=trust_score,
                checksum=checksum,
            )

        outcome = _StoreOutcome(extracted=list(extracted_facts), source_id=source_id, extraction=extraction_method)
        sibling_ids: set[str] = set()

        async def run(fact: str, grounding_source: str | None) -> None:
            fact_metadata = dict(request.metadata or {})
            if source_id:
                fact_metadata["source_id"] = source_id
            if extraction_method in ("fallback", "disabled"):
                # Sentence-split fallback: mark for reprocessing (REL-7).
                fact_metadata["extraction"] = extraction_method
            result = await self._store_single_fact(
                fact=fact,
                user_id=request.user_id or "",
                project_id=request.project_id,
                metadata=fact_metadata,
                expires_at=expires_at,
                now=now,
                source=source,
                trust_score=trust_score,
                checksum=checksum,
                visibility=request.visibility,
                space_id=request.space_id,
                team_id=request.team_id,
                memory_type=request.memory_type,
                scope=request.scope,
                supersedes=request.supersedes,
                contradicts=request.contradicts,
                # Atomic stores (skip_extraction) must not be merged/deduped.
                skip_consolidation=skip_extraction,
                grounding_source=grounding_source,
                exclude_ids=sibling_ids,
            )
            outcome.add(result)
            if result.memory_id:
                # ING-7: facts from the same store never consolidate each other.
                sibling_ids.add(result.memory_id)

        for fact in extracted_facts:
            await run(fact, request.content if derived else None)

        if derived and not outcome.stored and not outcome.decided and outcome.dropped:
            # Every derived fact failed grounding. Keep the caller's content
            # verbatim (trivially grounded) rather than storing nothing.
            log.warning("all_facts_ungrounded_storing_verbatim", dropped=len(outcome.dropped))
            outcome.extraction = "grounding_fallback"
            await run(content, None)

        return outcome

    async def store_fact(
        self,
        fact: str,
        user_id: str,
        project_id: str,
        metadata: dict[str, Any],
        source: str,
        trust_score: float = 1.0,
        grounding_source: str | None = None,
        consolidate: bool = True,
        exclude_ids: set[str] | None = None,
        expires_at: datetime | None = None,
    ) -> "_FactResult":
        """Run one already-extracted fact through grounding, consolidation and storage.

        Public entry point for pipelines that do their own extraction
        (conversation ingest), so they share the exact same decision and
        persistence path as store() instead of re-implementing it.
        """
        return await self._store_single_fact(
            fact=fact,
            user_id=user_id,
            project_id=project_id,
            metadata=metadata,
            expires_at=expires_at,
            now=utcnow(),
            source=source,
            trust_score=trust_score,
            skip_consolidation=not consolidate,
            grounding_source=grounding_source,
            exclude_ids=exclude_ids,
        )

    async def bulk_import(
        self,
        items: list[StoreRequest],
        user_id: str,
        project_id: str = "default",
        embeddings: list[list[float]] | None = None,
    ) -> dict[str, Any]:
        """
        Fast bulk import for pre-structured data.

        Optimized path that:
        1. Uses pre-computed embeddings OR batch embeds in one OpenAI call
        2. Bulk upserts to Qdrant
        3. Bulk inserts to SQLite

        Skips: fact extraction, consolidation, conflict detection.
        Use for importing known-good structured data.

        Args:
            items: List of StoreRequest objects
            user_id: User ID for all items
            project_id: Project ID for all items
            embeddings: Optional pre-computed embedding vectors (one per item).
                       When provided, skips OpenAI calls entirely.

        Returns:
            Dict with stored count and any errors
        """
        import uuid

        from remembra.models.memory import Memory

        if not items:
            return {"stored": 0, "errors": []}

        now = utcnow()
        errors = []

        # Use pre-computed embeddings or generate them
        if embeddings and len(embeddings) == len(items):
            log.info("bulk_using_precomputed_embeddings", count=len(embeddings))
            computed_embeddings = embeddings
        else:
            # Extract all contents for batch embedding
            contents = [item.content.strip() for item in items]

            # Batch embed all at once (single OpenAI call!)
            try:
                computed_embeddings = await self.embeddings.embed_batch(contents)
            except Exception as e:
                log.error("bulk_embed_failed", error=str(e), count=len(contents))
                return {"stored": 0, "errors": [f"Embedding failed: {str(e)}"]}

        # Build Memory objects
        memories = []
        memory_dicts = []

        for item, embedding in zip(items, computed_embeddings, strict=False):
            memory_id = str(uuid.uuid4())
            expires_at = self._resolve_expiry(item.expires_at, item.ttl, now, self.settings.default_ttl_days)

            memory = Memory(
                id=memory_id,
                user_id=user_id,
                project_id=project_id,
                content=item.content.strip(),
                embedding=embedding,
                extracted_facts=[item.content.strip()],
                entities=[],
                metadata=item.metadata or {},
                created_at=now,
                expires_at=expires_at,
            )
            memories.append(memory)

            memory_dicts.append(
                {
                    "id": memory_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "content": item.content.strip(),
                    "extracted_facts": [item.content.strip()],
                    "metadata": item.metadata or {},
                    "created_at": now,
                    "expires_at": expires_at,
                    "source": "bulk_import",
                    "trust_score": 1.0,
                    "checksum": None,
                    "visibility": item.visibility or "personal",
                    "space_id": item.space_id,
                    "team_id": item.team_id,
                }
            )

        # Bulk upsert to Qdrant
        try:
            qdrant_count = await self.qdrant.upsert_batch(memories)
        except Exception as e:
            log.error("bulk_qdrant_failed", error=str(e))
            errors.append(f"Qdrant bulk insert failed: {str(e)}")
            return {"stored": 0, "errors": errors}

        # Bulk insert to SQLite
        db_count = 0
        try:
            db_count = await self.db.save_memories_bulk(memory_dicts)
        except Exception as e:
            log.error("bulk_db_failed", error=str(e))
            errors.append(f"Database bulk insert failed: {str(e)}")
            # Note: Qdrant already has the data, might be inconsistent

        log.info(
            "bulk_import_complete",
            user_id=user_id,
            project_id=project_id,
            total=len(items),
            qdrant_stored=qdrant_count,
            db_stored=db_count,
        )

        return {
            "stored": min(qdrant_count, db_count) if not errors else 0,
            "qdrant_count": qdrant_count,
            "db_count": db_count,
            "errors": errors,
        }

    # -- Consolidation candidates ---------------------------------------------

    async def _consolidation_candidates(
        self,
        embedding: list[float],
        user_id: str,
        project_id: str,
        visibility: str,
        space_id: str | None,
        team_id: str | None,
        now: datetime,
        exclude_ids: set[str],
    ) -> tuple[list[ExistingMemory], dict[str, dict[str, Any]]]:
        """Existing memories a new fact may be a duplicate of or supersede.

        Candidates must (ING-4/6/7): belong to the same user and project, share
        the fact's visibility bucket (visibility + space + team), be live (not
        expired, not superseded), not be source records, and not be siblings
        stored earlier in the same call. The SQLite row is the source of truth
        for these fields; vector hits without a row are ignored.
        """
        similar = await self.qdrant.search(
            query_vector=embedding,
            user_id=user_id,
            project_id=project_id,
            limit=self.settings.consolidation_candidate_limit * 2,
            score_threshold=self.settings.consolidation_threshold,
        )
        if not similar:
            return [], {}
        rows = await self.db.get_memories_by_ids([str(mid) for mid, _, _ in similar])
        now_iso = now.isoformat()
        candidates: list[ExistingMemory] = []
        kept_rows: dict[str, dict[str, Any]] = {}
        for mid, score, _payload in similar:
            mid = str(mid)
            row = rows.get(mid)
            if row is None or mid in exclude_ids:
                continue
            if row.get("user_id") != user_id or row.get("project_id") != project_id:
                continue
            if row.get("superseded_by") or row.get("memory_type") == "source":
                continue
            expires = row.get("expires_at")
            if expires and str(expires) <= now_iso:
                continue
            if (row.get("visibility") or "personal") != (visibility or "personal"):
                continue
            if row.get("space_id") != space_id or row.get("team_id") != team_id:
                continue
            candidates.append(ExistingMemory(id=mid, content=row.get("content") or "", score=float(score)))
            kept_rows[mid] = row
            if len(candidates) >= self.settings.consolidation_candidate_limit:
                break
        return candidates, kept_rows

    @staticmethod
    def _outlives(existing_row: dict[str, Any], new_expires_at: datetime | None) -> bool:
        """True if the existing memory lives at least as long as the new fact would.

        A NOOP must never replace a permanent fact with a copy that expires
        (ING-5: keep the stricter lifetime).
        """
        existing_exp = existing_row.get("expires_at")
        if not existing_exp:
            return True
        if new_expires_at is None:
            return False
        return str(existing_exp) >= new_expires_at.isoformat()

    def _apply_guardrails(
        self,
        result: ConsolidationResult,
        candidates: list[ExistingMemory],
        rows: dict[str, dict[str, Any]],
        new_expires_at: datetime | None,
        gate_confidence: bool,
    ) -> ConsolidationResult:
        """Deterministic safety rules applied to every decider's output."""
        result = validate_decision(result, {c.id for c in candidates})
        if result.action == ConsolidationAction.SUPERSEDE and result.target_id:
            row = rows.get(result.target_id, {})
            if row.get("pinned"):
                return ConsolidationResult(
                    action=ConsolidationAction.ADD,
                    target_id=None,
                    reason=f"target {result.target_id} is pinned; not superseded automatically",
                    confidence=result.confidence,
                    decided_by=result.decided_by,
                )
            if gate_confidence and (result.confidence is None or result.confidence < self.settings.supersede_min_confidence):
                return ConsolidationResult(
                    action=ConsolidationAction.ADD,
                    target_id=None,
                    reason=f"possible conflict with {result.target_id}; confidence below gate",
                    confidence=result.confidence,
                    decided_by=result.decided_by,
                )
        if result.action == ConsolidationAction.NOOP and result.target_id:
            if not self._outlives(rows.get(result.target_id, {}), new_expires_at):
                return ConsolidationResult(
                    action=ConsolidationAction.ADD,
                    target_id=None,
                    reason=f"duplicate of {result.target_id} but that memory expires sooner",
                    confidence=result.confidence,
                    decided_by=result.decided_by,
                )
        return result

    @staticmethod
    def _exact_duplicate(fact: str, candidates: list[ExistingMemory]) -> ExistingMemory | None:
        key = " ".join(fact.casefold().split())
        for c in candidates:
            if " ".join(c.content.casefold().split()) == key:
                return c
        return None

    async def _log_decision(self, **kwargs: Any) -> None:
        try:
            await self.db.log_decision(**kwargs)
        except Exception as e:  # noqa: BLE001 — logging must never break a store
            log.warning("decision_log_write_failed", error=str(e))

    async def _jev_shadow(
        self,
        fact: str,
        user_id: str,
        project_id: str,
        memory_id: str | None,
        grounding_source: str | None,
        candidates: list[ExistingMemory],
        rows: dict[str, dict[str, Any]],
        heuristic_grounded: bool | None,
        llm_result: ConsolidationResult | None,
    ) -> None:
        """Background: ask Jev the same questions and log it next to the LLM path."""
        assessment = await self.jev.assess_fact(fact, grounding_source, [(c.id, c.content) for c in candidates])
        if assessment is None:
            return
        if assessment.grounding is not None and heuristic_grounded is not None:
            jev_grounded = assessment.grounding >= self.settings.typesafe_grounding_threshold
            await self._log_decision(
                decision_type="grounding",
                mode="shadow",
                user_id=user_id,
                project_id=project_id,
                memory_id=memory_id,
                subject=fact,
                llm_decision="grounded" if heuristic_grounded else "ungrounded",
                jev_decision="grounded" if jev_grounded else "ungrounded",
                jev_probs={"grounding": assessment.grounding},
                agreed=jev_grounded == heuristic_grounded,
                latency_ms=assessment.latency_ms,
            )
        if llm_result is not None and assessment.relations:
            pinned = {cid for cid, row in rows.items() if row.get("pinned")}
            jd = jev_decide(
                assessment,
                pinned,
                self.settings.typesafe_supersede_threshold,
                self.settings.typesafe_duplicate_threshold,
            )
            llm_label = _decision_label(llm_result.action.value, llm_result.target_id)
            jev_label = _decision_label(jd.action, jd.target_id)
            await self._log_decision(
                decision_type="consolidation",
                mode="shadow",
                user_id=user_id,
                project_id=project_id,
                memory_id=memory_id,
                subject=fact,
                llm_decision=llm_label,
                jev_decision=jev_label,
                jev_probs=assessment.probs_json(),
                agreed=llm_label == jev_label,
                latency_ms=assessment.latency_ms,
            )

    # -- Per-fact pipeline ---------------------------------------------------

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
        visibility: str = "personal",
        space_id: str | None = None,
        team_id: str | None = None,
        memory_type: str | None = None,
        scope: str | None = None,
        supersedes: str | None = None,
        contradicts: str | None = None,
        skip_consolidation: bool = False,
        grounding_source: str | None = None,
        exclude_ids: set[str] | None = None,
    ) -> "_FactResult":
        """Ground, decide and persist ONE fact. The stored text is always ``fact``.

        ``grounding_source`` is the text the fact was derived from; when given,
        the fact must be supported by it (heuristic always; Jev in enforce
        mode). ``skip_consolidation`` stores atomically: no candidate search,
        no dedupe, no supersession.
        """
        exclude_ids = exclude_ids or set()
        metadata = dict(metadata)

        # ── Grounding (UPG-2) ────────────────────────────────────────────
        heuristic_grounded: bool | None = None
        overlap: float | None = None
        if grounding_source is not None:
            overlap = self._fact_source_overlap(fact, grounding_source)
            heuristic_grounded = overlap >= self.settings.fact_verification_threshold
        drop_ungrounded = self.settings.grounding_action == "drop"

        if heuristic_grounded is False and drop_ungrounded and not self.jev.enforcing:
            log.warning("fact_dropped_ungrounded", overlap=round(overlap or 0.0, 3), fact_preview=fact[:80])
            if self.jev.enabled:
                spawn(
                    self._jev_shadow(fact, user_id, project_id, None, grounding_source, [], {}, False, None),
                    "jev_shadow",
                )
            return _FactResult(
                fact=fact,
                action="dropped",
                decided_by="heuristic",
                reason="not supported by source text",
                grounding_score=overlap,
            )

        # ── Candidates + decision ─────────────────────────────────────────
        embedding = await self.embeddings.embed(fact)
        candidates: list[ExistingMemory] = []
        rows: dict[str, dict[str, Any]] = {}
        jev_grounding: float | None = None
        if skip_consolidation:
            result = ConsolidationResult(ConsolidationAction.ADD, None, reason="atomic", decided_by="atomic")
        else:
            candidates, rows = await self._consolidation_candidates(
                embedding, user_id, project_id, visibility, space_id, team_id, now, exclude_ids
            )
            result, jev_grounding = await self._decide(fact, candidates, rows, grounding_source, expires_at, user_id, project_id)

        grounding_score = overlap
        grounded = heuristic_grounded
        grounding_by = "heuristic"
        if jev_grounding is not None:
            # Enforce mode: Jev's grounding verdict is authoritative.
            grounding_score = jev_grounding
            grounded = jev_grounding >= self.settings.typesafe_grounding_threshold
            grounding_by = "jev"
        if grounded is False and drop_ungrounded:
            log.warning("fact_dropped_ungrounded", by=grounding_by, fact_preview=fact[:80])
            return _FactResult(
                fact=fact,
                action="dropped",
                decided_by=grounding_by,
                reason="not supported by source text",
                grounding_score=grounding_score,
            )
        if grounded is not None:
            metadata["verified"] = bool(grounded)
            if grounding_score is not None:
                metadata["grounding_score"] = round(grounding_score, 3)

        if result.action == ConsolidationAction.NOOP:
            log.debug("fact_skipped_noop", fact=fact[:50], matched_id=result.target_id)
            if self.jev.enabled and not self.jev.enforcing:
                spawn(
                    self._jev_shadow(
                        fact,
                        user_id,
                        project_id,
                        None,
                        grounding_source,
                        candidates,
                        rows,
                        heuristic_grounded,
                        result if result.decided_by == "llm" else None,
                    ),
                    "jev_shadow",
                )
            return _FactResult(
                fact=fact,
                action="noop",
                target_id=result.target_id,
                confidence=result.confidence,
                decided_by=result.decided_by,
                reason=result.reason,
                grounding_score=grounding_score,
            )

        # ── Conflict strategy for SUPERSEDE ──────────────────────────────
        target_id = result.target_id if result.action == ConsolidationAction.SUPERSEDE else None
        target = next((c for c in candidates if c.id == target_id), None)
        strategy = self.conflict_manager.default_strategy if self.conflict_manager is not None else ConflictStrategy.UPDATE
        retire_target = target_id is not None and strategy != ConflictStrategy.FLAG
        if target_id:
            # FLAG keeps both memories active: the link is a conflict, not a supersession.
            metadata["supersedes" if retire_target else "conflicts_with"] = target_id
            metadata["consolidation_reason"] = result.reason[:300]

        memory = Memory(
            user_id=user_id,
            project_id=project_id,
            content=fact,
            extracted_facts=[fact],
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
            visibility=visibility,
            space_id=space_id,
            team_id=team_id,
            memory_type=memory_type,
            scope=scope,
            supersedes=(target_id if retire_target else None) or supersedes,
            contradicts=(target_id if target_id and not retire_target else None) or contradicts,
        )

        if retire_target and target_id:
            # Mark, never delete: the old memory stays queryable as history.
            await self.db.mark_memory_superseded(target_id, memory.id)

        if target_id and self.conflict_manager is not None:
            try:
                await self.conflict_manager.record(
                    MemoryConflict(
                        user_id=user_id,
                        project_id=project_id,
                        new_fact=fact,
                        existing_memory_id=target_id,
                        existing_content=target.content if target else "",
                        similarity_score=target.score if target else 0.0,
                        reason=f"[{result.decided_by}] {result.reason}",
                        strategy_applied=strategy,
                        status=ConflictStatus.OPEN if strategy == ConflictStrategy.FLAG else ConflictStatus.RESOLVED,
                        resolved_memory_id=None if strategy == ConflictStrategy.FLAG else memory.id,
                    )
                )
            except Exception as exc:
                log.warning("conflict_recording_failed", error=str(exc))

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

        if self.settings.enable_entity_resolution:
            spawn(self._bg_entities(memory.id, fact, user_id, project_id), "entity_resolution")

        if self.jev.enabled and not self.jev.enforcing and not skip_consolidation:
            spawn(
                self._jev_shadow(
                    fact,
                    user_id,
                    project_id,
                    memory.id,
                    grounding_source,
                    candidates,
                    rows,
                    heuristic_grounded,
                    # Only model decisions are comparable; rule/atomic ones would inflate agreement.
                    result if result.decided_by == "llm" else None,
                ),
                "jev_shadow",
            )

        action = "supersede" if retire_target else "add"
        return _FactResult(
            fact=fact,
            memory_id=memory.id,
            action=action,
            target_id=target_id,
            confidence=result.confidence,
            decided_by=result.decided_by,
            reason=result.reason,
            grounding_score=grounding_score,
        )

    async def _decide(
        self,
        fact: str,
        candidates: list[ExistingMemory],
        rows: dict[str, dict[str, Any]],
        grounding_source: str | None,
        expires_at: datetime | None,
        user_id: str,
        project_id: str,
    ) -> tuple[ConsolidationResult, float | None]:
        """Pick ADD / NOOP / SUPERSEDE for a fact; guard rails always applied.

        Returns (decision, jev_grounding). jev_grounding is only set in enforce
        mode when Jev answered, and then overrides the heuristic grounding.
        """
        exact = self._exact_duplicate(fact, candidates)
        if exact is not None and self._outlives(rows.get(exact.id, {}), expires_at):
            # Byte-for-byte duplicate (e.g. a retried store): no model needed.
            dup = ConsolidationResult(
                ConsolidationAction.NOOP, exact.id, reason="exact duplicate", confidence=1.0, decided_by="rule"
            )
            return dup, None

        if self.jev.enforcing and (candidates or grounding_source is not None):
            assessment = await self.jev.assess_fact(fact, grounding_source, [(c.id, c.content) for c in candidates])
            if assessment is not None:
                pinned = {cid for cid, row in rows.items() if row.get("pinned")}
                jd = jev_decide(
                    assessment,
                    pinned,
                    self.settings.typesafe_supersede_threshold,
                    self.settings.typesafe_duplicate_threshold,
                )
                result = ConsolidationResult(
                    action=ConsolidationAction(jd.action),
                    target_id=jd.target_id,
                    reason=jd.reason,
                    confidence=round(jd.confidence, 4),
                    decided_by="jev" if candidates else "rule",
                )
                result = self._apply_guardrails(result, candidates, rows, expires_at, gate_confidence=False)
                if candidates:
                    await self._log_decision(
                        decision_type="consolidation",
                        mode="enforce",
                        user_id=user_id,
                        project_id=project_id,
                        memory_id=None,
                        subject=fact,
                        llm_decision=None,
                        jev_decision=_decision_label(result.action.value, result.target_id),
                        jev_probs=assessment.probs_json(),
                        agreed=None,
                        latency_ms=assessment.latency_ms,
                    )
                return result, assessment.grounding
            log.warning("jev_enforce_fallback_to_llm", fact_preview=fact[:60])

        if not candidates:
            add = ConsolidationResult(ConsolidationAction.ADD, None, reason="No similar existing memories", decided_by="rule")
            return add, None
        result = await self.consolidator.consolidate(fact, candidates)
        return self._apply_guardrails(result, candidates, rows, expires_at, gate_confidence=True), None

    # -- Entities -------------------------------------------------------------

    def _entity_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._entity_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._entity_locks[user_id] = lock
        return lock

    async def _bg_entities(self, memory_id: str, content: str, user_id: str, project_id: str) -> None:
        try:
            await self._process_entities_for_memory(
                memory_id=memory_id,
                content=content,
                user_id=user_id,
                project_id=project_id,
            )
            log.info("bg_entity_extraction_done", memory_id=memory_id)
        except Exception as e:
            metrics.incr("entity_processing_failures_total")
            log.warning("entity_extraction_failed", error=str(e), memory_id=memory_id)

    async def _process_entities_for_memory(
        self,
        memory_id: str,
        content: str,
        user_id: str,
        project_id: str,
    ) -> list[EntityRef]:
        """
        Extract entities from content and link them to the memory.

        Runs under a per-user lock (ING-12) so concurrent stores cannot create
        duplicate entities. Matcher ids are only accepted if they are among
        the candidates offered; canonical-name suggestions and relationship
        validity windows are persisted (ING-22). Candidate loads are bounded
        and cached per type for the whole memory (ING-23).
        """
        try:
            async with self._entity_lock(user_id):
                extraction = await self.entity_extractor.extract(content)
                if not extraction.entities:
                    return []

                entity_refs: list[EntityRef] = []
                entity_id_map: dict[str, str] = {}
                by_type: dict[str, list[ExistingEntity]] = {}

                for extracted in extraction.entities:
                    etype = extracted.type.lower()
                    if etype not in by_type:
                        by_type[etype] = await self._get_existing_entities(user_id, project_id, extracted.type)
                    existing = by_type[etype]
                    valid_ids = {e.id for e in existing}

                    match_result = await self.entity_matcher.match(extracted, existing)
                    entity_id: str | None = None
                    if match_result.match and match_result.matched_entity_id in valid_ids:
                        entity_id = match_result.matched_entity_id
                    deterministic = match_result.match and match_result.confidence >= 0.95

                    if self.jev.enabled and existing and not deterministic:
                        entity_id = await self._jev_entity_check(extracted, existing, entity_id, user_id, project_id, memory_id)

                    if entity_id:
                        aliases = list(match_result.suggested_aliases)
                        matched = next((e for e in existing if e.id == entity_id), None)
                        if matched and extracted.name.casefold() != matched.name.casefold():
                            aliases.append(extracted.name)
                        if aliases:
                            await self._add_entity_aliases(entity_id, aliases)
                        canonical = matched.name if matched else extracted.name
                    else:
                        suggestion = match_result.new_entity
                        canonical = (suggestion.name if suggestion and suggestion.name else extracted.name).strip()
                        aliases = [*extracted.aliases, *(suggestion.aliases if suggestion else [])]
                        if canonical.casefold() != extracted.name.casefold():
                            aliases.append(extracted.name)
                        aliases = list(dict.fromkeys(a for a in aliases if isinstance(a, str) and a.strip() and a != canonical))
                        entity = Entity(
                            canonical_name=canonical,
                            type=etype,
                            aliases=aliases,
                            attributes={"description": (suggestion.description if suggestion else "") or extracted.description},
                            confidence=1.0,
                        )
                        await self.db.save_entity(entity, user_id, project_id)
                        entity_id = entity.id
                        # Later mentions in this same memory can match it.
                        existing.append(
                            ExistingEntity(
                                id=entity.id,
                                name=canonical,
                                type=etype,
                                description=str(entity.attributes.get("description", "")),
                                aliases=aliases,
                            )
                        )

                    entity_id_map[extracted.name] = entity_id
                    entity_refs.append(EntityRef(id=entity_id, canonical_name=canonical, type=etype, confidence=1.0))

                for rel in extraction.relationships:
                    subject_id = entity_id_map.get(rel.subject)
                    object_id = entity_id_map.get(rel.object)
                    if subject_id and object_id:
                        valid_from = _parse_date(rel.valid_from)
                        relationship = Relationship(
                            from_entity_id=subject_id,
                            to_entity_id=object_id,
                            type=rel.predicate.lower(),
                            properties={},
                            confidence=1.0,
                            source_memory_id=memory_id,
                            valid_to=_parse_date(rel.valid_to),
                            **({"valid_from": valid_from} if valid_from else {}),
                        )
                        try:
                            await self.db.save_relationship(relationship)
                        except Exception as e:
                            log.warning("relationship_save_failed", error=str(e))

                for eid in dict.fromkeys(entity_id_map.values()):
                    await self.db.link_memory_to_entity(memory_id, eid)

                log.info("entities_processed", memory_id=memory_id, entities_linked=len(set(entity_id_map.values())))
                return entity_refs

        except Exception as e:
            metrics.incr("entity_processing_failures_total")
            log.warning("entity_processing_error", error=str(e), memory_id=memory_id)
            return []

    async def _jev_entity_check(
        self,
        extracted: Any,
        existing: list[ExistingEntity],
        llm_choice: str | None,
        user_id: str,
        project_id: str,
        memory_id: str,
    ) -> str | None:
        """Jev coreference for one mention. Shadow: log only. Enforce: Jev picks."""
        ranked = rank_candidates(extracted, existing, limit=5)
        result = await self.jev.entity_coreference(
            {"name": extracted.name, "type": extracted.type, "description": extracted.description},
            [(e.id, {"name": e.name, "type": e.type, "description": e.description, "aliases": e.aliases}) for e in ranked],
        )
        if result is None:
            return llm_choice
        probs, latency = result
        best_id, best_p = max(probs.items(), key=lambda kv: kv[1])
        jev_choice = best_id if best_p >= self.settings.typesafe_entity_match_threshold else None
        await self._log_decision(
            decision_type="entity_coref",
            mode=self.jev.mode,
            user_id=user_id,
            project_id=project_id,
            memory_id=memory_id,
            subject=extracted.name,
            llm_decision=llm_choice or "new",
            jev_decision=jev_choice or "new",
            jev_probs=probs,
            agreed=(llm_choice or "new") == (jev_choice or "new"),
            latency_ms=latency,
        )
        return jev_choice if self.jev.enforcing else llm_choice

    async def _get_existing_entities(
        self,
        user_id: str,
        project_id: str,
        entity_type: str,
    ) -> list[ExistingEntity]:
        """Existing entities for matching (bounded to the most recently updated)."""
        entities = await self.db.get_entities_by_type(user_id, project_id, entity_type, limit=_ENTITY_CANDIDATE_LIMIT)
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
            new_aliases = list(dict.fromkeys([*entity.aliases, *aliases]))
            if new_aliases != entity.aliases:
                await self.db.update_entity_aliases(entity_id, new_aliases)

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
        # The API layer overrides request.user_id with the authenticated user_id
        # before calling recall (memories.py: body.user_id = current_user.user_id),
        # so it is always set here despite the model default of None.
        assert request.user_id is not None, "user_id must be set by the API layer before recall"

        # Resolve feature flags
        use_hybrid = self.settings.enable_hybrid_search
        use_rerank = self.settings.enable_reranking
        # slim mode caps context at 800 tokens
        slim_mode = getattr(request, "slim", False)
        max_tokens = 800 if slim_mode else (request.max_tokens or self.settings.context_max_tokens)

        # ----- Filter-only recall (no semantic query) -----
        if not request.query:
            return await self._recall_by_filters(request, max_tokens=max_tokens, slim_mode=slim_mode)

        log.info(
            "recalling_memories_v2",
            user_id=request.user_id,
            project_id=request.project_id,
            cross_project=request.project_id is None,
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

        # Graph retrieval requires a concrete project_id. When the caller
        # requested cross-project recall (project_id=None) we skip this
        # branch; semantic + FTS already span all projects in that case.
        if self.settings.enable_graph_retrieval and request.project_id is not None:
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
                        hybrid_results.append(
                            {
                                "id": result.id,
                                "content": result.content,
                                "semantic_score": result.semantic_score,
                                "keyword_score": result.keyword_score,
                                "relevance": result.combined_score,
                                "created_at": payload_map.get(result.id, {}).get("created_at"),
                                "matched_keywords": [],  # BM25 fallback doesn't track individual terms
                                "payload": result.payload or payload_map.get(result.id, {}),
                            }
                        )
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
                        hybrid_results.append(
                            {
                                "id": mid,
                                "content": payload.get("content", ""),
                                "semantic_score": sem,
                                "keyword_score": kw,
                                "relevance": combined,
                                "created_at": payload.get("created_at"),
                                "payload": payload,
                            }
                        )

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
                    hybrid_results.append(
                        {
                            "id": mem_id,
                            "content": mem_data.get("content", ""),
                            "relevance": 0.5,  # Default for graph-only
                            "semantic_score": 0.4,
                            "keyword_score": 0.0,
                            "created_at": mem_data.get("created_at"),
                            "payload": mem_data,
                        }
                    )

        # Step 4b: Metadata filter (Issue 3)
        # AND-combined exact-match over decrypted payload["metadata"].
        # Applied before ranking so downstream stages see the filtered set.
        requested_filters = getattr(request, "filters", None) or {}
        if requested_filters:
            before_count = len(hybrid_results)

            def _matches(r: dict[str, Any]) -> bool:
                meta = (r.get("payload") or {}).get("metadata") or {}
                return metadata_filters_match(meta, requested_filters)

            hybrid_results = [r for r in hybrid_results if _matches(r)]
            log.info(
                "recall_metadata_filtered",
                filters=requested_filters,
                before=before_count,
                after=len(hybrid_results),
            )

        # Supersession filter: drop memories retired by a newer belief so stale
        # facts ("I use Stripe" after switching to Paddle) never surface. One
        # indexed query over the candidate ids; opt back in via include_superseded
        # to query belief history. The marker is the source-of-truth column, so
        # this is robust even if a Qdrant payload is stale.
        if not getattr(request, "include_superseded", False) and hybrid_results:
            candidate_ids = [r["id"] for r in hybrid_results]
            active_ids = await self.db.filter_active_memory_ids(candidate_ids)
            if len(active_ids) != len(candidate_ids):
                before_sup = len(hybrid_results)
                hybrid_results = [r for r in hybrid_results if r["id"] in active_ids]
                log.info(
                    "recall_superseded_filtered",
                    before=before_sup,
                    after=len(hybrid_results),
                )

        # Exclude filter: remove specific memory IDs
        exclude_ids = set(getattr(request, "exclude", None) or [])
        if exclude_ids:
            hybrid_results = [r for r in hybrid_results if r["id"] not in exclude_ids]

        # Scope filter: restrict to memories whose scope starts with the requested prefix
        requested_scope = getattr(request, "scope", None)
        if requested_scope:

            def _scope_matches(r: dict[str, Any]) -> bool:
                mem_scope = (r.get("payload") or {}).get("scope") or r.get("scope")
                if not mem_scope:
                    return False
                matches: bool = mem_scope == requested_scope or mem_scope.startswith(requested_scope + ":")
                return matches

            hybrid_results = [r for r in hybrid_results if _scope_matches(r)]

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
                        **(r.payload or {}),
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
        # Uses retrieval_mode to adjust ranking weights (debug/operational/strategic)
        ranked = self.relevance_ranker.rank(
            memories=hybrid_results,
            query=request.query,
            query_entities=matched_entities,
            retrieval_mode=getattr(request, "retrieval_mode", "balanced"),
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
        final_ranked = ranked[: request.limit]

        # Step 8a: Divergence Detection (v0.13)
        # Compare top result by semantic vs top result by recency
        divergence_detected = False
        divergence_details = None
        staleness_threshold_days = self.settings.ranking_recency_decay_days

        if len(ranked) >= 2:
            # Find top by semantic score
            semantic_sorted = sorted(ranked, key=lambda r: r.semantic_score, reverse=True)
            semantic_top = semantic_sorted[0]

            # Find top by recency score
            recency_sorted = sorted(ranked, key=lambda r: r.recency_score, reverse=True)
            recency_top = recency_sorted[0]

            # Check for divergence: different memories AND both have meaningful scores
            if semantic_top.id != recency_top.id and semantic_top.semantic_score > 0.5 and recency_top.recency_score > 0.5:
                # Calculate divergence score (how much they disagree)
                divergence_score = abs(semantic_top.semantic_score - recency_top.semantic_score)
                divergence_score += abs(semantic_top.recency_score - recency_top.recency_score)
                divergence_score = min(1.0, divergence_score / 2.0)

                if divergence_score > 0.3:  # Threshold for flagging
                    divergence_detected = True
                    divergence_details = DivergenceDetail(
                        semantic_top_id=semantic_top.id,
                        recency_top_id=recency_top.id,
                        semantic_content=semantic_top.content[:200],
                        recency_content=recency_top.content[:200],
                        divergence_score=divergence_score,
                        recommendation=(
                            "The most semantically relevant memory differs from the most recent. "
                            "Consider reviewing both - they may represent evolving information."
                        ),
                    )
                    log.info(
                        "divergence_detected",
                        semantic_top_id=semantic_top.id,
                        recency_top_id=recency_top.id,
                        divergence_score=divergence_score,
                    )

        # Step 8b: Build memory results with freshness scores
        now = utcnow()
        memories: list[RecallResult] = []
        for r in final_ranked:
            created = r.created_at or now
            age_days = (now - created).days

            # Freshness score: 1.0 = today, decays to 0 over staleness_threshold_days
            freshness = max(0.0, 1.0 - (age_days / (staleness_threshold_days * 2)))

            # why_relevant: synthesise a brief explanation from available signals
            why_parts = []
            if r.semantic_score > 0.7:
                why_parts.append("high semantic match")
            elif r.semantic_score > 0.5:
                why_parts.append("semantic match")
            if r.recency_score > 0.8:
                why_parts.append("very recent")
            elif r.recency_score > 0.5:
                why_parts.append("recent")
            if age_days > staleness_threshold_days:
                why_parts.append("may be stale")
            why_relevant = "; ".join(why_parts) if why_parts else "matched query"

            # Extract memory_type, scope, and metadata from payload if available
            payload = getattr(r, "payload", {}) or {}
            mem_type = payload.get("memory_type")
            mem_scope = payload.get("scope")
            # Metadata may arrive as a JSON string from DB-backed fetch paths
            # (e.g. graph retrieval) — coerce so it never 500s recall.
            mem_metadata = _coerce_metadata(payload.get("metadata"))

            memories.append(
                RecallResult(
                    id=r.id,
                    relevance=r.final_score,
                    content=r.content,
                    created_at=created,
                    freshness_score=round(freshness, 3),
                    age_days=age_days,
                    staleness_warning=age_days > staleness_threshold_days,
                    semantic_score=round(r.semantic_score, 3),
                    recency_score=round(r.recency_score, 3),
                    why_relevant=why_relevant,
                    memory_type=mem_type,
                    scope=mem_scope,
                    metadata=mem_metadata,
                )
            )
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
            divergence_detected=divergence_detected,
        )

        return RecallResponse(
            context=optimized.context,
            memories=memories,
            entities=all_entities,
            context_budget_used=optimized.total_tokens,
            divergence_detected=divergence_detected,
            divergence_details=divergence_details,
        )

    # -----------------------------------------------------------------------
    # Filter-only Recall (no semantic query)
    # -----------------------------------------------------------------------

    async def _recall_by_filters(
        self,
        request: RecallRequest,
        *,
        max_tokens: int,
        slim_mode: bool,
    ) -> RecallResponse:
        """Retrieve memories by metadata filters only, sorted by created_at DESC.

        Used when the caller omits ``query`` but supplies ``filters``.
        Skips embedding, semantic search, graph retrieval, and reranking —
        returns a chronological list of matching memories.
        """
        import json as _json

        if not request.user_id:
            raise ValueError("RecallRequest.user_id must be set for filter-only recall.")

        log.info(
            "recall_by_filters_only",
            user_id=request.user_id,
            project_id=request.project_id,
            filters=request.filters,
            limit=request.limit,
        )

        def _parse_created(value: Any) -> datetime:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    return utcnow()
            return utcnow()

        filters = request.filters or {}
        exclude_ids = set(getattr(request, "exclude", None) or [])
        requested_scope = getattr(request, "scope", None)
        as_of = getattr(request, "as_of", None)

        # Scan through SQLite in pages. The previous implementation only
        # checked the most recent (limit * 3) rows, which could miss older
        # matches and made "filter-only recall" unreliable for long-lived
        # trading journals and rule libraries.
        batch_size = max(200, request.limit * 10)
        max_scan = 5000
        offset = 0
        scanned = 0

        matched: list[dict[str, Any]] = []
        while len(matched) < request.limit and scanned < max_scan:
            rows = await self.db.list_memories(
                user_id=request.user_id,
                project_id=request.project_id,
                limit=batch_size,
                offset=offset,
            )
            if not rows:
                break

            for row in rows:
                scanned += 1
                if scanned > max_scan:
                    break

                memory_id = row.get("id")
                if memory_id and memory_id in exclude_ids:
                    continue

                created_dt = _parse_created(row.get("created_at", ""))
                if as_of and created_dt > as_of:
                    continue

                raw_meta = row.get("metadata")
                if isinstance(raw_meta, str):
                    try:
                        meta = _json.loads(raw_meta)
                    except (ValueError, TypeError):
                        meta = {}
                elif isinstance(raw_meta, dict):
                    meta = raw_meta
                else:
                    meta = {}

                if requested_scope:
                    mem_scope = meta.get("scope")
                    if not mem_scope:
                        continue
                    if mem_scope != requested_scope and not str(mem_scope).startswith(str(requested_scope) + ":"):
                        continue

                if filters and not metadata_filters_match(meta, filters):
                    continue

                matched.append(
                    {
                        **row,
                        "_parsed_metadata": meta,
                        "_created_dt": created_dt,
                    }
                )
                if len(matched) >= request.limit:
                    break

            offset += batch_size

        # Build RecallResult objects
        memories: list[RecallResult] = []
        memories_for_context: list[dict[str, Any]] = []
        for row in matched:
            created_dt = row.get("_created_dt") or _parse_created(row.get("created_at", ""))

            meta = row.get("_parsed_metadata", {})
            content = row.get("content", "")

            memories.append(
                RecallResult(
                    id=row["id"],
                    relevance=1.0,  # filter-only = exact match
                    content=content,
                    created_at=created_dt,
                    freshness_score=1.0,
                    age_days=0,
                    staleness_warning=False,
                    semantic_score=0.0,
                    recency_score=1.0,
                    why_relevant="matched metadata filters",
                    memory_type=meta.get("memory_type"),
                    scope=meta.get("scope"),
                    metadata=meta,
                )
            )
            date_str = created_dt.strftime("%Y-%m-%d") if hasattr(created_dt, "strftime") else str(created_dt)[:10]
            memories_for_context.append(
                {
                    "id": row["id"],
                    "content": f"[{date_str}] {content}",
                    "relevance": 1.0,
                    "created_at": None,  # date embedded above
                }
            )

        # Apply token budgeting consistent with semantic recall.
        context_optimizer = ContextOptimizer(
            max_tokens=max_tokens,
            include_metadata=False,  # we embed [YYYY-MM-DD] ourselves
        )
        optimized = context_optimizer.optimize(
            memories=memories_for_context,
            sort_by_relevance=False,  # preserve created_at DESC ordering
        )

        return RecallResponse(
            context=optimized.context,
            memories=memories,
            entities=[],
            context_budget_used=optimized.total_tokens,
            divergence_detected=False,
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
            return await self.recall(
                RecallRequest(
                    query=query,
                    user_id=agent_id,
                    project_id=project_id,
                    limit=limit,
                    threshold=threshold,
                    max_tokens=max_tokens,
                )
            )

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
        own_result = await self.recall(
            RecallRequest(
                query=query,
                user_id=agent_id,
                project_id=project_id,
                limit=limit,
                threshold=threshold,
                max_tokens=max_tokens,
            )
        )

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
                space_results.append(
                    RecallResult(
                        id=mem_id,
                        content=mem_data.get("content", ""),
                        relevance=sim,
                        created_at=(datetime.fromisoformat(mem_data["created_at"]) if mem_data.get("created_at") else utcnow()),
                    )
                )
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
        # Note: Ownership already verified by API endpoint, but we still need the data
        existing = await self.db.get_memory(memory_id)
        if not existing:
            # Fallback: memory might only exist in Qdrant (legacy data)
            existing = await self.qdrant.get_by_id(memory_id)
            if not existing:
                raise ValueError(f"Memory {memory_id} not found")

        project_id = existing.get("project_id", "default")

        # 2. Re-extract facts from new content
        extracted_facts = await self.extractor.extract(new_content, reference_date=utcnow())
        if not extracted_facts:
            extracted_facts = [new_content.strip()]

        # 3. Generate new embedding
        try:
            embedding = await self.embeddings.embed(new_content)
        except Exception as e:
            log.error("embedding_failed_during_update", error=str(e), memory_id=memory_id)
            raise ValueError(f"Failed to generate embedding: {e}") from e

        if not embedding:
            log.error("embedding_empty_during_update", memory_id=memory_id)
            raise ValueError("Embedding returned empty result")

        # 4. Build the full current record once, so Qdrant and SQLite agree
        # (ING-20: previously Qdrant got stale metadata, a fresh created_at
        # and no expires_at).

        merged_metadata = {**_coerce_metadata(existing.get("metadata")), **(new_metadata or {})}
        created_at = _parse_date(existing.get("created_at")) or utcnow()
        expires_at = _parse_date(existing.get("expires_at"))

        memory = Memory(
            id=memory_id,
            user_id=user_id,
            project_id=project_id,
            content=new_content,
            extracted_facts=extracted_facts,
            entities=[],
            embedding=embedding,
            metadata=merged_metadata,
            created_at=created_at,
            updated_at=utcnow(),
            expires_at=expires_at,
        )
        await self.qdrant.upsert(memory)

        # 5. Update metadata in SQLite (also refreshes the FTS row)
        await self.db.update_memory(
            memory_id=memory_id,
            content=new_content,
            extracted_facts=extracted_facts,
            metadata=merged_metadata,
        )

        # 6. Update entities (delete old links, create new ones) — background
        await self.db.delete_memory_entities(memory_id)

        entity_refs: list[EntityRef] = []
        if self.settings.enable_entity_resolution:
            spawn(self._bg_entities(memory_id, new_content, user_id, project_id), "entity_update")

        # 7. Handle conflict detection if enabled
        if self.conflict_manager:
            try:
                from remembra.extraction.conflicts import ConflictStatus, MemoryConflict

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
    # Supersede (v0.13 - Explicit Belief Updates)
    # -----------------------------------------------------------------------

    async def supersede(
        self,
        old_memory_id: str,
        user_id: str,
        new_content: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> "SupersedeResponse":
        """
        Explicitly supersede a memory with new information.

        This creates a clear audit trail of belief updates:
        1. Old memory is marked as superseded (not deleted)
        2. New memory is created with reference to old
        3. Supersession reason is recorded for audit

        Args:
            old_memory_id: ID of memory to supersede
            user_id: User ID (must own the memory)
            new_content: New content that replaces the old
            reason: Why this supersession is happening

        Returns:
            SupersedeResponse with old/new IDs and audit confirmation
        """
        log.info(
            "superseding_memory",
            old_memory_id=old_memory_id,
            user_id=user_id,
            reason=reason[:50],
        )

        # 1. Fetch and verify ownership of old memory
        old_memory = await self.db.get_memory(old_memory_id)
        if not old_memory:
            raise ValueError(f"Memory {old_memory_id} not found")
        if old_memory.get("user_id") != user_id:
            raise ValueError(f"Memory {old_memory_id} not found")  # Don't reveal it exists

        project_id = old_memory.get("project_id", "default")

        # 2. Create the new memory with supersession metadata
        store_metadata = metadata or {}
        store_metadata["supersedes"] = old_memory_id
        store_metadata["supersession_reason"] = reason

        from remembra.models.memory import StoreRequest

        store_request = StoreRequest(
            content=new_content,
            user_id=user_id,
            project_id=project_id,
            metadata=store_metadata,
            supersedes=old_memory_id,
            visibility=old_memory.get("visibility") or "personal",
            space_id=old_memory.get("space_id"),
            team_id=old_memory.get("team_id"),
        )

        # ING-13: the replacement is stored atomically — no extraction and no
        # consolidation — so it can never be deduped into, or supersede, the
        # memory it is replacing (or anything else).
        store_result = await self.store(
            store_request,
            source="supersession",
            trust_score=1.0,
            skip_extraction=True,
        )
        new_memory_id = store_result.id
        if not new_memory_id or new_memory_id == old_memory_id:
            raise ValueError("Supersession did not produce a new memory")

        # 3. Mark old memory as superseded (update metadata, don't delete)
        old_meta = old_memory.get("metadata") or {}
        if isinstance(old_meta, str):
            import json

            try:
                old_meta = json.loads(old_meta)
            except json.JSONDecodeError:
                old_meta = {}

        old_meta["superseded_by"] = new_memory_id
        old_meta["superseded_at"] = utcnow().isoformat()
        old_meta["supersession_reason"] = reason

        await self.db.update_memory(
            memory_id=old_memory_id,
            content=old_memory.get("content", ""),  # Keep original content
            extracted_facts=old_memory.get("extracted_facts", []),
            metadata=old_meta,
        )
        # Queryable supersession marker so recall excludes this stale fact by
        # default (the metadata copy above stays for history/display).
        await self.db.mark_memory_superseded(old_memory_id, new_memory_id)

        # 4. Record conflict/supersession in conflict manager if enabled
        if self.conflict_manager:
            try:
                conflict = MemoryConflict(
                    user_id=user_id,
                    project_id=project_id,
                    new_fact=new_content,
                    existing_memory_id=old_memory_id,
                    existing_content=old_memory.get("content", ""),
                    similarity_score=1.0,
                    reason=f"Explicit supersession: {reason}",
                    strategy_applied=ConflictStrategy.VERSION,
                    status=ConflictStatus.RESOLVED,
                )
                await self.conflict_manager.record(conflict)
            except Exception as e:
                log.warning("supersession_conflict_recording_failed", error=str(e))

        log.info(
            "memory_superseded",
            old_memory_id=old_memory_id,
            new_memory_id=new_memory_id,
            reason=reason,
        )

        return SupersedeResponse(
            old_memory_id=old_memory_id,
            new_memory_id=new_memory_id,
            reason=reason,
            supersession_recorded=True,
        )

    # -----------------------------------------------------------------------
    # Forget
    # -----------------------------------------------------------------------

    async def forget(
        self,
        memory_id: str | None = None,
        user_id: str | None = None,
        entity: str | None = None,
        project_id: str | None = None,
    ) -> ForgetResponse:
        """
        GDPR-compliant deletion of memories.

        Can delete by:
        - Specific memory ID
        - All memories for a user
        - All memories for a user within a project (project-scoped delete)
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

            # Delete specific memory (ownership verified; deletes are also
            # user-scoped at the storage layer — ING-2)
            await self.qdrant.delete(memory_id, user_id=user_id)
            await self.db.delete_memory_fts(memory_id)  # Clean FTS5 index
            if await self.db.delete_memory(memory_id, user_id=user_id):
                deleted_memories = 1
            log.info("forgot_memory", memory_id=memory_id, user_id=user_id)

        elif user_id and project_id:
            # Project-scoped deletion: delete all memories for user within a specific project
            deleted_memories = await self.qdrant.delete_by_project(user_id, project_id)
            await self.db.delete_project_memories(user_id, project_id)
            log.info(
                "forgot_project",
                user_id=user_id,
                project_id=project_id,
                memories=deleted_memories,
            )

        elif user_id:
            # Delete all user data
            deleted_memories = await self.qdrant.delete_by_user(user_id)
            await self.db.delete_user_memories(user_id)
            deleted_relationships = await self.db.delete_user_relationships(user_id)
            deleted_entities = await self.db.delete_user_entities(user_id)
            await self.db.delete_user_decision_logs(user_id)
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

    async def _serialize_memory_record(self, memory: dict[str, Any]) -> dict[str, Any]:
        """Normalize DB memory metadata for dashboard/API consumers."""
        metadata = memory.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}

        entities = await self.db.get_memory_entities(memory["id"])

        return {
            "id": memory["id"],
            "user_id": memory["user_id"],
            "project_id": memory.get("project_id", "default"),
            "content": memory.get("content", ""),
            "created_at": memory.get("created_at"),
            "updated_at": memory.get("updated_at"),
            "accessed_at": memory.get("last_accessed"),
            "access_count": memory.get("access_count", 0) or 0,
            # memory_type and scope are first-class table columns; read them
            # from the row, falling back to metadata only for legacy records
            # that stored them there. Without this, source records (and any
            # typed/scoped memory) surface as memory_type=null over the API.
            "memory_type": memory.get("memory_type") or metadata.get("memory_type"),
            "scope": memory.get("scope") or metadata.get("scope"),
            "entities": [entity.canonical_name for entity in entities],
            "metadata": metadata,
        }

    async def list_memories(
        self,
        user_id: str,
        project_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List memories for browsing in the dashboard."""
        rows = await self.db.list_memories(
            user_id=user_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
        return [await self._serialize_memory_record(row) for row in rows]

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        """Get a memory by ID. Returns None for unknown or malformed IDs."""
        memory = await self.db.get_memory(memory_id)
        if memory is not None:
            return await self._serialize_memory_record(memory)
        # Fallback to Qdrant. Qdrant point IDs must be a UUID or unsigned int,
        # so a malformed id can't exist there — treat it as "not found" rather
        # than letting Qdrant raise (which would surface as a 500 to the caller).
        if not _is_qdrant_point_id(memory_id):
            return None
        qdrant_result = await self.qdrant.get_by_id(memory_id)
        if qdrant_result:
            # Ensure user_id is present from Qdrant payload
            return {
                "id": qdrant_result.get("id", memory_id),
                "user_id": qdrant_result.get("user_id"),
                "project_id": qdrant_result.get("project_id", "default"),
                "content": qdrant_result.get("content", ""),
                "created_at": qdrant_result.get("created_at"),
                "updated_at": qdrant_result.get("updated_at"),
                "accessed_at": qdrant_result.get("accessed_at"),
                "access_count": qdrant_result.get("access_count", 0),
                "memory_type": None,
                "entities": [],
                "metadata": qdrant_result.get("metadata", {}),
            }
        return None

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
        now = utcnow()

        # Calculate age-based decay (exponential)
        age_days = (now - created_at).total_seconds() / 86400.0
        decay_factor = math.exp(-math.log(2) * age_days / half_life_days)

        # Access count boost (log scale to prevent runaway)
        access_factor = 1.0 + access_boost * math.log(1 + access_count) if access_count > 0 else 1.0

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
            results.append(
                RecallResult(
                    id=mem["id"],
                    content=mem["content"],
                    relevance=0.8,  # Simplified - historical queries don't rank
                    created_at=datetime.fromisoformat(mem["created_at"]),
                )
            )

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
            project_id=project_id,
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
        project_id: str | None = None,
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
            last_accessed = datetime.fromisoformat(mem["last_accessed"]) if mem.get("last_accessed") else None
            access_count = mem.get("access_count", 0)

            decay_score = self.calculate_decay_score(
                created_at=created_at,
                last_accessed=last_accessed,
                access_count=access_count,
                half_life_days=half_life,
            )

            if decay_score >= min_decay_score:
                results.append(
                    {
                        **mem,
                        "decay_score": decay_score,
                    }
                )

        # Sort by decay score (highest first)
        results.sort(key=lambda x: x["decay_score"], reverse=True)
        return results
