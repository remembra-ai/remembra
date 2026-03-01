"""SDK type definitions."""

from dataclasses import dataclass
from datetime import datetime


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
