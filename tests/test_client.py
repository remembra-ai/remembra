"""Tests for the Remembra Python SDK client."""

import pytest
from unittest.mock import patch, MagicMock

from remembra import Memory, MemoryError
from remembra.client.types import StoreResult, RecallResult, ForgetResult


class TestMemoryClient:
    """Tests for the Memory client."""

    def test_memory_init_defaults(self):
        """Test Memory client initialization with defaults."""
        memory = Memory()
        assert memory.base_url == "http://localhost:8787"
        assert memory.user_id == "default"
        assert memory.project == "default"
        assert memory.api_key is None

    def test_memory_init_custom(self):
        """Test Memory client initialization with custom values."""
        memory = Memory(
            base_url="http://custom:9000",
            api_key="test_key",
            user_id="user_123",
            project="my_app",
        )
        assert memory.base_url == "http://custom:9000"
        assert memory.api_key == "test_key"
        assert memory.user_id == "user_123"
        assert memory.project == "my_app"

    def test_memory_repr(self):
        """Test Memory client string representation."""
        memory = Memory(user_id="test_user", project="test_project")
        repr_str = repr(memory)
        assert "test_user" in repr_str
        assert "test_project" in repr_str

    @patch("remembra.client.memory.httpx.Client")
    def test_store_success(self, mock_client_class):
        """Test successful memory storage."""
        # Setup mock
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "mem_123",
            "extracted_facts": ["John works at Acme."],
            "entities": [{"id": "ent_1", "canonical_name": "John", "type": "person", "confidence": 0.95}],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Test
        memory = Memory(user_id="user_123")
        result = memory.store("John works at Acme Corp")

        assert isinstance(result, StoreResult)
        assert result.id == "mem_123"
        assert len(result.extracted_facts) == 1
        assert len(result.entities) == 1
        assert result.entities[0].canonical_name == "John"

    @patch("remembra.client.memory.httpx.Client")
    def test_recall_success(self, mock_client_class):
        """Test successful memory recall."""
        # Setup mock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "context": "John works at Acme Corp.",
            "memories": [
                {
                    "id": "mem_123",
                    "content": "John works at Acme Corp",
                    "relevance": 0.92,
                    "created_at": "2026-03-01T10:00:00",
                }
            ],
            "entities": [],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Test
        memory = Memory(user_id="user_123")
        result = memory.recall("Where does John work?")

        assert isinstance(result, RecallResult)
        assert result.context == "John works at Acme Corp."
        assert len(result.memories) == 1
        assert result.memories[0].relevance == 0.92

    @patch("remembra.client.memory.httpx.Client")
    def test_forget_success(self, mock_client_class):
        """Test successful memory deletion."""
        # Setup mock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "deleted_memories": 5,
            "deleted_entities": 2,
            "deleted_relationships": 3,
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Test
        memory = Memory(user_id="user_123")
        result = memory.forget(user_id="user_123")

        assert isinstance(result, ForgetResult)
        assert result.deleted_memories == 5
        assert result.deleted_entities == 2

    @patch("remembra.client.memory.httpx.Client")
    def test_request_error(self, mock_client_class):
        """Test error handling for failed requests."""
        # Setup mock
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"detail": "Internal server error"}
        mock_response.text = "Internal server error"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Test
        memory = Memory(user_id="user_123")

        with pytest.raises(MemoryError) as exc_info:
            memory.store("Test content")

        assert exc_info.value.status_code == 500


