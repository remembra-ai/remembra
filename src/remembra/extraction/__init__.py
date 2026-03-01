"""
Remembra Intelligent Extraction Module

LLM-powered fact extraction and memory consolidation.
"""

from .extractor import FactExtractor, ExtractionConfig
from .consolidator import MemoryConsolidator, ConsolidationAction

__all__ = [
    "FactExtractor",
    "ExtractionConfig", 
    "MemoryConsolidator",
    "ConsolidationAction",
]
