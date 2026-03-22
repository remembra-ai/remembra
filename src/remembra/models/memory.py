"""Core domain models for memories, entities, and relationships."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def _new_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class EntityRef(BaseModel):
    """Lightweight reference to a resolved entity."""

    id: str
    canonical_name: str
    type: str  # "person" | "company" | "place" | "concept"
    confidence: float = Field(ge=0.0, le=1.0)


class Relationship(BaseModel):
    """
    Relationship between two entities with temporal validity.
    
    Temporal edges enable point-in-time queries like "What was Alice's role in January?"
    and automatic contradiction detection (newer relationships supersede older ones).
    """
    id: str = Field(default_factory=_new_id)
    from_entity_id: str
    to_entity_id: str
    type: str  # "works_at" | "knows" | "married_to" | …
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_memory_id: str | None = None
    
    # Temporal validity (bi-temporal model like Zep/Graphiti)
    valid_from: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this relationship became true in the real world"
    )
    valid_to: datetime | None = Field(
        default=None,
        description="When this relationship stopped being true (None = still valid)"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When we learned about this relationship"
    )
    superseded_by: str | None = Field(
        default=None,
        description="ID of the relationship that supersedes this one (for contradiction detection)"
    )
    
    @property
    def is_current(self) -> bool:
        """Check if this relationship is currently valid."""
        return self.valid_to is None
    
    def is_valid_at(self, point_in_time: datetime) -> bool:
        """Check if this relationship was valid at a specific point in time."""
        if point_in_time < self.valid_from:
            return False
        return not (self.valid_to is not None and point_in_time >= self.valid_to)


class Entity(BaseModel):
    id: str = Field(default_factory=_new_id)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class Memory(BaseModel):
    id: str = Field(default_factory=_new_id)
    user_id: str
    project_id: str = "default"
    content: str
    extracted_facts: list[str] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list, exclude=True)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    access_count: int = 0
    last_accessed: datetime | None = None

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v.strip()


# ---------------------------------------------------------------------------
# API request / response shapes
# ---------------------------------------------------------------------------


class StoreRequest(BaseModel):
    content: str = Field(..., max_length=50000, description="Content to memorize (max 50,000 characters)")
    project_id: str = "default"
    user_id: str | None = Field(
        default=None,
        description="Deprecated: user_id is determined from API key. This field is ignored.",
    )
    # Visibility control for team collaboration
    visibility: str = Field(
        default="personal",
        description="Memory visibility: 'personal' (only you), 'project' (project members), 'team' (all team members)",
    )
    space_id: str | None = Field(
        default=None,
        description="Space/project ID to link this memory to (required when visibility='project')",
    )
    team_id: str | None = Field(
        default=None,
        description="Team ID for team-visible memories (auto-detected from user's team if not provided)",
    )

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        if len(v) > 50000:
            raise ValueError("Content exceeds maximum length of 50,000 characters")
        # Remove null bytes and other control characters (except newlines/tabs)
        import re
        v = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', v)
        return v.strip()

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v: str) -> str:
        valid = {"personal", "project", "team"}
        if v not in valid:
            raise ValueError(f"visibility must be one of: {', '.join(valid)}")
        return v

    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl: str | None = Field(
        default=None,
        description="Optional time-to-live, e.g. '30d', '1y'.",
        examples=["30d"],
    )


class StoreResponse(BaseModel):
    id: str
    extracted_facts: list[str]
    entities: list[EntityRef]
    usage_warning: dict[str, Any] | None = Field(
        default=None,
        description="Usage warning when approaching plan limits (cloud only).",
    )


class RecallRequest(BaseModel):
    query: str = Field(..., max_length=10000, description="Search query (max 10,000 characters)")
    project_id: str = "default"
    user_id: str | None = Field(
        default=None,
        description="Deprecated: user_id is determined from API key. This field is ignored.",
    )
    limit: int = Field(default=5, ge=1, le=50)
    threshold: float = Field(default=0.40, ge=0.0, le=1.0)
    max_tokens: int | None = Field(
        default=None,
        description="Maximum tokens in context output. Overrides server default.",
        ge=100,
        le=128000,
    )
    enable_hybrid: bool | None = Field(
        default=None,
        description="Enable hybrid BM25+vector search. Uses config default if not specified.",
    )
    enable_rerank: bool | None = Field(
        default=None,
        description="Enable CrossEncoder reranking. Uses config default if not specified.",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Query memories as they existed at this point in time (historical/time-travel query).",
    )
    include_decay_score: bool = Field(
        default=False,
        description="Include decay scores in response (for debugging/analytics).",
    )


class RecallResult(BaseModel):
    id: str
    relevance: float
    content: str
    created_at: datetime


class RecallResponse(BaseModel):
    context: str
    memories: list[RecallResult]
    entities: list[EntityRef]
    usage_warning: dict[str, Any] | None = Field(
        default=None,
        description="Usage warning when approaching plan limits (cloud only).",
    )


class MemorySummary(BaseModel):
    """Browsable memory shape used by dashboard list/detail surfaces."""

    id: str
    user_id: str
    project_id: str = "default"
    content: str
    created_at: str
    updated_at: str | None = None
    accessed_at: str | None = None
    access_count: int = 0
    memory_type: str | None = None
    entities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateRequest(BaseModel):
    content: str
    metadata: dict[str, Any] | None = None


class UpdateResponse(BaseModel):
    id: str
    updated_entities: list[EntityRef]


class ForgetResponse(BaseModel):
    deleted_memories: int
    deleted_entities: int
    deleted_relationships: int


# ---------------------------------------------------------------------------
# Batch Operations Models
# ---------------------------------------------------------------------------


class BatchStoreRequest(BaseModel):
    """Request to store multiple memories in one call."""
    
    items: list[StoreRequest] = Field(..., min_length=1, max_length=100)


class BatchStoreResult(BaseModel):
    """Result for a single item in a batch store operation."""
    
    index: int
    success: bool
    response: StoreResponse | None = None
    error: str | None = None


class BatchStoreResponse(BaseModel):
    """Response from batch store operation."""
    
    results: list[BatchStoreResult]
    total: int
    succeeded: int
    failed: int


class BatchRecallRequest(BaseModel):
    """Request to recall for multiple queries in one call."""
    
    queries: list[RecallRequest] = Field(..., min_length=1, max_length=20)


class BatchRecallResponse(BaseModel):
    """Response from batch recall operation."""
    
    results: list[RecallResponse]
    total: int


# ---------------------------------------------------------------------------
# Conversation Ingestion Models (Phase 1)
# ---------------------------------------------------------------------------


class ConversationMessage(BaseModel):
    """A single message in a conversation."""

    role: str = Field(description="'user' | 'assistant' | 'system'")
    content: str
    timestamp: datetime | None = None
    name: str | None = Field(default=None, description="Speaker name for multi-user chats")
    metadata: dict[str, Any] | None = None

    @field_validator("content")
    @classmethod
    def content_length_limit(cls, v: str) -> str:
        if len(v) > 50000:
            raise ValueError("Content exceeds maximum length of 50,000 characters")
        return v


class IngestOptions(BaseModel):
    """Options for conversation ingestion."""

    extract_from: str = Field(
        default="both",
        description="'user' | 'assistant' | 'both' - which messages to extract from",
    )
    min_importance: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum importance threshold for facts",
    )
    dedupe: bool = Field(default=True, description="Enable deduplication")
    store: bool = Field(default=True, description="False = dry run mode")
    infer: bool = Field(
        default=True,
        description="True = full extraction, False = store raw messages",
    )


class ConversationIngestRequest(BaseModel):
    """Request to ingest a conversation."""

    messages: list[ConversationMessage] = Field(..., min_length=1, max_length=200)
    session_id: str | None = Field(default=None, description="Conversation session ID")
    project_id: str = "default"
    user_id: str | None = Field(
        default=None,
        description="Deprecated: user_id is determined from API key. This field is ignored.",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Context metadata (channel, timezone, etc.)",
    )
    options: IngestOptions = Field(default_factory=IngestOptions)


class ExtractedFact(BaseModel):
    """A fact extracted from conversation."""

    content: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_message_index: int
    speaker: str | None = None
    stored: bool = False
    memory_id: str | None = None
    action: str = Field(
        default="add",
        description="'add' | 'update' | 'delete' | 'noop' | 'skipped'",
    )
    action_reason: str | None = None


class ExtractedEntityResult(BaseModel):
    """An entity extracted from conversation."""

    name: str
    type: str
    relationship: str | None = None
    subtype: str | None = None


class DedupeResult(BaseModel):
    """Result of deduplication for a fact."""

    content: str
    existing_memory_id: str
    action: str = Field(description="'merged' | 'updated' | 'skipped'")


class IngestStats(BaseModel):
    """Statistics from conversation ingestion."""

    messages_processed: int = 0
    facts_extracted: int = 0
    facts_stored: int = 0
    facts_updated: int = 0
    facts_deduped: int = 0
    facts_skipped: int = 0
    entities_found: int = 0
    processing_time_ms: int = 0


class ConversationIngestResponse(BaseModel):
    """Response from conversation ingestion."""

    status: str = Field(default="ok", description="'ok' | 'partial' | 'error'")
    session_id: str | None = None
    facts: list[ExtractedFact] = Field(default_factory=list)
    entities: list[ExtractedEntityResult] = Field(default_factory=list)
    deduped: list[DedupeResult] = Field(default_factory=list)
    stats: IngestStats = Field(default_factory=IngestStats)


# ---------------------------------------------------------------------------
# Sleep-Time Consolidation Models (Phase 3)
# ---------------------------------------------------------------------------


class ConsolidationReport(BaseModel):
    """Report from sleep-time consolidation."""

    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    memories_scanned: int = 0
    duplicates_merged: int = 0
    entities_resolved: int = 0
    relationships_discovered: int = 0
    importance_rescored: int = 0
    memories_decayed: int = 0
    errors: list[str] = Field(default_factory=list)
