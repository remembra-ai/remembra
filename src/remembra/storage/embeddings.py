"""Embedding service — supports OpenAI, Azure OpenAI, Ollama, Cohere, Voyage AI, and Jina."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

import httpx
import structlog

from remembra.config import Settings

log = structlog.get_logger(__name__)

EmbeddingProvider = Literal[
    "openai", "azure_openai", "ollama", "cohere", "voyage", "jina",
]

# ---------------------------------------------------------------------------
# Known model → dimension mapping (for auto-detection)
# ---------------------------------------------------------------------------

MODEL_DIMENSIONS: dict[str, int] = {
    # OpenAI
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # Cohere
    "embed-english-v3.0": 1024,
    "embed-multilingual-v3.0": 1024,
    "embed-english-light-v3.0": 384,
    "embed-multilingual-light-v3.0": 384,
    # Voyage AI
    "voyage-3": 1024,
    "voyage-3-lite": 512,
    "voyage-code-3": 1024,
    "voyage-large-2-instruct": 1024,
    # Jina
    "jina-embeddings-v3": 1024,
    "jina-embeddings-v2-base-en": 768,
    "jina-embeddings-v2-small-en": 512,
    # Ollama / sentence-transformers
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseEmbedder(ABC):
    """Abstract base for embedding providers."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate embedding vector for text."""
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        ...

    async def close(self) -> None:
        """Close any persistent HTTP clients. Override in subclasses."""
        pass

    @property
    def provider_name(self) -> str:
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIEmbedder(BaseEmbedder):
    """OpenAI embedding provider."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.base_url = "https://api.openai.com/v1"
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.model,
            "input": texts,
        }
        # text-embedding-3 models support custom dimensions
        if self.dimensions and self.model.startswith("text-embedding-3"):
            payload["dimensions"] = self.dimensions

        response = await self._client.post(
            f"{self.base_url}/embeddings",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        embeddings = sorted(data["data"], key=lambda x: x["index"])
        return [e["embedding"] for e in embeddings]

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Azure OpenAI
# ---------------------------------------------------------------------------


class AzureOpenAIEmbedder(BaseEmbedder):
    """Azure OpenAI embedding provider.

    Uses the Azure-specific endpoint format:
      https://{resource}.openai.azure.com/openai/deployments/{deployment}/embeddings?api-version=...
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str,
        deployment: str,
        api_version: str = "2024-02-01",
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_version = api_version
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "api-key": self.api_key,
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/embeddings?api-version={self.api_version}"
        )
        response = await self._client.post(
            url,
            json={"input": texts},
        )
        response.raise_for_status()
        data = response.json()

        embeddings = sorted(data["data"], key=lambda x: x["index"])
        return [e["embedding"] for e in embeddings]

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


