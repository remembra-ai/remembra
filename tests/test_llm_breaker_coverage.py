"""Entity matching, OpenAI entity extraction and conversation ingest go through
the shared LLM breaker and AI-spend metering (gap analysis section 9.13).

These three paths used to build raw ``AsyncOpenAI`` clients: the SDK retried
twice, and a quota outage kept sending requests after the breaker used by
extraction and consolidation had opened. Here each component builds its client
the normal way (``_get_client``); only the HTTP transport under the breaker is
replaced, so the real SDK, BreakerTransport, ``metered_chat`` and the parsing
code all run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from remembra.core import ai_spend
from remembra.core.circuit_breaker import CircuitState
from remembra.core.llm_guard import BreakerTransport, get_llm_breaker
from remembra.extraction.entities import EntityExtractor, ExtractedEntity
from remembra.extraction.extractor import ExtractionConfig, FactExtractor
from remembra.extraction.matcher import EntityMatcher, ExistingEntity
from remembra.models.memory import ConversationIngestRequest, ConversationMessage, IngestOptions
from remembra.services.conversation_ingest import ConversationIngestService
from tests._ingest_fakes import all_rows, close_ingest_dbs, make_service  # noqa: F401

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
# gpt-4o-mini: $0.15 in / $0.60 out per 1M tokens
USAGE_USD = (1000 * 0.15 + 200 * 0.60) / 1_000_000
QUOTA = {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}


def _completion(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(payload)}, "finish_reason": "stop"}],
        "usage": USAGE,
    }


class Upstream:
    """Scripted OpenAI HTTP endpoint; counts every request that left the process."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.calls = 0

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(self.status, json=self.body, headers={"retry-after-ms": "1"})

        return handle


def _wire(client: Any, upstream: Upstream) -> BreakerTransport:
    """Assert ``client`` sits behind the shared LLM breaker and point it at ``upstream``."""
    transport = client._client._transport
    assert isinstance(transport, BreakerTransport)
    assert transport.breaker is get_llm_breaker()
    transport.inner = httpx.MockTransport(upstream.handler())
    return transport


@pytest.fixture(autouse=True)
def _reset_llm_breaker():
    get_llm_breaker().reset()
    yield
    get_llm_breaker().reset()


async def _open_breaker_with_quota() -> None:
    """A quota error on the fact extractor opens the breaker every LLM client shares."""
    extractor = FactExtractor(ExtractionConfig(api_key="t"))
    _wire(extractor._get_client(), Upstream(429, QUOTA))
    await extractor.extract("Mani moved to Kingston in 2024. He runs a contracting company.")
    assert get_llm_breaker().state == CircuitState.OPEN


def _job() -> ai_spend.SpendJob:
    return ai_spend.SpendJob(user_id="u1", budget_usd=1.0)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_each_path_builds_a_breaker_client_with_bounded_retries(tmp_path) -> None:
    from types import SimpleNamespace

    from remembra.config import Settings

    matcher_client = EntityMatcher(api_key="t")._get_client()
    entity_client = EntityExtractor(api_key="t")._get_client()
    settings = Settings(openai_api_key="t", typesafe_mode="off")
    ingest = ConversationIngestService(settings=settings, memory_service=SimpleNamespace(entity_extractor=None))
    ingest_client = ingest._get_client()

    for client in (matcher_client, entity_client, ingest_client):
        transport = client._client._transport
        assert isinstance(transport, BreakerTransport)
        assert transport.breaker is get_llm_breaker()
        assert client.max_retries == 1  # SDK default is 2
    assert ingest._get_client() is ingest_client  # built once, reused


# ---------------------------------------------------------------------------
# Entity matcher
# ---------------------------------------------------------------------------

EXISTING = [ExistingEntity(id="e1", name="John Smith", type="PERSON", description="CEO of Acme", aliases=[])]
MENTION = ExtractedEntity(name="Mr. Smith", type="PERSON", description="the CEO", aliases=[])


