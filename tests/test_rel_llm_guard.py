"""REL-7 / REL-17: extraction + consolidation LLM clients share a circuit breaker,
classify errors by kind, retry at most once, and mark their fallbacks.

The real OpenAI SDK is used end-to-end; only the HTTP transport is mocked.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from remembra.core.circuit_breaker import CircuitState
from remembra.core.llm_guard import (
    classify_llm_exception,
    get_llm_breaker,
    llm_fallback_scope,
    make_llm_client,
)
from remembra.extraction.consolidator import ConsolidationAction, ExistingMemory, MemoryConsolidator
from remembra.extraction.extractor import ExtractionConfig, FactExtractor

CONTENT = "Mani moved to Lisbon in 2024. She runs a small bakery."
FAST_RETRY = {"retry-after-ms": "1"}


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


class Upstream:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(self.status, json=self.body, headers=FAST_RETRY)

        return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _reset_llm_breaker():
    get_llm_breaker().reset()
    yield
    get_llm_breaker().reset()


def _extractor(upstream: Upstream) -> FactExtractor:
    extractor = FactExtractor(ExtractionConfig(api_key="t"))
    extractor._client = make_llm_client("t", inner_transport=upstream.transport())
    return extractor


def test_client_has_low_retries_and_bounded_timeout() -> None:
    client = make_llm_client("t")
    assert client.max_retries == 1
    assert client.timeout == 20.0
    extractor = FactExtractor(ExtractionConfig(api_key="t"))
    assert extractor._get_client().max_retries == 1
    assert extractor.config.timeout == 20.0


async def test_quota_opens_llm_breaker_immediately_and_stops_http_calls() -> None:
    upstream = Upstream(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})
    extractor = _extractor(upstream)

    with llm_fallback_scope() as fallbacks:
        facts = await extractor.extract(CONTENT)
    assert facts == ["Mani moved to Lisbon in 2024.", "She runs a small bakery."]
    assert fallbacks == {"extraction": "quota_exhausted"}
    assert get_llm_breaker().state == CircuitState.OPEN
    assert upstream.calls == 1  # the SDK's retry was refused locally by the open breaker

    with llm_fallback_scope() as fallbacks:
        await extractor.extract(CONTENT)
    assert upstream.calls == 1  # fail fast: nothing left the process
    assert fallbacks == {"extraction": "quota_exhausted"}


async def test_5xx_counts_toward_breaker_with_single_retry() -> None:
    upstream = Upstream(503, {"error": {"message": "overloaded"}})
    extractor = _extractor(upstream)
    for _ in range(2):
        with llm_fallback_scope() as fallbacks:
            await extractor.extract(CONTENT)
        assert fallbacks == {"extraction": "unavailable"}
    assert upstream.calls == 4  # 1 try + 1 retry each (SDK default would be 3 each)
    assert get_llm_breaker().state == CircuitState.CLOSED
    await extractor.extract(CONTENT)  # 5th consecutive failure opens it
    assert get_llm_breaker().state == CircuitState.OPEN


async def test_consolidator_auth_error_falls_back_without_tripping_breaker() -> None:
    upstream = Upstream(401, {"error": {"message": "Incorrect API key provided"}})
    consolidator = MemoryConsolidator(api_key="t")
    consolidator._client = make_llm_client("t", inner_transport=upstream.transport())
    existing = [ExistingMemory(id="m1", content="Mani lives in Lisbon", score=0.9)]
    with llm_fallback_scope() as fallbacks:
        result = await consolidator.consolidate("Mani lives in Lisbon, Portugal", existing)
    assert result.action == ConsolidationAction.ADD
    assert fallbacks == {"consolidation": "auth"}
    assert get_llm_breaker().state == CircuitState.CLOSED
    assert upstream.calls == 1  # 401 is not retried


async def test_success_path_unchanged_and_no_fallback_marked() -> None:
    upstream = Upstream(200, _completion(json.dumps({"facts": ["Mani moved to Lisbon in 2024"]})))
    extractor = _extractor(upstream)
    with llm_fallback_scope() as fallbacks:
        facts = await extractor.extract(CONTENT)
    assert facts == ["Mani moved to Lisbon in 2024"]
    assert fallbacks == {}
    assert get_llm_breaker().last_success_at is not None


async def test_bad_json_response_marks_fallback() -> None:
    upstream = Upstream(200, _completion("not json"))
    extractor = _extractor(upstream)
    with llm_fallback_scope() as fallbacks:
        await extractor.extract(CONTENT)
    assert fallbacks == {"extraction": "bad_response"}


def test_classify_llm_exception_from_sdk_errors() -> None:
    import openai

    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    quota = openai.RateLimitError(
        "quota",
        response=httpx.Response(429, request=req),
        body={"code": "insufficient_quota", "message": "You exceeded your current quota"},
    )
    rate = openai.RateLimitError("rate", response=httpx.Response(429, request=req), body={"code": "rate_limit_exceeded"})
    assert classify_llm_exception(quota) == "quota_exhausted"
    assert classify_llm_exception(rate) == "rate_limited"
    assert classify_llm_exception(openai.APITimeoutError(request=req)) == "unavailable"
    assert classify_llm_exception(ValueError("x")) == "internal"


async def test_a_client_built_with_its_own_breaker_feeds_that_breaker_and_not_llm() -> None:
    from remembra.core.circuit_breaker import CircuitBreaker

    own = CircuitBreaker("llm_test_own", failure_threshold=1, reset_timeout=30.0)
    upstream = Upstream(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})
    client = make_llm_client("t", inner_transport=upstream.transport(), breaker=own, max_retries=0)
    with pytest.raises(openai.RateLimitError):
        await client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert upstream.calls == 1
    assert own.state == CircuitState.OPEN
    assert get_llm_breaker().state == CircuitState.CLOSED  # the enrichment circuit never saw it
    with pytest.raises(openai.APIConnectionError):
        await client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert upstream.calls == 1  # the open breaker refused it locally


async def test_the_default_client_still_feeds_the_shared_llm_breaker() -> None:
    upstream = Upstream(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})
    client = make_llm_client("t", inner_transport=upstream.transport(), max_retries=0)
    with pytest.raises(openai.RateLimitError):
        await client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert get_llm_breaker().state == CircuitState.OPEN
