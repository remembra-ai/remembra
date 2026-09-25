"""REL-1 / REL-3 / REL-17: typed provider errors, the embedding circuit breaker,
and honest API status mapping.

All provider traffic is mocked with ``httpx.MockTransport`` — no real paid calls.
"""

from __future__ import annotations

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("REMEMBRA_AUTH_ENABLED", "false")
os.environ.setdefault("REMEMBRA_RATE_LIMIT_ENABLED", "false")

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from remembra.config import Settings
from remembra.core.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from remembra.core.metrics import EMBEDDING_ERRORS, STORE_FAILURES
from remembra.core.provider_errors import (
    ProviderErrorKind,
    classify_http_error,
    parse_duration_seconds,
    retry_after_from_headers,
)
from remembra.storage.embeddings import (
    AzureOpenAIEmbedder,
    CohereEmbedder,
    EmbeddingConfigError,
    EmbeddingProviderError,
    EmbeddingService,
    JinaEmbedder,
    OllamaEmbedder,
    OpenAIEmbedder,
    VoyageEmbedder,
)

OPENAI_QUOTA_BODY = {
    "error": {
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "type": "insufficient_quota",
        "param": None,
        "code": "insufficient_quota",
    }
}
OPENAI_RATE_BODY = {
    "error": {
        "message": "Rate limit reached for text-embedding-3-small on requests per min (RPM): Limit 3000.",
        "type": "requests",
        "code": "rate_limit_exceeded",
    }
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, OPENAI_QUOTA_BODY, ProviderErrorKind.QUOTA_EXHAUSTED),
        (429, OPENAI_RATE_BODY, ProviderErrorKind.RATE_LIMITED),
        (429, None, ProviderErrorKind.RATE_LIMITED),
        (402, {"detail": "Insufficient balance"}, ProviderErrorKind.QUOTA_EXHAUSTED),
        (403, {"message": "insufficient balance on account"}, ProviderErrorKind.QUOTA_EXHAUSTED),
        (401, {"error": {"message": "Incorrect API key provided"}}, ProviderErrorKind.AUTH),
        (403, {"error": {"message": "forbidden"}}, ProviderErrorKind.AUTH),
        (400, {"error": {"message": "maximum context length"}}, ProviderErrorKind.BAD_REQUEST),
        (404, "model not found", ProviderErrorKind.BAD_REQUEST),
        (500, None, ProviderErrorKind.UNAVAILABLE),
        (503, "overloaded", ProviderErrorKind.UNAVAILABLE),
        (None, None, ProviderErrorKind.UNAVAILABLE),
    ],
)
def test_classify_http_error(status, body, expected) -> None:
    assert classify_http_error(status, body) == expected


def test_parse_retry_hints() -> None:
    assert parse_duration_seconds("7") == 7.0
    assert parse_duration_seconds("6m0s") == 360.0
    assert parse_duration_seconds("20ms") == pytest.approx(0.02)
    assert parse_duration_seconds("1h2m3s") == 3723.0
    assert parse_duration_seconds("garbage") is None
    assert parse_duration_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past
    assert retry_after_from_headers(httpx.Headers({"retry-after-ms": "1500"})) == 1.5
    assert retry_after_from_headers(httpx.Headers({"x-ratelimit-reset-requests": "2s"})) == 2.0
    assert retry_after_from_headers(httpx.Headers({})) is None


def _transport(status: int, body, headers=None, counter: list[int] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter.append(1)
        return httpx.Response(status, json=body, headers=headers or {})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_every_provider_reports_quota_exhausted_from_body() -> None:
    providers = [
        OpenAIEmbedder(api_key="t"),
        AzureOpenAIEmbedder(api_key="t", endpoint="https://x.openai.azure.com", deployment="d"),
        OllamaEmbedder(),
        CohereEmbedder(api_key="t"),
        VoyageEmbedder(api_key="t"),
        JinaEmbedder(api_key="t"),
    ]
    for emb in providers:
        emb._client = httpx.AsyncClient(transport=_transport(429, OPENAI_QUOTA_BODY, {"retry-after": "20"}))
        with pytest.raises(EmbeddingProviderError) as exc_info:
            await emb.embed("hello")
        err = exc_info.value
        assert err.kind == ProviderErrorKind.QUOTA_EXHAUSTED, type(emb).__name__
        assert err.status_code == 429
        assert err.retry_after == 20.0
        # The sanitized message never leaks the provider body.
        assert "billing" not in str(err)


@pytest.mark.asyncio
async def test_rate_limit_keeps_upstream_retry_after() -> None:
    emb = OpenAIEmbedder(api_key="t")
    emb._client = httpx.AsyncClient(transport=_transport(429, OPENAI_RATE_BODY, {"retry-after": "12"}))
    with pytest.raises(EmbeddingProviderError) as exc_info:
        await emb.embed("hello")
    assert exc_info.value.kind == ProviderErrorKind.RATE_LIMITED
    assert exc_info.value.retry_after == 12.0


@pytest.mark.asyncio
async def test_timeout_is_unavailable_and_timeouts_are_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    emb = OpenAIEmbedder(api_key="t", timeout=7.0)
    assert emb._client.timeout.read == 7.0
    assert emb._client.timeout.connect == 5.0
    emb._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingProviderError) as exc_info:
        await emb.embed("hello")
    assert exc_info.value.kind == ProviderErrorKind.UNAVAILABLE

    service = EmbeddingService(Settings(openai_api_key="t", embedding_timeout_seconds=3.5))
    assert service._get_embedder()._client.timeout.read == 3.5  # type: ignore[attr-defined]


