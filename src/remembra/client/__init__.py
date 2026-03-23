"""Remembra Python SDK - Universal memory layer for AI applications."""

from remembra.client.memory import Memory, MemoryError
from remembra.client.shadow_ttl import ShadowTTLCache, parse_ttl_string
from remembra.client.temporal_parser import (
    TemporalDetection,
    TemporalGranularity,
    TemporalParser,
    detect_temporal,
    suggest_ttl,
)
from remembra.client.types import (
    EntityItem,
    ForgetResult,
    MemoryItem,
    RecallResult,
    StoreResult,
)

__all__ = [
    # Core client
    "Memory",
    "MemoryError",
    # Result types
    "RecallResult",
    "StoreResult",
    "ForgetResult",
    "MemoryItem",
    "EntityItem",
    # v0.12: Shadow TTL
    "ShadowTTLCache",
    "parse_ttl_string",
    # v0.12: Temporal parsing
    "TemporalParser",
    "TemporalDetection",
    "TemporalGranularity",
    "detect_temporal",
    "suggest_ttl",
]
