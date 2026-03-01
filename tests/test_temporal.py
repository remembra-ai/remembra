"""Tests for temporal features: TTL, decay, and cleanup."""

import pytest
from datetime import datetime, timedelta

from remembra.temporal.ttl import (
    parse_ttl,
    calculate_expires_at,
    format_ttl,
    get_preset_ttl,
    TTL_PRESETS,
)
from remembra.temporal.decay import (
    DecayConfig,
    calculate_stability,
    calculate_decay_factor,
    calculate_relevance_score,
    should_prune,
    calculate_memory_decay_info,
    rank_by_relevance,
)


# =============================================================================
# TTL Tests
# =============================================================================

class TestParseTTL:
    """Tests for TTL string parsing."""
    
    def test_parse_seconds(self):
        assert parse_ttl("30s") == timedelta(seconds=30)
        assert parse_ttl("1s") == timedelta(seconds=1)
    
    def test_parse_minutes(self):
        assert parse_ttl("5m") == timedelta(minutes=5)
        assert parse_ttl("60m") == timedelta(hours=1)
    
    def test_parse_hours(self):
        assert parse_ttl("24h") == timedelta(hours=24)
        assert parse_ttl("1h") == timedelta(hours=1)
    
    def test_parse_days(self):
        assert parse_ttl("7d") == timedelta(days=7)
        assert parse_ttl("30d") == timedelta(days=30)
    
    def test_parse_weeks(self):
        assert parse_ttl("2w") == timedelta(weeks=2)
        assert parse_ttl("1w") == timedelta(days=7)
    
    def test_parse_months(self):
        # Month = 30 days
        assert parse_ttl("1M") == timedelta(days=30)
        assert parse_ttl("6M") == timedelta(days=180)
    
    def test_parse_years(self):
        # Year = 365 days
        assert parse_ttl("1y") == timedelta(days=365)
        assert parse_ttl("2y") == timedelta(days=730)
    
    def test_parse_with_whitespace(self):
        assert parse_ttl("  30d  ") == timedelta(days=30)
    
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid TTL format"):
            parse_ttl("30")  # Missing unit
        
        with pytest.raises(ValueError, match="Invalid TTL format"):
            parse_ttl("d30")  # Wrong order
        
        with pytest.raises(ValueError, match="Invalid TTL format"):
            parse_ttl("abc")  # Not a number
    
    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            parse_ttl("")
    
    def test_zero_value_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            parse_ttl("0d")


class TestCalculateExpiresAt:
    """Tests for expiration datetime calculation."""
    
    def test_with_ttl_string(self):
        base = datetime(2026, 3, 1, 12, 0, 0)
        expires = calculate_expires_at("7d", from_time=base)
        assert expires == datetime(2026, 3, 8, 12, 0, 0)
    
    def test_with_timedelta(self):
        base = datetime(2026, 3, 1, 12, 0, 0)
        expires = calculate_expires_at(ttl_delta=timedelta(hours=24), from_time=base)
        assert expires == datetime(2026, 3, 2, 12, 0, 0)
    
    def test_no_ttl_returns_none(self):
        assert calculate_expires_at() is None
        assert calculate_expires_at(ttl_string=None, ttl_delta=None) is None
    
    def test_defaults_to_now(self):
        before = datetime.utcnow()
        expires = calculate_expires_at("1d")
        after = datetime.utcnow()
        
        # Should be within 1 day + small delta
        assert before + timedelta(days=1) <= expires <= after + timedelta(days=1, seconds=1)


class TestFormatTTL:
    """Tests for formatting timedelta as TTL string."""
    
    def test_format_days(self):
        # 7 days = 1 week, so it formats as weeks
        assert format_ttl(timedelta(days=7)) == "1w"
        assert format_ttl(timedelta(days=30)) == "1M"  # 30 days = 1 month
        assert format_ttl(timedelta(days=3)) == "3d"  # Not divisible by week
    
    def test_format_weeks(self):
        assert format_ttl(timedelta(weeks=2)) == "2w"
    
    def test_format_hours(self):
        # 24 hours = 1 day, so it formats as days
        assert format_ttl(timedelta(hours=24)) == "1d"
        assert format_ttl(timedelta(hours=12)) == "12h"  # Not divisible by day
    
    def test_format_years(self):
        assert format_ttl(timedelta(days=365)) == "1y"


class TestTTLPresets:
    """Tests for TTL presets."""
    
    def test_all_presets_exist(self):
        expected = ["session", "conversation", "short_term", "long_term", "permanent"]
        for preset in expected:
            assert preset in TTL_PRESETS
    
    def test_get_preset(self):
        assert get_preset_ttl("session") == "24h"
        assert get_preset_ttl("permanent") is None
    
    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown TTL preset"):
            get_preset_ttl("nonexistent")


# =============================================================================
# Decay Tests
# =============================================================================