class TestV012Features:
    """Tests for v0.12 SDK features."""

    def test_memory_init_with_auto_expire_temporal(self):
        """Test Memory client initialization with auto_expire_temporal."""
        memory = Memory(
            user_id="user_123",
            auto_expire_temporal=True,
        )
        assert memory._auto_expire_temporal is True
        assert memory._temporal_parser is not None

    def test_memory_init_with_shadow_ttl(self):
        """Test Memory client initialization with shadow TTL cache."""
        memory = Memory(
            user_id="user_123",
            enable_shadow_ttl=True,
            shadow_ttl_max_entries=500,
        )
        assert memory._enable_shadow_ttl is True
        assert memory._shadow_cache is not None

    def test_memory_repr_with_features(self):
        """Test Memory repr shows enabled features."""
        memory = Memory(
            user_id="user_123",
            auto_expire_temporal=True,
            enable_shadow_ttl=True,
        )
        repr_str = repr(memory)
        assert "auto_expire_temporal" in repr_str
        assert "shadow_ttl" in repr_str

    def test_detect_temporal_method(self):
        """Test detect_temporal method on Memory client."""
        memory = Memory(
            user_id="user_123",
            auto_expire_temporal=True,
        )

        result = memory.detect_temporal("Meeting tomorrow at 3pm")

        assert result is not None
        assert "phrase" in result
        assert "ttl_string" in result
        assert "ttl_seconds" in result
        assert "confidence" in result

    def test_detect_temporal_disabled(self):
        """Test detect_temporal returns None when feature disabled."""
        memory = Memory(user_id="user_123")  # auto_expire_temporal=False

        result = memory.detect_temporal("Meeting tomorrow")

        assert result is None

    def test_shadow_cache_stats_enabled(self):
        """Test shadow_cache_stats when enabled."""
        memory = Memory(
            user_id="user_123",
            enable_shadow_ttl=True,
        )

        stats = memory.shadow_cache_stats()

        assert stats is not None
        assert "entry_count" in stats
        assert "valid_count" in stats

    def test_shadow_cache_stats_disabled(self):
        """Test shadow_cache_stats returns None when disabled."""
        memory = Memory(user_id="user_123")  # enable_shadow_ttl=False

        stats = memory.shadow_cache_stats()

        assert stats is None

    def test_is_memory_valid_disabled(self):
        """Test is_memory_valid returns None when shadow cache disabled."""
        memory = Memory(user_id="user_123")

        result = memory.is_memory_valid("mem_123")

        assert result is None

    def test_clear_shadow_cache(self):
        """Test clearing shadow cache."""
        memory = Memory(
            user_id="user_123",
            enable_shadow_ttl=True,
        )

        # Manually add entry
        memory._shadow_cache.register("mem_123", ttl_seconds=3600)
        assert len(memory._shadow_cache) == 1

        count = memory.clear_shadow_cache()

        assert count == 1
        assert len(memory._shadow_cache) == 0

    @patch("remembra.client.memory.httpx.Client")
    def test_store_with_auto_expire_temporal(self, mock_client_class):
        """Test store auto-sets TTL from temporal phrases."""
        # Setup mock
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "mem_temporal",
            "extracted_facts": ["Meeting tomorrow."],
            "entities": [],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Test
        memory = Memory(
            user_id="user_123",
            auto_expire_temporal=True,
        )
        result = memory.store("Meeting tomorrow at 3pm")

        assert result.id == "mem_temporal"

        # Verify TTL was set in request
        call_args = mock_client.request.call_args
        payload = call_args.kwargs.get("json", {})
        assert "ttl" in payload
        # Should be ~36h format
        assert "h" in payload["ttl"] or "d" in payload["ttl"]

    @patch("remembra.client.memory.httpx.Client")
    def test_store_explicit_ttl_overrides_auto(self, mock_client_class):
        """Test explicit TTL overrides auto-detection."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "mem_explicit",
            "extracted_facts": [],
            "entities": [],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        memory = Memory(
            user_id="user_123",
            auto_expire_temporal=True,
        )
        # Explicit TTL should override auto-detection
        memory.store("Meeting tomorrow", ttl="7d")

        call_args = mock_client.request.call_args
        payload = call_args.kwargs.get("json", {})
        assert payload.get("ttl") == "7d"

    @patch("remembra.client.memory.httpx.Client")
    def test_store_registers_shadow_cache(self, mock_client_class):
        """Test store registers TTL in shadow cache."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "mem_cached",
            "extracted_facts": [],
            "entities": [],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        memory = Memory(
            user_id="user_123",
            enable_shadow_ttl=True,
        )
        memory.store("Test content", ttl="30d")

        # Memory should be registered in shadow cache
        assert memory.is_memory_valid("mem_cached") is True

    @patch("remembra.client.memory.httpx.Client")
    def test_forget_invalidates_shadow_cache(self, mock_client_class):
        """Test forget invalidates shadow cache entry."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "deleted_memories": 1,
            "deleted_entities": 0,
            "deleted_relationships": 0,
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        memory = Memory(
            user_id="user_123",
            enable_shadow_ttl=True,
        )

        # Pre-register a memory
        memory._shadow_cache.register("mem_delete", ttl_seconds=3600)
        assert memory.is_memory_valid("mem_delete") is True

        # Delete it
        memory.forget(memory_id="mem_delete")

        # Should be invalidated
        assert "mem_delete" not in memory._shadow_cache


class TestImports:
    """Test that public API is properly exposed."""

    def test_import_memory(self):
        """Test importing Memory from top-level."""
        from remembra import Memory

        assert Memory is not None

    def test_import_types(self):
        """Test importing types from top-level."""
        from remembra import StoreResult, RecallResult, ForgetResult

        assert StoreResult is not None
        assert RecallResult is not None
        assert ForgetResult is not None

    def test_import_version(self):
        """Test version is accessible."""
        from remembra import __version__

        assert __version__  # just check it's a non-empty string

    def test_import_v012_components(self):
        """Test importing v0.12 components."""
        from remembra.client import (
            ShadowTTLCache,
            TemporalParser,
            TemporalDetection,
            detect_temporal,
            suggest_ttl,
            parse_ttl_string,
        )

        assert ShadowTTLCache is not None
        assert TemporalParser is not None
        assert TemporalDetection is not None
        assert detect_temporal is not None
        assert suggest_ttl is not None
        assert parse_ttl_string is not None