def test_missing_key_is_config_error_with_auth_kind() -> None:
    service = EmbeddingService(Settings(openai_api_key=None))
    with pytest.raises(EmbeddingConfigError) as exc_info:
        service._get_embedder()
    assert isinstance(exc_info.value, ValueError)
    assert exc_info.value.kind == ProviderErrorKind.AUTH
    assert "REMEMBRA_OPENAI_API_KEY" in (service.config_problem() or "")


# ---------------------------------------------------------------------------
# Circuit breaker state machine
# ---------------------------------------------------------------------------


def _err(kind: ProviderErrorKind, retry_after: float | None = None) -> EmbeddingProviderError:
    return EmbeddingProviderError("x", kind=kind, retry_after=retry_after)


def test_quota_opens_immediately_with_long_reset() -> None:
    clock = FakeClock()
    b = CircuitBreaker("t-quota", failure_threshold=5, reset_timeout=30, quota_reset_timeout=900, clock=clock)
    b.record_failure(_err(ProviderErrorKind.QUOTA_EXHAUSTED))
    assert b.state == CircuitState.OPEN
    assert b.retry_after() == pytest.approx(900)
    with pytest.raises(CircuitOpenError) as exc_info:
        b.acquire()
    assert exc_info.value.last_error_kind == "quota_exhausted"
    clock.now += 899
    assert b.state == CircuitState.OPEN
    clock.now += 2
    assert b.state == CircuitState.HALF_OPEN


def test_only_provider_health_errors_count() -> None:
    b = CircuitBreaker("t-count", failure_threshold=3, clock=FakeClock())
    for _ in range(10):
        b.record_failure(_err(ProviderErrorKind.BAD_REQUEST))
        b.record_failure(_err(ProviderErrorKind.AUTH))
        b.record_failure(ValueError("empty"))
        b.record_failure(KeyError("x"))
    assert b.state == CircuitState.CLOSED
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    b.record_failure(TimeoutError())
    assert b.state == CircuitState.CLOSED
    b.record_failure(_err(ProviderErrorKind.RATE_LIMITED))
    assert b.state == CircuitState.OPEN


def test_success_resets_consecutive_failures() -> None:
    b = CircuitBreaker("t-reset", failure_threshold=3, clock=FakeClock())
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    b.record_success()
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    assert b.state == CircuitState.CLOSED


def test_half_open_allows_exactly_one_probe() -> None:
    clock = FakeClock()
    b = CircuitBreaker("t-probe", failure_threshold=1, reset_timeout=10, clock=clock)
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    clock.now += 11
    assert b.allows_requests()
    first = b.acquire()
    assert first is True
    assert not b.allows_requests()
    with pytest.raises(CircuitOpenError):
        b.acquire()  # second concurrent caller is rejected while the probe is out
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE), is_probe=True)
    assert b.state == CircuitState.OPEN  # failed probe re-opens
    clock.now += 11
    assert b.acquire() is True
    b.record_success(is_probe=True)
    assert b.state == CircuitState.CLOSED
    assert b.acquire() is False


def test_ignored_probe_failure_releases_probe_slot() -> None:
    clock = FakeClock()
    b = CircuitBreaker("t-release", failure_threshold=1, reset_timeout=10, clock=clock)
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    clock.now += 11
    assert b.acquire() is True
    b.record_failure(_err(ProviderErrorKind.BAD_REQUEST), is_probe=True)  # says nothing about health
    assert b.state == CircuitState.HALF_OPEN
    assert b.acquire() is True  # another probe may go