class TestDecayConfig:
    """Tests for decay configuration."""
    
    def test_default_config(self):
        config = DecayConfig()
        assert config.base_decay_rate == 0.07
        assert config.prune_threshold == 0.1
        assert config.newness_grace_days == 7.0
    
    def test_custom_config(self):
        config = DecayConfig(base_decay_rate=0.1, prune_threshold=0.2)
        assert config.base_decay_rate == 0.1
        assert config.prune_threshold == 0.2


class TestCalculateStability:
    """Tests for memory stability calculation."""
    
    def test_base_stability(self):
        # No accesses, default importance
        stability = calculate_stability(access_count=0, base_importance=0.5)
        assert stability >= 1.0  # At least min_stability
    
    def test_access_increases_stability(self):
        s0 = calculate_stability(access_count=0)
        s5 = calculate_stability(access_count=5)
        s10 = calculate_stability(access_count=10)
        
        assert s5 > s0
        assert s10 > s5
    
    def test_importance_affects_stability(self):
        s_low = calculate_stability(access_count=0, base_importance=0.2)
        s_high = calculate_stability(access_count=0, base_importance=0.9)
        
        assert s_high > s_low
    
    def test_stability_capped(self):
        config = DecayConfig(max_stability=50.0)
        stability = calculate_stability(access_count=1000, config=config)
        assert stability <= 50.0


class TestCalculateDecayFactor:
    """Tests for decay factor calculation."""
    
    def test_no_time_elapsed(self):
        decay = calculate_decay_factor(timedelta(0), stability=1.0)
        assert decay == 1.0
    
    def test_decay_over_time(self):
        # Same stability, increasing time
        d1 = calculate_decay_factor(timedelta(days=1), stability=2.0)
        d7 = calculate_decay_factor(timedelta(days=7), stability=2.0)
        d30 = calculate_decay_factor(timedelta(days=30), stability=2.0)
        
        assert 0 < d30 < d7 < d1 < 1.0
    
    def test_higher_stability_slower_decay(self):
        time = timedelta(days=7)
        d_weak = calculate_decay_factor(time, stability=1.0)
        d_strong = calculate_decay_factor(time, stability=5.0)
        
        assert d_strong > d_weak
    
    def test_decay_bounded(self):
        # Very long time should approach 0 but stay >= 0
        decay = calculate_decay_factor(timedelta(days=365), stability=1.0)
        assert 0.0 <= decay <= 1.0


class TestCalculateRelevanceScore:
    """Tests for relevance score calculation."""
    
    def test_fresh_memory_high_relevance(self):
        now = datetime.utcnow()
        score = calculate_relevance_score(
            created_at=now,
            last_accessed=now,
            access_count=0,
            importance_score=0.5,
        )
        assert score > 0.5  # Should be boosted by newness
    
    def test_old_unaccessed_memory_decays(self):
        now = datetime.utcnow()
        old_time = now - timedelta(days=60)
        
        score = calculate_relevance_score(
            created_at=old_time,
            last_accessed=None,  # Never accessed
            access_count=0,
            importance_score=0.5,
        )
        assert score < 0.5  # Should have decayed
    
    def test_frequently_accessed_retains_relevance(self):
        now = datetime.utcnow()
        old_time = now - timedelta(days=30)
        recent = now - timedelta(hours=2)
        
        score = calculate_relevance_score(
            created_at=old_time,
            last_accessed=recent,  # Recently accessed
            access_count=20,  # Frequently accessed
            importance_score=0.8,
        )
        assert score > 0.6  # Should retain relevance due to access patterns
    
    def test_importance_affects_score(self):
        now = datetime.utcnow()
        old = now - timedelta(days=14)
        
        score_low = calculate_relevance_score(
            created_at=old,
            last_accessed=old,
            importance_score=0.1,
        )
        score_high = calculate_relevance_score(
            created_at=old,
            last_accessed=old,
            importance_score=0.9,
        )
        assert score_high > score_low
    
    def test_as_of_parameter(self):
        created = datetime(2026, 1, 1)
        as_of_early = datetime(2026, 1, 15)
        as_of_late = datetime(2026, 3, 1)
        
        score_early = calculate_relevance_score(
            created_at=created,
            last_accessed=created,
            as_of=as_of_early,
        )
        score_late = calculate_relevance_score(
            created_at=created,
            last_accessed=created,
            as_of=as_of_late,
        )
        # Same memory, but later time = more decay
        assert score_late < score_early


