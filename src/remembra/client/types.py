"""SDK type definitions."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class EntityItem:
    """Entity reference returned from store/recall operations."""

    id: str
    canonical_name: str
    type: str
    confidence: float


@dataclass
class MemoryItem:
    """Memory item returned from recall operations."""

    id: str
    content: str
    relevance: float
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    memory_type: str | None = None
    scope: str | None = None
    source_id: str | None = None
    staleness_warning: bool = False
    age_days: int = 0


@dataclass
class StoreResult:
    """Result from storing a memory.

    ``duplicate_of`` is set when nothing new was stored because every
    extracted fact duplicated an existing memory (``id`` is then that memory).
    """

    id: str
    extracted_facts: list[str]
    entities: list[EntityItem]
    duplicate_of: str | None = None
    expires_at: str | None = None
    source_id: str | None = None
    enrichment: str | None = None


@dataclass
class RecallResult:
    """Result from recalling memories."""

    context: str
    memories: list[MemoryItem]
    entities: list[EntityItem]
    # "keyword_only" when the server could not embed the query (provider
    # outage) and answered from keyword + entity-graph search.
    degraded: str | None = None
    # Ranking mode the server used (inferred from the query when not given).
    retrieval_mode: str | None = None


@dataclass
class ForgetResult:
    """Result from forgetting memories."""

    deleted_memories: int
    deleted_entities: int
    deleted_relationships: int


@dataclass
class ChangelogIngestResult:
    """Result from ingesting a changelog."""

    releases_parsed: int
    memories_stored: int
    memory_ids: list[str]
    errors: list[str]


@dataclass
class ExtractedFactItem:
    """Fact extracted from conversation ingestion."""

    content: str
    confidence: float
    importance: float
    source_message_index: int
    speaker: str | None
    stored: bool
    memory_id: str | None
    action: str
    action_reason: str | None


@dataclass
class ExtractedEntityItem:
    """Entity extracted from conversation ingestion."""

    name: str
    type: str
    relationship: str | None


@dataclass
class IngestStatsItem:
    """Statistics from conversation ingestion."""

    messages_processed: int
    facts_extracted: int
    facts_stored: int
    facts_updated: int
    facts_deduped: int
    facts_skipped: int
    entities_found: int
    processing_time_ms: int


@dataclass
class ConversationIngestResult:
    """Result from ingesting a conversation."""

    status: str  # "ok" | "partial" | "error"
    session_id: str | None
    facts: list[ExtractedFactItem]
    entities: list[ExtractedEntityItem]
    stats: IngestStatsItem