def test_rate_limit_open_honors_longer_upstream_hint() -> None:
    clock = FakeClock()
    b = CircuitBreaker("t-hint", failure_threshold=1, reset_timeout=30, clock=clock)
    b.record_failure(_err(ProviderErrorKind.RATE_LIMITED, retry_after=120))
    assert b.retry_after() == pytest.approx(120)


def test_breaker_is_thread_safe_under_concurrent_failures() -> None:
    b = CircuitBreaker("t-threads", failure_threshold=500, clock=FakeClock())
    barrier = threading.Barrier(8)

    def hammer() -> None:
        barrier.wait()
        for _ in range(100):
            b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.stats.failed_calls == 800
    assert b.state == CircuitState.OPEN
    assert b.stats.state_changes == 1  # opened exactly once


@pytest.mark.asyncio
async def test_concurrent_async_probe_single_flight() -> None:
    clock = FakeClock()
    b = CircuitBreaker("t-async", failure_threshold=1, reset_timeout=5, clock=clock)
    b.record_failure(_err(ProviderErrorKind.UNAVAILABLE))
    clock.now += 6
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ok"

    probe = asyncio.create_task(b.call(slow))
    await started.wait()
    results = await asyncio.gather(*(b.call(slow) for _ in range(5)), return_exceptions=True)
    assert all(isinstance(r, CircuitOpenError) for r in results)
    release.set()
    assert await probe == "ok"
    assert calls == 1
    assert b.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# EmbeddingService wiring
# ---------------------------------------------------------------------------


def _service_with_transport(transport: httpx.MockTransport, **settings) -> EmbeddingService:
    service = EmbeddingService(Settings(openai_api_key="t", **settings))
    embedder = service._get_embedder()
    embedder._client = httpx.AsyncClient(transport=transport)  # type: ignore[attr-defined]
    return service


@pytest.mark.asyncio
async def test_service_quota_opens_breaker_and_stops_calling_provider() -> None:
    calls: list[int] = []
    service = _service_with_transport(_transport(429, OPENAI_QUOTA_BODY, counter=calls))
    before = EMBEDDING_ERRORS.get(provider="openai", kind="quota_exhausted")

    with pytest.raises(EmbeddingProviderError) as first:
        await service.embed("first text")
    assert first.value.kind == ProviderErrorKind.QUOTA_EXHAUSTED
    assert first.value.circuit_open is False
    assert service.breaker.state == CircuitState.OPEN

    for i in range(3):
        with pytest.raises(EmbeddingProviderError) as fast:
            await service.embed_batch([f"text {i}"])
        assert fast.value.circuit_open is True
        assert fast.value.kind == ProviderErrorKind.QUOTA_EXHAUSTED  # stays quota, not "unavailable"
        assert fast.value.retry_after and fast.value.retry_after > 800

    assert len(calls) == 1  # only the first call reached the provider
    assert EMBEDDING_ERRORS.get(provider="openai", kind="quota_exhausted") == before + 1


@pytest.mark.asyncio
async def test_service_recovers_after_probe_success() -> None:
    state = {"fail": True}
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if state["fail"]:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    clock = FakeClock()
    service = _service_with_transport(httpx.MockTransport(handler), embedding_breaker_failure_threshold=2)
    service.breaker._clock = clock
    for _ in range(2):
        with pytest.raises(EmbeddingProviderError):
            await service.embed_batch(["a"])
    assert service.breaker.state == CircuitState.OPEN
    state["fail"] = False
    with pytest.raises(EmbeddingProviderError):
        await service.embed_batch(["a"])  # still open, no provider call
    assert len(calls) == 2
    clock.now += 31
    assert await service.embed_batch(["a"]) == [[0.1, 0.2]]
    assert service.breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_bad_input_does_not_trip_breaker() -> None:
    service = _service_with_transport(_transport(400, {"error": {"message": "too long"}}), embedding_breaker_failure_threshold=2)
    for _ in range(5):
        with pytest.raises(EmbeddingProviderError) as exc_info:
            await service.embed_batch(["a"])
        assert exc_info.value.kind == ProviderErrorKind.BAD_REQUEST
    assert service.breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_switch_provider_resets_breaker() -> None:
    service = _service_with_transport(_transport(429, OPENAI_QUOTA_BODY))
    with pytest.raises(EmbeddingProviderError):
        await service.embed("x")
    assert service.breaker.state == CircuitState.OPEN
    service.switch_provider("ollama", model="nomic-embed-text")
    assert service.breaker.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# API mapping through the real store/recall runtime path
# ---------------------------------------------------------------------------


