"""``health_check`` tests the API key, not only that the server is up (F54).

``GET /health`` needs no key, so a client holding a revoked key got
``"status": "ok"`` from ``health_check`` and a 401 from every other tool a few
seconds later. The tool now makes one authenticated read through the same
auth dependency as store and recall, and says what the server answered.

The server here is an ``httpx.MockTransport`` behind the real SDK client and
the real MCP tool: ``/health`` is always up, and the authenticated read
answers as each test says.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
from remembra import __version__
from remembra.client.memory import Memory

KEY = "rem_SYNTHETICrevokedKey0123456789abcdefXYZ"
PROBE = "/api/v1/trail/summary"
CLOUDFLARE = "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head><body>blocked</body></html>"

Handler = Callable[[httpx.Request], httpx.Response]


def revoked(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"detail": "Invalid or revoked API key."})


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"agents": [], "projects": [], "week": {}})


class FakeServer:
    """``/health`` answers without a key (as production does); everything else goes to ``authed``."""

    def __init__(self, authed: Handler, version: str = __version__) -> None:
        self.authed = authed
        self.version = version
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": self.version})
        return self.authed(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture()
def wire(monkeypatch):
    """Point the MCP server's real ``_get_client`` at a :class:`FakeServer` (stdio transport, one key)."""

    def _wire(authed: Handler, *, key: str = KEY, transport: str = "stdio") -> FakeServer:
        fake = FakeServer(authed)

        class WiredMemory(Memory):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["base_url"] = "http://remembra.test"
                super().__init__(*args, **kwargs)
                self._client.close()
                self._client = httpx.Client(transport=httpx.MockTransport(fake.handler), headers=self._headers)

        monkeypatch.setattr(server, "Memory", WiredMemory)
        monkeypatch.setattr(server, "REMEMBRA_MCP_TRANSPORT", transport)
        monkeypatch.setattr(server, "REMEMBRA_API_KEY", key)
        monkeypatch.setattr(server, "REMEMBRA_AGENT_ID", "claude-desktop")
        monkeypatch.setattr(server, "REMEMBRA_PROJECT", "alpha")
        monkeypatch.setattr(server, "REMEMBRA_SESSION_ID", "sess-health")
        monkeypatch.setattr(server, "_client", None)
        monkeypatch.setattr(server, "_clients_by_key", {})
        monkeypatch.setattr(server, "_session_projects", {})
        return fake

    return _wire


def health() -> dict[str, Any]:
    raw = server.health_check()
    assert KEY not in raw
    return json.loads(raw)


# ---------------------------------------------------------------------------
# The fault: a revoked key
# ---------------------------------------------------------------------------


def test_revoked_key_is_an_error_not_ok(wire) -> None:
    fake = wire(revoked)
    out = health()
    assert out["status"] == "error" and out["code"] == 401
    assert out["auth"]["state"] == "rejected" and out["auth"]["http_status"] == 401
    assert "Invalid or revoked API key." in out["error"]
    # What to do, for a stdio client: the key lives in this MCP server's own entry.
    assert "REMEMBRA_API_KEY" in out["error"] and "restart" in out["error"]
    # The server itself is up, and the identity is still reported.
    assert out["health"] == {"status": "ok", "version": __version__}
    assert (out["agent_id"], out["project"], out["session_id"]) == ("claude-desktop", "alpha", "sess-health")
    assert out["server"] == "http://remembra.test" and out["client_version"] == __version__
    assert fake.paths() == ["/health", PROBE]
    probe = fake.requests[1]
    assert probe.method == "GET" and probe.headers["x-api-key"] == KEY  # the key every other tool sends


def test_health_check_and_the_next_tool_agree(wire) -> None:
    """The transcripts' sequence: health_check, then a store. Both now say 401."""
    wire(revoked)
    first = health()
    second = json.loads(server.store_memory("a fact"))
    assert second == {"status": "error", "error": "Request failed: Invalid or revoked API key.", "code": 401}
    assert (first["status"], first["code"]) == (second["status"], second["code"])


