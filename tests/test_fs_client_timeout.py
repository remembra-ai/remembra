"""F56 (the part still open): a server that does not answer in time is a typed, explained client error.

``Memory._request`` let ``httpx.ReadTimeout`` escape, so the MCP tools answered the bare text "The read
operation timed out": no hint that the write may have landed, and the MCP client's 30 s timeout was
shorter than the server's 45 s store budget and could not be changed. ``health_check`` read only
``/health`` (liveness), which stays "ok" while the embedding provider is down.

No network: ``httpx.MockTransport`` under the real client and, for the tool tests, the real MCP server
module through ``FastMCP.call_tool``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
from remembra.client.memory import Memory, MemoryError, MemoryTimeoutError
from remembra.config import Settings
from remembra.mcp.crew import CrewBridge

STORE_OK = {"id": "mem_1", "extracted_facts": ["a fact"], "entities": []}


def client_with(handler: Any, **kwargs: Any) -> Memory:
    client = Memory(base_url="https://remembra.test", api_key="test-key", project="tproj", agent_id="codex", **kwargs)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), headers=client._headers, timeout=client.timeout)
    return client


def read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("The read operation timed out", request=request)


def call(name: str, **args: Any) -> str:
    out = asyncio.run(server.mcp.call_tool(name, args))
    if isinstance(out, tuple):
        out = out[0]
    return str(getattr(out, "content", out)[0].text)


@pytest.fixture()
def fresh(monkeypatch):
    monkeypatch.setattr(server, "_crew", CrewBridge())
    monkeypatch.setattr(server, "_session_projects", {})
    monkeypatch.setattr(server, "_client", None)
    yield
    server._client = None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


def test_a_read_timeout_on_a_write_is_a_typed_error_that_says_the_outcome_is_unknown():
    client = client_with(read_timeout)
    with pytest.raises(MemoryTimeoutError) as caught:
        client.store("the release is tagged")
    e = caught.value
    assert isinstance(e, MemoryError) and e.status_code is None
    assert "did not answer within 30s" in str(e), str(e)
    assert "may still have been carried out" in str(e), str(e)
    assert isinstance(e.__cause__, httpx.ReadTimeout)


def test_a_read_timeout_on_a_read_does_not_claim_a_write_may_have_landed():
    client = client_with(read_timeout)
    with pytest.raises(MemoryTimeoutError) as caught:
        client.health()
    assert "did not answer within 30s" in str(caught.value)
    assert "carried out" not in str(caught.value)


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ConnectError])
def test_a_request_that_never_connected_is_not_reported_as_possibly_carried_out(error):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise error("no connection", request=request)

    client = client_with(refuse)
    with pytest.raises(error):  # unchanged: nothing was sent, the connect error itself is raised
        client.store("x")


def test_the_timeout_of_a_store_is_its_own_setting_and_defaults_to_the_clients_timeout():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.extensions["timeout"]["read"]
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json=STORE_OK)

    client = client_with(handler, timeout=12.0, store_timeout=40.0)
    client.store("x")
    client.health()
    assert seen == {"/api/v1/memories": 40.0, "/health": 12.0}
    with pytest.raises(MemoryTimeoutError, match="within 40s"):
        client_with(read_timeout, timeout=12.0, store_timeout=40.0).store("x")

    seen.clear()
    plain = client_with(handler, timeout=12.0)
    plain.store("x")
    assert seen == {"/api/v1/memories": 12.0}


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------


def test_the_mcp_client_waits_longer_for_a_store_than_the_servers_store_budget(monkeypatch):
    monkeypatch.delenv("REMEMBRA_TIMEOUT", raising=False)
    monkeypatch.delenv("REMEMBRA_STORE_TIMEOUT", raising=False)
    client = server._new_client("k", "p", "codex")
    budget = Settings.model_fields["store_time_budget_seconds"].default
    assert client.timeout == 30.0
    assert client.store_timeout > budget, (client.store_timeout, budget)


def test_the_mcp_timeouts_can_be_set_from_the_environment_and_bad_values_are_ignored(monkeypatch):
    monkeypatch.setenv("REMEMBRA_TIMEOUT", "20")
    monkeypatch.setenv("REMEMBRA_STORE_TIMEOUT", "75.5")
    client = server._new_client("k", "p", "codex")
    assert (client.timeout, client.store_timeout) == (20.0, 75.5)
    for bad in ("", "abc", "0", "-5", "nan", "inf"):
        monkeypatch.setenv("REMEMBRA_TIMEOUT", bad)
        monkeypatch.setenv("REMEMBRA_STORE_TIMEOUT", bad)
        client = server._new_client("k", "p", "codex")
        assert (client.timeout, client.store_timeout) == (30.0, 50.0), bad
    # a store never waits less than other calls
    monkeypatch.setenv("REMEMBRA_TIMEOUT", "90")
    monkeypatch.setenv("REMEMBRA_STORE_TIMEOUT", "40")
    client = server._new_client("k", "p", "codex")
    assert (client.timeout, client.store_timeout) == (90.0, 90.0)


def test_store_memory_answers_a_timeout_with_what_it_means(fresh):
    server._client = client_with(read_timeout)
    out = json.loads(call("store_memory", content="the release is tagged"))
    assert out["status"] == "error" and out["code"] is None and out["timed_out"] is True, out
    assert "did not answer within 30s" in out["error"] and "may still have been carried out" in out["error"], out
    assert out["error"] != "The read operation timed out"


def test_health_check_reports_degraded_readiness(fresh):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": server.__version__})
        assert request.url.path == "/health/ready"
        return httpx.Response(
            200,
            json={
                "status": "degraded",
                "degraded_components": ["embeddings"],
                "components": {
                    "embeddings": {"status": "degraded", "reason": "quota_exhausted", "provider": "openai"},
                    "qdrant": {"status": "ok"},
                },
            },
        )

    server._client = client_with(handler)
    out = json.loads(call("health_check"))
    assert out["status"] == "ok" and out["health"]["status"] == "ok"
    assert out["readiness"] == {"status": "degraded", "degraded": {"embeddings": "quota_exhausted"}}, out
    assert any("embeddings (quota_exhausted)" in w for w in out["warnings"]), out["warnings"]


def test_health_check_is_unchanged_when_the_server_is_ready_or_has_no_readiness_route(fresh):
    def ready(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": server.__version__})
        return httpx.Response(200, json={"status": "ok", "degraded_components": [], "components": {}})

    server._client = client_with(ready)
    out = json.loads(call("health_check"))
    assert out["readiness"] == {"status": "ok"} and not any("readiness" in w.lower() for w in out["warnings"])

    def old_server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": server.__version__})
        return httpx.Response(404, json={"detail": "Not Found"})

    server._client = client_with(old_server)
    out = json.loads(call("health_check"))
    assert out["status"] == "ok" and "readiness" not in out

    def ready_hangs(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": server.__version__})
        raise httpx.ReadTimeout("slow", request=request)

    server._client = client_with(ready_hangs)
    out = json.loads(call("health_check"))
    assert out["status"] == "ok" and "readiness" not in out