async def test_matcher_closed_breaker_matches_and_meters() -> None:
    upstream = Upstream(
        200,
        _completion({"match": True, "matched_entity_id": "e1", "confidence": 0.9, "reason": "same CEO"}),
    )
    matcher = EntityMatcher(api_key="t")
    _wire(matcher._get_client(), upstream)

    job = _job()
    with ai_spend.activate(job):
        result = await matcher.match(MENTION, EXISTING)
    await job.wait_settled()

    assert result.match is True
    assert result.matched_entity_id == "e1"
    assert upstream.calls == 1
    assert job.usd == pytest.approx(USAGE_USD)
    assert job.llm_calls == 1
    assert get_llm_breaker().last_success_at is not None


async def test_matcher_fails_fast_while_breaker_is_open() -> None:
    await _open_breaker_with_quota()
    upstream = Upstream(200, _completion({"match": True, "matched_entity_id": "e1", "confidence": 0.9}))
    matcher = EntityMatcher(api_key="t")
    _wire(matcher._get_client(), upstream)

    job = _job()
    with ai_spend.activate(job):
        result = await matcher.match(MENTION, EXISTING)
    await job.wait_settled()

    assert upstream.calls == 0  # nothing left the process
    assert result.match is False
    assert result.new_entity == MENTION  # the non-LLM default: create a new entity
    assert job.usd == 0.0
    assert job.inflight_usd == 0.0  # the estimate was released


async def test_matcher_5xx_counts_toward_the_shared_breaker() -> None:
    upstream = Upstream(503, {"error": {"message": "overloaded"}})
    matcher = EntityMatcher(api_key="t")
    _wire(matcher._get_client(), upstream)
    for _ in range(2):
        result = await matcher.match(MENTION, EXISTING)
        assert result.match is False
    assert upstream.calls == 4  # one retry per call (was two)
    assert get_llm_breaker().state == CircuitState.CLOSED
    await matcher.match(MENTION, EXISTING)  # fifth consecutive failure
    assert get_llm_breaker().state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# OpenAI entity extractor
# ---------------------------------------------------------------------------

ENTITY_PAYLOAD = {
    "entities": [{"name": "Mani", "type": "PERSON", "description": "contractor"}],
    "relationships": [{"subject": "Mani", "predicate": "LIVES_IN", "object": "Kingston"}],
}
TEXT = "Mani lives in Kingston and runs a contracting company."


async def test_entity_extractor_closed_breaker_extracts_and_meters() -> None:
    upstream = Upstream(200, _completion(ENTITY_PAYLOAD))
    extractor = EntityExtractor(api_key="t")
    _wire(extractor._get_client(), upstream)

    job = _job()
    with ai_spend.activate(job):
        result = await extractor.extract(TEXT)
    await job.wait_settled()

    assert [e.name for e in result.entities] == ["Mani"]
    assert [(r.subject, r.predicate, r.object) for r in result.relationships] == [("Mani", "LIVES_IN", "Kingston")]
    assert upstream.calls == 1
    assert job.usd == pytest.approx(USAGE_USD)


async def test_entity_extractor_quota_opens_breaker_then_fails_fast() -> None:
    upstream = Upstream(429, QUOTA)
    extractor = EntityExtractor(api_key="t")
    _wire(extractor._get_client(), upstream)

    first = await extractor.extract(TEXT)
    assert first.entities == [] and first.relationships == []
    assert get_llm_breaker().state == CircuitState.OPEN
    assert upstream.calls == 1  # the SDK retry was refused locally

    job = _job()
    with ai_spend.activate(job):
        second = await extractor.extract(TEXT)
    await job.wait_settled()
    assert second.entities == []
    assert upstream.calls == 1
    assert job.usd == 0.0


