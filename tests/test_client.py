"""Tests for the Remembra Python SDK client."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

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
            "entities": [
                {"id": "ent_1", "canonical_name": "John", "type": "person", "confidence": 0.95}
            ],
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
