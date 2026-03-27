"""
Shadow TTL Cache for Client-Side Latency Optimization.

This module provides a local cache of memory expiry times to reduce
unnecessary existence checks on writes. If a memory is known to be
valid (not expired), we can skip the server round-trip for existence
verification.

Usage:
    cache = ShadowTTLCache(default_ttl_seconds=3600)

    # Register a memory with its TTL
    cache.register("mem_123", ttl_seconds=86400)

    # Check if memory is likely still valid
    if cache.is_valid("mem_123"):
        # Skip existence check - memory is cached as valid
        pass
    else:
        # Memory expired or unknown - check server
        pass
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class CacheEntry:
    """A single cache entry tracking memory expiry."""

    memory_id: str
    expires_at: float  # Unix timestamp when memory expires
    created_at: float = field(default_factory=time.time)

    def is_expired(self, current_time: float | None = None) -> bool:
        """Check if this memory has expired."""
        now = current_time or time.time()
        return now >= self.expires_at


class ShadowTTLCache:
    """
    Thread-safe local cache for memory TTL tracking.

    Reduces write latency by caching known-valid memories locally.
    When a memory is stored with a TTL, we track its expiry locally.
    On subsequent operations, we can skip existence checks for memories
    that haven't expired yet.

    The cache uses a simple dictionary with periodic cleanup to avoid
    unbounded memory growth.

    Args:
        max_entries: Maximum number of entries before eviction (default: 10000)
        cleanup_threshold: Number of entries that trigger cleanup (default: 8000)
        clock_skew_buffer_seconds: Buffer for clock differences (default: 30)

    Example:
        >>> cache = ShadowTTLCache()
        >>> cache.register("mem_123", ttl_seconds=3600)  # Valid for 1 hour
        >>> cache.is_valid("mem_123")  # True - hasn't expired
        True
        >>> cache.invalidate("mem_123")  # Force invalidation
        >>> cache.is_valid("mem_123")  # False - manually invalidated
        False
    """

    def __init__(
        self,
        max_entries: int = 10000,
        cleanup_threshold: int = 8000,
        clock_skew_buffer_seconds: float = 30.0,
    ) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._cleanup_threshold = cleanup_threshold
        self._clock_skew_buffer = clock_skew_buffer_seconds

    def register(
        self,
        memory_id: str,
        ttl_seconds: float | None = None,
        expires_at: float | None = None,
    ) -> None:
        """
        Register a memory with its TTL information.

        Args:
            memory_id: The memory ID to track
            ttl_seconds: Time-to-live in seconds from now
            expires_at: Explicit Unix timestamp for expiry

        Note:
            Either ttl_seconds or expires_at must be provided.
            If both provided, expires_at takes precedence.
        """
        if expires_at is None and ttl_seconds is None:
            # No TTL info - don't cache
            return

        now = time.time()
        if expires_at is None:
            assert ttl_seconds is not None
            expires_at = now + ttl_seconds

        entry = CacheEntry(
            memory_id=memory_id,
            expires_at=expires_at,
            created_at=now,
        )

        with self._lock:
            self._entries[memory_id] = entry

            # Trigger cleanup if needed
            if len(self._entries) >= self._cleanup_threshold:
                self._cleanup_expired()

    def is_valid(self, memory_id: str) -> bool:
        """
        Check if a memory is known to be valid (not expired).

        Returns True if:
        - Memory is in cache AND
        - Memory hasn't expired (with clock skew buffer)

        Returns False if:
        - Memory not in cache (unknown)
        - Memory has expired
        - Memory was manually invalidated

        Args:
            memory_id: The memory ID to check

        Returns:
            True if memory is cached and valid, False otherwise
        """
        with self._lock:
            entry = self._entries.get(memory_id)
            if entry is None:
                return False

            # Add buffer for clock skew - expire slightly early
            now = time.time() + self._clock_skew_buffer
            if entry.is_expired(now):
                # Clean up expired entry
                del self._entries[memory_id]
                return False

            return True

    def invalidate(self, memory_id: str) -> bool:
        """
        Manually invalidate a memory's cache entry.

        Call this when a memory is deleted or explicitly updated
        in a way that changes its validity.

        Args:
            memory_id: The memory ID to invalidate

        Returns:
            True if entry existed and was removed, False if not found
        """
        with self._lock:
            if memory_id in self._entries:
                del self._entries[memory_id]
                return True
            return False

    def get_expiry(self, memory_id: str) -> float | None:
        """
        Get the cached expiry time for a memory.

        Args:
            memory_id: The memory ID to look up

        Returns:
            Unix timestamp of expiry, or None if not cached
        """
        with self._lock:
            entry = self._entries.get(memory_id)
            return entry.expires_at if entry else None

    def clear(self) -> int:
        """
        Clear all cache entries.

        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def stats(self) -> dict[str, int | float]:
        """
        Get cache statistics.

        Returns:
            Dict with entry_count, valid_count, expired_count
        """
        now = time.time() + self._clock_skew_buffer

        with self._lock:
            total = len(self._entries)
            expired = sum(1 for e in self._entries.values() if e.is_expired(now))

            return {
                "entry_count": total,
                "valid_count": total - expired,
                "expired_count": expired,
                "max_entries": self._max_entries,
            }

    def _cleanup_expired(self) -> int:
        """
        Remove expired entries from the cache.

        Called automatically when cleanup_threshold is reached.
        Must be called with lock held.

        Returns:
            Number of entries removed
        """
        now = time.time()
        expired_ids = [memory_id for memory_id, entry in self._entries.items() if entry.is_expired(now)]

        for memory_id in expired_ids:
            del self._entries[memory_id]

        # If still over max, evict oldest entries
        if len(self._entries) > self._max_entries:
            # Sort by created_at, remove oldest
            sorted_entries = sorted(
                self._entries.items(),
                key=lambda x: x[1].created_at,
            )
            to_evict = len(self._entries) - self._max_entries
            for memory_id, _ in sorted_entries[:to_evict]:
                del self._entries[memory_id]
                expired_ids.append(memory_id)

        return len(expired_ids)

    def __len__(self) -> int:
        """Return number of entries in cache."""
        with self._lock:
            return len(self._entries)

    def __contains__(self, memory_id: str) -> bool:
        """Check if memory_id is in cache (may be expired)."""
        with self._lock:
            return memory_id in self._entries