def test_no_key_at_all_is_an_error_when_the_server_wants_one(wire) -> None:
    def no_key(request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        return httpx.Response(401, json={"detail": "Authentication required. Provide X-API-Key header."})

    wire(no_key, key="")
    out = health()
    assert out["status"] == "error" and out["code"] == 401 and out["auth"]["state"] == "rejected"
    assert "no API key" in out["error"] and "REMEMBRA_API_KEY" in out["error"]


def test_remote_transport_blames_the_request_header_not_the_env(wire, monkeypatch) -> None:
    wire(revoked, transport="streamable-http")
    monkeypatch.setattr(server, "_caller_credentials", lambda: (KEY, None))
    monkeypatch.setattr(server, "_caller_agent_id", lambda: "claude-desktop")
    out = health()
    assert out["status"] == "error" and out["code"] == 401 and out["auth"]["state"] == "rejected"
    assert "X-API-Key" in out["error"] and "REMEMBRA_API_KEY" not in out["error"]


# ---------------------------------------------------------------------------
# A working key stays ok
# ---------------------------------------------------------------------------


def test_accepted_key_is_ok_and_says_so(wire) -> None:
    fake = wire(accepted)
    out = health()
    assert out["status"] == "ok" and "code" not in out and "error" not in out
    assert out["auth"] == {"state": "accepted", "checked_with": f"GET {PROBE}"}
    assert out["warnings"] == []
    assert fake.requests[1].url.params["days"] == "1"


def test_server_without_auth_and_no_key_is_ok(wire) -> None:
    wire(accepted, key="")
    out = health()
    assert out["status"] == "ok" and out["auth"]["state"] == "accepted"


def test_scoped_key_without_recall_is_accepted_with_a_warning(wire) -> None:
    """A Remembra 403 proves the key authenticated: a store-only key must not turn health red."""
    wire(lambda r: httpx.Response(403, json={"detail": "Permission denied: memory:recall required."}))
    out = health()
    assert out["status"] == "ok" and "code" not in out
    assert out["auth"]["state"] == "limited" and out["auth"]["http_status"] == 403
    assert any("accepted" in w and "403" in w for w in out["warnings"])


# ---------------------------------------------------------------------------
# Anything that does not answer the question is "unchecked", never a false error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, json={"detail": "Not Found"}),  # a server from before the trail routes
        httpx.Response(429, json={"detail": "Rate limit exceeded"}),
        httpx.Response(500, json={"detail": "Internal Server Error"}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(403, text=CLOUDFLARE, headers={"content-type": "text/html"}),  # a firewall, not Remembra
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, text="<html>a login page</html>", headers={"content-type": "text/html"}),
        httpx.Response(302, headers={"location": "https://elsewhere.test/login"}),
    ],
    ids=["404", "429", "500", "502", "firewall-403", "json-list", "html-200", "redirect"],
)
def test_unanswered_probe_is_unchecked_and_does_not_fail(wire, answer: httpx.Response) -> None:
    wire(lambda r: answer)
    out = health()
    assert out["status"] == "ok" and "code" not in out
    assert out["auth"]["state"] == "unchecked" and out["auth"]["error"]
    assert any("could not be checked" in w for w in out["warnings"])
    assert "<html" not in json.dumps(out).lower()  # a page is never echoed


def test_probe_timeout_is_unchecked(wire) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    wire(slow)
    out = health()
    assert out["status"] == "ok" and out["auth"]["state"] == "unchecked"
    assert "ReadTimeout" in out["auth"]["error"]


def test_server_down_is_still_the_old_error(wire, monkeypatch) -> None:
    fake = wire(accepted)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 61] Connection refused", request=request)

    monkeypatch.setattr(fake, "handler", down)
    monkeypatch.setattr(server, "_client", None)
    out = health()
    assert out["status"] == "error" and "auth" not in out  # nothing was asked of the key
    assert out["project"] == "alpha" and "warnings" in out


# ---------------------------------------------------------------------------
# memory://status carries the same verdict
# ---------------------------------------------------------------------------


def test_status_resource_reports_the_rejected_key(wire) -> None:
    wire(revoked)
    out = json.loads(server.memory_status())
    assert KEY not in json.dumps(out)
    assert out["auth"]["state"] == "rejected" and out["health"]["status"] == "ok"
    assert "Invalid or revoked API key." in out["error"]


def test_status_resource_reports_an_accepted_key(wire) -> None:
    wire(accepted)
    out = json.loads(server.memory_status())
    assert out["auth"]["state"] == "accepted" and "error" not in out


def test_setup_prompt_no_longer_overpromises() -> None:
    text = server.setup_check_prompt()[0]["content"]
    assert "health_check" in text and "key" in text
