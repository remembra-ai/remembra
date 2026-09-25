"""Classification of upstream AI-provider failures.

Every embedding / LLM provider signals failure differently, but operationally
there are only five things that matter to Remembra:

* ``quota_exhausted`` — the account is out of credits / over its billing quota.
  Retrying will not help until a human tops the account up.
* ``rate_limited``   — transient throttling. Retry after the upstream hint.
* ``auth``           — the API key is missing, revoked, or not permitted.
* ``bad_request``    — this specific input was rejected (too long, malformed).
* ``unavailable``    — upstream 5xx, timeouts, connection failures.

The 2026-08-21 outage happened because OpenAI's ``429 insufficient_quota``
(billing exhausted) was reported to clients as "rate limit, retry shortly".
This module parses the provider response body so the two are distinguishable.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any


class ProviderErrorKind(StrEnum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNAVAILABLE = "unavailable"


# Kinds that indicate the provider as a whole is unhealthy (as opposed to one
# bad input or one bad key). Only these count toward opening a circuit breaker.
BREAKER_COUNTED_KINDS: frozenset[ProviderErrorKind] = frozenset(
    {
        ProviderErrorKind.QUOTA_EXHAUSTED,
        ProviderErrorKind.RATE_LIMITED,
        ProviderErrorKind.UNAVAILABLE,
    }
)

# Body markers that mean "out of money", not "slow down". Matched
# case-insensitively against the error code/type/message of the body.
#   OpenAI / Azure: code|type "insufficient_quota",
#                   "You exceeded your current quota, please check your plan and billing details"
#   Jina:           402 / "insufficient balance"
#   Voyage:         "payment method" / "billing"
#   Cohere:         402 / "out of credits" / trial-key monthly limit
_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "quota exceeded",
    "quota_exceeded",
    "check your plan and billing",
    "billing_hard_limit",
    "billing hard limit",
    "insufficient balance",
    "insufficient_balance",
    "insufficient credit",
    "out of credits",
    "credit balance is too low",
    "payment required",
    "add a payment method",
    "monthly limit",
)

_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def _body_text(body: Any) -> str:
    """Flatten a provider error body into lowercase searchable text."""
    if body is None:
        return ""
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, str):
        return body.lower()
    try:
        return json.dumps(body).lower()
    except (TypeError, ValueError):
        return str(body).lower()


def is_quota_body(body: Any) -> bool:
    """True if a provider error body describes billing/quota exhaustion."""
    text = _body_text(body)
    return bool(text) and any(marker in text for marker in _QUOTA_MARKERS)


def parse_duration_seconds(value: str | None) -> float | None:
    """Parse ``Retry-After`` style values.

    Supports integer/float seconds ("5", "0.5"), HTTP dates, and OpenAI's
    ``x-ratelimit-reset-*`` compound durations ("6m0s", "20ms", "1h2m3s").
    Returns None when unparseable.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    parts = _DURATION_PART_RE.findall(raw)
    if parts and "".join(n + u for n, u in parts) == raw:
        mult = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
        return sum(float(n) * mult[u] for n, u in parts)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def retry_after_from_headers(headers: Any) -> float | None:
    """Extract the upstream retry hint (seconds) from response headers."""
    if headers is None:
        return None
    for name in ("retry-after", "retry-after-ms", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        try:
            value = headers.get(name)
        except Exception:
            value = None
        if value is None:
            continue
        if name == "retry-after-ms":
            try:
                return max(0.0, float(value) / 1000.0)
            except ValueError:
                continue
        parsed = parse_duration_seconds(value)
        if parsed is not None:
            return parsed
    return None


def classify_http_error(status_code: int | None, body: Any = None) -> ProviderErrorKind:
    """Map an upstream HTTP status + body to a ProviderErrorKind.

    ``status_code=None`` means the request never got a response (timeout,
    DNS, connection refused) and is classified as ``unavailable``.
    """
    if status_code is None:
        return ProviderErrorKind.UNAVAILABLE
    if status_code == 402:
        return ProviderErrorKind.QUOTA_EXHAUSTED
    if status_code == 429:
        return ProviderErrorKind.QUOTA_EXHAUSTED if is_quota_body(body) else ProviderErrorKind.RATE_LIMITED
    if status_code in (401, 403):
        # Some providers answer 403 for a disabled billing account.
        return ProviderErrorKind.QUOTA_EXHAUSTED if is_quota_body(body) else ProviderErrorKind.AUTH
    if status_code in (408, 409, 425) or status_code >= 500:
        return ProviderErrorKind.UNAVAILABLE
    if 400 <= status_code < 500:
        return ProviderErrorKind.QUOTA_EXHAUSTED if is_quota_body(body) else ProviderErrorKind.BAD_REQUEST
    return ProviderErrorKind.UNAVAILABLE


# Client-facing messages. Deliberately free of provider URLs, keys, or bodies.
CLIENT_MESSAGES: dict[ProviderErrorKind, str] = {
    ProviderErrorKind.QUOTA_EXHAUSTED: (
        "The embedding provider account is out of quota/credits. This is not a transient "
        "rate limit — the service operator must top up the provider account. Retrying "
        "shortly will not help."
    ),
    ProviderErrorKind.AUTH: (
        "The embedding provider rejected the server's credentials. The service operator "
        "must fix the provider API key. Retrying shortly will not help."
    ),
    ProviderErrorKind.RATE_LIMITED: "The embedding provider is rate limiting requests. Retry after the indicated delay.",
    ProviderErrorKind.BAD_REQUEST: "The embedding provider rejected this input.",
    ProviderErrorKind.UNAVAILABLE: "The embedding provider is temporarily unavailable. Retry shortly.",
}

# Retry-After (seconds) sent to clients for kinds that need a human fix.
LONG_RETRY_AFTER_SECONDS = 900
DEFAULT_RATE_LIMIT_RETRY_AFTER = 5
DEFAULT_UNAVAILABLE_RETRY_AFTER = 5
