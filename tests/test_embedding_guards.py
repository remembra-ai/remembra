"""Tests for embedding input guards, typed provider errors, and the embed cache.

Covers the fixes for the intermittent store-500s: oversized content is
truncated instead of failing, upstream provider errors carry their status
code so endpoints can map them honestly, and identical single-text embeds
are served from cache.
"""

from __future__ import annotations

import httpx
import pytest

from remembra.config import Settings
from remembra.storage.embeddings import (
    MAX_EMBED_CHARS,
    BaseEmbedder,
    EmbeddingProviderError,
    EmbeddingService,
    OpenAIEmbedder,
    _truncate_for_embedding,
)


# ---------------------------------------------------------------------------
# Truncation guard
# ---------------------------------------------------------------------------


def test_truncate_noop_under_limit() -> None:
    text = "x" * 100
    assert _truncate_for_embedding(text) == text


def test_truncate_clamps_oversized_input() -> None:
    text = "y" * (MAX_EMBED_CHARS + 5000)
    out = _truncate_for_embedding(text)
    assert len(out) == MAX_EMBED_CHARS
    assert out == text[:MAX_EMBED_CHARS]


@pytest.mark.asyncio
async def test_service_embed_truncates_before_provider() -> None:
    """Oversized content must reach the provider already clamped."""
    seen: list[str] = []

    class SpyEmbedder(BaseEmbedder):
        async def embed(self, text: str) -> list[float]:
            seen.append(text)
            return [0.1, 0.2]

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            seen.extend(texts)
            return [[0.1, 0.2] for _ in texts]

    service = EmbeddingService(Settings(openai_api_key="test"))
    service._embedder = SpyEmbedder()

    await service.embed("z" * (MAX_EMBED_CHARS + 1))
    assert len(seen[-1]) == MAX_EMBED_CHARS

    await service.embed_batch(["ok", "w" * (MAX_EMBED_CHARS * 2)])
    assert len(seen[-1]) == MAX_EMBED_CHARS


# ---------------------------------------------------------------------------
# Typed upstream errors
# ---------------------------------------------------------------------------


def _mock_openai_transport(status_code: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "boom"}})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [400, 429, 500])
async def test_openai_http_error_raises_typed_error(upstream_status: int) -> None:
    embedder = OpenAIEmbedder(api_key="test")
    embedder._client = httpx.AsyncClient(
        transport=_mock_openai_transport(upstream_status),
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(EmbeddingProviderError) as exc_info:
        await embedder.embed("hello")

    assert exc_info.value.status_code == upstream_status
    # Must stay a RuntimeError subclass so existing broad handlers keep working
    assert isinstance(exc_info.value, RuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [400, 429, 500])
async def test_all_providers_raise_typed_error(upstream_status: int) -> None:
    """Every embedding provider — not just OpenAI — must raise the typed error
    carrying the upstream status, so store/recall map it to an honest 429/502
    instead of an opaque 500 (regression guard for F2)."""
    from remembra.storage.embeddings import (
        AzureOpenAIEmbedder,
        CohereEmbedder,
        JinaEmbedder,
        OllamaEmbedder,
        VoyageEmbedder,
    )

    providers = [
        OllamaEmbedder(),
        CohereEmbedder(api_key="t"),
        VoyageEmbedder(api_key="t"),
        JinaEmbedder(api_key="t"),
        AzureOpenAIEmbedder(api_key="t", endpoint="https://x.openai.azure.com", deployment="d"),
    ]
    for emb in providers:
        emb._client = httpx.AsyncClient(
            transport=_mock_openai_transport(upstream_status),
            base_url=getattr(emb, "base_url", "https://example.test"),
        )
        with pytest.raises(EmbeddingProviderError) as exc_info:
            await emb.embed("hello")
        assert exc_info.value.status_code == upstream_status, type(emb).__name__


@pytest.mark.asyncio
async def test_openai_connection_error_raises_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    embedder = OpenAIEmbedder(api_key="test")
    embedder._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(EmbeddingProviderError) as exc_info:
        await embedder.embed("hello")
    assert exc_info.value.status_code is None


# ---------------------------------------------------------------------------
# Embed cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_text_embed_is_cached() -> None:
    calls = {"n": 0}

    class CountingEmbedder(BaseEmbedder):
        async def embed(self, text: str) -> list[float]:
            calls["n"] += 1
            return [1.0, 2.0, 3.0]

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            calls["n"] += len(texts)
            return [[1.0, 2.0, 3.0] for _ in texts]

    service = EmbeddingService(Settings(openai_api_key="test"))
    service._embedder = CountingEmbedder()

    text = "cache me — unique text for test_single_text_embed_is_cached"
    first = await service.embed(text)
    second = await service.embed(text)

    assert first == second == [1.0, 2.0, 3.0]
    assert calls["n"] == 1, "second embed of identical text must hit the cache"


@pytest.mark.asyncio
async def test_cache_key_isolates_models() -> None:
    """Switching models must never serve vectors cached under another model."""

    class StaticEmbedder(BaseEmbedder):
        def __init__(self, vec: list[float]) -> None:
            self.vec = vec

        async def embed(self, text: str) -> list[float]:
            return self.vec

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [self.vec for _ in texts]

    text = "same text, different model — isolation test"

    service = EmbeddingService(Settings(openai_api_key="test"))
    service._embedder = StaticEmbedder([1.0])
    assert await service.embed(text) == [1.0]

    service._current_model = "text-embedding-3-large"
    service._embedder = StaticEmbedder([2.0])
    assert await service.embed(text) == [2.0], "cache must not leak across models"
