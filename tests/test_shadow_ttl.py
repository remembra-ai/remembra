"""Tests for Shadow TTL Cache (v0.12)."""

import time

import pytest

from remembra.client.shadow_ttl import CacheEntry, ShadowTTLCache, parse_ttl_string


class TestCacheEntry:
    """Tests for CacheEntry dataclass."""

    def test_create_entry(self):
        """Test creating a cache entry."""
        now = time.time()
        entry = CacheEntry(
            memory_id="mem_123",
            expires_at=now + 3600,
            created_at=now,
        )
        assert entry.memory_id == "mem_123"
        assert entry.expires_at == now + 3600
        assert entry.created_at == now

    def test_is_expired_false(self):
        """Test entry that hasn't expired."""
        entry = CacheEntry(
            memory_id="mem_123",
            expires_at=time.time() + 3600,
        )
        assert entry.is_expired() is False

    def test_is_expired_true(self):
        """Test entry that has expired."""
        entry = CacheEntry(
            memory_id="mem_123",
            expires_at=time.time() - 1,
        )
        assert entry.is_expired() is True

    def test_is_expired_with_custom_time(self):
        """Test expiry check with custom current time."""
        now = time.time()
        entry = CacheEntry(
            memory_id="mem_123",
            expires_at=now + 100,
        )
        # Not expired at now
        assert entry.is_expired(now) is False
        # Expired at now + 200
        assert entry.is_expired(now + 200) is True


class TestShadowTTLCache:
    """Tests for ShadowTTLCache."""

    def test_init_defaults(self):
        """Test cache initialization with defaults."""
        cache = ShadowTTLCache()
        assert cache._max_entries == 10000
        assert cache._cleanup_threshold == 8000
        assert len(cache) == 0

    def test_init_custom(self):
        """Test cache initialization with custom values."""
        cache = ShadowTTLCache(
            max_entries=100,
            cleanup_threshold=80,
            clock_skew_buffer_seconds=60,
        )
        assert cache._max_entries == 100
        assert cache._cleanup_threshold == 80
        assert cache._clock_skew_buffer == 60

    def test_register_with_ttl_seconds(self):
        """Test registering memory with TTL in seconds."""
        cache = ShadowTTLCache()
        cache.register("mem_123", ttl_seconds=3600)

        assert len(cache) == 1
        assert "mem_123" in cache
        assert cache.is_valid("mem_123") is True

    def test_register_with_expires_at(self):
        """Test registering memory with explicit expiry time."""
        cache = ShadowTTLCache()
        expires_at = time.time() + 7200
        cache.register("mem_456", expires_at=expires_at)

        assert cache.get_expiry("mem_456") == expires_at

    def test_register_no_ttl_skipped(self):
        """Test that registering without TTL does nothing."""
        cache = ShadowTTLCache()
        cache.register("mem_789")  # No TTL info

        assert len(cache) == 0
        assert "mem_789" not in cache

    def test_is_valid_unknown_memory(self):
        """Test is_valid returns False for unknown memory."""
        cache = ShadowTTLCache()
        assert cache.is_valid("unknown") is False

    def test_is_valid_expired_memory(self):
        """Test is_valid returns False and cleans up expired memory."""
        cache = ShadowTTLCache(clock_skew_buffer_seconds=0)

        # Register with very short TTL
        cache.register("mem_expired", ttl_seconds=0.001)
        time.sleep(0.01)  # Wait for expiry

        assert cache.is_valid("mem_expired") is False
        assert len(cache) == 0  # Should be cleaned up

    def test_is_valid_with_clock_skew_buffer(self):
        """Test that clock skew buffer causes early expiration."""
        cache = ShadowTTLCache(clock_skew_buffer_seconds=60)

        # Register memory that expires in 30 seconds
        # But with 60s buffer, it should appear expired
        cache.register("mem_buffer", ttl_seconds=30)

        # Should be invalid due to buffer
        assert cache.is_valid("mem_buffer") is False

    def test_invalidate_existing(self):
        """Test invalidating an existing memory."""
        cache = ShadowTTLCache()
        cache.register("mem_123", ttl_seconds=3600)

        result = cache.invalidate("mem_123")

        assert result is True
        assert "mem_123" not in cache

    def test_invalidate_nonexistent(self):
        """Test invalidating a non-existent memory."""
        cache = ShadowTTLCache()
        result = cache.invalidate("nonexistent")
        assert result is False

    def test_get_expiry(self):
        """Test getting expiry time for a memory."""
        cache = ShadowTTLCache()
        now = time.time()
        cache.register("mem_123", ttl_seconds=3600)

        expiry = cache.get_expiry("mem_123")
        assert expiry is not None
        assert expiry > now
        assert expiry <= now + 3601  # Allow for timing

    def test_get_expiry_unknown(self):
        """Test getting expiry for unknown memory."""
        cache = ShadowTTLCache()
        assert cache.get_expiry("unknown") is None

    def test_clear(self):
        """Test clearing all entries."""
        cache = ShadowTTLCache()
        cache.register("mem_1", ttl_seconds=3600)
        cache.register("mem_2", ttl_seconds=3600)
        cache.register("mem_3", ttl_seconds=3600)

        count = cache.clear()

        assert count == 3
        assert len(cache) == 0

    def test_stats(self):
        """Test cache statistics."""
        cache = ShadowTTLCache(
            max_entries=100,
            clock_skew_buffer_seconds=0,
        )
        cache.register("mem_valid", ttl_seconds=3600)

        # Register already-expired entry
        cache._entries["mem_expired"] = CacheEntry(
            memory_id="mem_expired",
            expires_at=time.time() - 100,
        )

        stats = cache.stats()

        assert stats["entry_count"] == 2
        assert stats["valid_count"] == 1
        assert stats["expired_count"] == 1
        assert stats["max_entries"] == 100

    def test_cleanup_on_threshold(self):
        """Test automatic cleanup when threshold reached."""
        cache = ShadowTTLCache(
            max_entries=10,
            cleanup_threshold=5,
            clock_skew_buffer_seconds=0,
        )

        # Add expired entries
        for i in range(3):
            cache._entries[f"expired_{i}"] = CacheEntry(
                memory_id=f"expired_{i}",
                expires_at=time.time() - 100,
            )

        # Adding more entries should trigger cleanup at threshold
        for i in range(3):
            cache.register(f"valid_{i}", ttl_seconds=3600)

        # Expired entries should be cleaned up
        assert len(cache) == 3

    def test_eviction_when_over_max(self):
        """Test oldest entries are evicted when over max."""
        cache = ShadowTTLCache(
            max_entries=3,
            cleanup_threshold=3,
        )

        # Add entries with staggered times
        for i in range(5):
            cache.register(f"mem_{i}", ttl_seconds=3600)
            time.sleep(0.01)  # Small delay to order by created_at

        # Should only have 3 entries (max)
        assert len(cache) <= 3

    def test_len(self):
        """Test __len__ returns entry count."""
        cache = ShadowTTLCache()
        assert len(cache) == 0

        cache.register("mem_1", ttl_seconds=100)
        assert len(cache) == 1

    def test_contains(self):
        """Test __contains__ for membership check."""
        cache = ShadowTTLCache()
        cache.register("mem_123", ttl_seconds=3600)

        assert "mem_123" in cache
        assert "unknown" not in cache

    def test_thread_safety(self):
        """Test cache is thread-safe."""
        import threading

        cache = ShadowTTLCache()
        errors = []

        def register_memories():
            try:
                for i in range(100):
                    cache.register(f"thread_mem_{threading.current_thread().name}_{i}", ttl_seconds=3600)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_memories, name=f"t{i}") for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(cache) <= 500  # All entries may be present


