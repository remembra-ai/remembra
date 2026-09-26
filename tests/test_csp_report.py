"""POST /api/v1/csp-report: where the sites' report-only CSP sends its violations.

Through the real app (create_app: CORS, security headers, rate limiter), with
the two report formats browsers send: the legacy ``report-uri`` body
(``application/csp-report``) and a Reporting API batch
(``application/reports+json``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import structlog

import remembra.config as config_module
from remembra.api.v1 import csp_report
from tests.security_harness import make_settings

LEGACY = {
    "csp-report": {
        "document-uri": "https://app.remembra.dev/pay?_ptxn=txn_01abcdefghijklmnop#x",
        "referrer": "",
        "violated-directive": "frame-src",
        "effective-directive": "frame-src",
        "original-policy": "default-src 'self'; report-uri https://api.remembra.dev/api/v1/csp-report",
        "disposition": "report",
        "blocked-uri": "https://checkout-service.paddle.com/some/path?token=secret",
        "status-code": 200,
        "script-sample": "console.log('do not log me')",
    }
}

BATCH = [
    {
        "type": "csp-violation",
        "age": 10,
        "url": "https://remembra.dev/pricing",
        "user_agent": "Mozilla/5.0",
        "body": {
            "documentURL": "https://remembra.dev/pricing?plan=solo",
            "effectiveDirective": "script-src-elem",
            "blockedURL": "inline",
            "disposition": "report",
            "sample": "alert(document.cookie)",
        },
    },
    {"type": "deprecation", "body": {"id": "x"}},
]


@asynccontextmanager
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    previous = config_module._settings
    config_module._settings = make_settings(cors_origins=["https://app.remembra.dev", "https://remembra.dev"])
    try:
        from remembra.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 51234))
        async with httpx.AsyncClient(transport=transport, base_url="https://api.remembra.dev") as client:
            yield client
    finally:
        config_module._settings = previous


def _violations(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: e.get(k) for k in ("page", "directive", "blocked", "disposition")} for e in logs if e["event"] == "csp_violation"]


async def test_legacy_report_is_logged_without_query_strings_or_samples() -> None:
    async with api_client() as client:
        with structlog.testing.capture_logs() as logs:
            resp = await client.post(
                "/api/v1/csp-report",
                content=json.dumps(LEGACY),
                headers={"Content-Type": "application/csp-report", "Origin": "https://app.remembra.dev"},
            )
    assert resp.status_code == 204, resp.text
    assert _violations(logs) == [
        {
            "page": "https://app.remembra.dev/pay",
            "directive": "frame-src",
            "blocked": "https://checkout-service.paddle.com",
            "disposition": "report",
        }
    ]
    text = json.dumps(logs)
    assert "txn_01" not in text and "secret" not in text and "do not log me" not in text
    # The browser may read the response: CORS allows the site's origin.
    assert resp.headers.get("access-control-allow-origin") == "https://app.remembra.dev"


async def test_reporting_api_batch_logs_only_csp_violations() -> None:
    async with api_client() as client:
        with structlog.testing.capture_logs() as logs:
            resp = await client.post(
                "/api/v1/csp-report",
                content=json.dumps(BATCH),
                headers={"Content-Type": "application/reports+json", "Origin": "https://remembra.dev"},
            )
    assert resp.status_code == 204
    assert _violations(logs) == [
        {"page": "https://remembra.dev/pricing", "directive": "script-src-elem", "blocked": "inline", "disposition": "report"}
    ]
    assert "document.cookie" not in json.dumps(logs)


async def test_preflight_from_the_sites_is_allowed() -> None:
    async with api_client() as client:
        for origin in ("https://remembra.dev", "https://app.remembra.dev"):
            resp = await client.options(
                "/api/v1/csp-report",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert resp.status_code == 200, (origin, resp.status_code)
            assert resp.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"csp-report": "x"}', b"[1, 2]", b'{"a": 1}'])
async def test_junk_is_accepted_and_logs_nothing(body: bytes) -> None:
    async with api_client() as client:
        with structlog.testing.capture_logs() as logs:
            resp = await client.post("/api/v1/csp-report", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 204
    assert _violations(logs) == []


async def test_oversized_body_is_refused_and_not_parsed() -> None:
    big = json.dumps([BATCH[0]] * 200).encode()
    assert len(big) > csp_report.MAX_BODY_BYTES
    async with api_client() as client:
        with structlog.testing.capture_logs() as logs:
            resp = await client.post("/api/v1/csp-report", content=big, headers={"Content-Type": "application/reports+json"})
    assert resp.status_code == 413
    assert _violations(logs) == []


def test_at_most_max_reports_are_logged_per_request() -> None:
    body = json.dumps([BATCH[0]] * (csp_report.MAX_REPORTS + 5)).encode()
    assert len(csp_report.parse_reports(body)) == csp_report.MAX_REPORTS


@pytest.mark.parametrize(
    ("blocked", "expected"),
    [
        ("eval", "eval"),
        ("data", "data"),
        ("data:image/png;base64,AAAA", "data"),
        ("blob:https://app.remembra.dev/uuid", "blob"),
        ("wss://api.remembra.dev/api/v1/ws?token=abc", "wss://api.remembra.dev"),
        ("https://user:pw@evil.example:8443/x", "https://evil.example:8443"),
        ("javascript:alert(1)", "javascript"),
        ("", None),
        (123, None),
        ("not a url at all", None),
    ],
)
def test_blocked_is_reduced_to_an_origin_or_keyword(blocked: Any, expected: str | None) -> None:
    assert csp_report._blocked(blocked) == expected


def test_page_keeps_origin_and_path_only() -> None:
    assert csp_report._page("https://u:p@app.remembra.dev/pay?_ptxn=txn_1#f") == "https://app.remembra.dev/pay"
    assert csp_report._page("about:blank") is None and csp_report._page(None) is None


def test_the_sites_report_uri_is_this_route() -> None:
    """Both nginx configs send reports to a path the API serves (and the same host the dashboard may connect to)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # Mounted under /api/v1 (the POST tests above go through the real app at that path).
    paths = {"/api/v1" + route.path for route in csp_report.router.routes}  # type: ignore[attr-defined]
    for conf in (root / "landing" / "remembra-headers.conf", root / "dashboard" / "security-headers.conf"):
        found = re.findall(r"report-uri (https://api\.remembra\.dev(/[^;\"\s]+))", conf.read_text())
        assert len(found) == 1, conf
        assert found[0][1] in paths, found[0]
