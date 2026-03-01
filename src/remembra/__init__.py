"""
Remembra – Universal memory layer for AI applications.

Quick Start:
    from remembra import Memory
    
    memory = Memory(
        base_url="http://localhost:8787",
        user_id="user_123"
    )
    
    # Store
    memory.store("John works at Acme Corp as CTO")
    
    # Recall
    result = memory.recall("Where does John work?")
    print(result.context)  # "John works at Acme Corp as CTO."
"""

__version__ = "0.4.0"

# SDK exports (client-side)
from remembra.client.memory import Memory, MemoryError
from remembra.client.types import (
    EntityItem,
    ForgetResult,
    MemoryItem,
    RecallResult,
    StoreResult,
)

__all__ = [
    # Core
    "Memory",
    "MemoryError",
    # Types
    "StoreResult",
    "RecallResult",
    "ForgetResult",
    "MemoryItem",
    "EntityItem",
    # Metadata
    "__version__",
]
