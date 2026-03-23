"""Tests for v0.12 features: User Profiles, Strict Mode, Event-Driven Expiry."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from remembra.api.v1.memories import _is_memory_expired
from remembra.api.v1.users import (
    TopTopic,
    UserActivitySummary,
    UserEntitySummary,
    UserProfileResponse,
    UserStaticFacts,
)
from remembra.config import Settings
from remembra.models.memory import StoreRequest


class TestIsMemoryExpired:
    """Tests for _is_memory_expired helper function."""

    def test_expired_memory_returns_true(self):
        """Expired memories should return True."""
        # Memory expired 1 hour ago
        expired_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        memory = {"expires_at": expired_time}
        
        assert _is_memory_expired(memory) is True

    def test_not_expired_memory_returns_false(self):
        """Non-expired memories should return False."""
        # Memory expires in 1 hour
        future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        memory = {"expires_at": future_time}
        
        assert _is_memory_expired(memory) is False

    def test_no_expiry_returns_false(self):
        """Memories without expires_at should return False."""
        memory = {}
        assert _is_memory_expired(memory) is False
        
        memory_none = {"expires_at": None}
        assert _is_memory_expired(memory_none) is False

    def test_handles_z_suffix(self):
        """Should handle ISO timestamps with Z suffix."""
        expired_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        memory = {"expires_at": expired_time}
        
        assert _is_memory_expired(memory) is True

    def test_handles_datetime_object(self):
        """Should handle datetime objects directly."""
        # Expired
        expired_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        memory = {"expires_at": expired_dt}
        assert _is_memory_expired(memory) is True
        
        # Not expired
        future_dt = datetime.now(timezone.utc) + timedelta(hours=1)
        memory_future = {"expires_at": future_dt}
        assert _is_memory_expired(memory_future) is False


class TestStrictModeConfig:
    """Tests for strict_mode configuration."""

    def test_strict_mode_default_false(self):
        """strict_mode should default to False."""
        settings = Settings()
        assert settings.strict_mode is False

    def test_strict_mode_can_be_enabled(self):
        """strict_mode can be set to True via env."""
        with patch.dict("os.environ", {"REMEMBRA_STRICT_MODE": "true"}):
            settings = Settings()
            assert settings.strict_mode is True


class TestStoreRequestExpiresAt:
    """Tests for expires_at parameter in StoreRequest."""

    def test_expires_at_default_none(self):
        """expires_at should default to None."""
        req = StoreRequest(content="test content")
        assert req.expires_at is None

    def test_expires_at_accepts_datetime(self):
        """expires_at should accept datetime objects."""
        future = datetime.now(timezone.utc) + timedelta(days=7)
        req = StoreRequest(content="test content", expires_at=future)
        assert req.expires_at == future

    def test_expires_at_with_ttl_coexist(self):
        """Both expires_at and ttl can be provided (expires_at takes precedence)."""
        future = datetime.now(timezone.utc) + timedelta(days=7)
        req = StoreRequest(content="test content", expires_at=future, ttl="30d")
        
        assert req.expires_at == future
        assert req.ttl == "30d"


class TestUserProfileModels:
    """Tests for User Profile API models."""

    def test_user_entity_summary(self):
        """UserEntitySummary should have required fields."""
        entity = UserEntitySummary(
            id="entity-123",
            name="Alice",
            type="person",
            mention_count=5,
        )
        assert entity.id == "entity-123"
        assert entity.name == "Alice"
        assert entity.type == "person"
        assert entity.mention_count == 5

    def test_user_activity_summary_defaults(self):
        """UserActivitySummary should have sensible defaults."""
        activity = UserActivitySummary()
        assert activity.last_memory_at is None
        assert activity.last_recall_at is None
        assert activity.memories_last_24h == 0
        assert activity.memories_last_7d == 0
        assert activity.memories_last_30d == 0

    def test_top_topic(self):
        """TopTopic should capture topic info."""
        topic = TopTopic(
            topic="python",
            count=10,
            last_mentioned=datetime.now(timezone.utc),
        )
        assert topic.topic == "python"
        assert topic.count == 10
        assert topic.last_mentioned is not None

    def test_user_static_facts_defaults(self):
        """UserStaticFacts should have empty defaults."""
        facts = UserStaticFacts()
        assert facts.facts == []
        assert facts.entities == []
        assert facts.attributes == {}

    def test_user_profile_response_complete(self):
        """UserProfileResponse should aggregate all data."""
        profile = UserProfileResponse(
            user_id="user-123",
            project_id="default",
            total_memories=100,
            total_entities=50,
            total_relationships=25,
        )
        
        assert profile.user_id == "user-123"
        assert profile.project_id == "default"
        assert profile.total_memories == 100
        assert profile.total_entities == 50
        assert profile.total_relationships == 25
        # Check defaults
        assert isinstance(profile.static_facts, UserStaticFacts)
        assert isinstance(profile.activity, UserActivitySummary)
        assert profile.top_topics == []


class TestDatabaseMethods:
    """Tests for new database methods."""

    @pytest.mark.asyncio
    async def test_count_memories_with_since(self):
        """count_memories should support since parameter."""
        from remembra.storage.database import Database
        
        # Just verify the method signature accepts since
        db = Database(":memory:")
        # Check method exists and has since param
        import inspect
        sig = inspect.signature(db.count_memories)
        params = list(sig.parameters.keys())
        assert "since" in params

    @pytest.mark.asyncio
    async def test_get_recent_memories_exists(self):
        """get_recent_memories method should exist."""
        from remembra.storage.database import Database
        
        db = Database(":memory:")
        assert hasattr(db, "get_recent_memories")
        assert callable(db.get_recent_memories)
