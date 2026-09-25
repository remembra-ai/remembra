"""Memory service - core business logic for store, recall, update, forget."""

import asyncio
import contextvars
import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from remembra.config import Settings
from remembra.core import ai_spend
from remembra.core import metrics as core_metrics
from remembra.core.enrichment_queue import get_enrichment_queue
from remembra.core.llm_guard import llm_fallback_scope, mark_llm_fallback
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
    RELAY_MEMORY_TYPES,
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
from remembra.retrieval.intent import IntentRouter, ModeDecision
from remembra.retrieval.ranking import RankingConfig, RelevanceRanker
from remembra.retrieval.reranker import CrossEncoderReranker
from remembra.storage.database import Database
from remembra.storage.embeddings import EmbeddingProviderError, EmbeddingService
from remembra.storage.pending_embeddings import PendingEmbeddingQueue
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
    # True when the fact is stored in SQLite + FTS but its vector is queued
    # for the re-embedding worker (provider down / store budget exhausted).
    pending: bool = False


class _StoreRun:
    """Per-store() bookkeeping: time budget and what this request wrote.

    Carried in a ContextVar so every nested step (source record, each fact)
    can check the budget and record its writes without threading extra
    parameters through the pipeline. On a hard failure store() rolls back
    exactly what this run wrote (REL-4: no orphan source rows).
    """

    def __init__(self, budget_seconds: float) -> None:
        self.deadline = time.monotonic() + budget_seconds
        self.created_ids: list[str] = []  # rows (and maybe vectors) written, in order
        self.superseded: list[tuple[str, str]] = []  # (old_id, new_id)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0


_STORE_RUN: contextvars.ContextVar[_StoreRun | None] = contextvars.ContextVar("remembra_store_run", default=None)


class _BudgetExceeded(Exception):
    """The overall store time budget ran out before an optional step."""


async def _within_budget(coro: Any) -> Any:
    """Await ``coro`` bounded by the current store budget (REL-17).

    Raises _BudgetExceeded when the budget is already spent or runs out.
    Outside a store() (no run), the coroutine is awaited unbounded.
    """
    run = _STORE_RUN.get()
    if run is None:
        return await coro
    remaining = run.remaining()
    if remaining <= 0:
        coro.close()
        raise _BudgetExceeded()
    try:
        return await asyncio.wait_for(coro, timeout=remaining)
    except TimeoutError as e:
        raise _BudgetExceeded() from e


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
        enrichment: str | None = None
        if stored:
            status, rid, dup = "stored", stored[0].memory_id or "", None
            if any(r.pending for r in stored):
                # REL-4: saved (SQLite + keyword search) but the vector is queued.
                status, enrichment = "pending", "pending"
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
            enrichment=enrichment,
        )


