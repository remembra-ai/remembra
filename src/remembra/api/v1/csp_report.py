"""Content-Security-Policy violation reports from remembra.dev and app.remembra.dev.

At launch both sites send their CSP as ``Content-Security-Policy-Report-Only``
with ``report-uri`` pointing here (see "Content-Security-Policy: report-only
at launch" in docs/DEPLOYING.md). Browsers then load everything as before and
POST a report for each thing an enforced policy would have blocked. Each
report becomes one ``csp_violation`` log line, so the operator can read what
enforcing would break (a Paddle host missing from the allowlist, say) before
switching.

The endpoint takes no credentials (browsers send none with reports). It is
rate-limited per client, reads at most ``MAX_BODY_BYTES`` and logs at most
``MAX_REPORTS`` reports per request. Only the page's origin and path, the
directive, the blocked resource's origin (or a keyword such as ``inline``) and
the disposition are logged: never a query string (payment links carry
transaction ids) or the script sample.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Request, Response

from remembra.core.limiter import limiter

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["security"])

MAX_BODY_BYTES = 16 * 1024
MAX_REPORTS = 20
_DIRECTIVE = re.compile(r"^[a-z-]{1,40}$")
_KEYWORD = re.compile(r"^[a-z-]{1,24}$")


def _host(parts: Any) -> str | None:
    """``host[:port]`` without any user:password part."""
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return f"{host}:{port}" if port else host


def _page(value: Any) -> str | None:
    """``scheme://host/path`` of an http(s) URL; no credentials, query or fragment."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    host = _host(parts)
    if parts.scheme not in ("http", "https") or not host:
        return None
    return f"{parts.scheme}://{host}{parts.path}"[:200]


def _blocked(value: Any) -> str | None:
    """The blocked resource as an origin (``https://cdn.example``) or a keyword (``inline``, ``eval``, ``data``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = _host(parts)
    if parts.scheme in ("http", "https", "ws", "wss") and host:
        return f"{parts.scheme}://{host}"[:120]
    keyword = (parts.scheme or text).lower()
    return keyword if _KEYWORD.match(keyword) else None


def _directive(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    word = value.strip().split(" ", 1)[0].lower()  # violated-directive may carry the whole directive
    return word if _DIRECTIVE.match(word) else None


def parse_reports(body: bytes) -> list[dict[str, str | None]]:
    """Violations from a legacy ``report-uri`` body or a Reporting API batch; [] for anything else."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return []
    raw: list[dict[str, Any]] = []
    if isinstance(data, dict) and isinstance(data.get("csp-report"), dict):
        r = data["csp-report"]
        raw.append(
            {
                "page": r.get("document-uri"),
                "directive": r.get("effective-directive") or r.get("violated-directive"),
                "blocked": r.get("blocked-uri"),
                "disposition": r.get("disposition"),
            }
        )
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("type") == "csp-violation" and isinstance(item.get("body"), dict):
                b = item["body"]
                raw.append(
                    {
                        "page": b.get("documentURL"),
                        "directive": b.get("effectiveDirective"),
                        "blocked": b.get("blockedURL"),
                        "disposition": b.get("disposition"),
                    }
                )
    reports = []
    for r in raw[:MAX_REPORTS]:
        disposition = r.get("disposition")
        reports.append(
            {
                "page": _page(r.get("page")),
                "directive": _directive(r.get("directive")),
                "blocked": _blocked(r.get("blocked")),
                "disposition": disposition if disposition in ("report", "enforce") else None,
            }
        )
    return reports


@router.post("/csp-report", status_code=204, include_in_schema=False)
@limiter.limit("60/minute")
async def csp_report(request: Request) -> Response:
    """Log the CSP violations a browser reports; always 204 unless the body is too large."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return Response(status_code=413)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return Response(status_code=413)
    for report in parse_reports(body):
        logger.warning("csp_violation", **report)
    return Response(status_code=204)