class OllamaEmbedder(BaseEmbedder):
    """Ollama local embedding provider."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "nomic-embed-text") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=60.0)

    async def embed(self, text: str) -> list[float]:
        response = await self._client.post(
            f"{self.base_url}/api/embeddings",
            json={
                "model": self.model,
                "prompt": text,
            },
        )
        response.raise_for_status()
        data = response.json()

        return data["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            embedding = await self.embed(text)
            results.append(embedding)
        return results

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Cohere
# ---------------------------------------------------------------------------


class CohereEmbedder(BaseEmbedder):
    """Cohere embedding provider."""

    def __init__(self, api_key: str, model: str = "embed-english-v3.0") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.cohere.ai/v1"
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            f"{self.base_url}/embed",
            json={
                "model": self.model,
                "texts": texts,
                "input_type": "search_document",
            },
        )
        response.raise_for_status()
        data = response.json()

        return data["embeddings"]

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Voyage AI (best-in-class for code embeddings)
# ---------------------------------------------------------------------------


class VoyageEmbedder(BaseEmbedder):
    """Voyage AI embedding provider.

    Voyage offers top-tier code and document embeddings.
    Models: voyage-3, voyage-3-lite, voyage-code-3
    """

    def __init__(self, api_key: str, model: str = "voyage-3") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.voyageai.com/v1"
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            f"{self.base_url}/embeddings",
            json={
                "model": self.model,
                "input": texts,
                "input_type": "document",
            },
        )
        response.raise_for_status()
        data = response.json()

        embeddings = sorted(data["data"], key=lambda x: x["index"])
        return [e["embedding"] for e in embeddings]

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Jina AI (multilingual, long-context embeddings)
# ---------------------------------------------------------------------------


class JinaEmbedder(BaseEmbedder):
    """Jina AI embedding provider.

    Jina offers multilingual embeddings with long context windows (8192 tokens).
    Models: jina-embeddings-v3, jina-embeddings-v2-base-en
    """

    def __init__(self, api_key: str, model: str = "jina-embeddings-v3") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.jina.ai/v1"
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            f"{self.base_url}/embeddings",
            json={
                "model": self.model,
                "input": texts,
            },
        )
        response.raise_for_status()
        data = response.json()

        embeddings = sorted(data["data"], key=lambda x: x["index"])
        return [e["embedding"] for e in embeddings]

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Unified Embedding Service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """Unified embedding service that delegates to configured provider.

    Supports hot-swapping: change provider/model at runtime and trigger
    re-indexing of all memories.

    Usage::

        service = EmbeddingService(settings)
        vector = await service.embed("Hello world")

        # Switch provider at runtime
        service.switch_provider("voyage", model="voyage-code-3", api_key="...")
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._embedder: BaseEmbedder | None = None
        # Track current config for change detection
        self._current_provider: str = settings.embedding_provider
        self._current_model: str = settings.embedding_model

    @property
    def provider(self) -> str:
        return self._current_provider

    @property
    def model(self) -> str:
        return self._current_model

    def _get_embedder(self) -> BaseEmbedder:
        if self._embedder is not None:
            return self._embedder

        provider = self._current_provider.lower()
        model = self._current_model
        log.info("initializing_embedder", provider=provider, model=model)

        if provider == "openai":
            if not self.settings.openai_api_key:
                raise ValueError("REMEMBRA_OPENAI_API_KEY is required for OpenAI embeddings")
            self._embedder = OpenAIEmbedder(
                api_key=self.settings.openai_api_key,
                model=model,
                dimensions=self.settings.embedding_dimensions,
            )
        elif provider == "azure_openai":
            if not self.settings.azure_openai_api_key:
                raise ValueError("REMEMBRA_AZURE_OPENAI_API_KEY is required")
            self._embedder = AzureOpenAIEmbedder(
                api_key=self.settings.azure_openai_api_key,
                endpoint=self.settings.azure_openai_endpoint,
                deployment=self.settings.azure_openai_deployment,
                api_version=self.settings.azure_openai_api_version,
            )
        elif provider == "ollama":
            self._embedder = OllamaEmbedder(
                base_url=self.settings.ollama_url,
                model=model,
            )
        elif provider == "cohere":
            if not self.settings.cohere_api_key:
                raise ValueError("REMEMBRA_COHERE_API_KEY is required for Cohere embeddings")
            self._embedder = CohereEmbedder(
                api_key=self.settings.cohere_api_key,
                model=model,
            )
        elif provider == "voyage":
            if not self.settings.voyage_api_key:
                raise ValueError("REMEMBRA_VOYAGE_API_KEY is required for Voyage AI embeddings")
            self._embedder = VoyageEmbedder(
                api_key=self.settings.voyage_api_key,
                model=model,
            )
        elif provider == "jina":
            if not self.settings.jina_api_key:
                raise ValueError("REMEMBRA_JINA_API_KEY is required for Jina embeddings")
            self._embedder = JinaEmbedder(
                api_key=self.settings.jina_api_key,
                model=model,
            )
        else:
            raise ValueError(f"Unknown embedding provider: {provider}")

        return self._embedder

    async def close(self) -> None:
        """Close the current embedder's HTTP client."""
        if self._embedder is not None:
            await self._embedder.close()
            self._embedder = None

    def switch_provider(
        self,
        provider: str,
        model: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Hot-swap embedding provider at runtime.

        After switching, call ``reindex_all()`` on the ReindexManager to
        re-embed all stored memories with the new model.

        Note: The old embedder's HTTP client will be closed lazily on next
        ``_get_embedder()`` call. For immediate cleanup, call ``close()`` first.

        Args:
            provider: New provider name (openai, voyage, jina, etc.)
            model: Model name (uses provider default if omitted)
            api_key: API key override (uses env var if omitted)
            **kwargs: Extra provider-specific options
        """
        old = f"{self._current_provider}/{self._current_model}"
        self._current_provider = provider.lower()
        if model:
            self._current_model = model
        # Note: old embedder's client will be garbage collected.
        # For explicit cleanup, call close() before switch_provider().
        self._embedder = None  # Force re-init on next embed call

        # Store API key override if provided
        if api_key:
            provider_lower = provider.lower()
            if provider_lower == "openai":
                self.settings.openai_api_key = api_key
            elif provider_lower == "voyage":
                self.settings.voyage_api_key = api_key
            elif provider_lower == "jina":
                self.settings.jina_api_key = api_key
            elif provider_lower == "cohere":
                self.settings.cohere_api_key = api_key

        new = f"{self._current_provider}/{self._current_model}"
        log.info("embedding_provider_switched", old=old, new=new)

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        embedder = self._get_embedder()
        return await embedder.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        embedder = self._get_embedder()
        return await embedder.embed_batch(texts)

    @property
    def dimensions(self) -> int:
        """Return embedding dimensions for current model.

        Uses settings override first, then known model dimensions,
        then falls back to the settings default.
        """
        if self.settings.embedding_dimensions:
            return self.settings.embedding_dimensions
        return MODEL_DIMENSIONS.get(self._current_model, 1536)

    def get_info(self) -> dict[str, Any]:
        """Return current embedding provider information."""
        return {
            "provider": self._current_provider,
            "model": self._current_model,
            "dimensions": self.dimensions,
            "supported_providers": list(EmbeddingProvider.__args__),  # type: ignore[attr-defined]
        }
