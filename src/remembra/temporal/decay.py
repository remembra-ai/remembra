"""
Memory decay algorithm based on the Ebbinghaus Forgetting Curve.

The forgetting curve describes how memory retention decays exponentially over time:
    R = e^(-t/S)

Where:
    R = retrievability (probability of recall, 0-1)
    t = time elapsed since last access
    S = stability (strength of memory)

Key insight: Each access reinforces the memory, increasing stability.
This mimics human spaced repetition learning.

References:
    - Ebbinghaus, H. (1885). Memory: A Contribution to Experimental Psychology
    - SuperMemo Algorithm SM-17
    - Wixted & Carpenter (2007). Forgetting Curve Formula
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass
class DecayConfig:
    """
    Configuration for memory decay algorithm.
    
    Attributes:
        base_decay_rate: How fast memories fade (higher = faster decay)
        access_stability_bonus: Stability increase per access
        importance_weight: How much importance affects retention (0-1)
        prune_threshold: Relevance below this = candidate for removal
        newness_grace_days: Days before decay fully kicks in
        min_stability: Minimum stability value (prevents instant decay)
        max_stability: Maximum stability cap
    """
    base_decay_rate: float = 0.07  # Based on Ebbinghaus curve
    access_stability_bonus: float = 0.5
    importance_weight: float = 0.6
    prune_threshold: float = 0.1
    newness_grace_days: float = 7.0
    min_stability: float = 1.0
    max_stability: float = 100.0
    
    # Advanced tuning
    recency_boost_days: float = 1.0  # Boost for very recent memories
    recency_boost_factor: float = 1.2


# Default configuration
DEFAULT_CONFIG = DecayConfig()


def calculate_stability(
    access_count: int,
    base_importance: float = 0.5,
    config: DecayConfig | None = None,
) -> float:
    """
    Calculate memory stability based on access patterns.
    
    Stability increases with each access (spaced repetition effect).
    Uses square root growth to prevent runaway stability.
    
    Args:
        access_count: Number of times memory has been accessed
        base_importance: Initial importance score (0-1)
        config: Decay configuration
        
    Returns:
        Stability value (higher = slower decay)
    """
    cfg = config or DEFAULT_CONFIG
    
    # Base stability from importance
    base = cfg.min_stability + (base_importance * 2)
    
    # Access bonus with diminishing returns (sqrt growth)
    access_bonus = cfg.access_stability_bonus * math.sqrt(max(0, access_count))
    
    stability = base + access_bonus
    
    return min(cfg.max_stability, max(cfg.min_stability, stability))


def calculate_decay_factor(
    time_elapsed: timedelta,
    stability: float,
    config: DecayConfig | None = None,
) -> float:
    """
    Calculate decay factor using exponential decay formula.
    
    Formula: e^(-t/S)
    
    Args:
        time_elapsed: Time since last access
        stability: Memory stability value
        config: Decay configuration
        
    Returns:
        Decay factor between 0 and 1 (1 = no decay, 0 = fully decayed)
    """
    cfg = config or DEFAULT_CONFIG
    
    # Convert to days
    days_elapsed = time_elapsed.total_seconds() / 86400
    
    if days_elapsed <= 0:
        return 1.0
    
    # Exponential decay: R = e^(-t/S)
    # Using decay rate to scale: R = e^(-decay_rate * t / S)
    decay = math.exp(-cfg.base_decay_rate * days_elapsed / stability)
    
    return max(0.0, min(1.0, decay))


def calculate_relevance_score(
    created_at: datetime,
    last_accessed: datetime | None,
    access_count: int = 0,
    importance_score: float = 0.5,
    config: DecayConfig | None = None,
    as_of: datetime | None = None,
) -> float:
    """
    Calculate current relevance score for a memory.
    
    Combines:
    - Time-based decay (Ebbinghaus curve)
    - Access reinforcement (spaced repetition)
    - Importance weighting
    - Newness grace period
    
    Args:
        created_at: When memory was created
        last_accessed: When memory was last accessed (None = never)
        access_count: Number of times accessed
        importance_score: Importance rating (0-1)
        config: Decay configuration
        as_of: Calculate score as of this time (default: now)
        
    Returns:
        Relevance score between 0 and 1
        
    Example:
        >>> score = calculate_relevance_score(
        ...     created_at=datetime(2026, 2, 1),
        ...     last_accessed=datetime(2026, 2, 25),
        ...     access_count=5,
        ...     importance_score=0.8
        ... )
        >>> print(f"Relevance: {score:.2f}")
        Relevance: 0.72
    """
    cfg = config or DEFAULT_CONFIG
    now = as_of or datetime.utcnow()
    
    # Use last_accessed if available, otherwise use created_at
    reference_time = last_accessed or created_at
    time_elapsed = now - reference_time
    
    # Calculate stability based on access patterns
    stability = calculate_stability(access_count, importance_score, cfg)
    
    # Calculate base decay
    decay_factor = calculate_decay_factor(time_elapsed, stability, cfg)
    
    # Apply importance weighting
    importance_adjusted = (
        decay_factor * (1 - cfg.importance_weight) +
        importance_score * cfg.importance_weight
    ) * decay_factor
    
    # Newness grace period boost
    days_since_creation = (now - created_at).total_seconds() / 86400
    if days_since_creation < cfg.newness_grace_days:
        grace_factor = 1 - (days_since_creation / cfg.newness_grace_days)
        newness_boost = 1.0 + (0.3 * grace_factor)
        importance_adjusted *= newness_boost
    
    # Recency boost for very fresh accesses
    if last_accessed:
        days_since_access = (now - last_accessed).total_seconds() / 86400
        if days_since_access < cfg.recency_boost_days:
            importance_adjusted *= cfg.recency_boost_factor
    
    return max(0.0, min(1.0, importance_adjusted))


def should_prune(
    created_at: datetime,
    last_accessed: datetime | None,
    access_count: int = 0,
    importance_score: float = 0.5,
    expires_at: datetime | None = None,
    config: DecayConfig | None = None,
    as_of: datetime | None = None,
) -> bool:
    """
    Determine if a memory should be pruned.
    
    A memory should be pruned if:
    1. It has a TTL and has expired, OR
    2. Its relevance score falls below the threshold
    
    Args:
        created_at: When memory was created
        last_accessed: When last accessed
        access_count: Times accessed
        importance_score: Importance (0-1)
        expires_at: Hard expiration time (TTL)
        config: Decay configuration
        as_of: Check as of this time
        
    Returns:
        True if memory should be pruned
    """
    cfg = config or DEFAULT_CONFIG
    now = as_of or datetime.utcnow()
    
    # Check hard TTL expiration first
    if expires_at and now > expires_at:
        return True
    
    # Check decay threshold
    relevance = calculate_relevance_score(
        created_at=created_at,
        last_accessed=last_accessed,
        access_count=access_count,
        importance_score=importance_score,
        config=cfg,
        as_of=as_of,
    )
    
    return relevance < cfg.prune_threshold


def calculate_memory_decay_info(memory_data: dict[str, Any], config: DecayConfig | None = None) -> dict[str, Any]:
    """
    Calculate decay information for a memory dict.
    
    Convenience function that takes a memory dict (from database)
    and returns decay analytics.
    
    Args:
        memory_data: Dict with created_at, last_accessed, access_count, etc.
        config: Decay configuration
        
    Returns:
        Dict with relevance_score, days_since_access, stability, should_prune
    """
    cfg = config or DEFAULT_CONFIG
    now = datetime.utcnow()
    
    # Parse dates
    created_at = memory_data.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    
    last_accessed = memory_data.get("last_accessed")
    if isinstance(last_accessed, str):
        last_accessed = datetime.fromisoformat(last_accessed)
    
    expires_at = memory_data.get("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    
    access_count = memory_data.get("access_count", 0) or 0
    importance = memory_data.get("importance_score", 0.5) or 0.5
    
    # Calculate metrics
    relevance = calculate_relevance_score(
        created_at=created_at,
        last_accessed=last_accessed,
        access_count=access_count,
        importance_score=importance,
        config=cfg,
    )
    
    stability = calculate_stability(access_count, importance, cfg)
    
    reference_time = last_accessed or created_at
    days_since_access = (now - reference_time).total_seconds() / 86400
    
    prune = should_prune(
        created_at=created_at,
        last_accessed=last_accessed,
        access_count=access_count,
        importance_score=importance,
        expires_at=expires_at,
        config=cfg,
    )
    
    # Time until expiration (if TTL set)
    ttl_remaining = None
    if expires_at:
        remaining = expires_at - now
        ttl_remaining = max(0, remaining.total_seconds())
    
    return {
        "relevance_score": round(relevance, 4),
        "stability": round(stability, 2),
        "days_since_access": round(days_since_access, 2),
        "access_count": access_count,
        "should_prune": prune,
        "ttl_remaining_seconds": ttl_remaining,
        "is_expired": expires_at and now > expires_at if expires_at else False,
    }


def rank_by_relevance(
    memories: list[dict[str, Any]],
    config: DecayConfig | None = None,
) -> list[dict[str, Any]]:
    """
    Rank memories by relevance score (highest first).
    
    Adds 'decay_score' to each memory dict.
    
    Args:
        memories: List of memory dicts
        config: Decay configuration
        
    Returns:
        Sorted list with decay_score added
    """
    for memory in memories:
        decay_info = calculate_memory_decay_info(memory, config)
        memory["decay_score"] = decay_info["relevance_score"]
        memory["decay_info"] = decay_info
    
    return sorted(memories, key=lambda m: m["decay_score"], reverse=True)
