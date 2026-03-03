"""
In-Memory LRU Cache.

Caches hot retrieval paths to reduce latency.

Use cases:
- Recall results for popular queries
- Embeddings for repeated content
- Entity lookups
"""

import asyncio
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    """A cached value with metadata."""
    
    value: T
    created_at: datetime
    expires_at: datetime | None
    hits: int = 0


@dataclass
class CacheStats:
    """Cache statistics."""
    
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0
    
    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class MemoryCache(Generic[T]):
    """
    In-memory LRU cache with TTL support.
    
    Features:
    - LRU eviction when max_size reached
    - TTL expiration
    - Thread-safe with asyncio lock
    - Hit rate statistics
    
    Usage:
        cache = MemoryCache[str](max_size=1000, ttl_seconds=300)
        
        # Set with auto-generated key
        await cache.set(user_id="u1", query="hello", value="world")
        
        # Get
        result = await cache.get(user_id="u1", query="hello")
        
        # Or use convenience methods
        key = cache.make_key(user_id="u1", query="hello")
        await cache.set_by_key(key, value)
        result = await cache.get_by_key(key)
    """
    
    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: int = 300,
        name: str = "default",
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.name = name
        
        self._cache: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = asyncio.Lock()
        self.stats = CacheStats()
        
        log.info(
            "cache_initialized",
            name=name,
            max_size=max_size,
            ttl_seconds=ttl_seconds,
        )
    
    def make_key(self, **kwargs: Any) -> str:
        """
        Generate a cache key from keyword arguments.
        
        Args:
            **kwargs: Key components (e.g., user_id, query, project_id)
            
        Returns:
            Deterministic cache key string
        """
        # Sort kwargs for deterministic ordering
        sorted_items = sorted(kwargs.items())
        key_str = ":".join(f"{k}={v}" for k, v in sorted_items)
        
        # Hash if too long
        if len(key_str) > 100:
            key_str = hashlib.sha256(key_str.encode()).hexdigest()[:32]
        
        return f"{self.name}:{key_str}"
    
    async def get(self, **kwargs: Any) -> T | None:
        """
        Get a value from cache by key components.
        
        Args:
            **kwargs: Key components
            
        Returns:
            Cached value or None if not found/expired
        """
        key = self.make_key(**kwargs)
        return await self.get_by_key(key)
    
    async def get_by_key(self, key: str) -> T | None:
        """
        Get a value from cache by exact key.
        
        Args:
            key: Exact cache key
            
        Returns:
            Cached value or None if not found/expired
        """
        async with self._lock:
            entry = self._cache.get(key)
            
            if entry is None:
                self.stats.misses += 1
                return None
            
            # Check expiration
            if entry.expires_at and datetime.utcnow() > entry.expires_at:
                del self._cache[key]
                self.stats.expirations += 1
                self.stats.misses += 1
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.hits += 1
            self.stats.hits += 1
            
            return entry.value
    
    async def set(self, value: T, ttl: int | None = None, **kwargs: Any) -> str:
        """
        Set a value in cache.
        
        Args:
            value: Value to cache
            ttl: Optional TTL override (seconds)
            **kwargs: Key components
            
        Returns:
            Cache key used
        """
        key = self.make_key(**kwargs)
        await self.set_by_key(key, value, ttl=ttl)
        return key
    
    async def set_by_key(self, key: str, value: T, ttl: int | None = None) -> None:
        """
        Set a value in cache by exact key.
        
        Args:
            key: Exact cache key
            value: Value to cache
            ttl: Optional TTL override (seconds)
        """
        async with self._lock:
            now = datetime.utcnow()
            ttl_seconds = ttl if ttl is not None else self.ttl_seconds
            expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
            
            # Evict if at capacity
            while len(self._cache) >= self.max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                self.stats.evictions += 1
            
            # Store entry
            self._cache[key] = CacheEntry(
                value=value,
                created_at=now,
                expires_at=expires_at,
            )
    
    async def delete(self, **kwargs: Any) -> bool:
        """
        Delete a value from cache.
        
        Args:
            **kwargs: Key components
            
        Returns:
            True if deleted, False if not found
        """
        key = self.make_key(**kwargs)
        return await self.delete_by_key(key)
    
    async def delete_by_key(self, key: str) -> bool:
        """
        Delete a value from cache by exact key.
        
        Args:
            key: Exact cache key
            
        Returns:
            True if deleted, False if not found
        """
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    async def clear(self) -> int:
        """
        Clear all cached values.
        
        Returns:
            Number of entries cleared
        """
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count
    
    async def cleanup_expired(self) -> int:
        """
        Remove all expired entries.
        
        Returns:
            Number of entries removed
        """
        async with self._lock:
            now = datetime.utcnow()
            expired_keys = [
                key for key, entry in self._cache.items()
                if entry.expires_at and now > entry.expires_at
            ]
            
            for key in expired_keys:
                del self._cache[key]
                self.stats.expirations += 1
            
            return len(expired_keys)
    
    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        return {
            "name": self.name,
            "size": len(self._cache),
            "max_size": self.max_size,
            "ttl_seconds": self.ttl_seconds,
            "hits": self.stats.hits,
            "misses": self.stats.misses,
            "hit_rate": round(self.stats.hit_rate, 3),
            "evictions": self.stats.evictions,
            "expirations": self.stats.expirations,
        }


# ============================================================================
# Global Caches
# ============================================================================

_caches: dict[str, MemoryCache] = {}


def get_cache(
    name: str,
    max_size: int = 1000,
    ttl_seconds: int = 300,
) -> MemoryCache:
    """
    Get or create a named cache.
    
    Args:
        name: Cache name (e.g., "recall", "embeddings", "entities")
        max_size: Maximum cache entries
        ttl_seconds: Default TTL
        
    Returns:
        MemoryCache instance
    """
    if name not in _caches:
        _caches[name] = MemoryCache(
            name=name,
            max_size=max_size,
            ttl_seconds=ttl_seconds,
        )
    return _caches[name]


# Pre-configured caches for common use cases
recall_cache = get_cache("recall", max_size=500, ttl_seconds=300)  # 5 min
embedding_cache = get_cache("embeddings", max_size=1000, ttl_seconds=3600)  # 1 hour
entity_cache = get_cache("entities", max_size=500, ttl_seconds=600)  # 10 min
