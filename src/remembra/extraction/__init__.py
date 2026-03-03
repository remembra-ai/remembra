"""
Remembra Intelligent Extraction Module

LLM-powered fact extraction, memory consolidation, and entity resolution.
"""

from .consolidator import ConsolidationAction, ExistingMemory, MemoryConsolidator
from .entities import EntityExtractor, ExtractedEntity, ExtractedRelationship, ExtractionResult
from .extractor import ExtractionConfig, FactExtractor
from .matcher import EntityMatcher, ExistingEntity, MatchResult

__all__ = [
    # Fact extraction
    "FactExtractor",
    "ExtractionConfig", 
    # Memory consolidation
    "MemoryConsolidator",
    "ConsolidationAction",
    "ExistingMemory",
    # Entity extraction
    "EntityExtractor",
    "ExtractedEntity",
    "ExtractedRelationship",
    "ExtractionResult",
    # Entity matching
    "EntityMatcher",
    "ExistingEntity",
    "MatchResult",
]
