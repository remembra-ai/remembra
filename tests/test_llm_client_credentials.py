"""A missing provider credential must fail before allocating a transport."""

import httpx
from openai import OpenAIError
import pytest

from remembra.core import llm_guard


@pytest.mark.parametrize("key", [None, ""])
def test_missing_credential_does_not_allocate_tls_or_http_client(monkeypatch, key):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    def unexpected_transport(*args, **kwargs):
        pytest.fail("A missing credential allocated an HTTP client before rejection")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_transport)
    with pytest.raises(OpenAIError, match="Missing credentials"):
        llm_guard.make_llm_client(key)


@pytest.mark.parametrize("explicit", [True, False])
async def test_valid_explicit_and_ambient_credentials_keep_sdk_behavior(monkeypatch, explicit):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient")
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    client = llm_guard.make_llm_client(
        "synthetic-explicit" if explicit else None,
        inner_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        assert client.api_key == ("synthetic-explicit" if explicit else "synthetic-ambient")
        assert client.max_retries == 1 and client.timeout == 20.0
    finally:
        await client.close()
