"""Retain SDK credential semantics without allocating rejected HTTP clients."""

import httpx
import pytest
from openai import AsyncOpenAI, OpenAIError

from remembra.core import llm_guard
from remembra.extraction.consolidator import ConsolidationAction, ExistingMemory, MemoryConsolidator


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)


@pytest.mark.parametrize("argument,environment", [(None, None), ("", None), (None, ""), ("", "env-test-key")])
async def test_missing_credentials_rejected_before_http_allocation(monkeypatch, argument, environment):
    if environment is not None:
        monkeypatch.setenv("OPENAI_API_KEY", environment)
    # The real SDK rejection occurs before use of its supplied transport.
    sdk_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP")))
    try:
        with pytest.raises(OpenAIError) as sdk_error:
            AsyncOpenAI(api_key=argument, http_client=sdk_http)
    finally:
        await sdk_http.aclose()

    def forbidden(*args, **kwargs):
        pytest.fail("missing credentials allocated HTTP/TLS resources")

    monkeypatch.setattr(llm_guard.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(llm_guard, "BreakerTransport", forbidden)
    monkeypatch.setattr(llm_guard, "_settings_value", forbidden)
    with pytest.raises(OpenAIError) as factory_error:
        llm_guard.make_llm_client(argument, base_url="https://synthetic-rejected-input.example")
    assert str(factory_error.value) == str(sdk_error.value)
    assert "synthetic-rejected-input" not in str(factory_error.value)


@pytest.mark.parametrize(
    "argument,environment,admin",
    [
        ("explicit-test-key", "env-test-key", None),
        (None, "env-test-key", None),
        (None, None, "admin-test-key"),
        ("", "env-test-key", "admin-test-key"),
        (None, None, ""),
    ],
)
async def test_supported_credentials_and_transport_policy_unchanged(monkeypatch, argument, environment, admin):
    if environment is not None:
        monkeypatch.setenv("OPENAI_API_KEY", environment)
    if admin is not None:
        monkeypatch.setenv("OPENAI_ADMIN_KEY", admin)
    transport = httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP"))
    sdk_http = httpx.AsyncClient(transport=transport)
    sdk = AsyncOpenAI(api_key=argument, http_client=sdk_http)
    candidate = llm_guard.make_llm_client(argument, timeout=7.0, max_retries=0, inner_transport=transport)
    try:
        assert candidate.api_key == sdk.api_key
        assert candidate.admin_api_key == sdk.admin_api_key
        assert candidate.timeout == 7.0 and candidate.max_retries == 0
        assert isinstance(candidate._client._transport, llm_guard.BreakerTransport)
        assert candidate._client._transport.inner is transport
    finally:
        await candidate.close()
        await sdk.close()


async def test_repeated_missing_credential_fallback_allocates_no_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("fallback allocated HTTP/TLS resources")

    monkeypatch.setattr(llm_guard.httpx, "AsyncClient", forbidden)
    consolidator = MemoryConsolidator()
    relevant = [ExistingMemory("synthetic-id", "previous synthetic fact", 1.0)]
    for _ in range(10):
        with llm_guard.llm_fallback_scope() as fallbacks:
            result = await consolidator.consolidate("next synthetic fact", relevant)
        assert result.action == ConsolidationAction.ADD and result.decided_by == "fallback"
        assert fallbacks == {"consolidation": "internal"}
    assert consolidator._client is None
