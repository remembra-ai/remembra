"""HTTP mapping for upstream provider failures.

Single source of truth for how an ``EmbeddingProviderError`` becomes a client
response, used by the store/recall endpoints and by the app-wide exception
handler (so every other route that embeds gets the same honest mapping):

* ``quota_exhausted`` / ``auth`` -> **503** with a message that says an
  operator must act, plus a long ``Retry-After``. These are not transient.
* ``rate_limited``               -> **429** honoring the upstream retry hint.
* ``unavailable``                -> **503** with a short ``Retry-After``.
* ``bad_request``                -> **502** (the provider rejected our request).
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import HTTPException, status

from remembra.core.metrics import RECALL_FAILURES, STORE_FAILURES
from remembra.core.provider_errors import (
    CLIENT_MESSAGES,
    DEFAULT_RATE_LIMIT_RETRY_AFTER,
    DEFAULT_UNAVAILABLE_RETRY_AFTER,
    LONG_RETRY_AFTER_SECONDS,
    ProviderErrorKind,
)

_NOT_STORED_SUFFIX = " The memory was not stored."


def _retry_after_header(seconds: float | None, default: int) -> str:
    if seconds is None or seconds <= 0:
        return str(default)
    return str(max(1, math.ceil(seconds)))


def embedding_error_response(exc: Any, operation: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Return ``(status_code, body, headers)`` for an embedding provider error.

    ``exc`` is duck-typed (``.kind``, ``.retry_after``, ``.circuit_open``) so
    this module does not import the storage layer.
    """
    raw_kind = getattr(exc, "kind", None) or ProviderErrorKind.UNAVAILABLE
    try:
        kind = ProviderErrorKind(raw_kind)
    except ValueError:
        kind = ProviderErrorKind.UNAVAILABLE
    retry_after = getattr(exc, "retry_after", None)
    message = CLIENT_MESSAGES[kind]
    if operation == "store":
        message += _NOT_STORED_SUFFIX

    if kind in (ProviderErrorKind.QUOTA_EXHAUSTED, ProviderErrorKind.AUTH):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
        # Never tell clients to come back sooner than the long window, even if
        # the breaker's remaining open time is shorter — a human must act.
        headers = {"Retry-After": str(LONG_RETRY_AFTER_SECONDS)}
    elif kind == ProviderErrorKind.RATE_LIMITED:
        code = status.HTTP_429_TOO_MANY_REQUESTS
        headers = {"Retry-After": _retry_after_header(retry_after, DEFAULT_RATE_LIMIT_RETRY_AFTER)}
    elif kind == ProviderErrorKind.UNAVAILABLE:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
        headers = {"Retry-After": _retry_after_header(retry_after, DEFAULT_UNAVAILABLE_RETRY_AFTER)}
    else:
        code = status.HTTP_502_BAD_GATEWAY
        headers = {}

    body = {
        "detail": message,
        "error": {
            "type": "embedding_provider_error",
            "kind": kind.value,
            "circuit_open": bool(getattr(exc, "circuit_open", False)),
        },
    }
    if operation == "store":
        STORE_FAILURES.inc(reason=f"embedding_{kind.value}")
    elif operation == "recall":
        RECALL_FAILURES.inc(reason=f"embedding_{kind.value}")
    return code, body, headers


def embedding_http_exception(exc: Any, operation: str) -> HTTPException:
    """``HTTPException`` form of :func:`embedding_error_response`.

    The detail is the human-readable message (FastAPI renders it as
    ``{"detail": ...}``); the machine-readable kind travels in the
    ``X-Remembra-Error-Kind`` header.
    """
    code, body, headers = embedding_error_response(exc, operation)
    headers = {**headers, "X-Remembra-Error-Kind": body["error"]["kind"]}
    return HTTPException(status_code=code, detail=body["detail"], headers=headers)
