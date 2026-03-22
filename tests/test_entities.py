"""Tests for entity extraction — OpenAI, Anthropic, and Ollama providers."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from remembra.extraction.entities import (
    EntityExtractor,
    AnthropicEntityExtractor,
    OllamaEntityExtractor,
    ExtractionResult,
    _parse_extraction_json,
    create_entity_extractor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_EXTRACTION_JSON = json.dumps({
    "entities": [
        {
            "name": "Alice",
            "type": "PERSON",
            "description": "CEO of Acme Corp",
            "aliases": ["A"],
        },
        {
            "name": "Acme Corp",
            "type": "ORG",
            "description": "Company",
            "aliases": ["Acme"],
        },
    ],
    "relationships": [
        {
            "subject": "Alice",
            "predicate": "WORKS_AT",
            "object": "Acme Corp",
        },
        {
            "subject": "Alice",
            "predicate": "ROLE",
            "object": "CEO",
        },
    ],
})


# ---------------------------------------------------------------------------
# _parse_extraction_json tests
# ---------------------------------------------------------------------------


class TestParseExtractionJson:
    def test_parse_clean_json(self):
        result = _parse_extraction_json(SAMPLE_EXTRACTION_JSON)
        assert len(result.entities) == 2
        assert len(result.relationships) == 2
        assert result.entities[0].name == "Alice"
        assert result.entities[0].type == "PERSON"
        assert result.relationships[0].predicate == "WORKS_AT"

    def test_parse_markdown_fenced_json(self):
        fenced = f"```json\n{SAMPLE_EXTRACTION_JSON}\n```"
        result = _parse_extraction_json(fenced)
        assert len(result.entities) == 2

    def test_parse_fenced_no_language_tag(self):
        fenced = f"```\n{SAMPLE_EXTRACTION_JSON}\n```"
        result = _parse_extraction_json(fenced)
        assert len(result.entities) == 2

    def test_parse_empty_entities(self):
        result = _parse_extraction_json('{"entities": [], "relationships": []}')
        assert result.entities == []
        assert result.relationships == []

    def test_parse_missing_fields_default(self):
        """Entities missing type/description get defaults."""
        raw = json.dumps({
            "entities": [{"name": "Bob"}],
            "relationships": [],
        })
        result = _parse_extraction_json(raw)
        assert result.entities[0].type == "CONCEPT"
        assert result.entities[0].description == ""

    def test_parse_skips_nameless_entities(self):
        raw = json.dumps({
            "entities": [{"name": ""}, {"type": "PERSON"}],
            "relationships": [],
        })
        result = _parse_extraction_json(raw)
        assert len(result.entities) == 0

    def test_parse_skips_incomplete_relationships(self):
        raw = json.dumps({
            "entities": [],
            "relationships": [
                {"subject": "A"},
                {"subject": "A", "object": "B"},
            ],
        })
        result = _parse_extraction_json(raw)
        # Only the second one has both subject and object
        assert len(result.relationships) == 1

    def test_parse_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_extraction_json("not json at all")


# ---------------------------------------------------------------------------
# OpenAI EntityExtractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEntityExtractor:
    async def test_extract_returns_entities(self):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=SAMPLE_EXTRACTION_JSON))
        ]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor(model="gpt-4o-mini", api_key="sk-test")
        extractor._client = mock_client

        result = await extractor.extract("Alice is CEO of Acme Corp")

        assert isinstance(result, ExtractionResult)
        assert len(result.entities) == 2
        assert result.entities[0].name == "Alice"
        assert len(result.relationships) == 2

    async def test_extract_short_content_returns_empty(self):
        extractor = EntityExtractor(api_key="sk-test")
        result = await extractor.extract("hi")
        assert result.entities == []
        assert result.relationships == []

    async def test_extract_empty_content_returns_empty(self):
        extractor = EntityExtractor(api_key="sk-test")
        result = await extractor.extract("   ")
        assert result.entities == []

    async def test_extract_handles_none_response(self):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content=None))
        ]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor(api_key="sk-test")
        extractor._client = mock_client

        result = await extractor.extract("This is a longer test sentence for entities")
        assert result.entities == []

    async def test_extract_handles_malformed_json(self):
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="not valid json {{{"))
        ]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        extractor = EntityExtractor(api_key="sk-test")
        extractor._client = mock_client

        result = await extractor.extract("This is a longer test sentence for entities")
        assert result.entities == []
        assert result.relationships == []

    async def test_extract_handles_api_error(self):
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=Exception("API timeout")
        )

        extractor = EntityExtractor(api_key="sk-test")
        extractor._client = mock_client

        result = await extractor.extract("This is a longer test sentence for entities")
        assert result.entities == []

    async def test_lazy_client_init(self):
        extractor = EntityExtractor(api_key="sk-test")
        assert extractor._client is None
        with patch("remembra.extraction.entities.AsyncOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = extractor._get_client()
            assert client is not None
            mock_cls.assert_called_once_with(api_key="sk-test")


# ---------------------------------------------------------------------------
# Anthropic EntityExtractor
# ---------------------------------------------------------------------------


def _make_anthropic_extractor(client=None):
    """Create an AnthropicEntityExtractor bypassing __init__ (avoids import anthropic)."""
    extractor = AnthropicEntityExtractor.__new__(AnthropicEntityExtractor)
    extractor._client = client or AsyncMock()
    extractor.model = "claude-sonnet-4-5"
    return extractor


@pytest.mark.asyncio
class TestAnthropicEntityExtractor:
    async def test_extract_returns_entities(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = SAMPLE_EXTRACTION_JSON

        mock_response = MagicMock()
        mock_response.content = [text_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        extractor = _make_anthropic_extractor(mock_client)
        result = await extractor.extract("Alice is CEO of Acme Corp")
        assert len(result.entities) == 2
        assert result.entities[0].name == "Alice"

    async def test_extract_short_content_returns_empty(self):
        extractor = _make_anthropic_extractor()
        result = await extractor.extract("hi")
        assert result.entities == []

    async def test_extract_handles_empty_response(self):
        mock_response = MagicMock()
        mock_response.content = []

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        extractor = _make_anthropic_extractor(mock_client)
        result = await extractor.extract("This is long enough to extract entities from")
        assert result.entities == []


# ---------------------------------------------------------------------------
# Ollama EntityExtractor
# ---------------------------------------------------------------------------


def _make_ollama_extractor(client=None):
    """Create an OllamaEntityExtractor bypassing __init__ (avoids import httpx)."""
    extractor = OllamaEntityExtractor.__new__(OllamaEntityExtractor)
    extractor._base_url = "http://localhost:11434"
    extractor.model = "llama3.1"
    extractor._client = client or AsyncMock()
    return extractor


@pytest.mark.asyncio
class TestOllamaEntityExtractor:
    async def test_extract_returns_entities(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "message": {"content": SAMPLE_EXTRACTION_JSON}
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        extractor = _make_ollama_extractor(mock_client)
        result = await extractor.extract("Alice is CEO of Acme Corp")
        assert len(result.entities) == 2
        assert len(result.relationships) == 2

    async def test_extract_short_content_returns_empty(self):
        extractor = _make_ollama_extractor()
        result = await extractor.extract("hi")
        assert result.entities == []

    async def test_extract_handles_http_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))

        extractor = _make_ollama_extractor(mock_client)
        result = await extractor.extract("This is long enough content for extraction")
        assert result.entities == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestCreateEntityExtractor:
    def test_factory_openai_default(self, mock_settings):
        mock_settings.llm_provider = "openai"
        extractor = create_entity_extractor(mock_settings)
        assert isinstance(extractor, EntityExtractor)

    def test_factory_anthropic(self, mock_settings):
        mock_settings.llm_provider = "anthropic"
        mock_settings.anthropic_api_key = "test-key"
        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            extractor = create_entity_extractor(mock_settings)
        assert isinstance(extractor, AnthropicEntityExtractor)

    def test_factory_ollama(self, mock_settings):
        mock_settings.llm_provider = "ollama"
        extractor = create_entity_extractor(mock_settings)
        assert isinstance(extractor, OllamaEntityExtractor)

    def test_factory_unknown_defaults_to_openai(self, mock_settings):
        mock_settings.llm_provider = "unknown_provider"
        extractor = create_entity_extractor(mock_settings)
        assert isinstance(extractor, EntityExtractor)

    def test_factory_missing_llm_provider_defaults_openai(self, mock_settings):
        # If llm_provider isn't set, getattr defaults to "openai"
        del mock_settings.llm_provider
        mock_settings.llm_provider = MagicMock(side_effect=AttributeError)
        # Use a fresh mock without the attribute
        settings = MagicMock(spec=[])
        settings.extraction_model = "gpt-4o-mini"
        settings.openai_api_key = "test-key"
        extractor = create_entity_extractor(settings)
        assert isinstance(extractor, EntityExtractor)