@pytest.fixture()
async def api_with_real_embeddings(tmp_path):
    """App wired with a real MemoryService + Database + EmbeddingService.

    Only Qdrant is mocked; the embedding provider is an httpx MockTransport
    the test controls.
    """
    import remembra.config
    import remembra.main as main
    from remembra.security.audit import AuditLogger
    from remembra.security.sanitizer import ContentSanitizer
    from remembra.services.memory import MemoryService
    from remembra.storage.database import Database

    remembra.config._settings = None
    settings = Settings(
        openai_api_key="t",
        smart_extraction_enabled=False,
        async_enrichment=False,
        enable_source_records=False,
        enable_entity_resolution=False,
        enable_reranking=False,
    )
    db = Database(str(tmp_path / "rel.db"))
    await db.connect()
    await db.init_schema()
    qdrant = MagicMock()
    qdrant.search = AsyncMock(return_value=[])
    qdrant.upsert = AsyncMock()
    embeddings = EmbeddingService(settings)
    service = MemoryService(settings=settings, qdrant=qdrant, db=db, embeddings=embeddings)

    audit = MagicMock(spec=AuditLogger)
    audit.log_memory_store = AsyncMock()
    audit.log_memory_recall = AsyncMock()
    app = main.app
    app.state.memory_service = service
    app.state.audit_logger = audit
    app.state.sanitizer = ContentSanitizer()
    app.state.pii_detector = None
    app.state.anomaly_detector = None
    app.state.usage_meter = None
    app.state.webhook_manager = None

    def set_transport(transport: httpx.MockTransport) -> None:
        embedder = embeddings._get_embedder()
        embedder._client = httpx.AsyncClient(transport=transport)  # type: ignore[attr-defined]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, set_transport, embeddings, qdrant
    await db.close()


@pytest.mark.asyncio
async def test_store_quota_exhausted_returns_503_with_quota_message(api_with_real_embeddings) -> None:
    client, set_transport, embeddings, qdrant = api_with_real_embeddings
    set_transport(_transport(429, OPENAI_QUOTA_BODY, {"retry-after": "3"}))
    before = STORE_FAILURES.get(reason="embedding_quota_exhausted")

    r = await client.post("/api/v1/memories", json={"content": "Mani likes jerk chicken", "skip_extraction": True})
    assert r.status_code == 503, r.text
    assert "out of quota" in r.json()["detail"]
    assert "not stored" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) >= 900  # never the upstream "3"
    assert r.headers["X-Remembra-Error-Kind"] == "quota_exhausted"
    qdrant.upsert.assert_not_called()
    assert STORE_FAILURES.get(reason="embedding_quota_exhausted") == before + 1

    # Breaker is now open: recall fails fast with the same honest message.
    r = await client.post("/api/v1/memories/recall", json={"query": "what does Mani like?"})
    assert r.status_code == 503
    assert "out of quota" in r.json()["detail"]
    assert embeddings.breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_recall_rate_limited_returns_429_with_upstream_retry_after(api_with_real_embeddings) -> None:
    client, set_transport, _, _ = api_with_real_embeddings
    set_transport(_transport(429, OPENAI_RATE_BODY, {"retry-after": "17"}))
    r = await client.post("/api/v1/memories/recall", json={"query": "anything"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "17"
    assert r.headers["X-Remembra-Error-Kind"] == "rate_limited"


@pytest.mark.asyncio
async def test_store_auth_error_returns_503(api_with_real_embeddings) -> None:
    client, set_transport, _, _ = api_with_real_embeddings
    set_transport(_transport(401, {"error": {"message": "Incorrect API key provided: sk-..."}}))
    r = await client.post("/api/v1/memories", json={"content": "a fact worth keeping", "skip_extraction": True})
    assert r.status_code == 503
    assert "credentials" in r.json()["detail"]
    assert "sk-" not in r.text
    assert int(r.headers["Retry-After"]) >= 900


@pytest.mark.asyncio
async def test_app_wide_handler_maps_embedding_errors_on_other_routes() -> None:
    """Routes that don't catch EmbeddingProviderError still get the honest mapping."""
    import remembra.main as main
    from fastapi import FastAPI

    probe_app = FastAPI()
    main.register_provider_error_handlers(probe_app)

    @probe_app.get("/boom")
    async def boom() -> None:
        raise EmbeddingProviderError("x", 429, kind=ProviderErrorKind.QUOTA_EXHAUSTED)

    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://t") as client:
        r = await client.get("/boom")
    assert r.status_code == 503
    assert r.json()["error"]["kind"] == "quota_exhausted"