def parse_ttl_string(ttl: str) -> float | None:
    """
    Parse a TTL string into seconds.

    Supported formats:
        - "30s" or "30sec" → 30 seconds
        - "5m" or "5min" → 5 minutes
        - "24h" or "24hr" → 24 hours
        - "7d" or "7day" → 7 days
        - "1w" or "1week" → 1 week
        - "1mo" or "1month" → 30 days (approximate)
        - "1y" or "1year" → 365 days (approximate)

    Args:
        ttl: TTL string like "30d", "24h", "1y"

    Returns:
        Number of seconds, or None if parsing fails

    Example:
        >>> parse_ttl_string("30d")
        2592000.0
        >>> parse_ttl_string("1h")
        3600.0
    """
    import re

    ttl = ttl.strip().lower()

    # Match number + unit
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", ttl)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2)

    # Map units to seconds
    unit_map = {
        "s": 1,
        "sec": 1,
        "second": 1,
        "seconds": 1,
        "m": 60,
        "min": 60,
        "minute": 60,
        "minutes": 60,
        "h": 3600,
        "hr": 3600,
        "hour": 3600,
        "hours": 3600,
        "d": 86400,
        "day": 86400,
        "days": 86400,
        "w": 604800,
        "week": 604800,
        "weeks": 604800,
        "mo": 2592000,  # 30 days
        "month": 2592000,
        "months": 2592000,
        "y": 31536000,  # 365 days
        "yr": 31536000,
        "year": 31536000,
        "years": 31536000,
    }

    multiplier = unit_map.get(unit)
    if multiplier is None:
        return None

    return value * multiplier
