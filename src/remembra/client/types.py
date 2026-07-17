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


@dataclass
class StoreResult:
    """Result from storing a memory."""

    id: str
    extracted_facts: list[str]
    entities: list[EntityItem]


@dataclass
class RecallResult:
    """Result from recalling memories."""

    context: str
    memories: list[MemoryItem]
    entities: list[EntityItem]


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
