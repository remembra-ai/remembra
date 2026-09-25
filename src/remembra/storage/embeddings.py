"""Embedding service — supports OpenAI, Azure OpenAI, Ollama, Cohere, Voyage AI, and Jina."""

from __future__ import annotations

import asyncio
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Literal, NoReturn, TypeVar

import httpx
import structlog

from remembra.config import Settings
from remembra.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from remembra.core.metrics import EMBEDDING_ERRORS
from remembra.core.provider_errors import (
    ProviderErrorKind,
    classify_http_error,
    retry_after_from_headers,
)

log = structlog.get_logger(__name__)

EmbeddingProvider = Literal[
    "openai",
    "azure_openai",
    "ollama",
    "cohere",
    "voyage",
    "jina",
]

# Hard character cap applied before any embedding call. OpenAI's
# text-embedding-3 models reject inputs over 8,192 tokens; ~4 chars/token
# means anything past ~32K chars is guaranteed to 400. We truncate at a
# conservative 24K chars so oversized agent payloads degrade gracefully
# (stored + searchable on their head) instead of failing the whole store.
MAX_EMBED_CHARS = 24_000


# Per-request HTTP timeout for embedding providers (seconds). Kept well under
# typical client timeouts so one slow provider call can't eat a whole request.
DEFAULT_EMBED_TIMEOUT = 20.0
DEFAULT_OLLAMA_TIMEOUT = 60.0

T = TypeVar("T")


def _http_timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(seconds, connect=min(5.0, seconds))


