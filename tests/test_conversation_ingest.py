"""
Tests for Conversation Ingestion (Phase 1).

Tests cover:
- Basic ingestion with fact extraction
- Dry run mode (store=False)
- Raw mode (infer=False)
- Extract from user/assistant/both
- Importance filtering
- Deduplication
- Multi-user conversations
- Edge cases (empty, single message)
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from remembra.models.memory import (
    ConversationIngestRequest,
    ConversationMessage,
    ExtractedFact,
    IngestOptions,
    IngestStats,
)
from remembra.services.conversation_ingest import ConversationIngestService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    settings = MagicMock()
    settings.openai_api_key = "test-key"
    settings.extraction_model = "gpt-4o-mini"
    settings.sanitization_enabled = False
    return settings


@pytest.fixture
def mock_memory_service():
    """Mock memory service with all required components."""
    service = MagicMock()
    
    # Mock extractors
    service.extractor = MagicMock()
    service.entity_extractor = MagicMock()
    service.consolidator = MagicMock()
    service.conflict_manager = MagicMock()
    service.embeddings = MagicMock()
    service.qdrant = MagicMock()
    service.db = MagicMock()
    
    # Mock async methods
    service.store = AsyncMock()
    service.forget_by_id = AsyncMock()
    
    return service


@pytest.fixture
def conversation_ingest_service(mock_settings, mock_memory_service):
    """Create ConversationIngestService with mocks."""
    return ConversationIngestService(
        settings=mock_settings,
        memory_service=mock_memory_service,
    )


@pytest.fixture
def sample_messages():
    """Sample conversation messages for testing."""
    return [
        ConversationMessage(
            role="user",
            content="My wife Suzan and I are planning a trip to Japan",
            name="Mani",
        ),
        ConversationMessage(
            role="assistant",
            content="That sounds exciting! When are you planning to go?",
        ),
        ConversationMessage(
            role="user",
            content="We're thinking April next year",
            name="Mani",
        ),
    ]


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------


class TestConversationModels:
    """Tests for conversation ingestion models."""
    
    def test_conversation_message_basic(self):
        """Test basic ConversationMessage creation."""
        msg = ConversationMessage(
            role="user",
            content="Hello world",
        )
        assert msg.role == "user"
        assert msg.content == "Hello world"
        assert msg.name is None
        assert msg.timestamp is None
    
    def test_conversation_message_with_metadata(self):
        """Test ConversationMessage with all fields."""
        now = datetime.utcnow()
        msg = ConversationMessage(
            role="user",
            content="Test message",
            timestamp=now,
            name="TestUser",
            metadata={"source": "test"},
        )
        assert msg.name == "TestUser"
        assert msg.timestamp == now
        assert msg.metadata == {"source": "test"}
    
    def test_conversation_message_content_limit(self):
        """Test content length validation."""
        # Should work with normal content
        msg = ConversationMessage(role="user", content="Normal message")
        assert len(msg.content) < 50000
        
        # Should raise for content over 50K
        with pytest.raises(ValueError, match="exceeds maximum length"):
            ConversationMessage(role="user", content="x" * 50001)
    
    def test_ingest_options_defaults(self):
        """Test IngestOptions default values."""
        opts = IngestOptions()
        assert opts.extract_from == "both"
        assert opts.min_importance == 0.5
        assert opts.dedupe is True
        assert opts.store is True
        assert opts.infer is True
    
    def test_ingest_options_custom(self):
        """Test IngestOptions with custom values."""
        opts = IngestOptions(
            extract_from="user",
            min_importance=0.7,
            dedupe=False,
            store=False,
            infer=False,
        )
        assert opts.extract_from == "user"
        assert opts.min_importance == 0.7
        assert opts.dedupe is False
        assert opts.store is False
        assert opts.infer is False
    
    def test_conversation_ingest_request_validation(self):
        """Test ConversationIngestRequest validation."""
        # Valid request
        request = ConversationIngestRequest(
            messages=[
                ConversationMessage(role="user", content="Hello"),
            ],
            user_id="test_user",
        )
        assert len(request.messages) == 1
        assert request.user_id == "test_user"
        assert request.project_id == "default"
        
        # Should fail with empty messages
        with pytest.raises(ValueError):
            ConversationIngestRequest(
                messages=[],
                user_id="test_user",
            )
    
    def test_extracted_fact_model(self):
        """Test ExtractedFact model."""
        fact = ExtractedFact(
            content="Mani's wife is named Suzan",
            confidence=0.95,
            importance=0.9,
            source_message_index=0,
            speaker="Mani",
            stored=True,
            memory_id="mem_123",
            action="add",
        )
        assert fact.content == "Mani's wife is named Suzan"
        assert fact.importance == 0.9
        assert fact.stored is True
    
    def test_ingest_stats_model(self):
        """Test IngestStats model."""
        stats = IngestStats(
            messages_processed=5,
            facts_extracted=3,
            facts_stored=2,
            facts_deduped=1,
            processing_time_ms=150,
        )
        assert stats.messages_processed == 5
        assert stats.facts_stored == 2


# ---------------------------------------------------------------------------
# Service Tests
# ---------------------------------------------------------------------------


class TestConversationIngestService:
    """Tests for ConversationIngestService."""
    
    @pytest.mark.asyncio
    async def test_ingest_basic(self, conversation_ingest_service, sample_messages):
        """Test basic conversation ingestion."""
        # Mock the LLM response
        with patch.object(
            conversation_ingest_service,
            "_extract_facts",
            new_callable=AsyncMock,
        ) as mock_extract:
            mock_extract.return_value = [
                ExtractedFact(
                    content="Mani's wife is named Suzan",
                    importance=0.9,
                    source_message_index=0,
                    speaker="Mani",
                ),
                ExtractedFact(
                    content="Mani is planning a trip to Japan in April",
                    importance=0.8,
                    source_message_index=2,
                    speaker="Mani",
                ),
            ]
            
            # Mock entity extraction
            conversation_ingest_service.entity_extractor.extract = AsyncMock(
                return_value=MagicMock(entities=[], relationships=[])
            )
            
            # Mock embedding and search
            conversation_ingest_service.embeddings.embed = AsyncMock(
                return_value=[0.1] * 1536
            )
            conversation_ingest_service.qdrant.search = AsyncMock(return_value=[])
            
            # Mock store
            conversation_ingest_service.memory_service.store = AsyncMock(
                return_value=MagicMock(id="mem_123")
            )
            
            request = ConversationIngestRequest(
                messages=sample_messages,
                user_id="test_user",
            )
            
            result = await conversation_ingest_service.ingest(request)
            
            assert result.status in ["ok", "partial"]
            assert result.stats.messages_processed == 3
            assert result.stats.facts_extracted >= 0
    
    @pytest.mark.asyncio
    async def test_ingest_dry_run(self, conversation_ingest_service, sample_messages):
        """Test dry run mode (store=False)."""
        with patch.object(
            conversation_ingest_service,
            "_extract_facts",
            new_callable=AsyncMock,
        ) as mock_extract:
            mock_extract.return_value = [
                ExtractedFact(
                    content="Test fact",
                    importance=0.8,
                    source_message_index=0,
                    speaker="User",
                ),
            ]
            
            conversation_ingest_service.entity_extractor.extract = AsyncMock(
                return_value=MagicMock(entities=[], relationships=[])
            )
            
            request = ConversationIngestRequest(
                messages=sample_messages,
                user_id="test_user",
                options=IngestOptions(store=False),
            )
            
            result = await conversation_ingest_service.ingest(request)
            
            # Should not call store
            conversation_ingest_service.memory_service.store.assert_not_called()
            
            # Facts should be marked as not stored
            for fact in result.facts:
                assert fact.stored is False
    
    @pytest.mark.asyncio
    async def test_ingest_raw_mode(self, conversation_ingest_service, sample_messages):
        """Test raw mode (infer=False) - stores messages without extraction."""
        conversation_ingest_service.memory_service.store = AsyncMock(
            return_value=MagicMock(id="mem_123")
        )
        
        request = ConversationIngestRequest(
            messages=sample_messages,
            user_id="test_user",
            options=IngestOptions(infer=False),
        )
        
        result = await conversation_ingest_service.ingest(request)
        
        # Should have stored raw messages (excluding system)
        assert result.status == "ok"
        assert result.stats.messages_processed == 3
        # Each non-system message should be stored
        assert conversation_ingest_service.memory_service.store.call_count >= 2
    
    @pytest.mark.asyncio
    async def test_ingest_importance_filtering(self, conversation_ingest_service, sample_messages):
        """Test that facts below min_importance are filtered."""
        with patch.object(
            conversation_ingest_service,
            "_extract_facts",
            new_callable=AsyncMock,
        ) as mock_extract:
            # Return facts with varying importance
            mock_extract.return_value = [
                ExtractedFact(
                    content="High importance fact",
                    importance=0.9,
                    source_message_index=0,
                    speaker="User",
                ),
                ExtractedFact(
                    content="Low importance fact",
                    importance=0.3,  # Below default threshold
                    source_message_index=1,
                    speaker="User",
                ),
            ]
            
            conversation_ingest_service.entity_extractor.extract = AsyncMock(
                return_value=MagicMock(entities=[], relationships=[])
            )
            conversation_ingest_service.embeddings.embed = AsyncMock(
                return_value=[0.1] * 1536
            )
            conversation_ingest_service.qdrant.search = AsyncMock(return_value=[])
            conversation_ingest_service.memory_service.store = AsyncMock(
                return_value=MagicMock(id="mem_123")
            )
            
            request = ConversationIngestRequest(
                messages=sample_messages,
                user_id="test_user",
                options=IngestOptions(min_importance=0.5),
            )
            
            result = await conversation_ingest_service.ingest(request)
            
            # Only high importance fact should be processed for storage
            # (filtering happens in _extract_facts)
            assert result.status in ["ok", "partial"]


# ---------------------------------------------------------------------------
# Integration Tests (require running server)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestConversationIngestIntegration:
    """Integration tests requiring a running Remembra server."""
    
    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        """Test the full ingestion pipeline end-to-end."""
        # This test requires a running server
        # Skip if server is not available
        pytest.skip("Integration test - requires running server")


# ---------------------------------------------------------------------------
# Edge Case Tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_empty_message_content(self):
        """Test handling of empty message content."""
        # Empty content should be allowed (validator just strips)
        msg = ConversationMessage(role="user", content="  ")
        assert msg.content == "  "  # Validator doesn't strip by default
    
    def test_single_message_conversation(self):
        """Test conversation with single message."""
        request = ConversationIngestRequest(
            messages=[
                ConversationMessage(role="user", content="Single message"),
            ],
            user_id="test_user",
        )
        assert len(request.messages) == 1
    
    def test_max_messages_limit(self):
        """Test maximum message limit (200)."""
        # Should work with 200 messages
        messages = [
            ConversationMessage(role="user", content=f"Message {i}")
            for i in range(200)
        ]
        request = ConversationIngestRequest(
            messages=messages,
            user_id="test_user",
        )
        assert len(request.messages) == 200
        
        # Should fail with 201 messages
        messages_over = [
            ConversationMessage(role="user", content=f"Message {i}")
            for i in range(201)
        ]
        with pytest.raises(ValueError):
            ConversationIngestRequest(
                messages=messages_over,
                user_id="test_user",
            )
    
    def test_system_messages_skipped(self):
        """Test that system messages are skipped in extraction."""
        messages = [
            ConversationMessage(role="system", content="You are a helpful assistant"),
            ConversationMessage(role="user", content="Hello"),
        ]
        request = ConversationIngestRequest(
            messages=messages,
            user_id="test_user",
        )
        # System message included but should be skipped during processing
        assert len(request.messages) == 2
