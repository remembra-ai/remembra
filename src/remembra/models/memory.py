"""Core domain models for memories, entities, and relationships."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from ulid import ULID


def _new_id() -> str:
    return str(ULID())


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
    id: str = Field(default_factory=_new_id)
    from_entity_id: str
    to_entity_id: str
    type: str  # "works_at" | "knows" | "married_to" | …
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_memory_id: str | None = None


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
    user_id: str
    content: str
    project_id: str = "default"

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v.strip()
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


class RecallRequest(BaseModel):
    user_id: str
    query: str
    project_id: str = "default"
    limit: int = Field(default=5, ge=1, le=50)
    threshold: float = Field(default=0.70, ge=0.0, le=1.0)


class RecallResult(BaseModel):
    id: str
    relevance: float
    content: str
    created_at: datetime


class RecallResponse(BaseModel):
    context: str
    memories: list[RecallResult]
    entities: list[EntityRef]


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
