"""Embedding service - supports OpenAI, Ollama, and Cohere."""

from abc import ABC, abstractmethod
from typing import Literal

import httpx
import structlog

from remembra.config import Settings

log = structlog.get_logger(__name__)

EmbeddingProvider = Literal["openai", "ollama", "cohere"]


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


class OpenAIEmbedder(BaseEmbedder):
    """OpenAI embedding provider."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.openai.com/v1"

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": texts,
                },
            )
            response.raise_for_status()
            data = response.json()

        # Sort by index to ensure correct order
        embeddings = sorted(data["data"], key=lambda x: x["index"])
        return [e["embedding"] for e in embeddings]


class OllamaEmbedder(BaseEmbedder):
    """Ollama local embedding provider."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "nomic-embed-text"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
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
        # Ollama doesn't support batch, so we do it sequentially
        results = []
        for text in texts:
            embedding = await self.embed(text)
            results.append(embedding)
        return results


class CohereEmbedder(BaseEmbedder):
    """Cohere embedding provider."""

    def __init__(self, api_key: str, model: str = "embed-english-v3.0"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.cohere.ai/v1"

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/embed",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "texts": texts,
                    "input_type": "search_document",
                },
            )
            response.raise_for_status()
            data = response.json()

        return data["embeddings"]


class EmbeddingService:
    """
    Unified embedding service that delegates to configured provider.
    
    Usage:
        service = EmbeddingService(settings)
        vector = await service.embed("Hello world")
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._embedder: BaseEmbedder | None = None

    def _get_embedder(self) -> BaseEmbedder:
        if self._embedder is not None:
            return self._embedder

        provider = self.settings.embedding_provider.lower()
        log.info("initializing_embedder", provider=provider, model=self.settings.embedding_model)

        if provider == "openai":
            if not self.settings.openai_api_key:
                raise ValueError("REMEMBRA_OPENAI_API_KEY is required for OpenAI embeddings")
            self._embedder = OpenAIEmbedder(
                api_key=self.settings.openai_api_key,
                model=self.settings.embedding_model,
            )
        elif provider == "ollama":
            self._embedder = OllamaEmbedder(
                base_url=self.settings.ollama_url,
                model=self.settings.embedding_model,
            )
        elif provider == "cohere":
            # Would need cohere_api_key in settings
            raise NotImplementedError("Cohere support coming soon")
        else:
            raise ValueError(f"Unknown embedding provider: {provider}")

        return self._embedder

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
        """Return embedding dimensions for current model."""
        return self.settings.embedding_dimensions