class TestShouldPrune:
    """Tests for pruning decision logic."""
    
    def test_expired_ttl_should_prune(self):
        now = datetime.utcnow()
        expired = now - timedelta(hours=1)
        
        result = should_prune(
            created_at=now - timedelta(days=7),
            last_accessed=now - timedelta(days=7),
            expires_at=expired,  # Already expired
        )
        assert result is True
    
    def test_fresh_memory_should_not_prune(self):
        now = datetime.utcnow()
        
        result = should_prune(
            created_at=now,
            last_accessed=now,
            access_count=1,
            importance_score=0.5,
        )
        assert result is False
    
    def test_decayed_memory_should_prune(self):
        now = datetime.utcnow()
        very_old = now - timedelta(days=365)
        
        # Very old, never accessed, low importance
        result = should_prune(
            created_at=very_old,
            last_accessed=None,
            access_count=0,
            importance_score=0.1,
        )
        assert result is True
    
    def test_custom_threshold(self):
        config = DecayConfig(prune_threshold=0.99)  # Very high threshold
        now = datetime.utcnow()
        
        # Even fresh memory would fail high threshold
        result = should_prune(
            created_at=now - timedelta(days=7),
            last_accessed=now - timedelta(days=7),
            config=config,
        )
        assert result is True


class TestCalculateMemoryDecayInfo:
    """Tests for memory decay info helper."""
    
    def test_returns_all_fields(self):
        now = datetime.utcnow()
        memory_data = {
            "created_at": now.isoformat(),
            "last_accessed": now.isoformat(),
            "access_count": 5,
            "importance_score": 0.7,
            "expires_at": None,
        }
        
        info = calculate_memory_decay_info(memory_data)
        
        assert "relevance_score" in info
        assert "stability" in info
        assert "days_since_access" in info
        assert "should_prune" in info
        assert "ttl_remaining_seconds" in info
    
    def test_handles_string_dates(self):
        memory_data = {
            "created_at": "2026-02-01T12:00:00",
            "last_accessed": "2026-02-15T12:00:00",
            "access_count": 3,
        }
        
        info = calculate_memory_decay_info(memory_data)
        assert isinstance(info["relevance_score"], float)
    
    def test_ttl_remaining(self):
        now = datetime.utcnow()
        expires = now + timedelta(hours=2)
        
        memory_data = {
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        
        info = calculate_memory_decay_info(memory_data)
        
        # Should be approximately 2 hours in seconds
        assert info["ttl_remaining_seconds"] is not None
        assert 7000 < info["ttl_remaining_seconds"] < 7300  # ~2 hours


class TestRankByRelevance:
    """Tests for ranking memories by relevance."""
    
    def test_ranks_by_score(self):
        now = datetime.utcnow()
        
        memories = [
            {"id": "old", "created_at": (now - timedelta(days=60)).isoformat(), "access_count": 0},
            {"id": "new", "created_at": now.isoformat(), "access_count": 0},
            {"id": "accessed", "created_at": (now - timedelta(days=30)).isoformat(), 
             "last_accessed": now.isoformat(), "access_count": 10},
        ]
        
        ranked = rank_by_relevance(memories)
        
        # Should be ordered: new, accessed, old
        assert ranked[0]["id"] == "new"  # Newness boost
        assert ranked[-1]["id"] == "old"  # Most decayed
    
    def test_adds_decay_score(self):
        memories = [
            {"id": "test", "created_at": datetime.utcnow().isoformat()},
        ]
        
        ranked = rank_by_relevance(memories)
        
        assert "decay_score" in ranked[0]
        assert "decay_info" in ranked[0]


# =============================================================================
# Integration Tests
# =============================================================================

class TestTemporalIntegration:
    """Integration tests combining TTL and decay."""
    
    def test_memory_lifecycle(self):
        """Test a memory's relevance over its lifecycle."""
        config = DecayConfig()
        
        # Day 0: Memory created
        created = datetime(2026, 1, 1, 12, 0, 0)
        
        # Day 0: High relevance
        score_day0 = calculate_relevance_score(
            created_at=created,
            last_accessed=created,
            access_count=1,
            as_of=created,
            config=config,
        )
        
        # Day 7: Some decay
        score_day7 = calculate_relevance_score(
            created_at=created,
            last_accessed=created,
            access_count=1,
            as_of=created + timedelta(days=7),
            config=config,
        )
        
        # Day 30: More decay
        score_day30 = calculate_relevance_score(
            created_at=created,
            last_accessed=created,
            access_count=1,
            as_of=created + timedelta(days=30),
            config=config,
        )
        
        # Day 30 with access on day 25: Reinforced
        score_day30_accessed = calculate_relevance_score(
            created_at=created,
            last_accessed=created + timedelta(days=25),
            access_count=5,  # Accessed 5 times
            as_of=created + timedelta(days=30),
            config=config,
        )
        
        # Verify decay progression
        assert score_day0 > score_day7 > score_day30
        
        # Verify access reinforcement helps
        assert score_day30_accessed > score_day30
    
    def test_ttl_with_decay(self):
        """Test TTL expiration takes precedence over decay."""
        now = datetime.utcnow()
        
        # Memory with short TTL but high importance
        expired_but_important = should_prune(
            created_at=now - timedelta(hours=2),
            last_accessed=now - timedelta(minutes=5),
            access_count=100,
            importance_score=1.0,
            expires_at=now - timedelta(hours=1),  # Expired!
        )
        
        # Even high importance can't override TTL
        assert expired_but_important is True