class EmbeddingProviderError(RuntimeError):
    """An upstream embedding provider rejected or failed the request.

    Attributes:
        status_code: upstream HTTP status (None for timeouts / connection errors
            and for circuit-open fast failures).
        kind: a :class:`ProviderErrorKind` value — ``quota_exhausted``,
            ``rate_limited``, ``auth``, ``bad_request`` or ``unavailable`` —
            parsed from the provider's status AND response body, so billing
            exhaustion is never reported as a transient rate limit.
        retry_after: upstream retry hint in seconds (``Retry-After`` /
            ``x-ratelimit-reset-*``), or the breaker's remaining open time.
        provider: provider label (openai, cohere, ...).
        circuit_open: True when the call was rejected locally by the circuit
            breaker without contacting the provider.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        kind: ProviderErrorKind | str | None = None,
        retry_after: float | None = None,
        provider: str | None = None,
        circuit_open: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        resolved = kind if kind is not None else classify_http_error(status_code)
        self.kind = ProviderErrorKind(resolved)
        self.retry_after = retry_after
        self.provider = provider
        self.circuit_open = circuit_open


class EmbeddingConfigError(EmbeddingProviderError, ValueError):
    """The embedding provider is not configured (e.g. missing API key).

    A ValueError for backwards compatibility, and an EmbeddingProviderError
    (kind ``auth``) so API endpoints report it as a 503 operator problem.
    """

    def __init__(self, message: str, provider: str | None = None) -> None:
        super().__init__(message, None, kind=ProviderErrorKind.AUTH, provider=provider)


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        try:
            return response.text[:4000]
        except Exception:
            return None


def _raise_http_error(provider: str, exc: httpx.HTTPStatusError) -> NoReturn:
    """Translate an upstream HTTP error into a typed, sanitized error."""
    response = exc.response
    kind = classify_http_error(response.status_code, _response_body(response))
    retry_after = retry_after_from_headers(response.headers)
    log.error(
        f"{provider}_embedding_http_error",
        status_code=response.status_code,
        kind=kind.value,
        retry_after=retry_after,
    )
    raise EmbeddingProviderError(
        f"Embedding service error (status {response.status_code}, {kind.value})",
        status_code=response.status_code,
        kind=kind,
        retry_after=retry_after,
        provider=provider,
    ) from None


def _raise_request_error(provider: str, exc: httpx.RequestError) -> NoReturn:
    """Connection/timeout errors — never expose URLs."""
    log.error(f"{provider}_embedding_request_error", error_type=type(exc).__name__)
    raise EmbeddingProviderError(
        "Embedding service unavailable",
        kind=ProviderErrorKind.UNAVAILABLE,
        provider=provider,
    ) from None


def _setting_num(settings: Any, name: str, default: float) -> float:
    """Read a numeric setting, tolerating mocks / missing attributes."""
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _truncate_for_embedding(text: str) -> str:
    """Clamp text to the provider-safe embedding size, logging when we do."""
    if len(text) <= MAX_EMBED_CHARS:
        return text
    log.warning(
        "embedding_input_truncated",
        original_chars=len(text),
        truncated_to=MAX_EMBED_CHARS,
    )
    return text[:MAX_EMBED_CHARS]


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
        timeout: float = DEFAULT_EMBED_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.base_url = "https://api.openai.com/v1"
        self._client = httpx.AsyncClient(
            timeout=_http_timeout(timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # Validate input to prevent leaking URLs in error messages
        if not texts or all(not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")

        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
        }
        # text-embedding-3 models support custom dimensions
        if self.dimensions and self.model.startswith("text-embedding-3"):
            payload["dimensions"] = self.dimensions

        try:
            response = await self._client.post(
                f"{self.base_url}/embeddings",
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            _raise_http_error("openai", e)
        except httpx.RequestError as e:
            _raise_request_error("openai", e)

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
        timeout: float = DEFAULT_EMBED_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_version = api_version
        self._client = httpx.AsyncClient(
            timeout=_http_timeout(timeout),
            headers={
                "api-key": self.api_key,
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts or all(not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")

        url = f"{self.endpoint}/openai/deployments/{self.deployment}/embeddings?api-version={self.api_version}"
        try:
            response = await self._client.post(
                url,
                json={"input": texts},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            _raise_http_error("azure", e)
        except httpx.RequestError as e:
            _raise_request_error("azure", e)

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

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout: float = DEFAULT_OLLAMA_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=_http_timeout(timeout))

    async def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        try:
            response = await self._client.post(
                f"{self.base_url}/api/embeddings",
                json={
                    "model": self.model,
                    "prompt": text,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            _raise_http_error("ollama", e)
        except httpx.RequestError as e:
            _raise_request_error("ollama", e)

        data = response.json()

        embedding: list[float] = data["embedding"]
        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts or all(not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")

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

    def __init__(self, api_key: str, model: str = "embed-english-v3.0", timeout: float = DEFAULT_EMBED_TIMEOUT) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.cohere.ai/v1"
        self._client = httpx.AsyncClient(
            timeout=_http_timeout(timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts or all(not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")

        try:
            response = await self._client.post(
                f"{self.base_url}/embed",
                json={
                    "model": self.model,
                    "texts": texts,
                    "input_type": "search_document",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            _raise_http_error("cohere", e)
        except httpx.RequestError as e:
            _raise_request_error("cohere", e)

        data = response.json()

        embeddings: list[list[float]] = data["embeddings"]
        return embeddings

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

    def __init__(self, api_key: str, model: str = "voyage-3", timeout: float = DEFAULT_EMBED_TIMEOUT) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.voyageai.com/v1"
        self._client = httpx.AsyncClient(
            timeout=_http_timeout(timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts or all(not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")

        try:
            response = await self._client.post(
                f"{self.base_url}/embeddings",
                json={
                    "model": self.model,
                    "input": texts,
                    "input_type": "document",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            _raise_http_error("voyage", e)
        except httpx.RequestError as e:
            _raise_request_error("voyage", e)

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

    def __init__(self, api_key: str, model: str = "jina-embeddings-v3", timeout: float = DEFAULT_EMBED_TIMEOUT) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.jina.ai/v1"
        self._client = httpx.AsyncClient(
            timeout=_http_timeout(timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts or all(not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")

        try:
            response = await self._client.post(
                f"{self.base_url}/embeddings",
                json={
                    "model": self.model,
                    "input": texts,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            _raise_http_error("jina", e)
        except httpx.RequestError as e:
            _raise_request_error("jina", e)

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
        # One breaker per service (the app has exactly one EmbeddingService).
        # Quota exhaustion opens it immediately for the long reset window;
        # 5xx / timeouts / 429 open it after ``failure_threshold`` in a row.
        self.breaker = CircuitBreaker(
            name="embeddings",
            failure_threshold=int(_setting_num(settings, "embedding_breaker_failure_threshold", 5)),
            reset_timeout=_setting_num(settings, "embedding_breaker_reset_seconds", 30.0),
            quota_reset_timeout=_setting_num(settings, "provider_quota_reset_seconds", 900.0),
        )

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
                raise EmbeddingConfigError("REMEMBRA_OPENAI_API_KEY is required for OpenAI embeddings", provider="openai")
            self._embedder = OpenAIEmbedder(
                api_key=self.settings.openai_api_key,
                model=model,
                dimensions=self.settings.embedding_dimensions,
                timeout=self._timeout(),
            )
        elif provider == "azure_openai":
            if not self.settings.azure_openai_api_key:
                raise EmbeddingConfigError("REMEMBRA_AZURE_OPENAI_API_KEY is required", provider="azure_openai")
            self._embedder = AzureOpenAIEmbedder(
                api_key=self.settings.azure_openai_api_key,
                endpoint=self.settings.azure_openai_endpoint,
                deployment=self.settings.azure_openai_deployment,
                api_version=self.settings.azure_openai_api_version,
                timeout=self._timeout(),
            )
        elif provider == "ollama":
            self._embedder = OllamaEmbedder(
                base_url=self.settings.ollama_url,
                model=model,
                timeout=max(self._timeout(), DEFAULT_OLLAMA_TIMEOUT),
            )
        elif provider == "cohere":
            if not self.settings.cohere_api_key:
                raise EmbeddingConfigError("REMEMBRA_COHERE_API_KEY is required for Cohere embeddings", provider="cohere")
            self._embedder = CohereEmbedder(
                api_key=self.settings.cohere_api_key,
                model=model,
                timeout=self._timeout(),
            )
        elif provider == "voyage":
            if not self.settings.voyage_api_key:
                raise EmbeddingConfigError("REMEMBRA_VOYAGE_API_KEY is required for Voyage AI embeddings", provider="voyage")
            self._embedder = VoyageEmbedder(
                api_key=self.settings.voyage_api_key,
                model=model,
                timeout=self._timeout(),
            )
        elif provider == "jina":
            if not self.settings.jina_api_key:
                raise EmbeddingConfigError("REMEMBRA_JINA_API_KEY is required for Jina embeddings", provider="jina")
            self._embedder = JinaEmbedder(
                api_key=self.settings.jina_api_key,
                model=model,
                timeout=self._timeout(),
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
        self.breaker.reset()  # failures of the old provider say nothing about the new one

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
        """Generate embedding for a single text.

        Args:
            text: Non-empty text to embed

        Returns:
            Embedding vector

        Raises:
            ValueError: If text is empty or whitespace-only
        """
        # Defensive check: prevent empty strings from reaching external APIs
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        text = _truncate_for_embedding(text)

        # Cache identical single-text embeds (recall queries repeat heavily).
        # Key includes provider/model/dimensions so a provider switch never
        # serves stale vectors of the wrong dimensionality.
        from remembra.core.cache import embedding_cache

        cache_key = hashlib.sha256(
            f"{self._current_provider}|{self._current_model}|{self.dimensions}|{text}".encode()
        ).hexdigest()
        cached: list[float] | None = await embedding_cache.get_by_key(cache_key)
        if cached is not None:
            return cached

        result = await self._guarded(lambda: self._get_embedder().embed(text))
        await embedding_cache.set_by_key(cache_key, result)
        return result

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts (circuit-breaker protected)."""
        texts = [_truncate_for_embedding(t) for t in texts]
        return await self._guarded(lambda: self._get_embedder().embed_batch(texts))

    async def probe(self, text: str = "remembra readiness probe") -> int:
        """Embed a fixed string, bypassing the cache, through the breaker.

        Used by ``/health/ready``'s cached active probe. Returns the vector
        dimension. Raises EmbeddingProviderError on failure.
        """
        vector = await self._guarded(lambda: self._get_embedder().embed(text))
        return len(vector)

    def config_problem(self) -> str | None:
        """Describe a missing-credential problem without calling the provider."""
        try:
            self._get_embedder()
        except EmbeddingConfigError as e:
            return str(e)
        except ValueError as e:
            return str(e)
        return None

    async def _guarded(self, call: Callable[[], Awaitable[T]]) -> T:
        """Run one provider call under the circuit breaker, with typed errors.

        * Breaker open  -> EmbeddingProviderError(circuit_open=True) carrying the
          kind that opened it (so quota stays quota) and the remaining wait.
        * Provider error -> counted by kind in metrics, fed to the breaker.
        """
        try:
            is_probe = self.breaker.acquire()
        except CircuitOpenError as e:
            kind = e.last_error_kind or ProviderErrorKind.UNAVAILABLE.value
            raise EmbeddingProviderError(
                f"Embedding provider unavailable (circuit open: {kind})",
                kind=kind,
                retry_after=e.retry_after,
                provider=self._current_provider,
                circuit_open=True,
            ) from None
        try:
            result = await call()
        except asyncio.CancelledError:
            self.breaker.release(is_probe)
            raise
        except EmbeddingProviderError as e:
            if e.provider is None:
                e.provider = self._current_provider
            EMBEDDING_ERRORS.inc(provider=self._current_provider, kind=e.kind.value)
            self.breaker.record_failure(e, is_probe=is_probe)
            raise
        except BaseException as e:
            self.breaker.record_failure(e, is_probe=is_probe)
            raise
        self.breaker.record_success(is_probe=is_probe)
        return result

    def _timeout(self) -> float:
        return _setting_num(self.settings, "embedding_timeout_seconds", DEFAULT_EMBED_TIMEOUT)

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
