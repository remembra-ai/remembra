"""Tests for the embedding service and all 6 provider classes."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from remembra.storage.embeddings import (
    BaseEmbedder,
    OpenAIEmbedder,
    AzureOpenAIEmbedder,
    OllamaEmbedder,
    CohereEmbedder,
    VoyageEmbedder,
    JinaEmbedder,
    EmbeddingService,
    MODEL_DIMENSIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_VECTOR = [0.1] * 1536

OPENAI_RESPONSE = {
    "data": [{"index": 0, "embedding": FAKE_VECTOR}],
    "usage": {"prompt_tokens": 5, "total_tokens": 5},
}

COHERE_RESPONSE = {"embeddings": [FAKE_VECTOR]}

OLLAMA_RESPONSE = {"embedding": FAKE_VECTOR}


def _mock_client(json_data: dict):
    """Return a mock httpx.AsyncClient whose .post is an AsyncMock."""
    client = AsyncMock()
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    client.post.return_value = resp
    client.aclose = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Provider unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpenAIEmbedder:
    async def test_embed_single(self):
        embedder = OpenAIEmbedder(api_key="sk-test")
        embedder._client = _mock_client(OPENAI_RESPONSE)
        result = await embedder.embed("hello world")
        assert result == FAKE_VECTOR
        embedder._client.post.assert_called_once()

    async def test_embed_batch(self):
        batch_resp = {"data": [{"index": 0, "embedding": FAKE_VECTOR}, {"index": 1, "embedding": FAKE_VECTOR}]}
        embedder = OpenAIEmbedder(api_key="sk-test")
        embedder._client = _mock_client(batch_resp)
        result = await embedder.embed_batch(["hello", "world"])
        assert len(result) == 2

    async def test_close(self):
        embedder = OpenAIEmbedder(api_key="sk-test")
        embedder._client = _mock_client(OPENAI_RESPONSE)
        await embedder.close()
        embedder._client.aclose.assert_called_once()


@pytest.mark.asyncio
class TestAzureOpenAIEmbedder:
    async def test_embed_single(self):
        embedder = AzureOpenAIEmbedder(api_key="az-key", endpoint="https://test.openai.azure.com", deployment="emb")
        embedder._client = _mock_client(OPENAI_RESPONSE)
        result = await embedder.embed("hello")
        assert result == FAKE_VECTOR

    async def test_url_format(self):
        embedder = AzureOpenAIEmbedder(api_key="az-key", endpoint="https://test.openai.azure.com/", deployment="emb")
        embedder._client = _mock_client(OPENAI_RESPONSE)
        await embedder.embed_batch(["test"])
        call_url = embedder._client.post.call_args[0][0]
        assert "/openai/deployments/emb/embeddings" in call_url


@pytest.mark.asyncio
class TestOllamaEmbedder:
    async def test_embed_single(self):
        embedder = OllamaEmbedder(model="nomic-embed-text")
        embedder._client = _mock_client(OLLAMA_RESPONSE)
        result = await embedder.embed("hello")
        assert result == FAKE_VECTOR

    async def test_embed_batch_sequential(self):
        embedder = OllamaEmbedder()
        embedder._client = _mock_client(OLLAMA_RESPONSE)
        result = await embedder.embed_batch(["a", "b", "c"])
        assert len(result) == 3
        assert embedder._client.post.call_count == 3


@pytest.mark.asyncio
class TestCohereEmbedder:
    async def test_embed_batch(self):
        embedder = CohereEmbedder(api_key="co-key")
        embedder._client = _mock_client(COHERE_RESPONSE)
        result = await embedder.embed_batch(["hello"])
        assert result == [FAKE_VECTOR]


@pytest.mark.asyncio
class TestVoyageEmbedder:
    async def test_embed_single(self):
        embedder = VoyageEmbedder(api_key="va-key")
        embedder._client = _mock_client(OPENAI_RESPONSE)
        result = await embedder.embed("hello")
        assert result == FAKE_VECTOR


@pytest.mark.asyncio
class TestJinaEmbedder:
    async def test_embed_single(self):
        embedder = JinaEmbedder(api_key="jina-key")
        embedder._client = _mock_client(OPENAI_RESPONSE)
        result = await embedder.embed("hello")
        assert result == FAKE_VECTOR
        assert "jina.ai" in embedder.base_url


# ---------------------------------------------------------------------------
# EmbeddingService tests
# ---------------------------------------------------------------------------


class TestEmbeddingService:
    def test_dimensions_from_known_model(self, mock_settings):
        service = EmbeddingService(mock_settings)
        assert service.dimensions == 1536

    def test_dimensions_override(self, mock_settings):
        mock_settings.embedding_dimensions = 512
        service = EmbeddingService(mock_settings)
        assert service.dimensions == 512

    def test_switch_provider(self, mock_settings):
        service = EmbeddingService(mock_settings)
        service.switch_provider("voyage", model="voyage-3")
        assert service.provider == "voyage"
        assert service.model == "voyage-3"
        assert service._embedder is None

    def test_get_info(self, mock_settings):
        service = EmbeddingService(mock_settings)
        info = service.get_info()
        assert info["provider"] == "openai"
        assert info["model"] == "text-embedding-3-small"
        assert "openai" in info["supported_providers"]

    def test_unknown_provider_raises(self, mock_settings):
        mock_settings.embedding_provider = "unknown"
        service = EmbeddingService(mock_settings)
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            service._get_embedder()

    def test_missing_api_key_raises(self, mock_settings):
        mock_settings.openai_api_key = None
        service = EmbeddingService(mock_settings)
        with pytest.raises(ValueError, match="REMEMBRA_OPENAI_API_KEY"):
            service._get_embedder()

    @pytest.mark.asyncio
    async def test_close_resets_embedder(self, mock_settings):
        service = EmbeddingService(mock_settings)
        mock_embedder = AsyncMock(spec=BaseEmbedder)
        service._embedder = mock_embedder
        await service.close()
        mock_embedder.close.assert_called_once()
        assert service._embedder is None


class TestModelDimensions:
    def test_known_models_present(self):
        assert "text-embedding-3-small" in MODEL_DIMENSIONS
        assert "embed-english-v3.0" in MODEL_DIMENSIONS
        assert "voyage-3" in MODEL_DIMENSIONS
        assert "jina-embeddings-v3" in MODEL_DIMENSIONS
        assert "nomic-embed-text" in MODEL_DIMENSIONS
