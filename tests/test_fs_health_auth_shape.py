"""``health_check`` says something about the key only when Remembra's own answer says it (F54, review round).

The first fix read every 401 as "the server rejected this key" and every 403 that was not an HTML page as
"the key was accepted". A reverse proxy answers 401 with its own page, and a firewall can answer 403 with a
JSON body: neither asked the server about the key. Only a 401 or 403 in Remembra's error shape (a JSON object
whose ``detail`` is text, not sent by a firewall) is a verdict; the rest leaves the key unchecked.

Same wiring as ``test_fs_health_auth``: the real MCP tool and SDK client over an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import remembra.mcp.server as server
import tests.test_fs_health_auth as base
from tests.test_fs_health_auth import KEY, PROBE, health

wire = base.wire  # the same fixture: the real tool and SDK client over a fake server

NGINX_401 = "<html>\r\n<head><title>401 Authorization Required</title></head>\r\n<body>nginx</body>\r\n</html>"


def _unchecked(out: dict[str, Any]) -> None:
    assert out["status"] == "ok" and "code" not in out and "error" not in out
    assert out["auth"]["state"] == "unchecked" and out["auth"]["error"]
    assert any("could not be checked" in w for w in out["warnings"])
    assert not any("accepted" in w or "rejected" in w for w in out["warnings"])
    assert "REMEMBRA_API_KEY in this MCP server" not in json.dumps(out)  # no instruction to replace the key


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, text=NGINX_401, headers={"content-type": "text/html", "www-authenticate": 'Basic realm="x"'}),
        httpx.Response(401, text="Unauthorized"),
        httpx.Response(401, content=b""),
        httpx.Response(401, json=["x"]),
        httpx.Response(401, json={"message": "Unauthorized", "code": 16}),  # a gateway's own JSON
        httpx.Response(401, json={"detail": {"msg": "nope", "code": 7}}),
        httpx.Response(401, json={"detail": None}),
    ],
    ids=["html-page", "plain-text", "empty", "json-list", "gateway-json", "detail-object", "detail-null"],
)
def test_a_401_that_remembra_did_not_write_is_not_a_rejected_key(wire, answer: httpx.Response) -> None:
    wire(lambda r: answer)
    out = health()
    _unchecked(out)
    assert out["auth"]["http_status"] == 401
    assert "<html" not in json.dumps(out).lower()


@pytest.mark.parametrize(
    "answer",
    [
        # Cloudflare's JSON block (error 1010): a firewall's answer with a JSON body.
        httpx.Response(
            403,
            json={"detail": None, "error_code": 1010, "title": "Error 1010: Access denied"},
            headers={"server": "cloudflare", "cf-ray": "8a1b2c3d4e5f6a7b-EWR"},
        ),
        # Cloudflare's structured error (RFC 9457): it has a text ``detail``, and is still not Remembra's.
        httpx.Response(
            403,
            json={
                "type": "https://developers.cloudflare.com/support/troubleshooting/http-status-codes/",
                "title": "Error 1020: Access denied",
                "status": 403,
                "detail": "This site is using a security service to protect itself.",
                "error_code": 1020,
                "ray_id": "8a1b2c3d4e5f6a7b",
                "cloudflare_error": True,
            },
            headers={"server": "cloudflare", "cf-ray": "8a1b2c3d4e5f6a7b-EWR", "content-type": "application/problem+json"},
        ),
        httpx.Response(403, json={"message": "Forbidden"}),  # some other proxy's JSON
        httpx.Response(403, json={"detail": [{"loc": ["x"], "msg": "y"}]}),
        httpx.Response(403, text="Forbidden"),
        httpx.Response(403, content=b""),
    ],
    ids=["cloudflare-json", "cloudflare-problem-json", "proxy-json", "detail-list", "plain-text", "empty"],
)
def test_a_403_that_remembra_did_not_write_is_not_an_accepted_key(wire, answer: httpx.Response) -> None:
    wire(lambda r: answer)
    out = health()
    _unchecked(out)
    assert out["auth"]["http_status"] == 403


def test_remembras_own_401_is_still_a_rejected_key(wire) -> None:
    wire(lambda r: httpx.Response(401, json={"detail": "Invalid or revoked API key."}))
    out = health()
    assert out["status"] == "error" and out["code"] == 401 and out["auth"]["state"] == "rejected"
    assert out["auth"]["detail"] == "Invalid or revoked API key."
    assert "Replace REMEMBRA_API_KEY" in out["error"]


def test_remembras_own_403_is_still_an_accepted_key_with_limits(wire) -> None:
    wire(lambda r: httpx.Response(403, json={"detail": "This API key is scoped to agent codex"}))
    out = health()
    assert out["status"] == "ok" and out["auth"]["state"] == "limited"
    assert out["auth"]["detail"] == "This API key is scoped to agent codex"
    assert any("accepted" in w and "403" in w for w in out["warnings"])


def test_remembras_403_behind_a_proxy_that_stamps_its_headers_is_still_read(wire) -> None:
    """Production sits behind Cloudflare: its headers are on Remembra's own answers too, so the body decides."""
    cf = {"server": "cloudflare", "cf-ray": "8a1b2c3d4e5f6a7b-EWR"}
    wire(lambda r: httpx.Response(403, json={"detail": "Permission denied: memory:recall required."}, headers=cf))
    assert health()["auth"]["state"] == "limited"
    wire(lambda r: httpx.Response(401, json={"detail": "Invalid or revoked API key."}, headers=cf))
    out = health()
    assert out["status"] == "error" and out["auth"]["state"] == "rejected"


def test_a_401_that_echoes_the_key_does_not_leak_it(wire) -> None:
    wire(lambda r: httpx.Response(401, json={"detail": f"Invalid or revoked API key: {r.headers.get('x-api-key')}"}))
    raw = server.health_check()
    assert KEY not in raw and json.loads(raw)["auth"]["state"] == "rejected"


def test_the_probe_is_the_request_every_other_tool_sends(wire) -> None:
    """Made on the SDK client's own connection: same key header, same declared agent, one read, no write."""
    fake = wire(lambda r: httpx.Response(200, json={"agents": []}))
    health()
    assert fake.paths() == ["/health", PROBE]
    probe = fake.requests[1]
    assert probe.method == "GET" and probe.url.params["days"] == "1"
    assert probe.headers["x-api-key"] == KEY
    assert probe.headers["x-remembra-agent-id"] == "claude-desktop"


def test_the_status_resource_does_not_blame_the_key_for_a_proxys_401(wire) -> None:
    wire(lambda r: httpx.Response(401, text=NGINX_401, headers={"content-type": "text/html"}))
    out = json.loads(server.memory_status())
    assert out["auth"]["state"] == "unchecked" and "error" not in out