async def test_entity_extractor_short_text_makes_no_call() -> None:
    upstream = Upstream(200, _completion(ENTITY_PAYLOAD))
    extractor = EntityExtractor(api_key="t")
    _wire(extractor._get_client(), upstream)
    assert (await extractor.extract("   ")).entities == []
    assert (await extractor.extract("short")).entities == []
    assert upstream.calls == 0


# ---------------------------------------------------------------------------
# Conversation ingest (request in, response + DB state out)
# ---------------------------------------------------------------------------

MESSAGES = [
    ConversationMessage(role="user", content="My wife Suzan and I are planning a trip to Japan", name="Mani"),
    ConversationMessage(role="assistant", content="When are you planning to go?"),
    ConversationMessage(role="user", content="We're thinking April next year", name="Mani"),
]


class _NoEntities:
    async def extract(self, content: str) -> Any:
        from remembra.extraction.entities import ExtractionResult

        return ExtractionResult(entities=[], relationships=[])


async def _ingest_service(tmp_path) -> tuple[ConversationIngestService, Any]:  # type: ignore[no-untyped-def]
    service, db, _qdrant, _cons, _ext = await make_service(tmp_path)
    service.entity_extractor = _NoEntities()  # type: ignore[assignment]
    ingest = ConversationIngestService(settings=service.settings, memory_service=service)
    return ingest, db


def _request() -> ConversationIngestRequest:
    return ConversationIngestRequest(messages=MESSAGES, user_id="u1", session_id="s1", options=IngestOptions())


async def test_ingest_closed_breaker_stores_facts_and_meters(tmp_path) -> None:
    fact = "Mani and his wife Suzan are planning a trip to Japan in April"
    upstream = Upstream(
        200, _completion({"facts": [{"content": fact, "importance": 0.9, "speaker": "Mani", "source_message": 0}]})
    )
    ingest, db = await _ingest_service(tmp_path)
    _wire(ingest._get_client(), upstream)

    job = _job()
    with ai_spend.activate(job):
        response = await ingest.ingest(_request())
    await job.wait_settled()

    assert response.status == "ok", response.errors
    assert [f.content for f in response.facts] == [fact]
    assert response.stats.facts_stored == 1
    rows = await all_rows(db)
    assert [r["content"] for r in rows] == [fact]
    assert upstream.calls == 1
    assert job.usd >= USAGE_USD  # the ingest call itself is metered on the write's job
    assert get_llm_breaker().last_success_at is not None


async def test_ingest_while_breaker_open_sends_nothing_and_stores_nothing(tmp_path) -> None:
    await _open_breaker_with_quota()
    upstream = Upstream(200, _completion({"facts": [{"content": "x" * 20, "importance": 0.9, "source_message": 0}]}))
    ingest, db = await _ingest_service(tmp_path)
    _wire(ingest._get_client(), upstream)

    job = _job()
    with ai_spend.activate(job):
        response = await ingest.ingest(_request())
    await job.wait_settled()

    assert upstream.calls == 0
    assert response.status == "error"
    assert response.errors and response.errors[0].startswith("Extraction failed")
    assert response.facts == []
    assert await all_rows(db) == []
    assert job.usd == 0.0
    assert job.inflight_usd == 0.0


async def test_ingest_quota_error_opens_the_shared_breaker(tmp_path) -> None:
    upstream = Upstream(429, QUOTA)
    ingest, db = await _ingest_service(tmp_path)
    _wire(ingest._get_client(), upstream)

    response = await ingest.ingest(_request())
    assert response.status == "error"
    assert get_llm_breaker().state == CircuitState.OPEN
    assert upstream.calls == 1
    assert await all_rows(db) == []

    # Other LLM paths now fail fast too.
    matcher_upstream = Upstream(200, _completion({"match": False}))
    matcher = EntityMatcher(api_key="t")
    _wire(matcher._get_client(), matcher_upstream)
    await matcher.match(MENTION, EXISTING)
    assert matcher_upstream.calls == 0