def _naive_utc(value: datetime | None) -> datetime | None:
    """Normalise an aware datetime to naive UTC (storage convention); pass naive through."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _row_time(row: dict[str, Any]) -> datetime:
    return _parse_date(row.get("valid_from")) or _parse_date(row.get("created_at")) or datetime.min


@dataclass
class _RecallCtx:
    """What a recall is allowed to return (see MemoryService._row_eligible)."""

    user_id: str
    project_id: str | None
    active_at: datetime
    as_of: datetime | None
    include_superseded: bool
    scope: str | None
    filters: dict[str, str]
    exclude: set[str]


@dataclass
class _Candidate:
    """A recall candidate, hydrated from its SQLite row."""

    id: str
    row: dict[str, Any]
    semantic: float | None = None  # cosine similarity to the query
    keyword_raw: float = 0.0  # BM25 (higher = better)
    sources: set[str] = field(default_factory=set)


_WORDS_RE = re.compile(r"\w+", re.UNICODE)


def _drop_near_duplicates(ranked: list[Any], threshold: float) -> list[Any]:
    """RET-15: drop lower-ranked results whose words almost entirely overlap a
    higher-ranked one (Jaccard >= threshold). Order is otherwise preserved."""
    if threshold >= 1.0 or len(ranked) < 2:
        return ranked
    kept: list[Any] = []
    kept_words: list[set[str]] = []
    for item in ranked:
        words = set(_WORDS_RE.findall((item.content or "").casefold()))
        duplicate = False
        for other in kept_words:
            union = words | other
            if union and len(words & other) / len(union) >= threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
            kept_words.append(words)
    return kept


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
        pending_queue: PendingEmbeddingQueue | None = None,
    ) -> None:
        self.settings = settings
        self.qdrant = qdrant
        self.db = db
        self.embeddings = embeddings
        # REL-4: facts whose vector could not be written are queued here; the
        # app's PendingEmbeddingWorker drains the same table.
        self.pending_queue = pending_queue or PendingEmbeddingQueue(db)
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

        # CrossEncoder reranking (optional, gracefully degrades; runs in a thread)
        self.reranker = CrossEncoderReranker(
            model_name=settings.rerank_model,
            enabled=settings.enable_reranking,
            min_logit=settings.rerank_min_logit,
        )

        # Query intent -> ranking mode (RET-1): rules first, Jev optional.
        self.intent_router = IntentRouter(settings, self.jev, log_decision=self._log_decision, spawn=spawn)

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
        run = _STORE_RUN.get()
        if run is not None:
            run.created_ids.append(record.id)
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

    def _entities_status(self, stored_any: bool, atomic: bool = False) -> str:
        if not stored_any:
            return "none"
        # Atomic stores (skip_extraction, relay types, degraded writes) never run entity resolution.
        return "pending" if self.settings.enable_entity_resolution and not atomic else "disabled"

    @staticmethod
    def _enqueue_enrichment(user_id: str, coro: Any, name: str, *, droppable: bool = True) -> None:
        """Run background enrichment on the bounded per-tenant queue.

        Concurrency follows the plan of the write being served (the current
        spend job); the job's credit reservation settles after this work.
        """
        job = ai_spend.current_job()
        get_enrichment_queue().submit(
            user_id,
            coro,
            name=name,
            concurrency=job.concurrency if job is not None else None,
            droppable=droppable,
        )

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

        # REL-17: one time budget for the whole store; REL-4: everything this
        # request writes is rolled back if it fails hard part-way.
        run = _StoreRun(self.settings.store_time_budget_seconds)
        token = _STORE_RUN.set(run)
        try:
            return await self._store_with_run(request, now, expires_at, source, trust_score, checksum, skip_extraction)
        except Exception:
            await self._rollback_store(run)
            raise
        finally:
            _STORE_RUN.reset(token)

    async def _rollback_store(self, run: _StoreRun) -> None:
        """Undo the writes of a failed store: supersession marks, then rows,
        FTS entries, queued embeddings and vectors it created (newest first)."""
        if not run.created_ids and not run.superseded:
            return
        for old_id, new_id in reversed(run.superseded):
            try:
                await self.db.unmark_memory_superseded(old_id, new_id)
            except Exception as e:  # noqa: BLE001 - keep rolling back the rest
                log.error("store_rollback_unmark_failed", memory_id=old_id, error=str(e))
        for memory_id in reversed(run.created_ids):
            try:
                await self.pending_queue.mark_done(memory_id)
                await self.db.delete_memory(memory_id)
            except Exception as e:  # noqa: BLE001
                log.error("store_rollback_row_failed", memory_id=memory_id, error=str(e))
            try:
                await self.qdrant.delete(memory_id)
            except Exception as e:  # noqa: BLE001 - reconcile reports any leftover vector
                log.warning("store_rollback_vector_failed", memory_id=memory_id, error=str(e))
        metrics.incr("store_rollbacks_total")
        log.warning("store_rolled_back", rows=len(run.created_ids), supersessions=len(run.superseded))

    async def _store_with_run(
        self,
        request: StoreRequest,
        now: datetime,
        expires_at: datetime | None,
        source: str,
        trust_score: float,
        checksum: str | None,
        skip_extraction: bool,
    ) -> StoreResponse:
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
                # Own budget and journal: a failure rolls back only the facts
                # this task wrote, never the already-acknowledged source row.
                bg_run = _StoreRun(self.settings.store_time_budget_seconds)
                _STORE_RUN.set(bg_run)
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
                    await self._rollback_store(bg_run)
                    metrics.incr("enrichment_failures_total")
                    log.error("bg_enrichment_failed", source_id=source_id, error=str(e))

            # Not droppable: the verbatim source is only recallable once its facts exist.
            self._enqueue_enrichment(request.user_id or "", _bg_enrichment(), "store_enrichment", droppable=False)
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
        atomic = skip_extraction or request.memory_type in RELAY_MEMORY_TYPES
        response = outcome.to_response(
            expires_at=expires_at, entities_status=self._entities_status(bool(outcome.stored), atomic=atomic)
        )
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
        extraction_error_kind: str | None = None
        if skip_extraction:
            extracted_facts = [content]
        else:
            with llm_fallback_scope() as fallbacks:
                try:
                    extraction = await _within_budget(self.extractor.extract_detailed(request.content, reference_date=now))
                    extraction_method = extraction.method
                    extracted_facts = extraction.facts or [content]
                except _BudgetExceeded:
                    # REL-17: out of time - keep the caller's text verbatim and
                    # mark it for the reprocess job.
                    mark_llm_fallback("extraction", "store_budget_exceeded")
                    extraction_method, extracted_facts = "fallback", [content]
            if extraction_method == "fallback":
                metrics.incr("extraction_fallbacks_total")
                extraction_error_kind = fallbacks.get("extraction") or "unknown"
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
                if extraction_error_kind:
                    fact_metadata["extraction_error_kind"] = extraction_error_kind
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
        if not items:
            return {"stored": 0, "errors": []}

        now = utcnow()
        errors: list[str] = []
        contents = [item.content.strip() for item in items]

        # Use pre-computed embeddings or generate them (one batch call)
        computed: list[list[float]] | None
        pending_reason: str | None = None
        if embeddings and len(embeddings) == len(items):
            log.info("bulk_using_precomputed_embeddings", count=len(embeddings))
            computed = embeddings
        else:
            try:
                computed = await self.embeddings.embed_batch(contents)
            except EmbeddingProviderError as e:
                if not self.settings.store_pending_on_embedding_failure:
                    log.error("bulk_embed_failed", error=str(e), count=len(contents))
                    return {"stored": 0, "errors": [f"Embedding failed: {e.kind.value}"]}
                # REL-4: keep the rows (keyword-searchable), vectors come later.
                computed = None
                pending_reason = "circuit_open" if e.circuit_open else f"embedding_{e.kind.value}"
                log.warning("bulk_embed_deferred", reason=pending_reason, count=len(contents))
            except Exception as e:
                log.error("bulk_embed_failed", error=str(e), count=len(contents))
                return {"stored": 0, "errors": ["Embedding failed"]}

        memories: list[Memory] = []
        memory_dicts: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            expires_at = self._resolve_expiry(item.expires_at, item.ttl, now, self.settings.default_ttl_days)
            memory = Memory(
                id=str(uuid.uuid4()),
                user_id=user_id,
                project_id=project_id,
                content=contents[index],
                embedding=computed[index] if computed is not None else [],
                extracted_facts=[contents[index]],
                entities=[],
                metadata=item.metadata or {},
                created_at=now,
                expires_at=expires_at,
                valid_from=now,
            )
            memories.append(memory)
            memory_dicts.append(
                {
                    "id": memory.id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "content": memory.content,
                    "extracted_facts": memory.extracted_facts,
                    "metadata": memory.metadata,
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

        # ING-19 / REL-10: SQLite rows + FTS (+ queue entries when there are no
        # vectors) in ONE transaction, before any vector is written.
        async with self.db.transaction():
            db_count = await self.db.save_memories_bulk(memory_dicts)
            for memory in memories:
                await self.db.index_memory_fts(memory.id, user_id, project_id, memory.content)
            if computed is None:
                for memory in memories:
                    await self.pending_queue.enqueue(memory.id, user_id, project_id, reason=pending_reason or "embedding_failed")

        qdrant_count = 0
        pending = len(memories) if computed is None else 0
        if computed is not None:
            try:
                qdrant_count = await self.qdrant.upsert_batch(memories)
            except Exception as e:
                log.error("bulk_qdrant_failed", error=str(e))
                if not self.settings.store_pending_on_embedding_failure:
                    for memory in memories:
                        await self.db.delete_memory(memory.id)
                    return {"stored": 0, "qdrant_count": 0, "db_count": 0, "errors": ["Vector store bulk insert failed"]}
                for memory in memories:
                    await self.pending_queue.enqueue(memory.id, user_id, project_id, reason="vector_store_failed")
                pending = len(memories)

        log.info(
            "bulk_import_complete",
            user_id=user_id,
            project_id=project_id,
            total=len(items),
            qdrant_stored=qdrant_count,
            db_stored=db_count,
            pending=pending,
        )

        return {
            "stored": db_count,
            "pending": pending,
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

        # ── Embedding (REL-4: a provider failure defers it, never loses the fact)
        embedding: list[float] | None = None
        pending_reason: str | None = None
        try:
            embedding = await _within_budget(self.embeddings.embed(fact))
        except EmbeddingProviderError as e:
            if not self.settings.store_pending_on_embedding_failure:
                raise
            pending_reason = "circuit_open" if e.circuit_open else f"embedding_{e.kind.value}"
            log.warning("store_embedding_deferred", reason=pending_reason, kind=e.kind.value)
        except _BudgetExceeded:
            if not self.settings.store_pending_on_embedding_failure:
                raise TimeoutError("store time budget exceeded before the fact could be embedded") from None
            pending_reason = "store_budget_exceeded"
            log.warning("store_embedding_deferred", reason=pending_reason)

        # ── Candidates + decision ─────────────────────────────────────────
        candidates: list[ExistingMemory] = []
        rows: dict[str, dict[str, Any]] = {}
        jev_grounding: float | None = None
        if skip_consolidation:
            result = ConsolidationResult(ConsolidationAction.ADD, None, reason="atomic", decided_by="atomic")
        elif embedding is None:
            # No vector -> no candidate search. Store as new and flag it so a
            # later pass can dedupe; never guess a supersession blind.
            result = ConsolidationResult(
                ConsolidationAction.ADD,
                None,
                reason="embedding deferred; consolidation skipped",
                decided_by="fallback",
            )
            metadata["consolidation"] = "skipped_embedding_pending"
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
            memory_type=memory_type,
            scope=scope,
            extracted_facts=[fact],
            entities=[],
            embedding=embedding or [],
            metadata=metadata,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            valid_from=now,
        )

        # REL-10: SQLite row + FTS first (one transaction; the source of truth
        # and what keyword recall needs), then the vector. A deferred vector
        # is queued in the same transaction so it can never be forgotten.
        async with self.db.transaction():
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
            if self.settings.enable_hybrid_search or embedding is None:
                await self.db.index_memory_fts(
                    memory_id=memory.id,
                    user_id=memory.user_id,
                    project_id=memory.project_id,
                    content=memory.content,
                )
            if embedding is None:
                await self.pending_queue.enqueue(memory.id, user_id, project_id, reason=pending_reason or "embedding_failed")
        run = _STORE_RUN.get()
        if run is not None:
            run.created_ids.append(memory.id)

        pending = embedding is None
        if embedding is not None:
            try:
                await self.qdrant.upsert(memory)
            except Exception as e:
                if not self.settings.store_pending_on_embedding_failure:
                    raise
                # The row is safe; the worker re-embeds and upserts it later.
                await self.pending_queue.enqueue(memory.id, user_id, project_id, reason="vector_store_failed")
                pending = True
                log.warning("store_vector_deferred", memory_id=memory.id, error=str(e))

        if retire_target and target_id:
            # Mark, never delete: the old memory stays queryable as history.
            # UPG-1: this also closes the old memory's validity window.
            await self.db.mark_memory_superseded(target_id, memory.id)
            if run is not None:
                run.superseded.append((target_id, memory.id))

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

        # P0 relay cost gate: atomic stores (skip_extraction / skip_consolidation,
        # handoff / checkpoint / status, degraded writes) never run the LLM
        # entity pass — relay events must cost nothing but an embedding.
        if self.settings.enable_entity_resolution and not skip_consolidation and memory_type not in RELAY_MEMORY_TYPES:
            self._enqueue_enrichment(user_id, self._bg_entities(memory.id, fact, user_id, project_id), "entity_resolution")

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
            pending=pending,
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

        run = _STORE_RUN.get()
        if self.jev.enforcing and (candidates or grounding_source is not None) and not (run and run.expired):
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
        try:
            result = await _within_budget(self.consolidator.consolidate(fact, candidates))
        except _BudgetExceeded:
            # REL-17: out of time - add without a model decision rather than
            # fail the store or guess a supersession.
            mark_llm_fallback("consolidation", "store_budget_exceeded")
            return (
                ConsolidationResult(
                    ConsolidationAction.ADD,
                    None,
                    reason="store time budget exceeded; added without a consolidation decision",
                    decided_by="fallback",
                ),
                None,
            )
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
    # Recall (RET stream rewrite, 2026-09-25)
    # -----------------------------------------------------------------------

    def _recall_ctx(self, request: RecallRequest) -> "_RecallCtx":
        assert request.user_id is not None
        as_of = _naive_utc(request.as_of)
        return _RecallCtx(
            user_id=request.user_id,
            project_id=request.project_id,
            active_at=as_of or utcnow(),
            as_of=as_of,
            include_superseded=bool(request.include_superseded),
            scope=request.scope or None,
            filters=dict(request.filters or {}),
            exclude=set(request.exclude or []),
        )

    @staticmethod
    def _row_eligible(row: dict[str, Any], ctx: "_RecallCtx") -> bool:
        """Single source-of-truth gate for every recall path (semantic, keyword,
        graph, filter-only). Uses the SQLite row, never the Qdrant payload."""
        if row.get("user_id") != ctx.user_id:
            return False
        if ctx.project_id is not None and row.get("project_id") != ctx.project_id:
            return False
        if row.get("id") in ctx.exclude:
            return False
        meta = _coerce_metadata(row.get("metadata"))
        if row.get("memory_type") == "source" or meta.get("record_kind") == "source":
            return False
        expires = _parse_date(row.get("expires_at"))
        if expires is not None and expires <= ctx.active_at:
            return False  # RET-2: expired memories are never returned
        if ctx.as_of is not None:
            # UPG-1: what was known and valid at as_of (superseded rows included
            # when their validity window covered as_of).
            created = _parse_date(row.get("created_at"))
            if created is not None and created > ctx.as_of:
                return False
            valid_from = _parse_date(row.get("valid_from")) or created
            if valid_from is not None and valid_from > ctx.as_of:
                return False
            valid_to = _parse_date(row.get("valid_to"))
            if valid_to is None and row.get("superseded_by"):
                valid_to = _parse_date(row.get("superseded_at"))
            if valid_to is not None and valid_to <= ctx.as_of:
                return False
        elif not ctx.include_superseded and row.get("superseded_by"):
            return False
        if ctx.scope:
            mem_scope = row.get("scope") or meta.get("scope")
            if not mem_scope or not (mem_scope == ctx.scope or str(mem_scope).startswith(ctx.scope + ":")):
                return False
        # RET-18: metadata is coerced from the row's JSON text before matching.
        return not (ctx.filters and not metadata_filters_match(meta, ctx.filters))

    @staticmethod
    def _dedupe_status(candidates: dict[str, "_Candidate"]) -> None:
        """RET-2: for status memories only the newest value per (project, key) survives."""
        best: dict[tuple[str, str], _Candidate] = {}
        for cand in candidates.values():
            if cand.row.get("memory_type") != "status":
                continue
            key = _coerce_metadata(cand.row.get("metadata")).get("status_key")
            if not key:
                continue
            slot = (str(cand.row.get("project_id")), str(key))
            current = best.get(slot)
            if current is None or _row_time(cand.row) > _row_time(current.row):
                best[slot] = cand
        keep = {c.id for c in best.values()}
        for cand in list(candidates.values()):
            if cand.row.get("memory_type") == "status" and _coerce_metadata(cand.row.get("metadata")).get("status_key"):
                if cand.id not in keep:
                    del candidates[cand.id]

    def _stale_after_days(self, memory_type: str | None) -> float:
        if memory_type == "status":
            return float(self.settings.status_stale_days)
        if memory_type == "checkpoint":
            return float(self.settings.checkpoint_stale_days)
        return float(self.settings.ranking_recency_decay_days)

    async def _semantic_candidates(
        self,
        query_vector: list[float],
        ctx: "_RecallCtx",
        want: int,
        threshold: float,
    ) -> dict[str, "_Candidate"]:
        """Vector hits, over-fetched page by page until ``want`` eligible
        memories are found or the search is exhausted (RET-3). User, project,
        expiry and (plaintext) metadata filters run inside Qdrant; every hit is
        re-validated against its SQLite row."""
        out: dict[str, _Candidate] = {}
        page = max(want * 3, 20)
        cap = max(page, int(self.settings.recall_max_candidates))
        offset = 0
        while True:
            hits = await self.qdrant.search(
                query_vector=query_vector,
                user_id=ctx.user_id,
                project_id=ctx.project_id,
                limit=page,
                score_threshold=threshold,
                offset=offset,
                active_at=ctx.active_at,
                metadata_match=ctx.filters or None,
            )
            rows = await self.db.get_memories_by_ids([str(mid) for mid, _, _ in hits])
            for mid, score, _payload in hits:
                row = rows.get(str(mid))
                if row is None or not self._row_eligible(row, ctx):
                    continue
                out[str(mid)] = _Candidate(id=str(mid), row=row, semantic=float(score), sources={"semantic"})
            offset += len(hits)
            if len(out) >= want or len(hits) < page or offset >= cap:
                return out

    async def _resolve_mode(self, request: RecallRequest) -> ModeDecision:
        assert request.user_id is not None
        return await self.intent_router.resolve(request.retrieval_mode, request.query or "", request.user_id, request.project_id)

    async def recall(self, request: RecallRequest) -> RecallResponse:
        """
        Recall memories relevant to a query.

        Pipeline (every candidate is validated against its SQLite row):
        1. Ranking mode: caller's, else inferred from the query (RET-1).
        2. Semantic search, over-fetched until ``limit`` eligible memories
           are found (filters pushed into Qdrant where possible). If the query
           cannot be embedded, recall degrades to keyword + graph and says so
           (``degraded='keyword_only'``, REL-5).
        3. Keyword (FTS5/BM25) search - always, even with no semantic hits.
        4. Entity-graph search, scoped to user AND project (RET-3).
        5. Expired, superseded (unless requested / as_of) and source rows are
           dropped; status memories keep only the newest value per key.
        6. Optional CrossEncoder rerank (off the event loop), mode-weighted
           ranking with absolute similarity, feedback weight, near-duplicate
           removal, then the top ``limit`` - and the context is built from
           exactly those.
        """
        # The API layer overrides request.user_id with the authenticated user_id.
        assert request.user_id is not None, "user_id must be set by the API layer before recall"

        slim_mode = bool(request.slim)
        max_tokens = 800 if slim_mode else (request.max_tokens or self.settings.context_max_tokens)
        ctx = self._recall_ctx(request)

        # ----- Filter-only recall (no semantic query) -----
        if not request.query:
            return await self._recall_by_filters(request, max_tokens=max_tokens, slim_mode=slim_mode)

        use_hybrid = self.settings.enable_hybrid_search if request.enable_hybrid is None else bool(request.enable_hybrid)
        use_rerank = self.settings.enable_reranking if request.enable_rerank is None else bool(request.enable_rerank)
        mode = await self._resolve_mode(request)
        want = request.limit * 2  # ranking pool

        log.info(
            "recalling_memories_v3",
            user_id=request.user_id,
            project_id=request.project_id,
            cross_project=request.project_id is None,
            query_length=len(request.query),
            retrieval_mode=mode.mode,
            mode_source=mode.source,
            hybrid=use_hybrid,
            rerank=use_rerank,
            as_of=ctx.as_of.isoformat() if ctx.as_of else None,
        )

        # Step 1: embed the query (degrade to keyword-only on provider failure)
        degraded: str | None = None
        query_vector: list[float] | None = None
        try:
            query_vector = await self.embeddings.embed(request.query)
        except EmbeddingProviderError as e:
            if not self.settings.recall_keyword_fallback:
                raise
            degraded = "keyword_only"
            core_metrics.recall_degraded(degraded)
            log.warning(
                "recall_degraded_keyword_only",
                kind=e.kind.value,
                circuit_open=e.circuit_open,
                user_id=request.user_id,
            )

        candidates: dict[str, _Candidate] = {}

        # Step 2: semantic
        if query_vector is not None:
            candidates.update(await self._semantic_candidates(query_vector, ctx, want, request.threshold))

        # Step 3: keyword - always when hybrid is on, and always when degraded
        if use_hybrid or degraded:
            try:
                fts = await self.db.search_fts(
                    query=request.query,
                    user_id=request.user_id,
                    project_id=request.project_id,
                    limit=max(want * 2, 20),
                    active_at=ctx.active_at,
                    exclude_superseded=not ctx.include_superseded and ctx.as_of is None,
                )
                missing = [mid for mid, _ in fts if mid not in candidates]
                rows = await self.db.get_memories_by_ids(missing)
                for mid, bm25 in fts:
                    cand = candidates.get(mid)
                    if cand is None:
                        row = rows.get(mid)
                        if row is None or not self._row_eligible(row, ctx):
                            continue
                        cand = _Candidate(id=mid, row=row)
                        candidates[mid] = cand
                    cand.keyword_raw = float(bm25)
                    cand.sources.add("keyword")
            except Exception as e:
                log.warning("fts_search_failed", error=str(e))

        # Step 3b: recency-intent queries ("what was I just working on") also
        # consider the newest memories. Semantic/keyword search only surfaces
        # rows that share wording with the query, so a fresh handoff phrased
        # differently never entered the pool and debug-mode recency ranking had
        # nothing recent to promote (RET-1, observed live 2026-09-25).
        if mode.mode == "debug" and ctx.as_of is None and self.settings.recall_recent_pool > 0:
            try:
                recent = await self.db.list_memories(request.user_id, request.project_id, limit=self.settings.recall_recent_pool)
                missing = [r["id"] for r in recent if r["id"] not in candidates]
                rows = await self.db.get_memories_by_ids(missing)
                for mid in missing:
                    row = rows.get(mid)
                    if row is None or not self._row_eligible(row, ctx):
                        continue
                    cand = _Candidate(id=mid, row=row)
                    cand.sources.add("recent")
                    candidates[mid] = cand
            except Exception as e:
                log.warning("recent_candidates_failed", error=str(e))

        # Step 4: entity graph (scoped to user + project)
        matched_entities: list[EntityRef] = []
        related_entities: list[EntityRef] = []
        if self.settings.enable_graph_retrieval and request.project_id is not None:
            try:
                graph = await self.graph_retriever.search(
                    query=request.query, user_id=request.user_id, project_id=request.project_id
                )
                matched_entities = graph.matched_entities
                related_entities = graph.related_entities
                missing = [mid for mid in graph.memory_ids if mid not in candidates]
                rows = await self.db.get_memories_by_ids(missing)
                for mid in graph.memory_ids:
                    cand = candidates.get(mid)
                    if cand is None:
                        row = rows.get(mid)
                        if row is None or not self._row_eligible(row, ctx):
                            continue
                        cand = _Candidate(id=mid, row=row)
                        candidates[mid] = cand
                    cand.sources.add("graph")
            except Exception as e:
                log.warning("graph_retrieval_failed", error=str(e))

        self._dedupe_status(candidates)

        # Real similarity for keyword/graph-only hits (one filtered query).
        if query_vector is not None:
            unscored = [c.id for c in candidates.values() if c.semantic is None]
            if unscored:
                try:
                    scores = await self.qdrant.score_ids(query_vector, unscored, request.user_id)
                    for mid, sim in scores.items():
                        if mid in candidates:
                            candidates[mid].semantic = float(sim)
                except Exception as e:
                    log.warning("candidate_scoring_failed", error=str(e))

        if not candidates:
            log.info("recall_no_results", user_id=request.user_id, degraded=degraded)
            return RecallResponse(
                context="",
                memories=[],
                entities=[],
                degraded=degraded,
                retrieval_mode=mode.mode,
                retrieval_mode_source=mode.source,
            )

        # Step 5: optional CrossEncoder rerank, off the event loop (RET-4)
        rerank_scores: dict[str, float] = {}
        if use_rerank and self.reranker.enabled:
            docs = [
                {"id": c.id, "content": c.row.get("content") or "", "relevance": c.semantic or 0.0} for c in candidates.values()
            ]
            try:
                reranked = await self.reranker.arerank(request.query, docs)
                if any(r.raw_score is not None for r in reranked):
                    kept = {r.id for r in reranked}
                    for mid in [m for m in candidates if m not in kept]:
                        del candidates[mid]  # below the absolute logit cutoff
                    rerank_scores = {r.id: r.rerank_score for r in reranked if r.raw_score is not None}
            except Exception as e:
                log.warning("reranking_failed", error=str(e))

        ids = list(candidates)
        entity_map = await self.db.get_memory_entities_many(ids)
        feedback = await self.db.get_feedback_scores(ids, request.user_id) if self.settings.ranking_feedback_weight else {}
        max_kw = max((c.keyword_raw for c in candidates.values()), default=0.0) or 1.0

        # Step 6: mode-weighted ranking with absolute similarity
        ranked = self.relevance_ranker.rank(
            memories=[
                {
                    "id": c.id,
                    "content": c.row.get("content") or "",
                    "semantic_score": c.semantic or 0.0,
                    "keyword_score": c.keyword_raw / max_kw,
                    "created_at": c.row.get("created_at"),
                    "entities": entity_map.get(c.id, []),
                    "access_count": c.row.get("access_count") or 0,
                    "rerank_score": rerank_scores.get(c.id),
                    "feedback_score": feedback.get(c.id, 0.0),
                    "payload": c.row,
                }
                for c in candidates.values()
            ],
            query=request.query,
            query_entities=matched_entities,
            retrieval_mode=mode.mode,
            feedback_weight=self.settings.ranking_feedback_weight,
        )
        ranked = _drop_near_duplicates(ranked, self.settings.recall_dedup_similarity)
        final_ranked = ranked[: request.limit]

        divergence_detected, divergence_details = self._detect_divergence(ranked)

        now = utcnow()
        memories: list[RecallResult] = []
        for r in final_ranked:
            cand = candidates[r.id]
            memories.append(self._recall_result(cand, r, now, request.include_decay_score))

        # Context is built from exactly the returned memories (RET-8).
        optimized = ContextOptimizer(max_tokens=max_tokens, include_metadata=self.settings.context_include_metadata).optimize(
            memories=[
                {
                    "id": m.id,
                    "content": m.content,
                    "relevance": m.relevance,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in memories
            ],
            sort_by_relevance=False,
        )

        await self.db.update_access_many([m.id for m in memories])

        all_entities = list(matched_entities)
        seen_entity_ids = {e.id for e in all_entities}
        for entity in related_entities:
            if entity.id not in seen_entity_ids:
                all_entities.append(entity)
                seen_entity_ids.add(entity.id)

        log.info(
            "recall_complete_v3",
            user_id=request.user_id,
            results_count=len(memories),
            candidates=len(candidates),
            entities_count=len(all_entities),
            context_tokens=optimized.total_tokens,
            divergence_detected=divergence_detected,
            degraded=degraded,
        )

        return RecallResponse(
            context=optimized.context,
            memories=memories,
            entities=all_entities,
            context_budget_used=optimized.total_tokens,
            divergence_detected=divergence_detected,
            divergence_details=divergence_details,
            degraded=degraded,
            retrieval_mode=mode.mode,
            retrieval_mode_source=mode.source,
        )

    def _detect_divergence(self, ranked: list[Any]) -> tuple[bool, DivergenceDetail | None]:
        """Flag when the most similar and the most recent memory disagree."""
        if len(ranked) < 2:
            return False, None
        semantic_top = max(ranked, key=lambda r: r.semantic_score)
        recency_top = max(ranked, key=lambda r: r.recency_score)
        if semantic_top.id == recency_top.id or semantic_top.semantic_score <= 0.5 or recency_top.recency_score <= 0.5:
            return False, None
        score = abs(semantic_top.semantic_score - recency_top.semantic_score)
        score += abs(semantic_top.recency_score - recency_top.recency_score)
        score = min(1.0, score / 2.0)
        if score <= 0.3:
            return False, None
        log.info(
            "divergence_detected",
            semantic_top_id=semantic_top.id,
            recency_top_id=recency_top.id,
            divergence_score=score,
        )
        return True, DivergenceDetail(
            semantic_top_id=semantic_top.id,
            recency_top_id=recency_top.id,
            semantic_content=semantic_top.content[:200],
            recency_content=recency_top.content[:200],
            divergence_score=score,
            recommendation=(
                "The most semantically relevant memory differs from the most recent. "
                "Consider reviewing both - they may represent evolving information."
            ),
        )

    def _recall_result(self, cand: "_Candidate", r: Any, now: datetime, include_decay: bool) -> RecallResult:
        row = cand.row
        meta = _coerce_metadata(row.get("metadata"))
        created = _parse_date(row.get("created_at")) or now
        age_days = max(0, (now - created).days)
        mem_type = row.get("memory_type") or meta.get("memory_type")
        stale_days = self._stale_after_days(mem_type)
        freshness = max(0.0, min(1.0, 1.0 - (age_days / (stale_days * 2))))
        stale = age_days > stale_days

        why_parts: list[str] = []
        if r.semantic_score > 0.7:
            why_parts.append("high semantic match")
        elif r.semantic_score > 0.5:
            why_parts.append("semantic match")
        if "keyword" in cand.sources:
            why_parts.append("keyword match")
        if "graph" in cand.sources:
            why_parts.append("linked entity")
        if r.recency_score > 0.8:
            why_parts.append("very recent")
        elif r.recency_score > 0.5:
            why_parts.append("recent")
        if stale:
            why_parts.append("may be stale")

        decay: float | None = None
        if include_decay:
            decay = round(
                self.calculate_decay_score(
                    created_at=created,
                    last_accessed=_parse_date(row.get("last_accessed")),
                    access_count=int(row.get("access_count") or 0),
                    half_life_days=self.settings.ranking_recency_decay_days,
                ),
                4,
            )

        return RecallResult(
            id=cand.id,
            relevance=round(r.final_score, 4),
            content=row.get("content") or "",
            created_at=created,
            freshness_score=round(freshness, 3),
            age_days=age_days,
            staleness_warning=stale,
            semantic_score=round(r.semantic_score, 3),
            recency_score=round(r.recency_score, 3),
            why_relevant="; ".join(why_parts) if why_parts else "matched query",
            memory_type=mem_type,
            scope=row.get("scope") or meta.get("scope"),
            metadata=meta,
            project_id=row.get("project_id"),
            match_sources=sorted(cand.sources),
            valid_from=_parse_date(row.get("valid_from")) or created,
            valid_to=_parse_date(row.get("valid_to")),
            superseded_by=row.get("superseded_by"),
            expires_at=_parse_date(row.get("expires_at")),
            decay_score=decay,
            trust_score=float(row["trust_score"]) if row.get("trust_score") is not None else None,
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

        Used when the caller omits ``query`` but supplies ``filters``. Skips
        embedding, semantic search, graph retrieval and reranking. Applies the
        same row gate as query recall (RET-14): no source records, no
        superseded memories unless asked, as_of validity, scope from the row.
        """
        if not request.user_id:
            raise ValueError("RecallRequest.user_id must be set for filter-only recall.")

        log.info(
            "recall_by_filters_only",
            user_id=request.user_id,
            project_id=request.project_id,
            filters=request.filters,
            limit=request.limit,
        )
        ctx = self._recall_ctx(request)

        # Scan through SQLite in pages until the limit is met (bounded).
        batch_size = max(200, request.limit * 10)
        max_scan = 5000
        offset = 0
        scanned = 0

        matched: dict[str, _Candidate] = {}
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
                row = dict(row)
                row.setdefault("user_id", request.user_id)
                if not self._row_eligible(row, ctx):
                    continue
                matched[row["id"]] = _Candidate(id=row["id"], row=row, sources={"filter"})
                if len(matched) >= request.limit:
                    break
            offset += batch_size

        self._dedupe_status(matched)

        now = utcnow()
        memories: list[RecallResult] = []
        memories_for_context: list[dict[str, Any]] = []
        for cand in matched.values():
            row = cand.row
            meta = _coerce_metadata(row.get("metadata"))
            created_dt = _parse_date(row.get("created_at")) or now
            age_days = max(0, (now - created_dt).days)
            mem_type = row.get("memory_type") or meta.get("memory_type")
            stale_days = self._stale_after_days(mem_type)
            content = row.get("content", "")
            memories.append(
                RecallResult(
                    id=row["id"],
                    relevance=1.0,  # filter-only = exact match
                    content=content,
                    created_at=created_dt,
                    freshness_score=round(max(0.0, min(1.0, 1.0 - age_days / (stale_days * 2))), 3),
                    age_days=age_days,
                    staleness_warning=age_days > stale_days,
                    semantic_score=0.0,
                    recency_score=round(self.relevance_ranker._compute_recency_score(created_dt), 3),
                    why_relevant="matched metadata filters",
                    memory_type=mem_type,
                    scope=row.get("scope") or meta.get("scope"),
                    metadata=meta,
                    project_id=row.get("project_id"),
                    match_sources=["filter"],
                    valid_from=_parse_date(row.get("valid_from")) or created_dt,
                    valid_to=_parse_date(row.get("valid_to")),
                    superseded_by=row.get("superseded_by"),
                    expires_at=_parse_date(row.get("expires_at")),
                )
            )
            memories_for_context.append(
                {
                    "id": row["id"],
                    "content": f"[{created_dt.strftime('%Y-%m-%d')}] {content}",
                    "relevance": 1.0,
                    "created_at": None,  # date embedded above
                }
            )

        # Apply token budgeting consistent with semantic recall.
        optimized = ContextOptimizer(max_tokens=max_tokens, include_metadata=False).optimize(
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
        """Recall the agent's own memories plus memories shared into spaces it can read.

        Space memories are scored with their real cosine similarity (one
        filtered Qdrant query - RET-5), pass the same expired / superseded /
        source gate as normal recall, and are ranked with the same ranker so
        their scores are comparable with the agent's own results.

        Permission/ownership checks on spaces are enforced by the API/space
        layer (SEC-3), not here.
        """
        own_request = RecallRequest(
            query=query,
            user_id=agent_id,
            project_id=project_id,
            limit=limit,
            threshold=threshold,
            max_tokens=max_tokens,
        )
        own_result = await self.recall(own_request)
        if self.space_manager is None:
            return own_result

        log.info("recall_across_spaces", agent_id=agent_id, query_length=len(query))

        space_memory_ids: set[str] = set()
        for sid in await self.space_manager.get_accessible_space_ids(agent_id):
            space_memory_ids.update(await self.space_manager.get_space_memory_ids(sid, limit=500))

        seen_ids = {m.id for m in own_result.memories}
        rows = await self.db.get_memories_by_ids([mid for mid in space_memory_ids if mid not in seen_ids])
        now = utcnow()
        eligible: dict[str, dict[str, Any]] = {}
        for mid, row in rows.items():
            meta = _coerce_metadata(row.get("metadata"))
            if row.get("memory_type") == "source" or meta.get("record_kind") == "source" or row.get("superseded_by"):
                continue
            expires = _parse_date(row.get("expires_at"))
            if expires is not None and expires <= now:
                continue
            eligible[mid] = row
        if not eligible or own_result.degraded:
            return own_result

        try:
            query_vector = await self.embeddings.embed(query)
        except EmbeddingProviderError:
            return own_result
        # No user filter: shared memories belong to other users by design.
        sims = await self.qdrant.score_ids(query_vector, list(eligible))
        space_cands = {
            mid: _Candidate(id=mid, row=eligible[mid], semantic=sim, sources={"space"})
            for mid, sim in sims.items()
            if sim >= threshold and mid in eligible
        }
        mode = ModeDecision(own_result.retrieval_mode or "balanced", own_result.retrieval_mode_source or "default")
        ranked_space = self.relevance_ranker.rank(
            memories=[
                {
                    "id": c.id,
                    "content": c.row.get("content") or "",
                    "semantic_score": c.semantic or 0.0,
                    "created_at": c.row.get("created_at"),
                    "payload": c.row,
                }
                for c in space_cands.values()
            ],
            query=query,
            retrieval_mode=mode.mode,
        )
        space_results = [self._recall_result(space_cands[r.id], r, now, False) for r in ranked_space]

        all_memories = list(own_result.memories) + space_results
        all_memories.sort(key=lambda m: m.relevance, reverse=True)
        final_memories = all_memories[:limit]

        optimized = ContextOptimizer(
            max_tokens=max_tokens or self.settings.context_max_tokens,
            include_metadata=self.settings.context_include_metadata,
        ).optimize(
            memories=[
                {"id": m.id, "content": m.content, "relevance": m.relevance, "created_at": m.created_at.isoformat()}
                for m in final_memories
            ],
            sort_by_relevance=False,
        )

        log.info(
            "recall_across_spaces_done",
            agent_id=agent_id,
            own_count=len(own_result.memories),
            space_count=len(space_results),
            total=len(final_memories),
        )

        return RecallResponse(
            context=optimized.context,
            memories=final_memories,
            entities=own_result.entities or [],
            context_budget_used=optimized.total_tokens,
            retrieval_mode=own_result.retrieval_mode,
            retrieval_mode_source=own_result.retrieval_mode_source,
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
        enrich: bool = True,
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
            enrich: False when the account has no smart credits left — the
                content is stored as one fact and entities are not re-resolved.

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

        # 2. Re-extract facts from new content (skipped when enrichment is degraded)
        extracted_facts = await self.extractor.extract(new_content, reference_date=utcnow()) if enrich else []
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
        if self.settings.enable_entity_resolution and enrich:
            self._enqueue_enrichment(user_id, self._bg_entities(memory_id, new_content, user_id, project_id), "entity_update")

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
        """Recall memories as they were known and valid at ``as_of`` (UPG-1).

        Thin wrapper over :meth:`recall` with ``as_of``: memories created
        after ``as_of`` are hidden, and memories superseded since then are
        returned if their validity window covered ``as_of``.
        """
        return await self.recall(RecallRequest(query=query, user_id=user_id, project_id=project_id, limit=limit, as_of=as_of))

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