class TestParseTTLString:
    """Tests for parse_ttl_string function."""

    @pytest.mark.parametrize(
        "ttl,expected",
        [
            # Seconds
            ("30s", 30),
            ("30sec", 30),
            ("30second", 30),
            ("30seconds", 30),
            # Minutes
            ("5m", 300),
            ("5min", 300),
            ("5minute", 300),
            ("5minutes", 300),
            # Hours
            ("2h", 7200),
            ("2hr", 7200),
            ("2hour", 7200),
            ("2hours", 7200),
            # Days
            ("7d", 604800),
            ("7day", 604800),
            ("7days", 604800),
            # Weeks
            ("2w", 1209600),
            ("2week", 1209600),
            ("2weeks", 1209600),
            # Months (30 days)
            ("1mo", 2592000),
            ("1month", 2592000),
            ("1months", 2592000),
            # Years (365 days)
            ("1y", 31536000),
            ("1yr", 31536000),
            ("1year", 31536000),
            ("1years", 31536000),
            # Decimals
            ("1.5h", 5400),
            ("2.5d", 216000),
        ],
    )
    def test_valid_ttl_strings(self, ttl, expected):
        """Test parsing various valid TTL strings."""
        result = parse_ttl_string(ttl)
        assert result == expected

    def test_invalid_ttl_strings(self):
        """Test parsing invalid TTL strings."""
        assert parse_ttl_string("invalid") is None
        assert parse_ttl_string("") is None
        assert parse_ttl_string("10x") is None
        assert parse_ttl_string("abc") is None

    def test_whitespace_handling(self):
        """Test TTL parsing handles whitespace."""
        assert parse_ttl_string("  30d  ") == 2592000
        assert parse_ttl_string("30 d") == 2592000

    def test_case_insensitivity(self):
        """Test TTL parsing is case insensitive."""
        assert parse_ttl_string("30D") == 2592000
        assert parse_ttl_string("1H") == 3600
        assert parse_ttl_string("1HOUR") == 3600
